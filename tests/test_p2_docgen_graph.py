"""P2 测试：docgen 图结构化渲染接线（doc_type 状态 / 终稿不带稿次 / 材料行可选）+ CLI --doc-type。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from water_agent.cli import main
from water_agent.domain.citation import CitationClaim
from water_agent.domain.state import STATUS_FAILED, STATUS_SUCCEEDED, STATUS_WAITING
from water_agent.graphs.docgen import MAX_SELF_CHECK_ATTEMPTS, build_docgen_graph
from water_agent.session import default_checkpoint_serde

REPO_ROOT = Path(__file__).resolve().parents[1]


def _thread(thread_id: str = "t") -> dict:
    return {"configurable": {"thread_id": thread_id}}


class TestGraphStructuredRendering:
    def test_draft_and_final_have_compliant_structure(self) -> None:
        with MemorySaver() as saver:
            app = build_docgen_graph(checkpointer=saver)
            state = app.invoke(
                {
                    "query": "生成 DN150 给水管道计算书",
                    "status": "running",
                    "draft_rounds": 0,
                    "doc_type": "calculation_book",
                    "citations": [CitationClaim(source_id="FIX 1.1.1", declared_by="system")],
                    "calc_result": {"velocity_mps": 1.131768, "diameter_mm": 150},
                },
                config=_thread(),
            )
        preview = state["__interrupt__"][0].value["draft_preview"]
        assert "# 计算书草稿（第 1 稿）" in preview
        for heading in ["## 1 工程概况", "## 2 设计依据", "## 3 计算参数与结果", "## 4 结论"]:
            assert heading in preview
        assert "velocity_mps = 1.13177 m/s" in preview

    def test_final_doc_strips_draft_marker_keeps_content(self) -> None:
        with MemorySaver() as saver:
            app = build_docgen_graph(checkpointer=saver)
            app.invoke(
                {
                    "query": "生成 DN150 给水管道计算书",
                    "status": "running",
                    "draft_rounds": 0,
                    "doc_type": "calculation_book",
                    "calc_result": {"velocity_mps": 1.131768},
                    "citations": [],
                },
                config=_thread(),
            )
            state = app.invoke(Command(resume={"approved": True}), config=_thread())
        assert state["status"] == STATUS_SUCCEEDED
        final = state["final_doc"]
        assert final and "# 计算书草稿" not in final
        assert final.startswith("# 计算书\n")
        assert "velocity_mps = 1.13177 m/s" in final

    def test_design_note_doc_type_via_state(self) -> None:
        with MemorySaver() as saver:
            app = build_docgen_graph(checkpointer=saver)
            state = app.invoke(
                {
                    "query": "生成某小区给水改造设计说明",
                    "status": "running",
                    "draft_rounds": 0,
                    "doc_type": "design_note",
                    "citations": [CitationClaim(source_id="FIX 1.1.1")],
                    "calc_result": None,
                },
                config=_thread(),
            )
        preview = state["__interrupt__"][0].value["draft_preview"]
        assert "# 设计说明草稿（第 1 稿）" in preview
        assert "## 3 设计参数与计算结果" in preview

    def test_doc_type_persists_across_sqlite_resume(self, tmp_path: Path) -> None:
        db = tmp_path / "docgen.sqlite"
        conn1 = sqlite3.connect(str(db), check_same_thread=False)
        app1 = build_docgen_graph(checkpointer=SqliteSaver(conn1, serde=default_checkpoint_serde()))
        app1.invoke(
            {
                "query": "生成 DN150 计算书",
                "status": "running",
                "draft_rounds": 0,
                "doc_type": "calculation_book",
                "calc_result": {"velocity_mps": 1.0},
                "citations": [],
            },
            config=_thread("p2-persist"),
        )
        conn1.close()
        conn2 = sqlite3.connect(str(db), check_same_thread=False)
        try:
            app2 = build_docgen_graph(checkpointer=SqliteSaver(conn2, serde=default_checkpoint_serde()))
            state = app2.invoke(Command(resume={"approved": True}), config=_thread("p2-persist"))
        finally:
            conn2.close()
        assert state["status"] == STATUS_SUCCEEDED
        assert state.get("doc_type") == "calculation_book"


class TestCliDocType:
    def test_explicit_design_note_on_calc_query(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """calc 意图 + --doc-type design_note：标题走设计说明。"""
        it = iter(["y"])
        monkeypatch.setattr("builtins.input", lambda _prompt="": next(it))
        code = main(
            [
                "--flow", "docgen", "--doc-type", "design_note", "--mock-generator", "--json",
                "--session-dir", str(tmp_path / "s"), "--trace-dir", str(tmp_path / "t"),
                "DN150、Q=20L/s，求流速，生成给水管道设计说明",
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        assert code == 0 and payload["status"] == "succeeded"
        final = payload["final_doc"]
        assert final.startswith("# 设计说明\n")
        assert "设计参数与计算结果" in final
        assert "velocity_mps = 1.13177 m/s" in final


def _docgen_initial_state(**extra) -> dict:
    return {
        "query": "生成 DN150 给水管道计算书",
        "status": "running",
        "draft_rounds": 0,
        "doc_type": "calculation_book",
        "calc_result": {"diameter_mm": 150, "flow_lps": 20, "velocity_mps": 1.131768},
        "citations": [CitationClaim(source_id="FIX 1.1.1", declared_by="system")],
        **extra,
    }


class TestSelfCheckNode:
    """P2 收口：draft → self_check → approval。草稿必须通过自检才能到达审批。"""

    def test_normal_draft_passes_self_check_before_approval(self) -> None:
        """正常草稿：自检 1 次通过（attempts=1，无问题），才进入 approval 挂起。"""
        with MemorySaver() as saver:
            app = build_docgen_graph(checkpointer=saver)
            state = app.invoke(_docgen_initial_state(), config=_thread("sc-ok"))
        assert state.get("self_check_attempts") == 1
        assert state.get("self_check_errors") == []
        assert state.get("status") == STATUS_WAITING
        assert "__interrupt__" in state, "自检通过后才到达审批挂起"

    def test_self_check_failure_retries_then_passes(self, monkeypatch) -> None:
        """自检失败 1 次 → 有界重试回 draft → 第 2 次通过 → 到达审批；批准后走完。"""
        state = {"failures_left": 1}

        def flaky_check(rendered, *, calc_result=None):
            if state["failures_left"] > 0:
                state["failures_left"] -= 1
                return ["模拟自检失败：缺少必需章节：结论"]
            return []

        monkeypatch.setattr("water_agent.graphs.docgen.check_docgen_self_check", flaky_check)
        with MemorySaver() as saver:
            app = build_docgen_graph(checkpointer=saver)
            suspended = app.invoke(_docgen_initial_state(), config=_thread("sc-retry"))
            assert "__interrupt__" in suspended, "重试通过后必须到达审批"
            assert suspended.get("self_check_attempts") == 2
            assert suspended.get("self_check_errors") == []
            assert suspended.get("status") == STATUS_WAITING
            resumed = app.invoke(Command(resume={"approved": True}), config=_thread("sc-retry"))
        assert resumed.get("status") == STATUS_SUCCEEDED
        assert resumed.get("final_doc")

    def test_self_check_failure_exhausted_fails_without_approval(self, monkeypatch) -> None:
        """自检连续失败达上限 → failed，无 interrupt、无 final_doc，errors 可见。"""
        monkeypatch.setattr(
            "water_agent.graphs.docgen.check_docgen_self_check",
            lambda rendered, *, calc_result=None: ["模拟自检失败：缺少必需章节：结论"],
        )
        with MemorySaver() as saver:
            app = build_docgen_graph(checkpointer=saver)
            state = app.invoke(_docgen_initial_state(), config=_thread("sc-fail"))
        assert "__interrupt__" not in state, "自检失败的草稿不得到达审批"
        assert state.get("status") == STATUS_FAILED
        assert state.get("final_doc") is None
        assert state.get("self_check_attempts") == MAX_SELF_CHECK_ATTEMPTS
        assert any("自检" in error for error in state.get("errors", [])), state.get("errors")
        assert any("上限" in error for error in state.get("errors", [])), state.get("errors")

    def test_rejected_draft_gets_fresh_self_check_budget(self, monkeypatch) -> None:
        """人审拒绝 → 新稿 reset self_check_attempts=0，自检预算按新稿重新计数。"""
        fail_first_draft = {"round": 0}

        def flaky_check(rendered, *, calc_result=None):
            # 仅第 1 稿第 1 次自检失败；重检与第 2 稿都通过
            if fail_first_draft["round"] == 0 and fail_first_draft.get("hit", False) is False:
                fail_first_draft["hit"] = True
                return ["模拟自检失败：缺少必需章节：结论"]
            return []

        monkeypatch.setattr("water_agent.graphs.docgen.check_docgen_self_check", flaky_check)
        with MemorySaver() as saver:
            app = build_docgen_graph(checkpointer=saver)
            suspended = app.invoke(_docgen_initial_state(), config=_thread("sc-budget"))
            # 重试后到达审批；此时拒绝
            assert "__interrupt__" in suspended
            rejected = app.invoke(
                Command(resume={"approved": False, "reason": "补充管径表格"}),
                config=_thread("sc-budget"),
            )
        assert "__interrupt__" in rejected, "第 2 稿应再次挂起等待审批"
        assert rejected.get("self_check_attempts") == 1, "新稿自检预算已复位（新稿只自检 1 次）"
        assert rejected.get("draft_rounds") == 1
