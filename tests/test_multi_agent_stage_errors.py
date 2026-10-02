"""阶段失败带上 MULTI_* 错误码与原因摘要（review 2026-09-30 缺陷"阶段失败不带错误码和原因"的回归）。

此前编排器各阶段节点失败时只设 status、不追加 errors：``MULTI_CALC_BLOCKED / DESIGN_CONFLICT /
DRAWING_CONFLICT / DRAWING_UNAVAILABLE / RECALC_REQUIRED / INVALID_BRIEF`` 在 contracts 之外 0 处使用。
复现：CLI 输入 "Q=0 L/s…" 输出 ``status=blocked, errors=[], missing_inputs=[]``，真实原因只在
checkpoint 里；未知公式、未检索的引用、staging 不可写三种情况 errors 全空；``hydraulic_cases=[]``
返回 missing_inputs 但缺项列表为空。

口径（任务书 §10）：阶段收口时追加对应的 MULTI_* 码（单独一项，可精确匹配），后面逐条跟原因摘要
（带阶段前缀、有界，不含绝对路径）；status=missing_inputs 时缺项列表不得为空。成功的运行 errors 仍为空。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from water_agent.multi_agent.agents.drawing import run_drawing_agent
from water_agent.multi_agent.contracts import (
    MULTI_CALC_BLOCKED,
    MULTI_DESIGN_CONFLICT,
    MULTI_DRAWING_CONFLICT,
    MULTI_DRAWING_UNAVAILABLE,
    MULTI_ERROR_CODES,
    MULTI_INVALID_BRIEF,
    MULTI_MISSING_INPUTS,
    MULTI_RECALC_REQUIRED,
    MULTI_REVIEW_REJECTED,
    ArtifactEnvelope,
    DrawingPlan,
    compute_payload_hash,
)
from water_agent.multi_agent.orchestrator import build_orchestrator_graph, default_multi_agent_state
from water_agent.session import default_checkpoint_serde

_FAILED_STATUSES = {"blocked", "failed", "missing_inputs", "waiting"}


def _brief(**overrides: Any) -> dict[str, Any]:
    brief = {
        "project_id": "proj-stage-errors",
        "request": "给水管段设计，DN150 PE 管 10m，流量 10L/s",
        "systems": ["water_supply"],
        "unit_system": "SI",
        "hydraulic_cases": [
            {"case_id": "case-vel-1", "formula": "velocity", "inputs": {"flow_lps": 10, "diameter_mm": 150}}
        ],
        "topology": [{"start_node_id": "n1", "end_node_id": "n2", "length_m": 10.0}],
        "spatial_constraints": [{"kind": "material", "value": "PE"}],
        "required_outputs": ["drawing"],
        "user_constraints": ["material: PE", "DN150"],
    }
    brief.update(overrides)
    return brief


def _run(
    tmp_path: Path,
    name: str,
    brief: dict[str, Any],
    *,
    retrieved: list[str] | None = None,
    staging_dir: Path | None = None,
    **runners: Any,
) -> dict[str, Any]:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        app = build_orchestrator_graph(
            checkpointer=SqliteSaver(connection, serde=default_checkpoint_serde()), **runners
        )
        return dict(
            app.invoke(
                default_multi_agent_state(
                    project_brief=brief,
                    retrieved_source_ids=["GB50015-2019 3.2.1"] if retrieved is None else retrieved,
                    staging_dir=str(staging_dir if staging_dir is not None else tmp_path / name / "staging"),
                    final_dir=str(tmp_path / name / "final"),
                    output_file_id=name,
                ),
                config={"configurable": {"thread_id": name}},
            )
        )
    finally:
        connection.close()


def _reasons_after(errors: list[str], code: str) -> list[str]:
    """码后面紧跟的原因摘要（到下一个码为止）。"""
    index = errors.index(code)
    reasons: list[str] = []
    for item in errors[index + 1:]:
        if item in MULTI_ERROR_CODES:
            break
        reasons.append(item)
    return reasons


def _assert_failure_contract(result: dict[str, Any], tmp_path: Path) -> None:
    assert result["status"] in _FAILED_STATUSES
    assert any(item in MULTI_ERROR_CODES for item in result["errors"]), result["errors"]
    if result["status"] == "missing_inputs":
        assert result["missing_inputs"], "missing_inputs 状态必须明确列出缺项"
    assert not any(str(tmp_path) in item for item in result["errors"]), "原因摘要不得带绝对路径"


def test_unknown_formula_reports_calc_blocked_with_the_tool_reason(tmp_path: Path) -> None:
    brief = _brief(hydraulic_cases=[{"case_id": "c1", "formula": "nonexistent", "inputs": {"flow_lps": 10}}])
    result = _run(tmp_path, "unknown-formula", brief)
    assert result["status"] == "blocked"
    assert MULTI_CALC_BLOCKED in result["errors"]
    reasons = _reasons_after(result["errors"], MULTI_CALC_BLOCKED)
    assert any("c1" in item and "nonexistent" in item for item in reasons), reasons
    _assert_failure_contract(result, tmp_path)


def test_zero_flow_reports_calc_blocked_with_the_invalid_input(tmp_path: Path) -> None:
    brief = _brief(hydraulic_cases=[{"case_id": "c1", "formula": "velocity", "inputs": {"flow_lps": 0, "diameter_mm": 150}}])
    result = _run(tmp_path, "zero-flow", brief)
    assert result["status"] == "blocked"
    assert any("flow_lps" in item for item in _reasons_after(result["errors"], MULTI_CALC_BLOCKED))
    _assert_failure_contract(result, tmp_path)


def test_empty_hydraulic_cases_lists_what_is_missing(tmp_path: Path) -> None:
    result = _run(tmp_path, "no-cases", _brief(hydraulic_cases=[]))
    assert result["status"] == "missing_inputs"
    assert MULTI_MISSING_INPUTS in result["errors"]
    assert "hydraulic_cases" in result["missing_inputs"]
    _assert_failure_contract(result, tmp_path)


def test_unretrieved_citation_request_reports_design_conflict(tmp_path: Path) -> None:
    brief = _brief(user_constraints=["material: PE", "DN150", "source_id: GB99999-2099 1.1"])
    result = _run(tmp_path, "bad-source", brief)
    assert result["status"] == "blocked"
    assert MULTI_DESIGN_CONFLICT in result["errors"]
    assert any("GB99999-2099 1.1" in item for item in _reasons_after(result["errors"], MULTI_DESIGN_CONFLICT))
    _assert_failure_contract(result, tmp_path)


def test_unwritable_staging_reports_drawing_unavailable_without_the_path(tmp_path: Path) -> None:
    blocker = tmp_path / "staging-is-a-file"
    blocker.write_text("x", encoding="utf-8")
    result = _run(tmp_path, "staging-unwritable", _brief(), staging_dir=blocker / "sub")
    assert result["status"] == "failed"
    assert MULTI_DRAWING_UNAVAILABLE in result["errors"]
    reasons = _reasons_after(result["errors"], MULTI_DRAWING_UNAVAILABLE)
    assert any("staging render failed" in item for item in reasons), reasons
    _assert_failure_contract(result, tmp_path)


def test_unsupported_drawing_units_list_the_missing_unit(tmp_path: Path) -> None:
    def drawing_runner(*args: Any, **kwargs: Any) -> ArtifactEnvelope:
        return run_drawing_agent(*args, **{**kwargs, "insunits": 999})

    result = _run(tmp_path, "bad-units", _brief(), drawing_runner=drawing_runner)
    assert result["status"] == "missing_inputs"
    assert MULTI_MISSING_INPUTS in result["errors"]
    assert any("INSUNITS" in item for item in result["missing_inputs"]), result["missing_inputs"]
    _assert_failure_contract(result, tmp_path)


def test_drawing_plan_conflict_reports_drawing_conflict(tmp_path: Path) -> None:
    def drawing_runner(*args: Any, **kwargs: Any) -> ArtifactEnvelope:
        envelope = run_drawing_agent(*args, **kwargs)
        plan = DrawingPlan.model_validate(envelope.payload)
        plan.integration_conflicts.append("DN mismatch: design 150 vs calc 200 for case case-vel-1")
        plan.entities = [entity for entity in plan.entities if entity.segment_id is None]
        payload = plan.model_dump(mode="python")
        return envelope.model_copy(
            update={"payload": payload, "payload_hash": compute_payload_hash(payload), "status": "blocked"}
        )

    result = _run(tmp_path, "plan-conflict", _brief(), drawing_runner=drawing_runner)
    assert result["status"] == "blocked"
    assert MULTI_DRAWING_CONFLICT in result["errors"]
    assert any("DN mismatch" in item for item in _reasons_after(result["errors"], MULTI_DRAWING_CONFLICT))
    _assert_failure_contract(result, tmp_path)


def test_invalid_brief_field_reports_invalid_brief_and_the_field(tmp_path: Path) -> None:
    result = _run(tmp_path, "bad-brief", _brief(project_id="bad id!"))
    assert result["status"] == "missing_inputs"
    assert MULTI_INVALID_BRIEF in result["errors"]
    assert "project_id" in result["missing_inputs"]
    assert any("project_id" in item for item in _reasons_after(result["errors"], MULTI_INVALID_BRIEF))
    _assert_failure_contract(result, tmp_path)


def test_conflicting_brief_lists_the_conflicting_field(tmp_path: Path) -> None:
    brief = _brief(topology=[{"start_node_id": "n1", "end_node_id": "n2", "length_m": 10.0, "calc_case_id": "c9"}])
    result = _run(tmp_path, "brief-conflict", brief)
    assert result["status"] == "missing_inputs"
    assert MULTI_INVALID_BRIEF in result["errors"]
    assert "topology[0].calc_case_id" in result["missing_inputs"]
    _assert_failure_contract(result, tmp_path)


def test_design_missing_inputs_carries_the_code(tmp_path: Path) -> None:
    brief = _brief(
        systems=["drainage"],
        hydraulic_cases=[{"case_id": "c1", "formula": "velocity", "inputs": {"flow_lps": 5, "diameter_mm": 300}}],
    )
    result = _run(tmp_path, "drain-no-slope", brief)
    assert result["status"] == "missing_inputs"
    assert MULTI_MISSING_INPUTS in result["errors"]
    assert any("missing slope" in item for item in result["missing_inputs"])
    _assert_failure_contract(result, tmp_path)


def test_human_review_finding_reports_review_rejected_with_the_rule(tmp_path: Path) -> None:
    brief = _brief(
        systems=["drainage"],
        hydraulic_cases=[{"case_id": "c1", "formula": "velocity", "inputs": {"flow_lps": 5, "diameter_mm": 200, "slope": 0.003}}],
        user_constraints=["DN200"],
    )
    result = _run(tmp_path, "p3-defect", brief, retrieved=[])
    assert result["status"] == "waiting"
    assert MULTI_REVIEW_REJECTED in result["errors"]
    assert any("p3_drain.min_diameter" in item for item in _reasons_after(result["errors"], MULTI_REVIEW_REJECTED))
    _assert_failure_contract(result, tmp_path)


def test_recalculation_that_cannot_run_reports_recalc_required(tmp_path: Path) -> None:
    from water_agent.multi_agent.agents.design import run_design_agent

    def design_runner(*args: Any, **kwargs: Any) -> ArtifactEnvelope:
        return run_design_agent(*args, **kwargs, recalc_trigger_fields={"flow_lps"})

    result = _run(tmp_path, "recalc-missing", _brief(), design_runner=design_runner)
    assert result["status"] == "waiting"
    assert MULTI_RECALC_REQUIRED in result["errors"]
    assert MULTI_MISSING_INPUTS in result["errors"]
    assert any("input_updates.flow_lps" in item for item in result["missing_inputs"])
    _assert_failure_contract(result, tmp_path)


def test_successful_run_still_has_no_errors(tmp_path: Path) -> None:
    paused = _run(tmp_path, "ok", _brief())
    assert paused.get("__interrupt__")
    assert paused["errors"] == []


_CLI_ZERO_FLOW = "给水系统，Q=0 L/s，DN150，L=10m，材质=PE，起点=n1，终点=n2，输出=图纸，求流速"


def test_cli_zero_flow_puts_the_code_and_reason_in_the_top_level_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from water_agent import cli

    monkeypatch.setattr("builtins.input", lambda _prompt="": pytest.fail("不应走到审批"))
    code = cli.main(["--flow", "multi-agent", "--json", "--session-dir", str(tmp_path / "sessions"), _CLI_ZERO_FLOW])
    captured = capsys.readouterr()
    assert code == 1
    payload = json.loads(captured.out)
    assert payload["status"] == "blocked"
    assert MULTI_CALC_BLOCKED in payload["errors"]
    assert any("flow_lps" in item for item in _reasons_after(payload["errors"], MULTI_CALC_BLOCKED)), payload["errors"]
