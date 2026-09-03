"""Step 9 测试：Human-in-the-Loop 审批节点（interrupt + Command(resume)）。

覆盖（拒绝分支语义已选定：拒绝→带反馈回到 draft，最多 2 轮，超上限收口 failed）：
- 首次 invoke 触发 interrupt，状态快照含 draft_preview；
- approve 恢复后流程走完且状态含审批记录（approval=True、final_doc 生成）；
- reject + reason 带反馈回流到 draft，draft_rounds 递增，再审批通过；
- 拒绝达上限 → failed 且 errors 可见；
- 无 checkpointer 构建时报错（护住会话纪律）。
"""
from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from water_agent.graphs.docgen import (
    MAX_DRAFT_ROUNDS,
    STATUS_APPROVED,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    STATUS_WAITING,
    build_docgen_graph,
)


def _initial_state(query: str = "生成 DN150 给水管道计算书") -> dict:
    return {"query": query, "status": "running", "draft_rounds": 0}


def _thread(thread_id: str = "t1") -> dict:
    return {"configurable": {"thread_id": thread_id}}


class TestInterruptBasics:
    def test_invoke_suspends_with_preview(self) -> None:
        """首次 invoke 触发 interrupt，状态快照含 draft_preview。"""
        with MemorySaver() as saver:
            app = build_docgen_graph(checkpointer=saver)
            state = app.invoke(_initial_state(), config=_thread())
        assert "__interrupt__" in state
        interrupts = state["__interrupt__"]
        assert interrupts, "必须有 interrupt 挂起点"
        value = interrupts[0].value
        assert "draft_preview" in value
        assert value["draft_preview"], "预览草稿不能为空"
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

    def test_no_checkpointer_raises(self) -> None:
        """无 checkpointer 拒绝构建（interrupt 依赖持久化，护住纪律）。"""
        with pytest.raises(ValueError, match="checkpointer"):
            build_docgen_graph(checkpointer=None)


class TestRejectBranch:
    def test_reject_with_feedback_returns_to_draft(self) -> None:
        """拒绝+反馈 → 回到 draft，draft_rounds 递增，再次 interrupt；再批准 → 成功。"""
        with MemorySaver() as saver:
            app = build_docgen_graph(checkpointer=saver)
            app.invoke(_initial_state(), config=_thread())

            # 第一轮：拒绝并给反馈
            state = app.invoke(
                Command(resume={"approved": False, "reason": "补充管径表格"}),
                config=_thread(),
            )
            assert "__interrupt__" in state, "拒绝后必须回到 draft 并再次 interrupt"
            assert state["approval"] is False
            assert state["draft_rounds"] == 1
            assert state["approval_reason"] == "补充管径表格"
            assert state["status"] == STATUS_WAITING, "再次挂起时应处于等待审批状态"

            # 第二轮：批准
            state = app.invoke(Command(resume={"approved": True}), config=_thread())
        assert state["status"] == STATUS_SUCCEEDED
        assert state["final_doc"]
        assert state["approval_reason"] == "补充管径表格", "反馈必须保留在最终状态（可追溯）"

    def test_reject_exceeds_max_rounds_fails(self) -> None:
        """拒绝达上限（MAX_DRAFT_ROUNDS）→ failed 且 errors 可见。"""
        with MemorySaver() as saver:
            app = build_docgen_graph(checkpointer=saver)
            app.invoke(_initial_state(), config=_thread())
            # 连续拒绝 MAX_DRAFT_ROUNDS 次（最后一次触发上限收口）
            for _ in range(MAX_DRAFT_ROUNDS):
                state = app.invoke(
                    Command(resume={"approved": False, "reason": "再改一次"}),
                    config=_thread(),
                )
        assert state["status"] == STATUS_FAILED
        assert state["final_doc"] is None, "失败不得产出正式文档"
        assert any("用户拒绝" in e for e in state["errors"]), f"errors: {state['errors']}"

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