"""LangGraph 单一状态（WaterAgentState）与循环上限常量。"""
from __future__ import annotations

from typing import Any, TypedDict

from water_agent.domain.citation import CitationClaim
from water_agent.domain.models import RetrievedClause

# 循环/重试上限（约束：所有循环必须有明确上限）
MAX_TOTAL_STEPS = 10
MAX_CITATION_RETRIES = 2
MAX_RETRIEVAL_RETRIES = 2

STATUS_RUNNING = "running"
STATUS_WAITING = "waiting"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"


class WaterAgentState(TypedDict):
    """全项目共享状态（Loop Engineering 单一状态）。"""

    messages: list[Any]
    intent: str
    query: str
    context: list[RetrievedClause]
    calc_result: dict[str, Any] | None
    drawings: list[Any]
    doc_draft: str | None
    citations: list[CitationClaim]
    answer: str
    approval: bool | None
    errors: list[str]
    run_id: str
    trace_id: str
    retry_count: int
    step_count: int
    status: str
    missing_inputs: list[str]
    assumptions: list[str]
    # P0 RAG 图内部字段
    citation_ok: bool
    # approval 语义（Step 9 Human-in-the-Loop 消费）：
    #   None = 待审批（docgen approval 节点 interrupt 前/后）｜True = 已批准（放行 finalize 副作用）
    #   False = 已拒绝（带 approval_reason 回流 draft 或收口 failed，见 graphs/docgen.py）


def default_state(**overrides: Any) -> WaterAgentState:
    """构造带默认值的完整状态。"""
    state: WaterAgentState = {
        "messages": [],
        "intent": "",
        "query": "",
        "context": [],
        "calc_result": None,
        "drawings": [],
        "doc_draft": None,
        "citations": [],
        "answer": "",
        "approval": None,
        "errors": [],
        "run_id": "",
        "trace_id": "",
        "retry_count": 0,
        "step_count": 0,
        "status": STATUS_RUNNING,
        "missing_inputs": [],
        "assumptions": [],
        "citation_ok": True,
    }
    state.update(overrides)  # type: ignore[typeddict-item]
    return state
