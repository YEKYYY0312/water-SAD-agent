"""系统提示装配器（Step 8）。

按需组装系统提示，分段（缓存友好：稳定区在前、易变区在后）：
1. 角色与铁律（静态）
2. 规范版本注册表快照（动态，来自 data/versions.json）
3. 公式口径（静态，ADR-01）
4. 工具使用指南（随 ToolRegistry 动态生成）

每段带 token 预算（估算函数）：动态段超预算按行截断并标注省略行数；静态段超预算
视为编程错误直接抛错（规则文案不可静默截断）。
"""
from __future__ import annotations

from typing import Any

from langchain_core.messages import BaseMessage, SystemMessage

from water_agent.ingestion.version_registry import get_version_registry
from water_agent.tools.registry import ToolRegistry

# 段级 token 预算（粗估：中文约 1 token/字符，取整上限）
SEGMENT_BUDGETS: dict[str, int] = {
    "role_and_principles": 400,
    "version_snapshot": 400,
    "formula_specs": 400,
    "tool_guide": 800,
}

# 静态段：角色与铁律（不变，缓存友好放在最前）
ROLE_AND_PRINCIPLES = """你是给排水专业标准问答助手，处于工具调用型 Agent 环境。

【三条铁律】
1. 计算程序化：数值一律由水力计算工具算出，你不参与数值运算，只做参数理解与结果解释。
2. 引用可追溯：回答必须指回「规范号 + 条款号」，只引用检索结果中真实存在的条款，禁止编造规范号或条文。
3. 检索不足如实说明：未检索到相关内容就明说；参数缺失就说明缺什么，不猜测补默认值。"""


# 静态段：公式口径（ADR-01：公式由输入显式指定，禁止静默切换）
FORMULA_SPECS = """【公式口径（ADR-01）】
- 所有计算公式由输入显式指定，禁止静默切换；
- calc_velocity：流速 v = Q/A，需 diameter_mm 与 flow_lps；
- 水头损失/管径/坡度等计算需显式指定 formula 参数，口径详见 docs/design/09-hydraulics-formulas.md；
- 缺参返回 missing_inputs（向用户澄清，不猜参数）；越界返回 assumptions（回答中必须明示）。"""


def _estimate_tokens(text: str) -> int:
    """粗略 token 估算：按字符数计（中文 1 字符 ≈ 1 token，保守高估）。"""
    return len(text)


# 截断标记模板（预留字符数计入预算，保证最终长度 ≤ budget）
_OMITTED_TEMPLATE = "\n…（受预算限制省略 {n} 行）"
_OMITTED_RESERVE = len(_OMITTED_TEMPLATE.format(n=9999))


def _truncate_to_budget(text: str, budget: int) -> str:
    """按预算截断文本（按行聚合，超预算即停），并**显式标注省略行数**。

    动态段（版本快照/工具指南）被截断意味着模型看到的是残缺清单；不加标记会让模型
    把"没列出"误当"不存在"，与"引用可追溯"铁律冲突，所以截断必须可见。
    """
    if _estimate_tokens(text) <= budget:
        return text
    all_lines = text.splitlines(keepends=True)
    kept: list[str] = []
    used = 0
    limit = max(budget - _OMITTED_RESERVE, 0)
    for line in all_lines:
        line_len = _estimate_tokens(line)
        if used + line_len > limit:
            break
        kept.append(line)
        used += line_len
    omitted = len(all_lines) - len(kept)
    return "".join(kept).rstrip() + _OMITTED_TEMPLATE.format(n=omitted)


def _check_static_budget(name: str, text: str) -> str:
    """静态段不截断（截了规则就失真），超预算视为编程错误，立即失败。"""
    budget = SEGMENT_BUDGETS[name]
    used = _estimate_tokens(text)
    if used > budget:
        raise ValueError(f"静态提示段 {name!r} 超预算：{used} > {budget}，请精简文案或调高 SEGMENT_BUDGETS")
    return text


class PromptAssembler:
    """提示词装配器：按段组装 + 段级预算。

    ``create_react_agent`` 的 ``prompt`` 参数支持可调用对象（签名
    ``(state) -> list[BaseMessage] | str | SystemMessage``），本类以 ``__call__``
    提供该签名，可直接作为 ``prompt=`` 传入。
    """

    def __init__(
        self,
        *,
        version_registry: Any | None = None,
        tool_registry: ToolRegistry | None = None,
    ):
        self._version_registry = version_registry if version_registry is not None else get_version_registry()
        self._tool_registry = tool_registry

    def __call__(self, state: dict[str, Any] | None = None) -> list[BaseMessage]:
        """``create_react_agent`` 的 ``prompt`` 可调用签名。

        接收 state（含 messages），返回 ``[SystemMessage, ...] + state_messages``。
        """
        if state is None:
            state = {}
        messages = list(state.get("messages", []))
        sys_msg = self.assemble_prompt(state)
        return [sys_msg] + messages

    def assemble_prompt(self, state: dict[str, Any] | None = None) -> SystemMessage:
        """装配完整系统提示：静态段在前（缓存友好），动态段在后。"""
        segments = [
            _check_static_budget("role_and_principles", ROLE_AND_PRINCIPLES),
            _truncate_to_budget(self._build_version_snapshot(), SEGMENT_BUDGETS["version_snapshot"]),
            _check_static_budget("formula_specs", FORMULA_SPECS),
            _truncate_to_budget(self._build_tool_guide(), SEGMENT_BUDGETS["tool_guide"]),
        ]
        content = "\n\n".join(seg for seg in segments if seg)
        return SystemMessage(content=content)

    def _build_version_snapshot(self) -> str:
        """构建版本注册表快照段；注册表为空时返回空串（不注空段）。"""
        lines: list[str] = []
        if self._version_registry is not None and hasattr(self._version_registry, "snapshot_lines"):
            lines = self._version_registry.snapshot_lines()
        if not lines:
            return ""
        return "【规范版本状态（注册表快照）】\n" + "\n".join(lines)

    def _build_tool_guide(self) -> str:
        """构建工具使用指南段（随注册表动态生成）；无注册表时返回空串。"""
        if self._tool_registry is None:
            return ""
        lines = []
        for spec_dict in self._tool_registry.list_tools():
            fn = spec_dict.get("function", {})
            name = fn.get("name", "")
            description = (fn.get("description", "") or "").splitlines()[0] if fn.get("description") else ""
            if name:
                lines.append(f"- {name}：{description}")
        if not lines:
            return ""
        return "【可用工具】\n" + "\n".join(lines)
