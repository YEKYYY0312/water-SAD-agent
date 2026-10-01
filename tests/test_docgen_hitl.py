"""Step 9 测试：Human-in-the-Loop 审批节点（interrupt + Command(resume)）。

覆盖（拒绝分支语义已选定：拒绝→带反馈回到 draft 重拟，草稿总稿数上限 MAX_DRAFT_ROUNDS，
最后一稿仍被拒收口 failed）：
- 首次 invoke 触发 interrupt，状态快照含 draft_preview；
- approve 恢复后流程走完且状态含审批记录（approval=True、approval_log、final_doc 生成）；
- reject + reason 带反馈回流到 draft，draft_rounds 递增，再次挂起时 approval 复位 None；
- 拒绝达上限 → failed 且 errors 可见；
- 无 checkpointer 构建时报错（护住会话纪律）；
- checkpointer 落盘：SqliteSaver 挂起 → 关闭连接 → 新连接/新图恢复 → 走完；
- 非法 resume 值（非严格 bool）带可见 error 重新挂起、不进 approval_log，之后合法值照常恢复
  （含跨进程）。
"""
from __future__ import annotations

import sqlite3

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from water_agent.domain.citation import CitationClaim
from water_agent.graphs.docgen import (
    MAX_DRAFT_ROUNDS,
    STATUS_APPROVED,
    STATUS_FAILED,
    STATUS_REJECTED,
    STATUS_SUCCEEDED,
    STATUS_WAITING,
    build_docgen_graph,
    route_after_approval,
)


def _initial_state(query: str = "生成 DN150 给水管道计算书", **extra) -> dict:
    return {"query": query, "status": "running", "draft_rounds": 0, **extra}


def _thread(thread_id: str = "t1") -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _pending_requests(app, config: dict) -> list[dict]:
    """get_state 里挂起的审批请求。

    判挂起看 interrupts 而不是 next：非法 resume 值让会话重新挂起后，LangGraph 1.2.11
    的 ``get_state().next`` 是 ``()``（该任务已有 RESUME 写入），interrupts 才是准的。
    """
    snapshot = app.get_state(config)
    return [i.value for task in snapshot.tasks for i in task.interrupts]


# 严格 bool 之外的 resume 形态：字符串/数字/None/缺键都不得被当作审批决定
INVALID_RESUME_VALUES = [
    {"approved": "false"},
    {"approved": "true"},
    {"approved": 1},
    {"approved": 0},
    {"approved": None},
    {"reason": "缺 approved 键"},
    "false",
    1,
]


class TestResumeContract:
    @pytest.mark.parametrize("bad", INVALID_RESUME_VALUES, ids=repr)
    def test_invalid_resume_resuspends_and_valid_value_still_resumes(self, bad) -> None:
        """非法 resume 值不被当作决定：会话带可见 error 重新挂起，之后传合法值照常恢复。

        回归：approval 节点原先在 interrupt() 返回之后才校验、失败即抛 TypeError；而
        LangGraph 已把这个值记进本任务的 resume 列表，并随 ERROR 一起落进 checkpoint。
        之后每次恢复都先在第 0 个 interrupt 上重放它，合法值永远轮不到——会话被永久
        毒化（get_state().next 同时变成 ()，CLI --resume 以为没有挂起任务）。
        """
        cfg = _thread("strict-bool")
        with MemorySaver() as saver:
            app = build_docgen_graph(checkpointer=saver)
            app.invoke(_initial_state(), config=cfg)
            state = app.invoke(Command(resume=bad), config=cfg)

            assert state.get("__interrupt__"), "非法 resume 值必须让会话重新挂起，而不是抛异常"
            request = state["__interrupt__"][0].value
            offending = bad.get("approved") if isinstance(bad, dict) else bad
            assert "严格 bool" in request["error"] and repr(offending) in request["error"], request["error"]
            assert request["draft_preview"], "重新挂起时草稿预览仍在，审批人可以接着审"
            # 非法值不是决定：不进审批记录，审批状态不变
            assert state["approval_log"] == []
            assert state["approval"] is None
            assert state["status"] == STATUS_WAITING
            assert [r.get("error") for r in _pending_requests(app, cfg)] == [request["error"]], "get_state 必须仍显示挂起"

            resumed = app.invoke(Command(resume={"approved": True}), config=cfg)
        assert resumed["status"] == STATUS_SUCCEEDED
        assert resumed["final_doc"]
        assert resumed["approval_log"] == [{"decision": "approved", "reason": "", "draft_rounds": 0}]

    def test_repeated_invalid_resumes_then_reject_redrafts_cleanly(self) -> None:
        """连续多个非法值逐个重新挂起（错误指向最近一个）；随后的合法拒绝照常回流重拟。

        同一节点内的多次 interrupt 按序号匹配 resume 值：重放时每个非法值落回原来的
        序号再次被判非法，新的合法值接在最后被取到。新一稿是新任务，审批请求不带旧错误。
        """
        cfg = _thread("strict-seq")
        with MemorySaver() as saver:
            app = build_docgen_graph(checkpointer=saver)
            app.invoke(_initial_state(), config=cfg)
            for bad in ({"approved": "false"}, 1, "true"):
                state = app.invoke(Command(resume=bad), config=cfg)
                offending = bad.get("approved") if isinstance(bad, dict) else bad
                assert repr(offending) in state["__interrupt__"][0].value["error"]
                assert state["approval_log"] == []

            state = app.invoke(Command(resume={"approved": False, "reason": "补充管径表格"}), config=cfg)
        request = state["__interrupt__"][0].value
        assert "error" not in request, "新一稿的审批请求不得带着上一稿的输入错误"
        assert "补充管径表格" in request["draft_preview"]
        assert state["draft_rounds"] == 1
        assert state["approval_log"] == [{"decision": "rejected", "reason": "补充管径表格", "draft_rounds": 0}]

    def test_route_after_approval_failed_never_finalize(self) -> None:
        """failed 状态在 approval 之后直接终止，不得进入 finalize。"""
        assert route_after_approval({"status": STATUS_FAILED}) == "__end__"
        assert route_after_approval({"status": STATUS_REJECTED}) == "draft"
        assert route_after_approval({"status": STATUS_APPROVED}) == "finalize"
        assert route_after_approval({"status": STATUS_WAITING}) == "finalize"


