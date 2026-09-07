"""P2.5 护栏管线与工具策略（Step 14）。

固定顺序（规格 §6，不可重排）：

```
主体/请求上限 → 输入规范化与字节上限 → PII/secret 检测及策略处理
→ 直接注入检测 → 来源标签与不可信内容隔离 → 工具前置验证
→ provider 调用 → 输出格式/引用/PII/secret/策略检查 → 脱敏 trace/cache
```

**不可绕过**：`ToolRegistry.invoke` 在 Step 14 后**必须**先过
:meth:`GuardrailPipeline.validate_tool_call`。未注入 pipeline 时取进程级默认实例
（与 `binding.py::default_gateway` 同思路），因此不存在"不传即绕过"的后门。

**与既有检查是组合而非替代**（规格 §6）：输出护栏不替代
`validate_citations` 与 `docgen/quality.py` 的数值/结构自检；它在其**之外**
追加 PII/secret/危险指令检查。
"""
from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from water_agent.runtime.governance.contracts import (
    CODE_GUARDRAIL_BLOCKED,
    CODE_INPUT_TOO_LARGE,
    CODE_SOURCE_TAG_MISSING,
    Capability,
    ContentEnvelope,
    GovernanceError,
    PiiMode,
    PolicyAction,
    PolicyDecision,
    Principal,
    RequestContext,
    SourceTag,
    is_instruction_bearing,
)
from water_agent.runtime.governance.inventory import capability_for_tool
from water_agent.runtime.governance.rules import (
    MAX_CONTENT_BYTES,
    MAX_USER_INPUT_BYTES,
    RULESET_VERSION,
    RuleCategory,
    RuleHit,
    check_size,
    detect_dangerous_execution,
    detect_injection,
    detect_pii,
    detect_secrets,
    redact_text,
)


