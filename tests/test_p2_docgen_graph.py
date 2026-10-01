"""P2 测试：docgen 图结构化渲染接线（doc_type 状态 / 终稿不带稿次 / 材料行可选）+ CLI --doc-type
+ CLI 人审的恢复安全（非法 resume 值后仍可 --resume、CLI 边界先校验再 invoke）。"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from water_agent.cli import main
from water_agent.domain.citation import CitationClaim
from water_agent.domain.state import STATUS_FAILED, STATUS_SUCCEEDED, STATUS_WAITING
from water_agent.graphs.docgen import MAX_SELF_CHECK_ATTEMPTS, build_docgen_graph
from water_agent.session import default_checkpoint_serde, session_db_path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _thread(thread_id: str = "t") -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _docgen_cli_args(tmp_path: Path) -> list[str]:
    return ["--flow", "docgen", "--mock-generator", "--session-dir", str(tmp_path / "s"), "--trace-dir", str(tmp_path / "t")]


def _scripted_input(monkeypatch: pytest.MonkeyPatch, answers: list) -> None:
    """按序喂 CLI 的 input()；元素是异常类（EOFError / KeyboardInterrupt）时在那一次提示处抛出。"""
    it = iter(answers)

    def _input(_prompt: str = "") -> str:
        answer = next(it)
        if isinstance(answer, type) and issubclass(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr("builtins.input", _input)


def _suspended_session_id(text: str) -> str:
    """从"……用 --resume <id> 恢复"提示里取会话 ID。"""
    return text.split("--resume ")[1].split()[0]


@contextmanager
def _session_graph(tmp_path: Path) -> Iterator:
    """直接打开 CLI 落盘的会话库（与 CLI 同一 serde），模拟别的调用方操作同一 thread。"""
    conn = sqlite3.connect(str(session_db_path(tmp_path / "s")), check_same_thread=False)
    try:
        yield build_docgen_graph(checkpointer=SqliteSaver(conn, serde=default_checkpoint_serde()))
    finally:
        conn.close()


def _pending_requests(app, config: dict) -> list[dict]:
    """挂起的审批请求（看 interrupts，不看 next——非法值重新挂起后 next 为 ()）。"""
    snapshot = app.get_state(config)
    return [i.value for task in snapshot.tasks for i in task.interrupts]


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

    def test_second_document_in_thread_gets_full_self_check_budget(self, monkeypatch) -> None:
        """同一 thread 的第二份文档自检从 0 计：首检失败仍可重试一次。

        回归：曾继承上一份的 self_check_attempts=1，首检即记为第 2 次、直接 failed。
        """
        cfg = _thread("sc-two-docs")
        with MemorySaver() as saver:
            app = build_docgen_graph(checkpointer=saver)
            app.invoke(_docgen_initial_state(), config=cfg)
            first = app.invoke(Command(resume={"approved": True}), config=cfg)
            assert first.get("status") == STATUS_SUCCEEDED and first.get("self_check_attempts") == 1

            failures = {"left": 1}

            def flaky_check(rendered, *, calc_result=None):
                if failures["left"] > 0:
                    failures["left"] -= 1
                    return ["模拟自检失败：缺少必需章节：结论"]
                return []

            monkeypatch.setattr("water_agent.graphs.docgen.check_docgen_self_check", flaky_check)
            second = app.invoke(_docgen_initial_state(query="生成 DN100 给水管道计算书"), config=cfg)
        assert "__interrupt__" in second, "新文档首检失败应有界重试后到达审批，而不是直接 failed"
        assert second.get("self_check_attempts") == 2
        assert second.get("self_check_errors") == []
        assert second.get("status") == STATUS_WAITING


class TestCliResumeSafety:
    """CLI 人审的恢复安全：非法 resume 值不毒化会话；CLI 自己绝不把非法决定送进图。"""

    def test_resume_recovers_session_after_invalid_resume_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """别的调用方对挂起会话传过非法 resume 值后，CLI --resume 仍识别为挂起并能走完。

        回归：修复前非法值让 approval 节点抛 TypeError，并被钉进 checkpoint（之后合法值也
        恢复不了）；此时 get_state().next 为 ()，CLI 只看 next，判为"没有挂起任务"。
        """
        args = _docgen_cli_args(tmp_path)
        _scripted_input(monkeypatch, [EOFError])
        assert main([*args, "生成 DN150 给水管道计算书"]) == 0
        session_id = _suspended_session_id(capsys.readouterr().out)

        with _session_graph(tmp_path) as app:
            app.invoke(Command(resume={"approved": "false"}), config=_thread(session_id))

        _scripted_input(monkeypatch, ["y"])
        code = main([*args, "--resume", session_id, "--json"])
        captured = capsys.readouterr()
        assert code == 0, captured.err
        payload = json.loads(captured.out)
        assert payload["status"] == "succeeded" and payload["final_doc"]
        assert payload["approval_log"] == [{"decision": "approved", "reason": "", "draft_rounds": 0}]
        assert "严格 bool" in captured.err, "上一次被拒收的输入要让审批人看见"

    def test_cli_validates_decision_before_invoking_graph(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """CLI 边界先校验再 invoke：构造出的决定不是严格 bool 时不送进图，会话原样挂起。"""
        args = _docgen_cli_args(tmp_path)
        monkeypatch.setattr("water_agent.cli._prompt_docgen_decision", lambda ui: {"approved": "false"})
        code = main([*args, "生成 DN150 给水管道计算书"])
        captured = capsys.readouterr()
        assert code == 2
        assert "严格 bool" in captured.err
        session_id = _suspended_session_id(captured.out)
        with _session_graph(tmp_path) as app:
            pending = _pending_requests(app, _thread(session_id))
            values = app.get_state(_thread(session_id)).values
        assert len(pending) == 1 and "error" not in pending[0], "非法决定不得进图（图内校验都不该见到它）"
        assert values["approval_log"] == []

        monkeypatch.undo()
        _scripted_input(monkeypatch, ["y"])
        assert main([*args, "--resume", session_id]) == 0

    def test_resume_finished_session_with_new_query_starts_a_clean_document(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """--resume <已完成会话> "新描述"：同一 thread 上的第二份文档不继承第一份的拒绝原因与审批记录。"""
        args = _docgen_cli_args(tmp_path)
        _scripted_input(monkeypatch, ["n", "第一份的拒绝原因", "y"])
        assert main([*args, "--json", "生成 DN150 给水管道计算书"]) == 0
        first = json.loads(capsys.readouterr().out)
        assert first["status"] == "succeeded" and len(first["approval_log"]) == 2

        _scripted_input(monkeypatch, ["y"])
        code = main([*args, "--json", "--resume", first["session_id"], "生成某小区给水改造设计说明"])
        captured = capsys.readouterr()
        assert code == 0, captured.err
        second = json.loads(captured.out)
        assert second["session_id"] == first["session_id"]
        assert second["status"] == "succeeded"
        assert second["approval_log"] == [{"decision": "approved", "reason": "", "draft_rounds": 0}]
        assert "第一份的拒绝原因" not in second["final_doc"]
        assert "第一份的拒绝原因" not in captured.err, "第二份文档的草稿预览里也不得出现上一份的拒绝原因"
