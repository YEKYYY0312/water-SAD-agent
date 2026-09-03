"""Human-in-the-Loop 审批图（Step 9）：gather → draft → approval(interrupt) → finalize。

审批语义（已在任务书二选一，代码与描述一致）：
**拒绝 → 带反馈回到 draft，最多 ``MAX_DRAFT_ROUNDS`` 轮；超上限收口为 failed。**

interrupt 放置原则（skill-stategraph-designer）：审批放在**副作用（finalize 产出正式文档）之前**，
不是之后。``finalize`` 只在 ``status == "approved"`` 时产出 ``final_doc``；拒绝回流不产出、
失败不产出。

状态机：
  START → gather_inputs → draft → approval
    approval 内 ``interrupt({draft_preview, approval_required})`` 挂起
    resume({"approved": True})                  → approved → finalize → succeeded
    resume({"approved": False, "reason": ...})  → rejected → draft（draft_rounds+1）
    resume 拒绝且 draft_rounds ≥ MAX_DRAFT_ROUNDS → failed（errors 可见，不产出）
"""
from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from water_agent.domain.state import (
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    STATUS_WAITING,
)

# 拒绝后可重拟的最大轮数（超出收口 failed，保证循环有硬上限）
MAX_DRAFT_ROUNDS = 2

STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"


class DocGenState(TypedDict):
    """文档生成图状态：审批字段语义明确（approval: None=待审批/True=已批/False=已拒）。"""

    query: str
    intent: str
    doc_draft: str | None          # 草稿预览（approval 时展示给人）
    final_doc: str | None          # 正式文档（仅批准后由 finalize 产出）
    approval: bool | None          # None=待审批 / True=已批准 / False=已拒绝
    approval_reason: str           # 拒绝时的人工反馈（回流 draft 的依据）
    draft_rounds: int              # 已重拟轮数（拒绝回流计数）
    errors: list[str]
    status: str


def default_docgen_state(**overrides: Any) -> DocGenState:
    """构造带默认值的 docgen 状态。"""
    state: DocGenState = {
        "query": "",
        "intent": "docgen",
        "doc_draft": None,
        "final_doc": None,
        "approval": None,
        "approval_reason": "",
        "draft_rounds": 0,
        "errors": [],
        "status": STATUS_RUNNING,
    }
    state.update(overrides)
    return state


def _render_draft(state: DocGenState) -> str:
    """模板化草稿（DocumentTemplate 占位）：query + 反馈回显，确定性、离线可测。"""
    lines = [
        f"# 计算书草稿（第 {state.get('draft_rounds', 0) + 1} 稿）",
        "",
        f"项目/查询：{state.get('query', '')}",
        "",
        "## 依据条款",
        "- （待补：gather_inputs 汇集 citations 后由模板展开）",
        "",
        "## 计算参数",
        "- （待补：calc_result 结构化展开）",
    ]
    reason = state.get("approval_reason", "")
    if reason:
        lines += ["", "## 用户反馈（已吸收）", f"- {reason}"]
    return "\n".join(lines)


def build_docgen_graph(*, checkpointer=None):
    """构建 Human-in-the-Loop 审批图。

    ``checkpointer`` 必配：``interrupt()`` 依赖 checkpoint 保存挂起状态，
    无 checkpointer 时挂起/恢复均不可用，直接拒绝构建（护住会话纪律）。
    """
    if checkpointer is None:
        raise ValueError("docgen 图必须配置 checkpointer（interrupt 依赖持久化挂起/恢复）")

    def gather_inputs_node(state: DocGenState) -> dict:
        """汇集输入（本步模板化占位：query + 意图；citations/calc_result 留扩展位）。"""
        return {
            "intent": "docgen",
            "status": STATUS_RUNNING,
            "errors": state.get("errors") or [],
        }

    def draft_node(state: DocGenState) -> dict:
        """模板化生成草稿；若上一轮被拒，反馈体现在新草稿（可行动闭环）。

        草稿就绪即进入等待审批（waiting）；拒绝回流时 draft_rounds/approval_reason
        由 approval 节点写入 state，这里基于新值渲染第 N 稿。
        """
        return {"doc_draft": _render_draft(state), "status": STATUS_WAITING}

    def approval_node(state: DocGenState) -> Command | dict:
        """人工审批：interrupt 挂起展示草稿；恢复时按 decision 路由。"""
        decision = interrupt(
            {
                "draft_preview": state.get("doc_draft", ""),
                "approval_required": True,
                "draft_rounds": state.get("draft_rounds", 0),
            }
        )
        approved = bool(decision.get("approved"))
        if approved:
            return {"approval": True, "status": STATUS_APPROVED}
        reason = str(decision.get("reason", ""))
        rounds = state.get("draft_rounds", 0) + 1
        if rounds < MAX_DRAFT_ROUNDS:
            # 拒绝且还有预算：带反馈回到 draft
            return {
                "approval": False,
                "approval_reason": reason,
                "draft_rounds": rounds,
                "status": STATUS_REJECTED,
            }
        # 拒绝已达上限：收口 failed（不产出正式文档）
        errors = list(state.get("errors", []))
        errors.append(f"用户拒绝（已达 {MAX_DRAFT_ROUNDS} 轮上限）：{reason or '未提供原因'}")
        return {"approval": False, "approval_reason": reason, "status": STATUS_FAILED, "errors": errors}

    def route_after_approval(state: DocGenState) -> str:
        if state.get("status") == STATUS_REJECTED:
            return "draft"
        return "finalize"

    def finalize_node(state: DocGenState) -> dict:
        """副作用节点：只有已批准才产出正式文档；否则保持失败/等待。"""
        if state.get("status") == STATUS_APPROVED:
            return {
                "final_doc": state.get("doc_draft"),
                "status": STATUS_SUCCEEDED,
                "errors": state.get("errors") or [],
            }
        if state.get("status") == STATUS_FAILED:
            return {"final_doc": None, "status": STATUS_FAILED, "errors": state.get("errors") or []}
        return {"final_doc": None, "status": STATUS_WAITING, "errors": state.get("errors") or []}

    graph = StateGraph(DocGenState)
    graph.add_node("gather_inputs", gather_inputs_node)
    graph.add_node("draft", draft_node)
    graph.add_node("approval", approval_node)
    graph.add_node("finalize", finalize_node)

    graph.add_edge(START, "gather_inputs")
    graph.add_edge("gather_inputs", "draft")
    graph.add_edge("draft", "approval")
    graph.add_conditional_edges(
        "approval",
        route_after_approval,
        {"draft": "draft", "finalize": "finalize"},
    )
    graph.add_edge("finalize", END)

    return graph.compile(checkpointer=checkpointer)
