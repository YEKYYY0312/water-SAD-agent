"""Human-in-the-Loop 审批图（Step 9 + P2 收口）：gather → draft → self_check → approval(interrupt) → finalize。

审批语义（已在任务书二选一，代码与描述一致）：
**拒绝 → 带反馈回到 draft 重拟；草稿总稿数上限 ``MAX_DRAFT_ROUNDS``（含初稿），
最后一稿仍被拒即收口 failed。** 即 MAX_DRAFT_ROUNDS=2 时：初稿被拒 → 重拟第 2 稿；
第 2 稿再被拒 → failed，不再重拟。

草稿自检（P2 收口，见 ``docs/design/02-loop-engineering.md`` §3.4 与
``docs/design/11-docgen-materials.md`` §4 I-10）：draft 之后、approval 之前插入
``self_check`` 节点（结构合规 / 引用策略 / 数值完整性，确定性），通过才可进审批；
不通过有界重试（预算 ``MAX_SELF_CHECK_ATTEMPTS``，回 draft 重渲染）或带可见 errors
收口 failed——草稿绝不带着未通过的自检进入审批/终稿。

interrupt 放置原则（skill-stategraph-designer）：审批放在**副作用（finalize 产出正式文档）之前**，
不是之后。``finalize`` 只在 ``status == "approved"`` 时产出 ``final_doc``；拒绝回流不产出、
失败不产出。

状态机：
  START → gather_inputs → draft → self_check → approval
    self_check 通过 → waiting（approval 复位 None）
    self_check 不通过且预算内 → 回 draft（attempts+1）
    self_check 不通过且超限       → failed（errors 可见，不产出）
    approval 内 ``interrupt({draft_preview, approval_required})`` 挂起
    resume({"approved": True})                  → approved → finalize → succeeded
    resume({"approved": False, "reason": ...})  → rejected → draft（draft_rounds+1，approval 复位 None）
    resume 拒绝且已是第 MAX_DRAFT_ROUNDS 稿      → failed（errors 可见，不产出）

审批记录：每次恢复的决定追加进 ``approval_log``（由 checkpointer 持久化），CLI/trace
接线后可直接落盘，不依赖进程内对象。
"""
from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from water_agent.docgen import RenderedDoc, check_docgen_self_check, render_docgen_doc
from water_agent.domain.citation import CitationClaim
from water_agent.domain.state import (
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    STATUS_WAITING,
)

# 草稿总稿数上限（含初稿）：第 MAX_DRAFT_ROUNDS 稿被拒即收口 failed，保证循环有硬上限
MAX_DRAFT_ROUNDS = 2

STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_SELF_CHECK_FAILED = "self_check_failed"
# 自检重试预算（loop-engineering：draft → format_template → self_check → approval）：
# 初检 + ≤1 次重检，仍失败即收口 failed，草稿绝不以未通过状态到达审批
MAX_SELF_CHECK_ATTEMPTS = 2


class DocGenState(TypedDict):
    """文档生成图状态：审批字段语义明确（approval: None=待审批/True=已批/False=已拒）。"""

    query: str
    intent: str
    doc_type: str                # P2：calculation_book 计算书 / design_note 设计说明（渲染前确定）
    citations: list[CitationClaim]    # gather_inputs 汇集的引用（Step 10 接真实数据前可为空）
    calc_result: dict[str, Any] | None  # gather_inputs 汇集的计算结果（同上）
    doc_draft: str | None          # 草稿预览（approval 时展示给人）
    final_doc: str | None          # 正式文档（仅批准后由 finalize 产出）
    approval: bool | None          # None=待审批 / True=已批准 / False=已拒绝（重拟时复位 None）
    approval_reason: str           # 最近一次拒绝的人工反馈（回流 draft 的依据）
    approval_log: list[dict[str, Any]]  # 审批记录：每次 resume 的 {decision, reason, draft_rounds}
    draft_rounds: int              # 已重拟轮数（初稿为 0；总稿数 = draft_rounds + 1）
    self_check_errors: list[str]   # 最近一次草稿自检的问题清单（空 = 通过）
    self_check_attempts: int       # 已执行自检次数（含初检；上限 MAX_SELF_CHECK_ATTEMPTS）
    errors: list[str]
    status: str


