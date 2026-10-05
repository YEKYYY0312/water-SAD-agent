"""人工拒绝 / 审批漂移收口在 approval 节点（review 2026-09-30 缺陷 2 回归）。

此前 ``approval → release_gate`` 是无条件边：人工拒绝后仍进发布门，errors 变成
``[MULTI_APPROVAL_REQUIRED, MULTI_APPROVAL_STALE]``，trace 出现
``export_attempt(approval_valid=false)``，``unapproved_export`` 检测器对每一次正常拒绝都
报警；拒绝原因只在 LangGraph writes 表里，state 的 ``approval_record`` 为空；审批漂移的
STALE 记两次。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from pydantic import ValidationError

from water_agent.harness.multi_agent_metrics import detect_multi_agent_events
from water_agent.multi_agent.contracts import (
    MULTI_APPROVAL_REQUIRED,
    MULTI_APPROVAL_STALE,
    ApprovalRecord,
    ArtifactEnvelope,
    DrawingPlan,
    compute_payload_hash,
)
from water_agent.multi_agent.export_adapter import assemble_export_path
from water_agent.multi_agent.orchestrator import (
    _final_package,
    _registry,
    _release_artifacts,
    build_orchestrator_graph,
    default_multi_agent_state,
)
from water_agent.session import default_checkpoint_serde

_RELEASE_CHECKS = {
    "approval_exists",
    "approval_not_expired",
    "package_hash_matches",
    "no_stale_artifacts",
    "review_passed",
    "no_unresolved_blocking_findings",
}


def _brief() -> dict[str, Any]:
    return {
        "project_id": "proj-approval-reject",
        "request": "给水管段设计，DN150 PE 管 10m，流量 10L/s",
        "systems": ["water_supply"],
        "unit_system": "SI",
        "hydraulic_cases": [
            {"case_id": "case-vel-1", "formula": "velocity", "inputs": {"flow_lps": 10, "diameter_mm": 150}}
        ],
        "topology": [{"start_node_id": "n1", "end_node_id": "n2", "length_m": 10.0}],
        "spatial_constraints": [{"kind": "material", "value": "PE"}],
        "required_outputs": ["calculation_book", "drawing"],
        "user_constraints": ["material: PE", "DN150"],
    }


class _Run:
    """One persisted orchestrator thread paused at the approval interrupt."""

    def __init__(self, tmp_path: Path, *, thread_id: str = "approval-reject") -> None:
        self.db = tmp_path / "state.sqlite"
        self.final_dir = tmp_path / "final"
        self.exports = 0
        self.conn = sqlite3.connect(self.db, check_same_thread=False)
        self.config = {"configurable": {"thread_id": thread_id}}

        def exporter(**kwargs: Any) -> str:
            self.exports += 1
            return assemble_export_path(**kwargs)

        self.app = build_orchestrator_graph(
            checkpointer=SqliteSaver(self.conn, serde=default_checkpoint_serde()),
            exporter=exporter,
        )
        self.paused = dict(
            self.app.invoke(
                default_multi_agent_state(
                    project_brief=_brief(),
                    retrieved_source_ids=["GB50015-2019 3.2.1"],
                    staging_dir=str(tmp_path / "staging"),
                    final_dir=str(self.final_dir),
                    output_file_id="approval-reject",
                ),
                config=self.config,
            )
        )
        assert self.paused["__interrupt__"]
        self.shown_hash = self.paused["__interrupt__"][0].value["final_package_hash"]

    def resume(self, decision: dict[str, Any]) -> dict[str, Any]:
        return dict(self.app.invoke(Command(resume=decision), config=self.config))

    def close(self) -> None:
        self.conn.close()


@pytest.fixture()
def run(tmp_path: Path):
    paused = _Run(tmp_path)
    try:
        yield paused
    finally:
        paused.close()


def _names(result: dict[str, Any]) -> list[str]:
    return [event["event"] for event in result["trace_events"]]


def _detectors(result: dict[str, Any]) -> set[str]:
    return {finding["detector_id"] for finding in detect_multi_agent_events(result["trace_events"])}


def test_rejection_ends_at_approval_with_only_approval_required(run: _Run) -> None:
    result = run.resume({"approved": False, "reason": "管径需复核"})

    assert result["status"] == "blocked"
    assert result["errors"] == [MULTI_APPROVAL_REQUIRED]
    names = _names(result)
    assert "release_gate_checked" not in names
    assert "export_attempt" not in names
    assert "export_started" not in names
    assert result["release_checks"] == {}
    assert names[-1] == "run_finished"
    assert result["trace_events"][-1]["run_status"] == "blocked"
    resumed = [event for event in result["trace_events"] if event["event"] == "approval_resumed"]
    assert resumed[-1]["approval_decision"] == "rejected"
    assert resumed[-1]["error_code"] == MULTI_APPROVAL_REQUIRED
    assert result["exported_file_path"] is None
    assert run.exports == 0


def test_rejection_is_persisted_as_an_approval_record(run: _Run) -> None:
    result = run.resume({"approved": False, "reason": "管径需复核"})

    record = ApprovalRecord.model_validate(result["approval_record"])
    assert record.approved is False
    assert record.reason == "管径需复核"
    assert record.final_package_hash == run.shown_hash
    assert record.artifact_collection_hash == run.shown_hash
    assert result["final_project_package"] is None

    # 拒绝原因必须进 checkpoint 状态本身（不是只在 LangGraph 的 writes 表里）
    run.conn.close()
    reopened = sqlite3.connect(run.db, check_same_thread=False)
    try:
        app = build_orchestrator_graph(checkpointer=SqliteSaver(reopened, serde=default_checkpoint_serde()))
        values = app.get_state(run.config).values
        assert ApprovalRecord.model_validate(values["approval_record"]).reason == "管径需复核"
        assert values["status"] == "blocked"
    finally:
        reopened.close()
        run.conn = sqlite3.connect(run.db, check_same_thread=False)


def test_rejection_without_reason_still_records_the_decision(run: _Run) -> None:
    result = run.resume({"approved": False})

    record = ApprovalRecord.model_validate(result["approval_record"])
    assert record.approved is False
    assert record.reason
    assert result["errors"] == [MULTI_APPROVAL_REQUIRED]


def test_rejection_does_not_trigger_export_detectors(run: _Run) -> None:
    result = run.resume({"approved": False, "reason": "管径需复核"})

    detectors = _detectors(result)
    assert "unapproved_export" not in detectors
    assert "approval_drift" not in detectors


def test_post_interrupt_drift_records_one_stale_and_skips_release_gate(run: _Run) -> None:
    paused = run.paused
    plan_id = paused["active_artifact_ids"]["drawing_plan"]
    envelope = ArtifactEnvelope.model_validate(paused["artifacts"][plan_id])
    plan = DrawingPlan.model_validate(envelope.payload)
    plan.integration_conflicts.append("changed after approval interrupt")
    payload = plan.model_dump(mode="python")
    drifted = envelope.model_copy(update={"payload": payload, "payload_hash": compute_payload_hash(payload)})
    run.app.update_state(
        run.config, {"artifacts": {**paused["artifacts"], plan_id: drifted.model_dump(mode="json")}}
    )

    result = run.resume({"approved": True, "reason": "looks fine"})

    assert result["status"] == "blocked"
    assert result["errors"] == [MULTI_APPROVAL_STALE]
    names = _names(result)
    assert "release_gate_checked" not in names
    assert "export_attempt" not in names
    assert names[-1] == "run_finished"
    detectors = _detectors(result)
    assert "approval_drift" in detectors
    assert "unapproved_export" not in detectors
    assert run.exports == 0


def test_release_gate_still_blocks_an_export_attempt_that_bypassed_approval(run: _Run) -> None:
    # 注入：审批节点被跳过（没有任何人工决定）却把运行推向发布门。发布门是导出前最后一道门，
    # 6 项检查必须照常重算并拦下，且错误码是"未获批准"而不是"批准已过期"。
    run.app.update_state(run.config, {"status": "running"}, as_node="approval")
    result = dict(run.app.invoke(None, config=run.config))

    assert result["status"] == "blocked"
    assert result["errors"] == [MULTI_APPROVAL_REQUIRED]
    assert set(result["release_checks"]) == _RELEASE_CHECKS
    assert result["release_checks"]["approval_exists"] is False
    assert result["release_checks"]["package_hash_matches"] is False
    assert result["exported_file_path"] is None
    assert run.exports == 0
    assert not (run.final_dir / "approval-reject.dxf").exists()
    checked = [event for event in result["trace_events"] if event["event"] == "release_gate_checked"]
    assert checked[-1]["error_code"] == MULTI_APPROVAL_REQUIRED
    assert "unapproved_export" in _detectors(result)


def test_approved_resume_still_reaches_release_gate_and_exports(run: _Run) -> None:
    result = run.resume({"approved": True, "reason": "checked"})

    assert result["status"] == "succeeded"
    assert result["errors"] == []
    assert all(result["release_checks"].values())
    assert run.exports == 1
    resumed = [event for event in result["trace_events"] if event["event"] == "approval_resumed"]
    assert resumed[-1]["approval_decision"] == "approved"
    assert "unapproved_export" not in _detectors(result)


# ---------------------------------------------------------------------------
# 非法 resume 值：重新挂起，不毒化 checkpoint（与 docgen PR #13 同一类问题）
# ---------------------------------------------------------------------------

_INVALID_RESUMES: dict[str, Any] = {
    "approved_string_false": {"approved": "false"},
    "approved_int": {"approved": 1},
    "approved_none": {"approved": None},
    "missing_approved_key": {"reason": "looks fine"},
    "non_dict": "yes",
    "bare_bool": True,
    "reason_not_text": {"approved": True, "reason": 5},
    "expires_at_not_text": {"approved": True, "expires_at": 123},
    "expires_at_not_iso": {"approved": True, "expires_at": "next tuesday"},
}


def _pending(app: Any, config: dict[str, Any]) -> list[Any]:
    snapshot = app.get_state(config)
    return [item for task in snapshot.tasks for item in (getattr(task, "interrupts", ()) or ())]


@pytest.mark.parametrize("name", sorted(_INVALID_RESUMES))
def test_invalid_resume_value_resuspends_and_a_valid_value_still_resumes(run: _Run, name: str) -> None:
    resuspended = run.resume(_INVALID_RESUMES[name])  # 此前在这里抛 TypeError，并把非法值钉进 checkpoint

    assert resuspended["status"] == "waiting"
    request = resuspended["__interrupt__"][0].value
    assert request["error"]
    assert request["final_package_hash"] == run.shown_hash
    assert resuspended.get("approval_record") is None
    assert "approval_resumed" not in _names(resuspended)

    result = run.resume({"approved": True, "reason": "checked"})
    assert result["status"] == "succeeded"
    assert ApprovalRecord.model_validate(result["approval_record"]).reason == "checked"
    assert run.exports == 1


def test_invalid_resume_stays_suspended_across_reopen(run: _Run) -> None:
    run.resume({"approved": "false"})
    run.conn.close()

    reopened = sqlite3.connect(run.db, check_same_thread=False)
    try:
        app = build_orchestrator_graph(checkpointer=SqliteSaver(reopened, serde=default_checkpoint_serde()))
        pending = _pending(app, run.config)  # 判挂起看 interrupts；此时 next 可能为 ()
        assert len(pending) == 1
        assert pending[0].value["error"]
        result = dict(app.invoke(Command(resume={"approved": False, "reason": "管径需复核"}), config=run.config))
        assert result["status"] == "blocked"
        assert result["errors"] == [MULTI_APPROVAL_REQUIRED]
        assert ApprovalRecord.model_validate(result["approval_record"]).reason == "管径需复核"
    finally:
        reopened.close()
        run.conn = sqlite3.connect(run.db, check_same_thread=False)


# ---------------------------------------------------------------------------
# CLI：--resume 判挂起看 interrupts；"拒绝原因"处 Ctrl-C/EOF 挂起而不是崩溃或提交
# ---------------------------------------------------------------------------

_CLI_QUERY = "给水系统，Q=12 L/s，DN200，L=25m，材质=steel，起点=n10，终点=n11，输出=图纸，求流速"
_CLI_SESSION = "macli0000000000000000000000000001"


def _cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, answers: list[Any], *args: str) -> int:
    from water_agent import cli

    feed = iter(answers)

    def fake_input(_prompt: str = "") -> str:
        answer = next(feed)
        if isinstance(answer, BaseException):
            raise answer
        return str(answer)

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(cli, "new_session_id", lambda: _CLI_SESSION)
    try:
        return cli.main(["--flow", "multi-agent", "--json", "--session-dir", str(tmp_path / "sessions"), *args])
    except KeyboardInterrupt:  # 修复前 Ctrl-C 直接冲出 CLI；别让它中断整个 pytest 会话
        pytest.fail("KeyboardInterrupt escaped the multi-agent CLI")


def _cli_app(tmp_path: Path) -> tuple[Any, sqlite3.Connection]:
    conn = sqlite3.connect(tmp_path / "sessions" / "sessions.sqlite", check_same_thread=False)
    return build_orchestrator_graph(checkpointer=SqliteSaver(conn, serde=default_checkpoint_serde())), conn


@pytest.mark.parametrize("interruption", [EOFError(), KeyboardInterrupt()], ids=["eof", "ctrl_c"])
def test_cli_interruption_at_rejection_reason_suspends_without_submitting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, interruption: BaseException
) -> None:
    code = _cli(monkeypatch, tmp_path, ["n", interruption], _CLI_QUERY)

    captured = capsys.readouterr()
    assert code == 0, f"stdout:\n{captured.out}\nstderr:\n{captured.err}"
    assert f"--resume {_CLI_SESSION}" in captured.err
    app, conn = _cli_app(tmp_path)
    try:
        config = {"configurable": {"thread_id": _CLI_SESSION}}
        assert len(_pending(app, config)) == 1  # 仍挂起，没有提交任何决定
        values = app.get_state(config).values
        assert values.get("approval_record") is None
        assert values["status"] == "waiting"
    finally:
        conn.close()


def test_cli_resume_finds_a_session_resuspended_by_an_invalid_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    assert _cli(monkeypatch, tmp_path, [EOFError()], _CLI_QUERY) == 0  # 第一个提示处挂起
    app, conn = _cli_app(tmp_path)
    try:
        config = {"configurable": {"thread_id": _CLI_SESSION}}
        resuspended = dict(app.invoke(Command(resume={"approved": "yes"}), config=config))
        assert resuspended["__interrupt__"][0].value["error"]
    finally:
        conn.close()
    capsys.readouterr()

    code = _cli(monkeypatch, tmp_path, ["y"], "--resume", _CLI_SESSION)

    captured = capsys.readouterr()
    assert code == 0, f"stdout:\n{captured.out}\nstderr:\n{captured.err}"
    assert "上一次提交的审批决定无效" in captured.err
    payload = json.loads(captured.out)
    assert payload["status"] == "succeeded"
    assert payload["session_id"] == _CLI_SESSION


def test_cli_resume_of_a_session_without_pending_approval_reports_instead_of_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    assert _cli(monkeypatch, tmp_path, ["y"], _CLI_QUERY) == 0  # 跑完、已导出
    capsys.readouterr()

    code = _cli(monkeypatch, tmp_path, [], "--resume", _CLI_SESSION)

    captured = capsys.readouterr()
    assert code == 2, f"stdout:\n{captured.out}\nstderr:\n{captured.err}"
    assert "没有待批准" in captured.err


def test_final_project_package_refuses_a_rejection_record(run: _Run) -> None:
    artifacts = _release_artifacts(_registry(run.paused), dict(run.paused["active_artifact_ids"]))
    assert artifacts is not None
    ids = sorted(artifacts)
    rejection = ApprovalRecord(
        approved=False,
        reason="管径需复核",
        artifact_ids=ids,
        artifact_hashes=[artifacts[artifact_id].payload_hash for artifact_id in ids],
        artifact_revisions=[artifacts[artifact_id].revision for artifact_id in ids],
        artifact_parent_artifact_ids=[list(artifacts[artifact_id].parent_artifact_ids) for artifact_id in ids],
        final_package_hash=run.shown_hash,
        approved_at="2026-10-01T00:00:00Z",
    )

    with pytest.raises(ValidationError, match=MULTI_APPROVAL_REQUIRED):
        _final_package(artifacts, rejection)
