"""危险操作人工确认门（P7-S7，``docs/design/16-p7-plugin-mcp.md`` §3.7）。

内层 react agent 是 ``checkpointer=False``，工具执行中途的 interrupt 无法恢复，所以确认门放在
**外层图**（先例：``graphs/docgen.py`` 的审批节点、``multi_agent/orchestrator.py``）：

```text
agent_loop ──本轮有待确认消息──→ confirmation_request ──→ confirmation_gate(interrupt)
    ↑                               │ 超过每轮上限 → finalize（failed）   │
    └──────────── 没有更多待确认 ←──────────────────────────────────────┘
                  （同一条 AI 消息里还有待确认的调用 → 回 confirmation_request，逐条确认）
```

- 需确认的工具在 ToolNode 里是延迟工具（``tools/ds_adapters.tool_spec_to_deferred_tool``）：执行时
  只回一条"待确认"ToolMessage（标记在 artifact 里），``return_direct`` 让内层循环随即结束；
- ``confirmation_request``：每个待确认调用恰好经过一次，记 ``confirmation_requested`` trace 事件，
  并执行每轮上限 :data:`MAX_CONFIRMATIONS_PER_TURN`；
- ``confirmation_gate``：``interrupt(request)`` 挂起；恢复后**批准** → 签发一次性
  ``ConfirmationGrant``、经 ``ToolRegistry.invoke_confirmed`` 执行一次，用真实结果**同 id 替换**待确认
  消息；**拒绝** → 同 id 替换为拒绝 observation。模型看到的始终是"一次调用、一个结果"。

resume 值契约与 docgen 相同的思路：只接受 ``{"confirmation_id": str, "approved": bool,
"reason": str | None}``（严格 bool、``confirmation_id`` 必须等于当前挂起的那一个）；非法值**不抛
异常**，带 ``error`` 再 interrupt——抛异常会让非法值随 ERROR 落进 checkpoint、之后每次恢复都先
重放它，会话被永久毒化。判挂起看 ``get_state().interrupts``，不只看 ``next``。

执行用的参数是 state 里那条 AI 消息记下的参数（批准的就是执行的）；凭证绑定参数摘要，护栏在
执行时重算比对。审计：``confirmation_log`` 随 checkpoint 持久化、跨轮累积；参数只记摘要、主体
只记 hash、原因经输出护栏脱敏后截断。
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import interrupt

from water_agent.domain.state import STATUS_FAILED
from water_agent.graphs.agent_rag import current_turn_messages
from water_agent.runtime.governance.binding import local_request_context
from water_agent.runtime.governance.contracts import (
    ConfirmationGrant,
    Principal,
    RequestContext,
    confirmation_args_hash,
)
from water_agent.runtime.governance.inventory import mcp_tool_binding
from water_agent.runtime.governance.pipeline import (
    GuardrailPipeline,
    default_guardrail_pipeline,
)
from water_agent.tools.ds_adapters import PENDING_CONFIRMATION_ARTIFACT
from water_agent.tools.registry import ToolRegistry

#: 作答停在确认门时，入口（Web run）对外报告的状态。不是终态：决定之后同一个 run 继续。
STATUS_WAITING_CONFIRMATION = "waiting_confirmation"
#: 每轮最多请求几次人工确认。每次确认都要人参与，模型反复请求会让用户陷入点击循环；
#: 第 MAX + 1 次请求即本轮 failed（结构化错误）。
MAX_CONFIRMATIONS_PER_TURN = 3
#: interrupt 载荷的类型标记：挂起判定只认带这个标记的 interrupt。
CONFIRMATION_REQUEST_KIND = "tool_confirmation"
#: 替换后的消息在 artifact 里留下确认结果（不发给模型），用于审计与"不再视为待确认"。
CONFIRMATION_RESULT_ARTIFACT = "water_agent_confirmation"
CODE_TOOL_CONFIRMATION_REJECTED = "TOOL_CONFIRMATION_REJECTED"
CODE_TOOL_CONFIRMATION_LIMIT = "TOOL_CONFIRMATION_LIMIT"
DECISION_APPROVED = "approved"
DECISION_REJECTED = "rejected"
#: 审计里的原因最多保留多少字（脱敏之后截断）。
MAX_REASON_CHARS = 200
#: 送去脱敏之前先截一刀，避免超长原因拖慢护栏。
_REASON_INPUT_LIMIT = 2_000
_REASON_WITHHELD = "[原因未通过输出护栏，已省略]"
#: resume 值只允许这三个键。
_DECISION_KEYS = frozenset({"confirmation_id", "approved", "reason"})


@dataclass(frozen=True, slots=True)
class ConfirmationRuntime:
    """确认门需要的装配产物：执行用的注册表、签发凭证用的主体、要求确认的工具名。

    ``registry`` / ``principal`` 必须是本次装配的同一份（``ToolAssembly.registry`` /
    ``ToolAssembly.principal``）：凭证里的主体 hash 与注册表持有的主体不一致时，护栏判
    ``tool.confirmation_mismatch``。``pipeline`` / ``request_context`` 只用于给原因脱敏。
    """

    registry: ToolRegistry
    principal: Principal
    tools: frozenset[str]
    request_context: RequestContext | None = None
    pipeline: GuardrailPipeline | None = None


def is_pending_confirmation(message: Any, tools: frozenset[str]) -> bool:
    """延迟工具产出的待确认消息：工具名在确认集合里、artifact 带待确认标记。

    只认 artifact 不认正文：正文由工具输出决定，任何远端都能把它写成同样的 JSON。
    """
    if not isinstance(message, ToolMessage) or message.name not in tools:
        return False
    artifact = message.artifact
    return isinstance(artifact, dict) and bool(artifact.get(PENDING_CONFIRMATION_ARTIFACT))


def pending_confirmations(messages: Sequence[Any], tools: frozenset[str]) -> list[ToolMessage]:
    """本轮尚未决定的待确认消息，按出现顺序（同一条 AI 消息里的多条按调用顺序）。"""
    return [m for m in current_turn_messages(messages) if is_pending_confirmation(m, tools)]


def _call_args(messages: Sequence[Any], tool_call_id: str) -> dict[str, Any]:
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            for call in message.tool_calls or []:
                if call.get("id") == tool_call_id:
                    return dict(call.get("args") or {})
    return {}


def confirmation_request(message: ToolMessage, messages: Sequence[Any]) -> dict[str, Any]:
    """一条待确认消息对应的确认请求（interrupt 载荷）。

    完全由 state 推出（confirmation_id 来自 artifact、参数来自 AI 消息），恢复时节点重跑得到同一份。
    ``arguments`` 是原文：确认的人必须看到要执行的参数；它只进 checkpoint（AI 消息里本就有）与
    挂起请求，不进 trace / 日志。
    """
    artifact = message.artifact if isinstance(message.artifact, dict) else {}
    tool_name = str(message.name or "")
    args = _call_args(messages, str(message.tool_call_id or ""))
    binding = mcp_tool_binding(tool_name)
    server_id = binding.server_id if binding is not None else ""
    remote_tool = binding.remote_tool if binding is not None else tool_name
    return {
        "kind": CONFIRMATION_REQUEST_KIND,
        "confirmation_id": str(artifact.get(PENDING_CONFIRMATION_ARTIFACT) or ""),
        "tool_call_id": str(message.tool_call_id or ""),
        "tool_name": tool_name,
        "server_id": server_id,
        "remote_tool": remote_tool,
        "arguments": args,
        "args_hash": confirmation_args_hash(args),
        "message": (
            f"Agent 请求调用 MCP server '{server_id}' 的工具 '{remote_tool}'。"
            "该 server 被运维配置为需要人工确认：批准后按上面的参数执行一次，拒绝则不执行。"
        ),
    }


def _short_repr(value: Any, limit: int = 80) -> str:
    text = repr(value)
    return text if len(text) <= limit else f"{text[: limit - 3]}..."


def confirmation_decision_error(decision: Any, confirmation_id: str) -> str | None:
    """resume 值校验：合法返回 ``None``，非法返回给确认人看的原因（不抛异常）。

    只接受 ``{"confirmation_id": str, "approved": bool, "reason": str | None}``：``approved`` 必须是
    严格 bool（``"yes"``、``1`` 一律非法，``bool("false") == True`` 这类静默误判必须显式拒绝），
    ``confirmation_id`` 必须等于当前挂起的请求（过期页面、重复提交不会决定到别的请求上）。
    """
    if not isinstance(decision, dict):
        return (
            "确认的 resume 值必须是 {'confirmation_id': str, 'approved': bool, 'reason': str | None}，"
            f"得到 {type(decision).__name__}（{_short_repr(decision)}）"
        )
    unknown = set(decision) - _DECISION_KEYS
    if unknown:
        return f"确认的 resume 值含未知字段：{sorted(str(key) for key in unknown)}"
    if decision.get("confirmation_id") != confirmation_id:
        return (
            f"confirmation_id 与当前挂起的确认请求不一致（得到 "
            f"{_short_repr(decision.get('confirmation_id'))}）；请按最新的确认请求决定"
        )
    if not isinstance(decision.get("approved"), bool):
        return (
            f"approved 必须是严格 bool（True/False），得到 {_short_repr(decision.get('approved'))}；"
            "禁止把字符串/数字（如 \"yes\"、1）当作布尔值传入"
        )
    reason = decision.get("reason")
    if reason is not None and not isinstance(reason, str):
        return f"reason 必须是字符串或 None，得到 {type(reason).__name__}"
    return None


def _observation(code: str, message: str) -> str:
    """不执行时回填给模型的 observation（形态与 ``ToolRegistry`` 的结构化错误一致）。"""
    return json.dumps(
        {"ok": False, "error": True, "code": code, "message": message, "suggestions": []},
        ensure_ascii=False,
    )


def _replace(message: ToolMessage, content: str, result: dict[str, Any]) -> ToolMessage:
    """同 id 替换待确认消息：正文换成结果、artifact 换成确认结果（不再是待确认）。"""
    return message.model_copy(
        update={"content": content, "artifact": {CONFIRMATION_RESULT_ARTIFACT: result}}
    )


def make_confirmation_request_node(runtime: ConfirmationRuntime, trace: Any | None = None):
    """``confirmation_request`` 节点：每个待确认调用恰好经过一次（记 ``confirmation_requested``），
    并执行每轮上限。它不挂起——挂起在下一个节点，恢复时只重跑那一个。"""

    def confirmation_request_node(state: dict[str, Any]) -> dict[str, Any]:
        messages = state.get("messages", [])
        pending = pending_confirmations(messages, runtime.tools)
        if not pending:
            return {}
        count = int(state.get("confirmations_this_turn", 0) or 0)
        if count >= MAX_CONFIRMATIONS_PER_TURN:
            text = (
                f"本轮人工确认请求已达上限（{MAX_CONFIRMATIONS_PER_TURN} 次），"
                "后续需要确认的调用未执行"
            )
            return {
                "messages": [
                    _replace(
                        item,
                        _observation(CODE_TOOL_CONFIRMATION_LIMIT, text),
                        {"decision": "limit_exceeded"},
                    )
                    for item in pending
                ],
                "status": STATUS_FAILED,
                "errors": list(state.get("errors", [])) + [f"{CODE_TOOL_CONFIRMATION_LIMIT}：{text}"],
            }
        request = confirmation_request(pending[0], messages)
        _record(
            trace,
            "confirmation_requested",
            {
                "confirmation_id": request["confirmation_id"],
                "tool_name": request["tool_name"],
                "server_id": request["server_id"],
                "args_hash": request["args_hash"],
                "principal_hash": runtime.principal.principal_hash,
            },
        )
        return {}

    return confirmation_request_node


def make_confirmation_gate(runtime: ConfirmationRuntime, trace: Any | None = None):
    """``confirmation_gate`` 节点：对本轮第一条待确认调用 ``interrupt``，按决定执行或拒绝。"""

    pipeline = runtime.pipeline if runtime.pipeline is not None else default_guardrail_pipeline()
    reason_context = runtime.request_context or local_request_context(thread_id="confirmation-reason")

    def sanitize_reason(reason: str | None) -> str:
        if not reason:
            return ""
        outcome = pipeline.check_output(reason[:_REASON_INPUT_LIMIT], reason_context)
        text = outcome.content if outcome.allowed else _REASON_WITHHELD
        return text[:MAX_REASON_CHARS]

    def confirmation_gate(state: dict[str, Any]) -> dict[str, Any]:
        messages = state.get("messages", [])
        pending = pending_confirmations(messages, runtime.tools)
        if not pending:
            return {}
        target = pending[0]
        request = confirmation_request(target, messages)
        confirmation_id = request["confirmation_id"]
        decision = interrupt(request)
        # 非法值不抛：带 error 再挂起（每次恢复本节点都从头重跑、依次重放此前的值，非法值落回
        # 原来的序号再次被判非法，新的合法值接在最后被取到）
        while (error := confirmation_decision_error(decision, confirmation_id)) is not None:
            decision = interrupt({**request, "error": error})

        approved = bool(decision["approved"])
        reason = sanitize_reason(decision.get("reason"))
        decided_at = datetime.now(UTC)
        tool_name = request["tool_name"]
        _record(
            trace,
            "confirmation_decided",
            {
                "confirmation_id": confirmation_id,
                "tool_name": tool_name,
                "decision": DECISION_APPROVED if approved else DECISION_REJECTED,
                "reason_given": bool(reason),
            },
        )
        if approved:
            grant = ConfirmationGrant(
                confirmation_id=confirmation_id,
                tool_name=tool_name,
                args_hash=request["args_hash"],
                principal_hash=runtime.principal.principal_hash,
                decided_at=decided_at,
            )
            # 参数取 state 里记下的那一份（与请求里给人看的是同一份），不重新问模型
            result = runtime.registry.invoke_confirmed(tool_name, request["arguments"], grant=grant)
            result_code = str(result.get("code") or "TOOL_ERROR") if result.get("error") else "ok"
            content = json.dumps(result, ensure_ascii=False, default=str)
            _record(
                trace,
                "confirmation_executed",
                {"confirmation_id": confirmation_id, "tool_name": tool_name, "result_code": result_code},
            )
        else:
            result_code = "not_executed"
            content = _observation(
                CODE_TOOL_CONFIRMATION_REJECTED,
                f"用户拒绝执行工具 {tool_name!r}，该调用未执行。请向用户说明，不要换个方式重试同一操作。",
            )
        entry = {
            "confirmation_id": confirmation_id,
            "server_id": request["server_id"],
            "tool_name": tool_name,
            "args_hash": request["args_hash"],
            "principal_hash": runtime.principal.principal_hash,
            "decision": DECISION_APPROVED if approved else DECISION_REJECTED,
            "reason": reason,
            "decided_at": decided_at.isoformat(),
            "result_code": result_code,
        }
        return {
            "messages": [
                _replace(
                    target,
                    content,
                    {"confirmation_id": confirmation_id, "decision": entry["decision"]},
                )
            ],
            "confirmation_log": list(state.get("confirmation_log", []) or []) + [entry],
            "confirmations_this_turn": int(state.get("confirmations_this_turn", 0) or 0) + 1,
        }

    return confirmation_gate


def _record(trace: Any | None, event: str, data: dict[str, Any]) -> None:
    """确认门的 trace 事件：显式 ``query=""``，只有 id、工具名、摘要与 hash，不写参数原文。"""
    if trace is None or not getattr(trace, "started", False):
        return
    try:
        trace.record(node="confirmation", event=event, query="", data=data)
    except Exception:  # noqa: BLE001, S110 — 观测失败不影响确认与执行
        pass


def pending_confirmation_request(interrupts: Sequence[Any]) -> dict[str, Any] | None:
    """从 ``get_state().interrupts``（或 checkpoint 的 interrupt 写入）里取挂起的确认请求。"""
    for item in interrupts:
        value = getattr(item, "value", item)
        if isinstance(value, dict) and value.get("kind") == CONFIRMATION_REQUEST_KIND:
            return dict(value)
    return None


def public_confirmation_request(request: dict[str, Any]) -> dict[str, Any]:
    """给入口（Web / 调用方）的确认请求视图：去掉内部字段（tool_call_id、参数摘要）。"""
    view = {
        key: request.get(key)
        for key in ("confirmation_id", "tool_name", "server_id", "remote_tool", "arguments", "message")
    }
    if request.get("error"):
        view["error"] = str(request["error"])
    return view


__all__ = [
    "CODE_TOOL_CONFIRMATION_LIMIT",
    "CODE_TOOL_CONFIRMATION_REJECTED",
    "CONFIRMATION_REQUEST_KIND",
    "CONFIRMATION_RESULT_ARTIFACT",
    "MAX_CONFIRMATIONS_PER_TURN",
    "MAX_REASON_CHARS",
    "STATUS_WAITING_CONFIRMATION",
    "ConfirmationRuntime",
    "confirmation_decision_error",
    "confirmation_request",
    "is_pending_confirmation",
    "make_confirmation_gate",
    "make_confirmation_request_node",
    "pending_confirmation_request",
    "pending_confirmations",
    "public_confirmation_request",
]
