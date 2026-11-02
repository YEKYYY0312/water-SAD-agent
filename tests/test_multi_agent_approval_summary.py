"""审批前给出可审阅的最终包摘要（review 2026-09-30 缺陷"盲批"的回归）。

此前审批 interrupt 的载荷只有最终包哈希、产物 ID 与披露项，CLI 在"批准发布最终草图？"之前只打印
``待批准最终包：<hash>``——批准人看不到要批准的管段、管径、计算结果、审查结论和图纸在哪，批准等于盲批。

口径：审批请求与 CLI 共用 ``orchestrator.approval_summary``，它只读当前发布集里已通过哈希校验的
登记产物、原样搬运其中的数值与文本，不重新计算任何数值（铁律 1）；``--json`` 时人读摘要走 stderr，
stdout 仍是唯一一份最终 JSON（结构化摘要作为其中一个字段）。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from water_agent.multi_agent.contracts import (
    ArtifactEnvelope,
    CalcPackage,
    DesignPackage,
    ReviewPackage,
)
from water_agent.multi_agent.orchestrator import build_orchestrator_graph, default_multi_agent_state
from water_agent.session import default_checkpoint_serde

_ROLES = ("calc", "design", "drawing_plan", "document", "drawing", "review")
_CLI_QUERY = "给水系统，Q=12 L/s，DN200，L=25m，材质=steel，起点=n10，终点=n11，输出=图纸，求流速"
_PROMPT = "批准发布最终草图？[y/n]"


def _brief() -> dict[str, Any]:
    return {
        "project_id": "proj-approval-summary",
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


@pytest.fixture()
def paused(tmp_path: Path):
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    app = build_orchestrator_graph(checkpointer=SqliteSaver(connection, serde=default_checkpoint_serde()))
    config = {"configurable": {"thread_id": "approval-summary"}}
    staging = tmp_path / "staging"
    state = dict(
        app.invoke(
            default_multi_agent_state(
                project_brief=_brief(),
                retrieved_source_ids=["GB50015-2019 3.2.1"],
                staging_dir=str(staging),
                final_dir=str(tmp_path / "final"),
                output_file_id="approval-summary",
            ),
            config=config,
        )
    )
    assert state.get("__interrupt__"), "fixture must pause at the approval interrupt"
    try:
        yield app, config, state, staging
    finally:
        connection.close()


def _envelope(state: dict[str, Any], role: str) -> ArtifactEnvelope:
    return ArtifactEnvelope.model_validate(state["artifacts"][state["active_artifact_ids"][role]])


def test_approval_request_carries_a_reviewable_summary_of_the_registered_package(paused) -> None:
    _, _, state, staging = paused
    request = state["__interrupt__"][0].value
    summary = request.get("summary")
    assert isinstance(summary, dict), f"审批请求里没有可审阅的摘要：{sorted(request)}"
    assert summary["final_package_hash"] == request["final_package_hash"]

    # 产物清单：每个角色一行，类型 / revision / payload_hash 前缀都取自登记的 envelope
    rows = {row["role"]: row for row in summary["artifacts"]}
    assert set(rows) == set(_ROLES)
    for role in _ROLES:
        env = _envelope(state, role)
        assert rows[role]["artifact_id"] == env.artifact_id
        assert rows[role]["artifact_type"] == env.artifact_type
        assert rows[role]["revision"] == env.revision
        assert rows[role]["status"] == env.status
        assert len(rows[role]["payload_hash_prefix"]) >= 8
        assert env.payload_hash.startswith(rows[role]["payload_hash_prefix"])

    # 管段表：逐段逐字段等于活动 DesignPackage
    design = DesignPackage.model_validate(_envelope(state, "design").payload)
    assert summary["segments"] == [
        {
            "segment_id": seg.segment_id,
            "system": seg.system,
            "start_node_id": seg.start_node_id,
            "end_node_id": seg.end_node_id,
            "dn_mm": seg.dn_mm,
            "length_m": seg.length_m,
            "material": seg.material,
            "slope": seg.slope,
            "hydraulic_status": seg.hydraulic_status,
            "calc_case_ids": list(seg.calc_case_ids),
        }
        for seg in design.segments
    ]

    # 水力工况：关键结果与 assumptions 原样来自活动 CalcPackage
    calc = CalcPackage.model_validate(_envelope(state, "calc").payload)
    cases = {row["calc_case_id"]: row for row in summary["calc_cases"]}
    assert set(cases) == {case.calc_case_id for case in calc.cases}
    for case in calc.cases:
        row = cases[case.calc_case_id]
        assert row["formula"] == case.formula
        assert row["status"] == case.status
        assert row["inputs"] == case.inputs
        assert row["outputs"] == case.outputs
        assert row["assumptions"] == case.assumptions

    # 审查结论：decision / issues / not_checked / disclosed_limitations
    review = ReviewPackage.model_validate(_envelope(state, "review").payload)
    assert summary["review"]["decision"] == review.decision
    assert summary["review"]["not_checked"] == review.not_checked
    assert summary["review"]["disclosed_limitations"] == review.disclosed_limitations
    assert [issue["rule_id"] for issue in summary["review"]["issues"]] == [i.rule_id for i in review.issues]

    # staging 草图在哪：批准人能打开它看
    drawing = _envelope(state, "drawing")
    expected_path = staging / f"{drawing.payload['staged_file_id']}.dxf"
    assert summary["staging_dxf_path"] == str(expected_path)
    assert expected_path.is_file()


def test_summary_copies_registered_numbers_without_recomputing(paused, monkeypatch: pytest.MonkeyPatch) -> None:
    from water_agent.multi_agent.orchestrator import approval_summary

    _, _, state, _ = paused

    def _forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("approval summary must not run any calculation")

    monkeypatch.setattr("water_agent.tools.hydraulics.run_calc", _forbidden)
    monkeypatch.setattr("water_agent.multi_agent.agents.hydraulic.run_calc", _forbidden)

    summary = approval_summary(state)
    assert summary is not None
    calc = CalcPackage.model_validate(_envelope(state, "calc").payload)
    for row, case in zip(summary["calc_cases"], calc.cases, strict=True):
        for key, value in case.outputs.items():
            assert row["outputs"][key] == value and type(row["outputs"][key]) is type(value)
    design = DesignPackage.model_validate(_envelope(state, "design").payload)
    assert [row["dn_mm"] for row in summary["segments"]] == [seg.dn_mm for seg in design.segments]


def test_summary_is_none_when_the_release_set_is_incomplete(paused) -> None:
    from water_agent.multi_agent.orchestrator import approval_summary

    _, _, state, _ = paused
    incomplete = {**state, "active_artifact_ids": {k: v for k, v in state["active_artifact_ids"].items() if k != "review"}}
    assert approval_summary(incomplete) is None


def test_resumed_result_keeps_the_same_summary_source(paused) -> None:
    from water_agent.multi_agent.orchestrator import approval_summary

    app, config, state, _ = paused
    shown = state["__interrupt__"][0].value["summary"]
    result = dict(app.invoke(Command(resume={"approved": True, "reason": "checked"}), config=config))
    assert result["status"] == "succeeded"
    assert approval_summary(result) == shown


def _run_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *extra: str
) -> tuple[int, list[str]]:
    from water_agent import cli

    prompts: list[str] = []

    def fake_input(prompt: str = "") -> str:
        prompts.append(prompt)
        return "y"

    monkeypatch.setattr("builtins.input", fake_input)
    code = cli.main(["--flow", "multi-agent", *extra, "--session-dir", str(tmp_path / "sessions"), _CLI_QUERY])
    return code, prompts


def test_cli_prints_the_summary_before_the_prompt_and_keeps_stdout_pure_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    code, _ = _run_cli(monkeypatch, tmp_path, "--json")
    captured = capsys.readouterr()
    assert code == 0, captured.err

    payload = json.loads(captured.out)  # stdout 只有最终 JSON，人读摘要没有混进来
    summary = payload["approval_summary"]
    assert summary["segments"] and summary["calc_cases"]

    err = captured.err
    assert _PROMPT in err
    before_prompt = err[: err.index(_PROMPT)]
    segment = summary["segments"][0]
    for fragment in (
        summary["final_package_hash"],
        segment["segment_id"],
        "DN200",
        "steel",
        "n10",
        "n11",
        segment["hydraulic_status"],
        "case-cli-1",
        "velocity_mps",
        str(summary["calc_cases"][0]["outputs"]["velocity_mps"]),
        "低于给水常用区间",
        f"decision={summary['review']['decision']}",
        summary["staging_dxf_path"],
    ):
        assert fragment in before_prompt, f"批准前没有展示 {fragment!r}：\n{before_prompt}"
    for row in summary["artifacts"]:
        assert row["artifact_type"] in before_prompt
        assert row["payload_hash_prefix"] in before_prompt


def test_cli_resume_of_a_request_without_summary_rebuilds_it_from_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """升级前挂起的会话：持久化的审批请求里没有 summary，--resume 时从会话状态重建，不能显示空表。"""
    from water_agent import cli

    session = "masummary000000000000000000000001"
    monkeypatch.setattr(cli, "new_session_id", lambda: session)

    def eof(_prompt: str = "") -> str:
        raise EOFError

    with monkeypatch.context() as legacy:
        legacy.setattr("water_agent.multi_agent.orchestrator.approval_summary", lambda _state: None)
        legacy.setattr("builtins.input", eof)
        assert cli.main(["--flow", "multi-agent", "--json", "--session-dir", str(tmp_path / "sessions"), _CLI_QUERY]) == 0
    capsys.readouterr()

    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")
    code = cli.main(["--flow", "multi-agent", "--json", "--session-dir", str(tmp_path / "sessions"), "--resume", session])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    before_prompt = captured.err[: captured.err.index(_PROMPT)]
    assert "管段（1 段）" in before_prompt and "case-cli-1" in before_prompt and "velocity_mps" in before_prompt
    assert json.loads(captured.out)["status"] == "succeeded"


def test_cli_without_json_prints_the_summary_on_stdout_before_the_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    code, _ = _run_cli(monkeypatch, tmp_path)
    captured = capsys.readouterr()
    assert code == 0, captured.err

    out = captured.out
    assert _PROMPT in out
    before_prompt, after_prompt = out.split(_PROMPT, 1)
    assert "case-cli-1" in before_prompt and "velocity_mps" in before_prompt
    # 假 input() 不回显回车，最终 JSON 紧跟在提示符后面；真实终端里它在新的一行
    assert json.loads(after_prompt[after_prompt.index("{"):].strip())["status"] == "succeeded"