class TestInterruptBasics:
    def test_invoke_suspends_with_preview(self) -> None:
        """首次 invoke 触发 interrupt，状态快照含 draft_preview；approval 为 None（待审批）。"""
        with MemorySaver() as saver:
            app = build_docgen_graph(checkpointer=saver)
            state = app.invoke(_initial_state(), config=_thread())
        assert "__interrupt__" in state
        interrupts = state["__interrupt__"]
        assert interrupts, "必须有 interrupt 挂起点"
        value = interrupts[0].value
        assert "draft_preview" in value
        assert value["draft_preview"], "预览草稿不能为空"
        assert state.get("approval") is None
        # 中断发生在 finalize（副作用）之前
        assert state.get("final_doc") is None

    def test_approve_resume_completes(self) -> None:
        """Command(resume={'approved': True}) 后流程走完且状态含审批记录。"""
        with MemorySaver() as saver:
            app = build_docgen_graph(checkpointer=saver)
            app.invoke(_initial_state(), config=_thread())
            state = app.invoke(Command(resume={"approved": True}), config=_thread())
        assert state["approval"] is True
        assert state["status"] == STATUS_SUCCEEDED
        assert state["final_doc"], "批准后必须产出正式文档"
        assert state["approval_log"] == [{"decision": "approved", "reason": "", "draft_rounds": 0}]

    def test_no_checkpointer_raises(self) -> None:
        """无 checkpointer 拒绝构建（interrupt 依赖持久化，护住纪律）。"""
        with pytest.raises(ValueError, match="checkpointer"):
            build_docgen_graph(checkpointer=None)

    def test_non_dict_resume_value_is_tolerated(self) -> None:
        """resume 值不是 dict（如 True）→ 按布尔处理，不抛裸异常。"""
        with MemorySaver() as saver:
            app = build_docgen_graph(checkpointer=saver)
            app.invoke(_initial_state(), config=_thread())
            state = app.invoke(Command(resume=True), config=_thread())
        assert state["status"] == STATUS_SUCCEEDED

    def test_gathered_citations_and_calc_rendered(self) -> None:
        """gather_inputs 汇集的 citations/calc_result 出现在草稿预览里。"""
        with MemorySaver() as saver:
            app = build_docgen_graph(checkpointer=saver)
            state = app.invoke(
                _initial_state(
                    citations=[CitationClaim(source_id="GB50015-2019 3.7.6")],
                    calc_result={"velocity_mps": 1.23},
                ),
                config=_thread(),
            )
        preview = state["__interrupt__"][0].value["draft_preview"]
        assert "GB50015-2019 3.7.6" in preview
        assert "velocity_mps" in preview and "1.23" in preview


