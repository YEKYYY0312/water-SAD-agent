"""按意图最小暴露（P7-S4，``docs/design/16-p7-plugin-mcp.md`` §3.6）。

每轮在外层图 ``agent_loop`` 入口决定一次：本轮把哪些已注册工具发给模型、工具指引列哪几行。
ToolNode 仍持有全部已注册工具——**暴露不是授权边界**，授权只看 ``Principal``；未开放的工具被
模型调用时照常经 ``ToolRegistry.invoke`` 执行（护栏不变），trace 标记 ``unexposed_call``。

决定是**确定性纯函数**：关键词 / 正则匹配 + 两项预算，不调模型、不读时钟、不用随机数
（有 AST 断言）。分组来自插件贡献：

- ``core``：每轮必开、永不因预算丢弃（检索、引用、流速计算——铁律①②）；
- ``intent``：本轮问题命中关键词 / 正则才开；
- ``request``（P7-S12）：由**入口标志**决定，不看 query——用户对这一条消息勾选了联网，入口把组 id 交给
  ``decide(request_enabled=…)`` 才开；没勾选就永远不开，也**不**参与 sticky（上一轮勾选不延续到这一轮）；
- ``ambient``：不看意图（MCP 每台 server 一组，以及未声明规则的插件），预算不够时最先让位。

入选顺序 core → 命中的 intent → request（被勾选的）→ 上一轮实际调用过的组（sticky，只回看一轮）→ ambient，同类按插件
顺序；按这个顺序逐组入选，放不下的整组跳过并记进 ``dropped``。**request 排在 intent 之后**：用户已经明确说了要读图 / 统计
材料时，这些本地工具优先；预算最紧（读图 + 材料 = 4497）放不下 request 组时它被整组让位，由调用方向用户发
``WEB_SEARCH_NOT_EXPOSED`` 警告——用户勾选了联网却被静默丢弃，会让用户以为查过了。两项预算：发出的 schema ≤ 4500
（与 ``tests/test_answer_cache.py`` 同一上限、同一序列化口径），按意图格式渲染的工具指引 ≤ 800
（``SEGMENT_BUDGETS["tool_guide"]``）。

回滚开关 ``WATER_AGENT_TOOL_EXPOSURE``：``intent``（缺省）或 ``full``（全量暴露、现行指引格式，
与 P7-S3 行为一致）；其他值装配期抛 ``ValueError``。
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

ExposureMode = Literal["core", "intent", "request", "ambient"]
ExposureStrategy = Literal["intent", "full"]

EXPOSURE_ENV = "WATER_AGENT_TOOL_EXPOSURE"
#: 一轮发出的工具 schema 上限（字符数，``json.dumps(..., ensure_ascii=False)``）。与
#: ``tests/test_answer_cache.py`` 的受保护断言是同一个数，**不得上调**。
EXPOSED_SCHEMA_BUDGET = 4500
#: 工具指引段的预算，取自 ``prompting.assembler.SEGMENT_BUDGETS["tool_guide"]``（不在这里改）。
GUIDE_HEADER = "【可用工具】"
UNEXPOSED_TEMPLATE = "（本轮未开放：{names}。需要时请让用户说明具体需求）"


def resolve_exposure_strategy(environ: Mapping[str, str]) -> ExposureStrategy:
    """读回滚开关。空值即缺省 ``intent``；写错不静默回退成全量。"""

    value = (environ.get(EXPOSURE_ENV) or "").strip()
    if value in ("", "intent"):
        return "intent"
    if value == "full":
        return "full"
    raise ValueError(f"{EXPOSURE_ENV} 只接受 intent / full，收到 {value!r}")


def normalize(text: str) -> str:
    """匹配前的规范化：NFKC（全角数字 / 字母转半角）+ 小写（只影响有大小写的字母）。"""

    return unicodedata.normalize("NFKC", text).lower()


@dataclass(frozen=True, slots=True, kw_only=True)
class ExposureRule:
    """一组工具何时开放。``keywords`` 是规范化后的子串，``patterns`` 是对规范化文本的正则。"""

    mode: ExposureMode
    keywords: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode != "intent" and (self.keywords or self.patterns):
            raise ValueError(f"只有 intent 规则可以带关键词 / 正则（mode={self.mode!r}）")
        if self.mode == "intent" and not (self.keywords or self.patterns):
            raise ValueError("intent 规则至少要有一个关键词或正则，否则永远不会开放")
        object.__setattr__(self, "keywords", tuple(normalize(word) for word in self.keywords))
        for pattern in self.patterns:
            re.compile(pattern)  # 非法正则在构造期抛 re.error

    def matches(self, normalized_query: str) -> bool:
        if any(word and word in normalized_query for word in self.keywords):
            return True
        return any(re.search(pattern, normalized_query) for pattern in self.patterns)


CORE = ExposureRule(mode="core")
AMBIENT = ExposureRule(mode="ambient")
#: 由入口标志决定的组（P7-S12 联网搜索）：不看 query。
REQUEST = ExposureRule(mode="request")


@dataclass(frozen=True, slots=True)
class ToolGroup:
    """一组一起开放、一起丢弃的工具。内置组的 id 是 ``plugin_id``，MCP 是 ``mcp:<server_id>``。"""

    group_id: str
    tools: tuple[str, ...]
    rule: ExposureRule


@dataclass(frozen=True, slots=True)
class ExposureDecision:
    strategy: ExposureStrategy
    exposed: tuple[str, ...]
    reasons: tuple[tuple[str, str], ...]
    dropped: tuple[tuple[str, str], ...]
    schema_chars: int
    #: 按意图格式渲染的指引字符数；``full`` 策略下指引用现行格式，由提示装配器渲染，记 ``None``。
    guide_chars: int | None
    unexposed: tuple[str, ...] = ()

    def to_state(self) -> dict[str, Any]:
        """写进图 state / trace 的形式（只含工具名与原因，不含 query）。"""

        return {
            "strategy": self.strategy,
            "exposed": list(self.exposed),
            "unexposed": list(self.unexposed),
            "reasons": [list(item) for item in self.reasons],
            "dropped": [list(item) for item in self.dropped],
            "schema_chars": self.schema_chars,
            "guide_chars": self.guide_chars,
        }


def bound_tool_definition(tool: Any) -> dict[str, Any]:
    """一个 LangChain 工具发给模型时的定义。``_BudgetChatModel.bind_tools`` 与预算判定共用它，
    保证"预算按什么算"和"实际发出什么"是同一个东西。"""

    schema = tool.args_schema.model_json_schema() if getattr(tool, "args_schema", None) else {}
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": schema,
        },
    }


def schema_chars(definitions: Sequence[dict[str, Any]]) -> int:
    return len(json.dumps(list(definitions), ensure_ascii=False))


def guide_line(name: str, description: str) -> str:
    """按意图格式的一行指引：工具名 + 描述首句（完整描述模型在 schema 里照样看得到）。"""

    first = (description or "").splitlines()[0] if description else ""
    if "。" in first:
        first = first[: first.index("。") + 1]
    return f"- {name}：{first}"


def render_guide(lines: Sequence[str], unexposed: Sequence[str]) -> str:
    """按意图格式的工具指引段。未开放的工具只列名字，让模型知道它们存在、需要时请用户说明。"""

    if not lines and not unexposed:
        return ""
    parts = [GUIDE_HEADER, *lines]
    if unexposed:
        parts.append(UNEXPOSED_TEMPLATE.format(names="、".join(unexposed)))
    return "\n".join(parts)


class ToolExposurePolicy:
    """由 ``assemble_tools`` 构造；``decide`` 每轮调用一次。"""

    def __init__(
        self,
        *,
        strategy: ExposureStrategy,
        groups: Sequence[ToolGroup],
        definitions: Mapping[str, dict[str, Any]],
        order: Sequence[str],
        guide_budget: int,
        schema_budget: int = EXPOSED_SCHEMA_BUDGET,
    ) -> None:
        self.strategy: ExposureStrategy = strategy
        self.groups = tuple(groups)
        self.order = tuple(order)
        self.schema_budget = schema_budget
        self.guide_budget = guide_budget
        self._definitions = dict(definitions)
        ids = [group.group_id for group in self.groups]
        if len(set(ids)) != len(ids):
            raise ValueError(f"工具组 id 重复：{ids}")
        grouped = [name for group in self.groups for name in group.tools]
        if sorted(grouped) != sorted(self.order) or len(set(grouped)) != len(grouped):
            raise ValueError(f"每个已注册工具必须恰好属于一个组：注册 {list(self.order)}，分组 {grouped}")
        self._lines = {
            name: guide_line(name, str(self._definitions[name]["function"]["description"]))
            for name in self.order
        }
        core = [group for group in self.groups if group.rule.mode == "core"]
        core_tools = {name for group in core for name in group.tools}
        if self.strategy == "intent":
            schema_used, guide_used = self._measure(core_tools)
            if schema_used > self.schema_budget or guide_used > self.guide_budget:
                raise ValueError(
                    f"core 组自身超预算（schema {schema_used}/{self.schema_budget}，"
                    f"指引 {guide_used}/{self.guide_budget}）：这是编程错误，不能降级"
                )

    @property
    def tool_order(self) -> tuple[str, ...]:
        return self.order

    def guide_line(self, name: str) -> str:
        return self._lines[name]

    def _ordered(self, names: Collection[str]) -> list[str]:
        return [name for name in self.order if name in names]

    def _measure(self, names: Collection[str]) -> tuple[int, int]:
        exposed = self._ordered(names)
        unexposed = [name for name in self.order if name not in names]
        schema = schema_chars([self._definitions[name] for name in exposed])
        guide = len(render_guide([self._lines[name] for name in exposed], unexposed))
        return schema, guide

    def render_guide(self, exposed: Collection[str]) -> str:
        """按意图格式渲染本轮指引（提示装配器调用）；与预算判定用的是同一个渲染函数。"""

        ordered = self._ordered(exposed)
        unexposed = [name for name in self.order if name not in exposed]
        return render_guide([self._lines[name] for name in ordered], unexposed)

    def decide(
        self,
        query: str,
        *,
        previous_turn_tools: Collection[str] = (),
        request_enabled: Collection[str] = (),
    ) -> ExposureDecision:
        """``request_enabled``：本条消息由入口标志开启的 ``request`` 组 id（缺省空）。不传时与此前逐字相同。"""

        if self.strategy == "full":
            return ExposureDecision(
                strategy="full",
                exposed=self.order,
                reasons=tuple((group.group_id, "full") for group in self.groups),
                dropped=(),
                schema_chars=schema_chars([self._definitions[name] for name in self.order]),
                guide_chars=None,
            )
        text = normalize(query)
        previous = set(previous_turn_tools)
        candidates: list[tuple[ToolGroup, str]] = []
        taken: set[str] = set()

        def offer(group: ToolGroup, reason: str) -> None:
            if group.group_id not in taken:
                taken.add(group.group_id)
                candidates.append((group, reason))

        for group in self.groups:
            if group.rule.mode == "core":
                offer(group, "core")
        for group in self.groups:
            if group.rule.mode == "intent" and group.rule.matches(text):
                offer(group, "intent")
        requested = set(request_enabled)
        for group in self.groups:
            if group.rule.mode == "request" and group.group_id in requested:
                offer(group, "request")
        for group in self.groups:
            # request 组不参与 sticky：勾选只对那一条消息有效，上一轮调用过 web_search 不得让这一轮自动开放
            if group.rule.mode != "request" and previous & set(group.tools):
                offer(group, "sticky")
        for group in self.groups:
            if group.rule.mode == "ambient":
                offer(group, "ambient")

        chosen: set[str] = set()
        reasons: list[tuple[str, str]] = []
        dropped: list[tuple[str, str]] = []
        for group, reason in candidates:
            trial = chosen | set(group.tools)
            schema_used, guide_used = self._measure(trial)
            if reason != "core" and schema_used > self.schema_budget:
                dropped.append((group.group_id, "schema_budget"))
                continue
            if reason != "core" and guide_used > self.guide_budget:
                dropped.append((group.group_id, "guide_budget"))
                continue
            chosen = trial
            reasons.append((group.group_id, reason))
        schema_used, guide_used = self._measure(chosen)
        return ExposureDecision(
            strategy="intent",
            exposed=tuple(self._ordered(chosen)),
            reasons=tuple(reasons),
            dropped=tuple(dropped),
            schema_chars=schema_used,
            guide_chars=guide_used,
            unexposed=tuple(name for name in self.order if name not in chosen),
        )


__all__ = [
    "AMBIENT",
    "CORE",
    "EXPOSED_SCHEMA_BUDGET",
    "EXPOSURE_ENV",
    "REQUEST",
    "ExposureDecision",
    "ExposureMode",
    "ExposureRule",
    "ExposureStrategy",
    "ToolExposurePolicy",
    "ToolGroup",
    "bound_tool_definition",
    "guide_line",
    "normalize",
    "render_guide",
    "resolve_exposure_strategy",
    "schema_chars",
]
