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
from water_agent.graphs.docgen import STATUS_SUCCEEDED, build_docgen_graph
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