class TestRejectBranch:
    def test_reject_with_feedback_returns_to_draft(self) -> None:
        """拒绝+反馈 → 回到 draft，draft_rounds 递增，再次 interrupt 且 approval 复位 None；再批准 → 成功。"""
        with MemorySaver() as saver:
            app = build_docgen_graph(checkpointer=saver)
            app.invoke(_initial_state(), config=_thread())

            # 第一轮：拒绝并给反馈
            state = app.invoke(
                Command(resume={"approved": False, "reason": "补充管径表格"}),
                config=_thread(),
            )
            assert "__interrupt__" in state, "拒绝后必须回到 draft 并再次 interrupt"
            assert state["approval"] is None, "重拟后再次挂起 = 待审批，approval 必须复位 None"
            assert state["draft_rounds"] == 1
            assert state["approval_reason"] == "补充管径表格"
            assert state["status"] == STATUS_WAITING, "再次挂起时应处于等待审批状态"
            assert state["approval_log"][-1] == {"decision": "rejected", "reason": "补充管径表格", "draft_rounds": 0}

            # 第二轮：批准
            state = app.invoke(Command(resume={"approved": True}), config=_thread())
        assert state["status"] == STATUS_SUCCEEDED
        assert state["final_doc"]
        assert state["approval_reason"] == "补充管径表格", "反馈必须保留在最终状态（可追溯）"
        assert [e["decision"] for e in state["approval_log"]] == ["rejected", "approved"]

    def test_reject_exceeds_max_rounds_fails(self) -> None:
        """第 MAX_DRAFT_ROUNDS 稿仍被拒 → failed 且 errors 可见；共 MAX_DRAFT_ROUNDS 次拒绝。"""
        with MemorySaver() as saver:
            app = build_docgen_graph(checkpointer=saver)
            app.invoke(_initial_state(), config=_thread())
            for _ in range(MAX_DRAFT_ROUNDS):
                state = app.invoke(
                    Command(resume={"approved": False, "reason": "再改一次"}),
                    config=_thread(),
                )
        assert state["status"] == STATUS_FAILED
        assert state["final_doc"] is None, "失败不得产出正式文档"
        assert any("用户拒绝" in e for e in state["errors"]), f"errors: {state['errors']}"
        assert len(state["approval_log"]) == MAX_DRAFT_ROUNDS
        assert state["draft_rounds"] == MAX_DRAFT_ROUNDS - 1  # 总稿数 = draft_rounds + 1 = 上限

    def test_feedback_updates_draft_content(self) -> None:
        """反馈内容应体现在新一轮 draft 的预览里（可行动闭环）。"""
        with MemorySaver() as saver:
            app = build_docgen_graph(checkpointer=saver)
            first = app.invoke(_initial_state(), config=_thread())
            first_preview = first["__interrupt__"][0].value["draft_preview"]

            state = app.invoke(
                Command(resume={"approved": False, "reason": "补充管径表格"}),
                config=_thread(),
            )
            second_preview = state["__interrupt__"][0].value["draft_preview"]
        assert "补充管径表格" in second_preview, "新一轮 draft 预览必须体现用户反馈"
        assert second_preview != first_preview


class TestPersistence:
    def test_sqlite_checkpoint_survives_reopen(self, tmp_path, capsys) -> None:
        """验收：中断后可恢复——SqliteSaver 落盘，关闭连接、重建图与连接后 resume 走完。

        引用状态（CitationClaim）必须完整跨进程恢复且无反序列化告警（S9-01 回归）。
        """
        from langgraph.checkpoint.sqlite import SqliteSaver

        from water_agent.session import default_checkpoint_serde

        db = tmp_path / "docgen.sqlite"

        conn1 = sqlite3.connect(str(db), check_same_thread=False)
        try:
            app1 = build_docgen_graph(checkpointer=SqliteSaver(conn1, serde=default_checkpoint_serde()))
            suspended = app1.invoke(
                _initial_state(citations=[CitationClaim(source_id="FIX 1.1.1", declared_by="system")]),
                config=_thread("persist-1"),
            )
            assert "__interrupt__" in suspended
        finally:
            conn1.close()

        # 模拟进程重启：新连接 + 新图实例，只凭 thread_id 恢复
        conn2 = sqlite3.connect(str(db), check_same_thread=False)
        try:
            app2 = build_docgen_graph(checkpointer=SqliteSaver(conn2, serde=default_checkpoint_serde()))
            state = app2.invoke(Command(resume={"approved": True}), config=_thread("persist-1"))
        finally:
            conn2.close()
        assert state["status"] == STATUS_SUCCEEDED
        assert state["final_doc"]
        assert state["approval_log"][-1]["decision"] == "approved"
        # 非空引用跨进程恢复，且 CitationClaim 未被 msgpack 阻止反序列化
        assert state.get("citations") and state["citations"][0].source_id == "FIX 1.1.1"
        captured = capsys.readouterr()
        assert "Blocked deserialization" not in captured.err
        assert "Blocked deserialization" not in captured.out

    def test_invalid_resume_stays_suspended_across_reopen(self, tmp_path) -> None:
        """非法 resume 值落盘后跨进程仍是"挂起 + 可见错误"，新进程传合法值能恢复。"""
        from langgraph.checkpoint.sqlite import SqliteSaver

        from water_agent.session import default_checkpoint_serde

        db = tmp_path / "docgen.sqlite"
        cfg = _thread("persist-invalid")

        conn1 = sqlite3.connect(str(db), check_same_thread=False)
        try:
            app1 = build_docgen_graph(checkpointer=SqliteSaver(conn1, serde=default_checkpoint_serde()))
            app1.invoke(_initial_state(), config=cfg)
            app1.invoke(Command(resume={"approved": "false"}), config=cfg)
        finally:
            conn1.close()

        conn2 = sqlite3.connect(str(db), check_same_thread=False)
        try:
            app2 = build_docgen_graph(checkpointer=SqliteSaver(conn2, serde=default_checkpoint_serde()))
            pending = _pending_requests(app2, cfg)
            assert len(pending) == 1 and "严格 bool" in pending[0]["error"]
            state = app2.invoke(Command(resume={"approved": True}), config=cfg)
        finally:
            conn2.close()
        assert state["status"] == STATUS_SUCCEEDED
        assert state["approval_log"] == [{"decision": "approved", "reason": "", "draft_rounds": 0}]