def default_docgen_state(**overrides: Any) -> DocGenState:
    """构造带默认值的 docgen 状态。"""
    state: DocGenState = {
        "query": "",
        "intent": "docgen",
        "doc_type": "calculation_book",
        "citations": [],
        "calc_result": None,
        "doc_draft": None,
        "final_doc": None,
        "approval": None,
        "approval_reason": "",
        "approval_log": [],
        "draft_rounds": 0,
        "self_check_errors": [],
        "self_check_attempts": 0,
        "errors": [],
        "status": STATUS_RUNNING,
    }
    state.update(overrides)  # type: ignore[typeddict-item]  # 与 state.py:71 同模式，基线型告警，Step 10 清零
    return state


def _docgen_inputs(state: DocGenState) -> dict[str, Any]:
    """模板引擎入参（结构化渲染共用：草稿/终稿同一数据源，数值与引用不因稿次变化）。"""
    return {
        "query": state.get("query", ""),
        "doc_type": state.get("doc_type") or "calculation_book",
        "calc_result": state.get("calc_result"),
        "citations": list(state.get("citations") or []),
        "feedback": state.get("approval_reason", ""),
    }


def _render_doc_obj(state: DocGenState) -> RenderedDoc:
    """结构化渲染（草稿稿次标题）：自检与草稿共用同一数据源（数值与引用不因稿次变化）。

    确定性、离线可测；数值与引用只来自 gather 汇集的 calc_result/citations（模板不运算、不编造）。
    """
    draft_no = state.get("draft_rounds", 0) + 1
    return render_docgen_doc(**_docgen_inputs(state), draft_no=draft_no)


def _render_draft(state: DocGenState) -> str:
    """模板化草稿文本（P2 docgen 引擎）：计算书/设计说明结构化渲染 + 引用/数值 + 反馈回显。"""
    return _render_doc_obj(state).text


def _render_final(state: DocGenState) -> str:
    """正式文档：批准后基于同一数据源渲染（不带"草稿/稿次"标题，反馈痕迹保留可追溯）。"""
    rendered = render_docgen_doc(**_docgen_inputs(state))
    return rendered.text