@dataclass(frozen=True, slots=True)
class GuardrailOutcome:
    """一次护栏检查的结果。

    ``content`` 是**处理后**内容（脱敏可能改写）；``decision`` 是裁定；
    ``hits`` 是脱敏摘要列表（**不含原始命中文本**）。
    """

    decision: PolicyDecision
    content: str
    hits: tuple[RuleHit, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.decision.allowed

    @property
    def redacted(self) -> bool:
        return self.decision.action is PolicyAction.REDACT

    def raise_if_blocked(self) -> None:
        self.decision.raise_if_blocked()

    def trace_payload(self) -> dict[str, Any]:
        """trace 用脱敏载荷：只有规则 ID、动作、计数与摘要。"""
        return {
            "action": self.decision.action.value,
            "rule_id": self.decision.rule_id,
            "ruleset_version": self.decision.ruleset_version,
            "code": self.decision.code,
            "detail": self.decision.detail,
            "hits": [
                {"rule_id": h.rule_id, "category": h.category.value, "count": h.count}
                for h in self.hits
            ],
        }


def _allow(rule_id: str, detail: str = "") -> PolicyDecision:
    return PolicyDecision(
        action=PolicyAction.ALLOW, rule_id=rule_id,
        ruleset_version=RULESET_VERSION, detail=detail,
    )


def _redact(rule_id: str, detail: str) -> PolicyDecision:
    return PolicyDecision(
        action=PolicyAction.REDACT, rule_id=rule_id,
        ruleset_version=RULESET_VERSION, detail=detail,
    )


def _block(rule_id: str, code: str, detail: str) -> PolicyDecision:
    return PolicyDecision(
        action=PolicyAction.BLOCK, rule_id=rule_id, ruleset_version=RULESET_VERSION,
        code=code, detail=detail,
    )


def _summarize(hits: Sequence[RuleHit]) -> str:
    """脱敏摘要：规则 ID + 计数，绝不含命中文本。"""
    return "; ".join(f"{h.rule_id}×{h.count}" for h in hits)


# ---------------------------------------------------------------------------
# 工具策略
# ---------------------------------------------------------------------------

#: 路径参数应按**只读**白名单裁定的能力集（P3 Step 18）。
#: 不在此集合内的能力一律查 `allowed_export_dirs`（默认空集 = 全拒）。
#: 新增只读文件能力必须同时加进这里，否则它的路径参数会被按"写出"口径裁定 ——
#: 那样只会更严（读白名单配了也不放行），不会更松，故遗漏是 fail closed 的。
_READ_ONLY_PATH_CAPABILITIES: frozenset[Capability] = frozenset({Capability.READ_DRAWING})


def _within_allowed_dirs(target: Path, allowed_dirs: frozenset[Path]) -> bool:
    """``target`` 是否落在 ``allowed_dirs`` 之内（解析后比较，防路径穿越）。

    读、写两个白名单共用本函数 —— 两份各自实现必然漂移，而"能不能碰这个路径"
    的判定逻辑一旦漂移就是安全缺口。
    """
    try:
        resolved = target.resolve()
    except (OSError, RuntimeError):
        return False
    for allowed in allowed_dirs:
        try:
            resolved.relative_to(allowed.resolve())
        except (ValueError, OSError):
            continue
        return True
    return False


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """工具执行策略。

    P2.5 默认（规格 §6）：只允许**只读检索、纯计算、导出到用户显式允许目录**。
    新写入、网络、shell、邮件工具在 P4 授权/人工确认前**默认拒绝** ——
    由 `Principal` 构造期拒绝授予危险能力 + 本策略的 allowlist 双重保证。

    P3 Step 18：读与写是**两个独立白名单**。`allowed_read_dirs` 授予"能读"，
    `allowed_export_dirs` 授予"能写出"，互不蕴含 —— 用户让 Agent 看图纸，
    不等于允许它往图纸目录里写东西（决策记录 `p3-step-17-decision.md` D4）。
    """

    #: 允许导出的目录（绝对路径）。空集合 = 不允许任何文件写入。
    allowed_export_dirs: frozenset[Path] = frozenset()
    #: 允许**只读**访问的目录（绝对路径）。空集合 = 不允许任何文件读取。
    #: 与 `allowed_export_dirs` 语义分离：在此不授予任何写权限。
    allowed_read_dirs: frozenset[Path] = frozenset()
    #: 允许访问的域名。空集合 = 不允许任何工具发起网络请求
    #: （当前工具都不需要，provider 调用走 gateway 而非工具）。
    allowed_domains: frozenset[str] = frozenset()
    #: 单个工具参数的字节上限。
    max_arg_bytes: int = 64 * 1024

    def export_dir_allowed(self, target: Path) -> bool:
        """目标路径是否可**写出**（解析后比较，防路径穿越）。"""
        return _within_allowed_dirs(target, self.allowed_export_dirs)

    def read_dir_allowed(self, target: Path) -> bool:
        """目标路径是否可**读取**（解析后比较，防路径穿越）。

        **不**回落到 `allowed_export_dirs`：可写目录未必是用户想让模型读的目录，
        反向蕴含同样不成立。两个白名单都为空时读写皆拒（fail closed）。
        """
        return _within_allowed_dirs(target, self.allowed_read_dirs)


#: P2.5 默认工具策略：**不允许任何文件读写与网络访问**。
#: 材料导出当前由 CLI 显式触发（不是模型可调用的工具），故无需 allowlist；
#: 若日后把导出暴露为工具，必须在此配置 `allowed_export_dirs`。
#: P3 的图纸读取工具由 CLI 装配期注入带 `allowed_read_dirs` 的策略，默认仍是空集。
DEFAULT_TOOL_POLICY = ToolPolicy()


# ---------------------------------------------------------------------------
# 护栏管线
# ---------------------------------------------------------------------------


class GuardrailPipeline:
    """分层护栏：输入 → 来源隔离 → 工具前置 → 输出 → 脱敏。

    Args:
        policy: 工具策略。
        pii_mode: 默认 PII 处理模式；``RequestContext.pii_mode`` 优先。
        declared_capabilities: **装配期**声明的工具→能力映射，用于治理清单之外
            的工具（测试夹具、尚未进清单的实验工具）。它**不放松**任何检查 ——
            声明的能力照样要过主体授权、危险能力拒绝、大小与 allowlist；
            它只是把"这个工具需要什么能力"这件事变成**显式声明**而不是默认放行。
            生产 4 个工具走 `inventory.TOOL_CAPABILITIES`，不用这里。
    """

    def __init__(
        self,
        *,
        policy: ToolPolicy | None = None,
        pii_mode: PiiMode = PiiMode.REDACT,
        declared_capabilities: Mapping[str, Capability] | None = None,
    ) -> None:
        self.policy = policy if policy is not None else DEFAULT_TOOL_POLICY
        self.default_pii_mode = pii_mode
        self.ruleset_version = RULESET_VERSION
        self.declared_capabilities: Mapping[str, Capability] = dict(declared_capabilities or {})

    # -- 1. 输入 ---------------------------------------------------------------

    def check_user_input(self, text: str, ctx: RequestContext) -> GuardrailOutcome:
        """用户输入检查：上限 → PII/secret → 注入。

        注入命中一律 **BLOCK**（用户直接注入没有合理用途）；
        PII/secret 按 ``pii_mode`` 处理（默认 redact）。
        """
        size_hits = check_size(text, limit_bytes=MAX_USER_INPUT_BYTES, label="用户输入")
        if size_hits:
            return GuardrailOutcome(
                _block(size_hits[0].rule_id, CODE_INPUT_TOO_LARGE, size_hits[0].detail),
                text, tuple(size_hits),
            )

        injection_hits = detect_injection(text)
        if injection_hits:
            return GuardrailOutcome(
                _block(injection_hits[0].rule_id, CODE_GUARDRAIL_BLOCKED,
                       f"检测到提示注入：{_summarize(injection_hits)}"),
                text, tuple(injection_hits),
            )

        return self._apply_pii_policy(text, ctx, stage="input")

    def _apply_pii_policy(
        self, text: str, ctx: RequestContext, *, stage: str
    ) -> GuardrailOutcome:
        """按 ``pii_mode`` 处理 PII/secret。

        - ``REDACT``（默认）：替换为占位符，动作 REDACT；
        - ``BLOCK``：拒绝；
        - ``ALLOW``：放行但**仍记录命中**（必须可审计，规格 §2.2）。
        """
        mode = ctx.pii_mode if ctx is not None else self.default_pii_mode
        pii_hits = detect_pii(text)
        secret_hits = detect_secrets(text)
        hits = tuple(pii_hits + secret_hits)
        if not hits:
            return GuardrailOutcome(_allow(f"{stage}.clean"), text)

        # 凭据在**任何模式下**都必须脱敏：API Key 泄漏没有"允许"的场景
        if secret_hits and mode is PiiMode.ALLOW:
            redacted, _ = redact_text(text)
            return GuardrailOutcome(
                _redact("secret.force_redact",
                        f"凭据强制脱敏（pii_mode=allow 不适用于凭据）：{_summarize(hits)}"),
                redacted, hits,
            )
        if mode is PiiMode.BLOCK:
            return GuardrailOutcome(
                _block(f"{stage}.pii_blocked", CODE_GUARDRAIL_BLOCKED,
                       f"内容含敏感信息且策略为 block：{_summarize(hits)}"),
                text, hits,
            )
        if mode is PiiMode.ALLOW:
            return GuardrailOutcome(
                _allow(f"{stage}.pii_allowed",
                       f"pii_mode=allow，已记录命中：{_summarize(hits)}"),
                text, hits,
            )
        redacted, _ = redact_text(text)
        return GuardrailOutcome(
            _redact(f"{stage}.pii_redacted", f"已脱敏：{_summarize(hits)}"), redacted, hits
        )

    # -- 2. 来源隔离 -----------------------------------------------------------

    def wrap_untrusted(
        self, text: str, *, source_tag: SourceTag, reference_id: str = ""
    ) -> ContentEnvelope:
        """把不可信内容包成带来源标签的信封。

        Raises:
            GovernanceError: 试图用 ``SYSTEM_POLICY`` 标记外部内容
                （那等于把不可信文本升格为系统指令，规格 §6 禁止）。
        """
        if is_instruction_bearing(source_tag):
            raise GovernanceError(
                CODE_SOURCE_TAG_MISSING,
                f"禁止把外部内容标记为可承载指令的来源 {source_tag.value!r}",
            )
        return ContentEnvelope(source_tag=source_tag, text=text, reference_id=reference_id)

    def check_retrieved_content(self, envelope: ContentEnvelope) -> GuardrailOutcome:
        """检索/工具内容的**间接注入**检查。

        与用户输入的关键差别：间接注入**不 BLOCK 整条内容**，
        而是标记为 REDACT 并要求调用方以数据形态隔离 ——
        条文里出现"忽略以上指令"多半是语料被投毒或条文本身含引用性文字，
        直接丢弃会让正常检索失效。真正的防线是"该内容不能成为指令"
        （`ContentEnvelope` 已保证），检测只提供可观测信号。
        """
        if envelope.is_instruction_bearing:
            return GuardrailOutcome(
                _block("source.instruction_bearing_external", CODE_SOURCE_TAG_MISSING,
                       f"来源 {envelope.source_tag.value} 不得承载指令"),
                envelope.text,
            )
        size_hits = check_size(
            envelope.text, limit_bytes=MAX_CONTENT_BYTES,
            label=f"{envelope.source_tag.value} 内容",
        )
        if size_hits:
            return GuardrailOutcome(
                _block(size_hits[0].rule_id, CODE_INPUT_TOO_LARGE, size_hits[0].detail),
                envelope.text, tuple(size_hits),
            )
        injection_hits = detect_injection(envelope.text)
        secret_hits = detect_secrets(envelope.text)
        hits = tuple(injection_hits + secret_hits)
        if not hits:
            return GuardrailOutcome(_allow("retrieved.clean"), envelope.text)
        redacted, _ = redact_text(envelope.text)
        return GuardrailOutcome(
            _redact("retrieved.indirect_injection",
                    f"检索内容含可疑指令/凭据，已按数据隔离并脱敏：{_summarize(hits)}"),
            redacted, hits,
        )

    # -- 3. 工具前置验证（不可绕过） --------------------------------------------

    def validate_tool_call(
        self, tool_name: str, args: dict[str, Any], principal: Principal
    ) -> GuardrailOutcome:
        """``ToolRegistry.invoke`` 执行前的强制校验。

        顺序：capability 登记 → 主体授权 → 参数大小 → 路径/域名 allowlist
        → 参数注入标记。**未登记的工具 fail closed**（`capability_for_tool` 抛 KeyError）。
        """
        try:
            capability = capability_for_tool(tool_name)
        except KeyError:
            declared = self.declared_capabilities.get(tool_name)
            if declared is None:
                # 既不在治理清单、也未装配期声明 = 不可判定 = 拒绝（fail closed）
                return GuardrailOutcome(
                    _block("tool.unregistered", CODE_GUARDRAIL_BLOCKED,
                           f"工具 {tool_name!r} 未在治理清单登记且未声明能力（fail closed）"),
                    "",
                )
            capability = declared

        if not principal.has(capability):
            return GuardrailOutcome(
                _block("tool.capability_denied", CODE_GUARDRAIL_BLOCKED,
                       f"主体未被授予能力 {capability.value!r}"),
                "",
            )
        if capability in {Capability.WRITE_FILE, Capability.NETWORK_FETCH,
                          Capability.EXECUTE_SHELL, Capability.SEND_MESSAGE}:
            return GuardrailOutcome(
                _block("tool.dangerous_capability", CODE_GUARDRAIL_BLOCKED,
                       f"能力 {capability.value!r} 在 P4 授权与人工确认前默认拒绝"),
                "",
            )

        rendered = _render_args(args)
        size_hits = check_size(
            rendered, limit_bytes=self.policy.max_arg_bytes, label=f"{tool_name} 参数"
        )
        if size_hits:
            return GuardrailOutcome(
                _block(size_hits[0].rule_id, CODE_INPUT_TOO_LARGE, size_hits[0].detail),
                "", tuple(size_hits),
            )

        path_decision = self._check_paths_and_domains(tool_name, args, capability)
        if path_decision is not None:
            return GuardrailOutcome(path_decision, "")

        injection_hits = detect_injection(rendered)
        if injection_hits:
            return GuardrailOutcome(
                _block("tool.injected_args", CODE_GUARDRAIL_BLOCKED,
                       f"工具参数含注入标记：{_summarize(injection_hits)}"),
                "", tuple(injection_hits),
            )
        return GuardrailOutcome(_allow("tool.allowed", f"capability={capability.value}"), "")

    def _check_paths_and_domains(
        self, tool_name: str, args: dict[str, Any], capability: Capability
    ) -> PolicyDecision | None:
        """路径穿越与域名 allowlist 检查。返回 ``None`` 表示通过。

        白名单按 **capability** 选，不按参数名启发式：参数名启发式
        （"叫 path 的查读、叫 dest 的查写"）可被换个参数名绕过，
        而 capability 由治理清单 `TOOL_CAPABILITIES` 静态登记、模型无法影响。
        只读能力查 `allowed_read_dirs`，其余一律查 `allowed_export_dirs`
        （默认空集 = 全拒，fail closed 语义不变）。

        **已知局限**：`_looks_like_path` 刻意保守，不识别相对路径
        （见 `docs/design/p2.5-threat-model.md` §5 第 23 条）。
        新增直接收路径参数的工具必须在工具层自行校验绝对路径，
        不得依赖本函数作为唯一防线。
        """
        path_allowed = (
            self.policy.read_dir_allowed
            if capability in _READ_ONLY_PATH_CAPABILITIES
            else self.policy.export_dir_allowed
        )
        for key, value in _iter_strings(args):
            if _looks_like_path(value) and not path_allowed(Path(value)):
                return _block(
                    "tool.path_not_allowed", CODE_GUARDRAIL_BLOCKED,
                    f"参数 {key} 指向不在允许目录内的路径（工具 {tool_name}）",
                )
            if _looks_like_url(value) and _host_of(value) not in self.policy.allowed_domains:
                return _block(
                    "tool.domain_not_allowed", CODE_GUARDRAIL_BLOCKED,
                    f"参数 {key} 指向不在 allowlist 内的域名（工具 {tool_name}）",
                )
        return None

    # -- 4. 输出检查 -----------------------------------------------------------

    def check_output(
        self,
        text: str,
        ctx: RequestContext,
        *,
        citations_verified: bool = True,
        structured_output_ok: bool = True,
    ) -> GuardrailOutcome:
        """输出检查。**与既有引用/数值/结构校验组合，不替代它们**（规格 §6）。

        Args:
            citations_verified: 既有 `validate_citations` 的结论。
            structured_output_ok: 既有结构/数值自检的结论。

        危险执行指令、未验证引用、结构不合规 → **fail closed**（BLOCK）。
        PII/secret 按 ``pii_mode`` 处理。
        """
        if not citations_verified:
            return GuardrailOutcome(
                _block("output.unverified_citation", CODE_GUARDRAIL_BLOCKED,
                       "引用未通过校验，拒绝返回（fail closed）"),
                text,
            )
        if not structured_output_ok:
            return GuardrailOutcome(
                _block("output.structure_invalid", CODE_GUARDRAIL_BLOCKED,
                       "结构化输出不合规，拒绝返回（fail closed）"),
                text,
            )
        danger_hits = detect_dangerous_execution(text)
        if danger_hits:
            return GuardrailOutcome(
                _block(danger_hits[0].rule_id, CODE_GUARDRAIL_BLOCKED,
                       f"输出含危险执行指令：{_summarize(danger_hits)}"),
                text, tuple(danger_hits),
            )
        return self._apply_pii_policy(text, ctx, stage="output")

    # -- 5. 记忆写入守卫 -------------------------------------------------------

    def check_memory_write(self, envelope: ContentEnvelope) -> GuardrailOutcome:
        """`MemoryWriteGuard`：拒绝把指令形态的外部内容写入会话记忆。

        规格 §6：「任何后续可写 memory 的功能必须先有 MemoryWriteGuard；
        当前会话 checkpointer 不能把外部内容错误标作可信系统规则」。
        持久化记忆一旦被投毒会**跨轮反复污染**，比单轮注入更严重。
        """
        if envelope.is_instruction_bearing:
            return GuardrailOutcome(
                _block("memory.instruction_bearing", CODE_SOURCE_TAG_MISSING,
                       "禁止把可承载指令的来源写入会话记忆"),
                envelope.text,
            )
        injection_hits = detect_injection(envelope.text)
        if injection_hits:
            return GuardrailOutcome(
                _block("memory.directive_like", CODE_GUARDRAIL_BLOCKED,
                       f"拒绝写入指令形态内容（防持久化投毒）：{_summarize(injection_hits)}"),
                envelope.text, tuple(injection_hits),
            )
        return self._apply_pii_policy(
            envelope.text,
            RequestContext(principal=Principal.local("memory-guard"), request_id="mem"),
            stage="memory",
        )

    # -- 6. trace/cache 脱敏 ---------------------------------------------------

    def sanitize_for_persistence(self, text: str) -> tuple[str, tuple[RuleHit, ...]]:
        """落 trace / 写缓存前的强制脱敏。**无条件执行**，不看 pii_mode。

        理由：`pii_mode=allow` 可能允许把 PII 送往模型（用户显式授权），
        但**磁盘留存是另一件事** —— trace 与缓存长期存在，必须始终脱敏。
        """
        redacted, hits = redact_text(text)
        return redacted, tuple(hits)


# ---------------------------------------------------------------------------
# 参数遍历辅助
# ---------------------------------------------------------------------------


def _render_args(args: dict[str, Any]) -> str:
    import json

    try:
        return json.dumps(args, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(args)


def _iter_strings(value: Any, prefix: str = "") -> list[tuple[str, str]]:
    """递归收集 (键路径, 字符串值)。"""
    out: list[tuple[str, str]] = []
    if isinstance(value, str):
        out.append((prefix or "<root>", value))
    elif isinstance(value, dict):
        for key, item in value.items():
            out.extend(_iter_strings(item, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            out.extend(_iter_strings(item, f"{prefix}[{index}]"))
    return out


def _looks_like_path(value: str) -> bool:
    """是否像文件系统路径。刻意保守：只认明确的路径形态。

    避免把 `GB50015-2019 3.1.1`、`DN200/DN150` 等正常内容误判为路径。
    """
    if len(value) > 4096 or "\n" in value:
        return False
    if value.startswith(("/", "~", "./", "../")) or value[1:3] in (":\\", ":/"):
        return True
    return "../" in value or "..\\" in value


def _looks_like_url(value: str) -> bool:
    return value.startswith(("http://", "https://", "ftp://", "file://"))


def _host_of(url: str) -> str:
    from urllib.parse import urlparse

    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# 进程级默认实例（消灭"不传即绕过"的后门）
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_default_pipeline: GuardrailPipeline | None = None


def default_guardrail_pipeline() -> GuardrailPipeline:
    """进程级默认护栏管线（懒构造单例）。

    `ToolRegistry.invoke` 未注入 pipeline 时取用它 —— **不是跳过校验**。
    """
    global _default_pipeline
    with _lock:
        if _default_pipeline is None:
            _default_pipeline = GuardrailPipeline()
        return _default_pipeline


def set_default_guardrail_pipeline(pipeline: GuardrailPipeline | None) -> None:
    """替换默认管线（测试与 CLI 装配用）。传 ``None`` 恢复懒构造。"""
    global _default_pipeline
    with _lock:
        _default_pipeline = pipeline


__all__ = [
    "DEFAULT_TOOL_POLICY",
    "GuardrailOutcome",
    "GuardrailPipeline",
    "RuleCategory",
    "ToolPolicy",
    "default_guardrail_pipeline",
    "set_default_guardrail_pipeline",
]
