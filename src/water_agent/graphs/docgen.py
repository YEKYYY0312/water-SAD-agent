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
失败不产出；**failed 在 approval 处直接终止（条件路由到 END），不进入 finalize**。

resume 值契约（P2 收口，严格布尔）：
- 允许 ``{"approved": True/False}``（拒绝可带 ``reason``）；
- 允许裸严格 ``bool``（``Command(resume=True)``，兼容早期用法）；
- 其余形态（``"false"``/``1``/缺 ``approved`` 键等）一律 ValueError 明确拒绝——
  ``bool("false") == True`` 这类静默误判是 P2 评审点，禁止。

状态机：
  START → gather_inputs → draft → self_check → approval
    self_check 通过 → waiting（approval 复位 None）
    self_check 不通过且预算内 → 回 draft（attempts+1）
    self_check 不通过且超限       → failed（errors 可见，不产出，终止）
    approval 内 ``interrupt({draft_preview, approval_required})`` 挂起
    resume({"approved": True})                  → approved → finalize → succeeded
    resume({"approved": False, "reason": ...})  → rejected → draft（draft_rounds+1，approval 复位 None）
    resume 拒绝且已是第 MAX_DRAFT_ROUNDS 稿      → failed（errors 可见，终止，不进 finalize）

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
    gather_artifacts: dict[str, Any]



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
        "gather_artifacts": {},
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



def _normalize_approval_decision(decision: Any) -> tuple[bool, str]:
    """审批 resume 值归一：只接受**严格 bool**（P2 收口，防 bool() 静默误判）。

    - dict：必须含严格 bool 键 ``approved``（拒绝可带 ``reason`` 文本）；
    - 非 dict：只接受裸严格 bool（``Command(resume=True)``，早期用法兼容）；
    - 其余形态（``"false"`` / ``1`` / ``None`` / 缺 ``approved`` 键 …）→ ValueError。
      ``bool("false") == True`` 会把"字符串 false"误判为批准，属于必须显式拒绝的静默错误。
    """
    if isinstance(decision, dict):
        approved = decision.get("approved")
        if not isinstance(approved, bool):
            raise TypeError(
                f"审批 resume 的 approved 必须是严格 bool（True/False），得到 {approved!r}；"
                "禁止把字符串/数字（如 \"false\"、1）当作布尔值传入"
            )
        reason = str(decision.get("reason", "") or "")
        return approved, reason
    if isinstance(decision, bool):
        return decision, ""
    raise TypeError(
        f"审批 resume 必须是 {{'approved': True/False}} 或裸 bool，"
        f"得到 {type(decision).__name__}（{decision!r}）"
    )



def route_after_self_check(state: DocGenState) -> str:
    """自检路由：通过 → approval；不通过且预算内 → draft（有界重试）；不通过且超限 → 终止。"""
    if state.get("status") == STATUS_SELF_CHECK_FAILED:
        return "draft"
    if state.get("status") == STATUS_FAILED:
        return END
    return "approval"



def route_after_approval(state: DocGenState) -> str:
    """审批路由：rejected（还有稿数预算）→ draft 重拟；failed（达上限/异常）→ 终止。

    **只有 approved 才进入 finalize**——failed 直接收口 END，不经过 finalize 节点
    （finalize 守卫保留为第二道保险，路由是主约束）。
    """
    if state.get("status") == STATUS_REJECTED:
        return "draft"
    if state.get("status") == STATUS_FAILED:
        return END
    return "finalize"



def build_docgen_graph(*, checkpointer=None):
    """构建 Human-in-the-Loop 审批图。

    ``checkpointer`` 必配：``interrupt()`` 依赖 checkpoint 保存挂起状态，
    无 checkpointer 时挂起/恢复均不可用，直接拒绝构建（护住会话纪律）。
    """
    if checkpointer is None:
        raise ValueError("docgen 图必须配置 checkpointer（interrupt 依赖持久化挂起/恢复）")

    def gather_inputs_node(state: DocGenState) -> dict:
        """汇集输入：query/意图 + 已有 citations/calc_result（调用方预置；本步不主动检索/计算）。

        ``gather_artifacts`` 只承载**可 JSON 序列化**的检索快照（上下文/声明清单/回答），
        供审批人查看与 trace 落盘；citations 本体走 state 的 ``citations`` 字段（CitationClaim）。
        """
        artifacts = state.get("gather_artifacts") or {}
        return {
            "intent": "docgen",
            "status": STATUS_RUNNING,
            "citations": list(state.get("citations") or []),
            "calc_result": state.get("calc_result"),
            "approval_log": list(state.get("approval_log") or []),
            "errors": state.get("errors") or [],
            "gather_artifacts": {
                "retrieval_context": artifacts.get("retrieval_context", []),
                "retrieval_claims": artifacts.get("retrieval_claims", []),
                "retrieval_payload": artifacts.get("retrieval_payload", {}),
            },
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
        rendered = _render_doc_obj(state)
        errors = check_docgen_self_check(rendered, calc_result=state.get("calc_result"))
        attempts = state.get("self_check_attempts", 0) + 1
        if not errors:
            return {
                "status": STATUS_WAITING,
                "self_check_errors": [],
                "self_check_attempts": attempts,
                "approval": None,
            }
        if attempts < MAX_SELF_CHECK_ATTEMPTS:
            return {
                "status": STATUS_SELF_CHECK_FAILED,
                "self_check_errors": errors,
                "self_check_attempts": attempts,
                "errors": list(state.get("errors", [])) + errors,
            }
        errors_out = list(state.get("errors", [])) + errors + ["草稿自检失败次数已达上限，终止文档生成"]
        return {
            "status": STATUS_FAILED,
            "self_check_errors": errors,
            "self_check_attempts": attempts,
            "errors": errors_out,
            "final_doc": None,
        }

    def approval_node(state: DocGenState) -> Command | dict:
        """审批节点：interrupt 挂起，等待 Command(resume=...) 决策。"""
        preview = state.get("doc_draft") or _render_draft(state)
        decision = interrupt(
            {
                "draft_preview": preview,
                "approval_required": True,
                "draft_rounds": state.get("draft_rounds", 0),
                "gather_artifacts": state.get("gather_artifacts", {}),
            }
        )
        approved, reason = _normalize_approval_decision(decision)
        draft_rounds = state.get("draft_rounds", 0)
        log = list(state.get("approval_log", []))
        log.append({"decision": "approved" if approved else "rejected", "reason": reason, "draft_rounds": draft_rounds})
        if approved:
            return {
                "approval": True,
                "approval_log": log,
                "status": STATUS_APPROVED,
            }
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
        # 最后一稿仍被拒：收口 failed（不产出正式文档；路由到 END，不进 finalize）
        errors = list(state.get("errors", []))
        errors.append(f"用户拒绝（已达 {MAX_DRAFT_ROUNDS} 稿上限）：{reason or '未提供原因'}")
        return {
            "approval": False,
            "approval_reason": reason,
            "approval_log": log,
            "status": STATUS_FAILED,
            "errors": errors,
            "final_doc": None,
        }

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
        {"draft": "draft", "approval": "approval", END: END},
    )
    graph.add_conditional_edges(
        "approval",
        route_after_approval,
        {"draft": "draft", "finalize": "finalize", END: END},
    )
    graph.add_edge("finalize", END)

    return graph.compile(checkpointer=checkpointer)