def build_docgen_graph(*, checkpointer=None):
    """构建 Human-in-the-Loop 审批图。

    ``checkpointer`` 必配：``interrupt()`` 依赖 checkpoint 保存挂起状态，
    无 checkpointer 时挂起/恢复均不可用，直接拒绝构建（护住会话纪律）。
    """
    if checkpointer is None:
        raise ValueError("docgen 图必须配置 checkpointer（interrupt 依赖持久化挂起/恢复）")

    def gather_inputs_node(state: DocGenState) -> dict:
        """汇集输入：query/意图 + 已有 citations/calc_result（调用方预置；本步不主动检索/计算）。"""
        return {
            "intent": "docgen",
            "status": STATUS_RUNNING,
            "citations": list(state.get("citations") or []),
            "calc_result": state.get("calc_result"),
            "approval_log": list(state.get("approval_log") or []),
            "errors": state.get("errors") or [],
        }

    def draft_node(state: DocGenState) -> dict:
        """模板化生成草稿（P2 引擎：结构化章节 + 稿次标题）；若上一轮被拒，反馈体现在新草稿。

        草稿就绪后由 self_check 校验（结构/引用/数值完整性），通过才置 waiting 进审批、
        并把 ``approval`` 复位为 None（待审批）；不通过不达审批（有界重试或收口 failed）。
        """
        return {"doc_draft": _render_draft(state), "approval": None}

    def self_check_node(state: DocGenState) -> dict:
        """草稿自检（loop-engineering：draft → format_template → self_check → approval）。

        确定性检查三项（check_docgen_self_check）：结构合规 / 引用策略 / 数值完整性。
        - 通过 → status=waiting（可进审批，approval 复位 None）；
        - 不通过且自检次数未达上限 → status=self_check_failed（回 draft 有界重试）；
        - 不通过且已达上限 → status=failed（可见 errors，不产出、不进审批）。
        """
        attempts = state.get("self_check_attempts", 0) + 1
        rendered = _render_doc_obj(state)
        problems = check_docgen_self_check(rendered, calc_result=state.get("calc_result"))
        if problems:
            errors = list(state.get("errors") or [])
            errors.append(f"草稿自检未通过（第 {attempts}/{MAX_SELF_CHECK_ATTEMPTS} 次）：" + "；".join(problems))
            if attempts >= MAX_SELF_CHECK_ATTEMPTS:
                errors.append("自检连续失败已达上限：草稿未通过自检不得进入审批/终稿，不产出文档")
                return {
                    "self_check_errors": problems,
                    "self_check_attempts": attempts,
                    "errors": errors,
                    "status": STATUS_FAILED,
                }
            return {
                "self_check_errors": problems,
                "self_check_attempts": attempts,
                "errors": errors,
                "status": STATUS_SELF_CHECK_FAILED,
            }
        return {
            "self_check_errors": [],
            "self_check_attempts": attempts,
            "errors": [],
            "status": STATUS_WAITING,
            "approval": None,
        }

    def approval_node(state: DocGenState) -> Command | dict:
        """人工审批：interrupt 挂起展示草稿；恢复时按 decision 路由，并把决定追加进 approval_log。"""
        decision = interrupt(
            {
                "draft_preview": state.get("doc_draft", ""),
                "approval_required": True,
                "draft_rounds": state.get("draft_rounds", 0),
            }
        )
        # resume 值只认 dict；bool/None 等按"未批准、无理由"处理（不抛裸异常）
        if not isinstance(decision, dict):
            decision = {"approved": bool(decision)}
        approved = bool(decision.get("approved"))
        reason = str(decision.get("reason", "") or "")
        draft_rounds = state.get("draft_rounds", 0)
        log = list(state.get("approval_log") or [])
        log.append({"decision": "approved" if approved else "rejected", "reason": reason, "draft_rounds": draft_rounds})

        if approved:
            return {"approval": True, "status": STATUS_APPROVED, "approval_log": log}
        if draft_rounds + 1 < MAX_DRAFT_ROUNDS:
            # 拒绝且还有稿数预算：带反馈回到 draft 重拟（新一轮稿子的自检预算复位，
            # 保证每次新草稿都获得完整的 MAX_SELF_CHECK_ATTEMPTS 自检机会）
            return {
                "approval": False,
                "approval_reason": reason,
                "approval_log": log,
                "draft_rounds": draft_rounds + 1,
                "self_check_attempts": 0,
                "self_check_errors": [],
                "status": STATUS_REJECTED,
            }
        # 最后一稿仍被拒：收口 failed（不产出正式文档）
        errors = list(state.get("errors", []))
        errors.append(f"用户拒绝（已达 {MAX_DRAFT_ROUNDS} 稿上限）：{reason or '未提供原因'}")
        return {
            "approval": False,
            "approval_reason": reason,
            "approval_log": log,
            "status": STATUS_FAILED,
            "errors": errors,
        }

    def route_after_self_check(state: DocGenState) -> str:
        """自检路由：通过 → approval；不通过且预算内 → draft（有界重试）；不通过且超限 → 终止。"""
        if state.get("status") == STATUS_SELF_CHECK_FAILED:
            return "draft"
        if state.get("status") == STATUS_FAILED:
            return "end"
        return "approval"

    def route_after_approval(state: DocGenState) -> str:
        if state.get("status") == STATUS_REJECTED:
            return "draft"
        return "finalize"

    def finalize_node(state: DocGenState) -> dict:
        """副作用节点：只有已批准才产出正式文档；失败不产出（路由保证不会以其他状态进入）。

        正式文档 = 基于同一数据源的正式渲染（标题不带"草稿/稿次"，章节内容与已批准草稿一致）。
        """
        if state.get("status") == STATUS_APPROVED:
            return {
                "final_doc": _render_final(state),
                "status": STATUS_SUCCEEDED,
                "errors": state.get("errors") or [],
            }
        return {"final_doc": None, "status": STATUS_FAILED, "errors": state.get("errors") or []}

    graph = StateGraph(DocGenState)
    graph.add_node("gather_inputs", gather_inputs_node)
    graph.add_node("draft", draft_node)
    graph.add_node("self_check", self_check_node)
    graph.add_node("approval", approval_node)
    graph.add_node("finalize", finalize_node)

    graph.add_edge(START, "gather_inputs")
    graph.add_edge("gather_inputs", "draft")
    graph.add_edge("draft", "self_check")
    graph.add_conditional_edges(
        "self_check",
        route_after_self_check,
        {"draft": "draft", "approval": "approval", "end": END},
    )
    graph.add_conditional_edges(
        "approval",
        route_after_approval,
        {"draft": "draft", "finalize": "finalize"},
    )
    graph.add_edge("finalize", END)

    return graph.compile(checkpointer=checkpointer)
