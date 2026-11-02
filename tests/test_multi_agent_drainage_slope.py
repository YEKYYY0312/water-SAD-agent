"""排水主路径：坡度取绑定工况的程序化结果（review 2026-09-30 缺陷"排水主路径走不通"的回归）。

此前 Design Agent 只从工况的 ``inputs`` 里读 ``slope``。排水工况用 ``slope_manning`` 由曼宁公式
算出 ``outputs.slope``（例如 0.00093）时，设计仍报 ``missing slope for drainage``，整个运行以
``missing_inputs`` 收口——排水管段设计在多 Agent 链路里根本走不到出图与审批。

口径：坡度只来自该管段**绑定的那一个工况**（TopologyEdge.calc_case_id / 单工况自动绑定，见 Step 33
决策 9），优先取程序算出的 ``outputs.slope``，其次取工况输入里的 ``slope``；都没有才是缺参。
数值原样搬运，不重算（铁律 1）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from water_agent.harness.multi_agent_eval import _active_registry, _numeric_integrity
from water_agent.multi_agent.agents.design import run_design_agent
from water_agent.multi_agent.agents.hydraulic import run_hydraulic_agent
from water_agent.multi_agent.contracts import (
    ArtifactEnvelope,
    CalcPackage,
    DesignPackage,
    ProjectBrief,
    compute_payload_hash,
)
from water_agent.multi_agent.orchestrator import build_orchestrator_graph, default_multi_agent_state
from water_agent.session import default_checkpoint_serde

_MANNING = {"fill_ratio": 0.5, "manning_n": 0.013}


def _brief(*, dn: int = 300, flow: float = 30, extra_inputs: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    brief = {
        "project_id": "proj-drain-slope",
        "request": "室外排水管段设计，坡度由曼宁公式计算",
        "systems": ["drainage"],
        "unit_system": "SI",
        "hydraulic_cases": [
            {
                "case_id": "case-drain-mn",
                "formula": "slope_manning",
                "inputs": {"flow_lps": flow, "diameter_mm": dn, **_MANNING, **(extra_inputs or {})},
            }
        ],
        "topology": [{"start_node_id": "n1", "end_node_id": "n2", "length_m": 30.0}],
        "spatial_constraints": [{"kind": "material", "value": "钢筋混凝土"}],
        "required_outputs": ["calculation_book", "drawing"],
        "user_constraints": [],
    }
    brief.update(overrides)
    return brief


def _design(brief_data: dict[str, Any]) -> tuple[CalcPackage, ArtifactEnvelope]:
    brief = ProjectBrief.model_validate(brief_data)
    calc_env = run_hydraulic_agent(brief)
    assert calc_env.status == "ready", calc_env.payload
    return CalcPackage.model_validate(calc_env.payload), run_design_agent(
        brief, calc_env, retrieved_source_ids=["GB50014-2021 5.1.1"]
    )


def test_drainage_slope_comes_from_the_bound_case_manning_output() -> None:
    calc, env = _design(_brief())
    computed = calc.cases[0].outputs["slope"]
    design = DesignPackage.model_validate(env.payload)
    assert design.segments[0].slope == computed
    assert not any("missing slope" in item for item in design.unresolved_items), design.unresolved_items
    assert design.segments[0].hydraulic_status == "verified"
    assert env.status == "ready"


def test_programmatic_slope_wins_over_a_slope_left_in_the_inputs() -> None:
    """slope_manning 不读输入里的 slope；设计必须用程序算出的那个，而不是被忽略的输入值。"""
    calc, env = _design(_brief(extra_inputs={"slope": 0.01}))
    computed = calc.cases[0].outputs["slope"]
    assert computed != 0.01
    assert DesignPackage.model_validate(env.payload).segments[0].slope == computed


def test_input_slope_is_still_used_when_the_formula_does_not_compute_one() -> None:
    """守护（修复前后都成立）：velocity 工况不产出坡度，仍取工况输入里的 slope。"""
    brief = _brief(
        hydraulic_cases=[
            {"case_id": "case-drain-v", "formula": "velocity", "inputs": {"flow_lps": 5, "diameter_mm": 300, "slope": 0.003}}
        ]
    )
    _, env = _design(brief)
    assert DesignPackage.model_validate(env.payload).segments[0].slope == 0.003


def test_each_segment_takes_the_slope_of_its_own_bound_case() -> None:
    brief = _brief(
        hydraulic_cases=[
            {"case_id": "case-a", "formula": "slope_manning", "inputs": {"flow_lps": 20, "diameter_mm": 300, **_MANNING}},
            {"case_id": "case-b", "formula": "slope_manning", "inputs": {"flow_lps": 60, "diameter_mm": 400, **_MANNING}},
        ],
        topology=[
            {"start_node_id": "n1", "end_node_id": "n2", "length_m": 20.0, "calc_case_id": "case-a"},
            {"start_node_id": "n2", "end_node_id": "n3", "length_m": 25.0, "calc_case_id": "case-b"},
        ],
    )
    calc, env = _design(brief)
    by_case = {case.calc_case_id: case.outputs["slope"] for case in calc.cases}
    design = DesignPackage.model_validate(env.payload)
    assert {seg.calc_case_ids[0]: seg.slope for seg in design.segments} == by_case
    assert by_case["case-a"] != by_case["case-b"]
    assert env.status == "ready"


def _run(tmp_path: Path, name: str, brief: dict[str, Any], *, approve: bool | None) -> tuple[dict[str, Any], dict[str, Any]]:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        app = build_orchestrator_graph(checkpointer=SqliteSaver(connection, serde=default_checkpoint_serde()))
        config = {"configurable": {"thread_id": name}}
        paused = dict(
            app.invoke(
                default_multi_agent_state(
                    project_brief=brief,
                    retrieved_source_ids=["GB50014-2021 5.1.1"],
                    staging_dir=str(tmp_path / name / "staging"),
                    final_dir=str(tmp_path / name / "final"),
                    output_file_id=name,
                ),
                config=config,
            )
        )
        result = paused
        if paused.get("__interrupt__") and approve is not None:
            result = dict(app.invoke(Command(resume={"approved": approve, "reason": "checked"}), config=config))
        return paused, result
    finally:
        connection.close()


def _active(state: dict[str, Any], role: str) -> dict[str, Any]:
    return state["artifacts"][state["active_artifact_ids"][role]]["payload"]


def test_reviewer_repro_no_longer_stops_at_missing_slope(tmp_path: Path) -> None:
    """review 原样复现：DN200、Q=5 L/s、h/d=0.5、n=0.013 → 程序算出 slope≈0.00093。

    修复后设计不再缺坡度、链路走到图纸与审查；DN200 小于排水工程惯例最小 DN300，P3 发现由人处理
    （停在 waiting，见 Step 32 决策 7），而不是在设计阶段以 missing_inputs 收口。
    """
    brief = _brief(dn=200, flow=5)
    paused, _ = _run(tmp_path, "drain-repro", brief, approve=None)
    assert not any("missing slope" in item for item in paused.get("missing_inputs", []))
    assert paused["status"] != "missing_inputs", paused.get("missing_inputs")
    calc = CalcPackage.model_validate(_active(paused, "calc"))
    assert calc.cases[0].outputs["slope"] == pytest.approx(0.00093, rel=0.01)
    design = DesignPackage.model_validate(_active(paused, "design"))
    assert design.segments[0].slope == calc.cases[0].outputs["slope"]
    assert "drawing" in paused["active_artifact_ids"], "排水管段应走到出图与回读"


def test_drainage_with_manning_slope_reaches_approval_and_exports(tmp_path: Path) -> None:
    paused, result = _run(tmp_path, "drain-ok", _brief(), approve=True)
    assert paused.get("__interrupt__"), paused.get("missing_inputs")
    summary = paused["__interrupt__"][0].value["summary"]
    computed = CalcPackage.model_validate(_active(paused, "calc")).cases[0].outputs["slope"]
    assert summary["segments"][0]["slope"] == computed
    assert result["status"] == "succeeded"
    assert Path(result["exported_file_path"]).is_file()

    # 评测口径：设计坡度必须能追溯到绑定工况的程序化结果（numeric_source_integrity）
    registry = _active_registry(result)
    assert _numeric_integrity(result, registry) == (True, True)
    tampered = dict(result)
    design_id = result["active_artifact_ids"]["design"]
    envelope = ArtifactEnvelope.model_validate(result["artifacts"][design_id])
    payload = envelope.model_dump(mode="python")["payload"]
    payload["segments"][0]["slope"] = computed * 2
    tampered["artifacts"] = {
        **result["artifacts"],
        design_id: envelope.model_copy(
            update={"payload": payload, "payload_hash": compute_payload_hash(payload)}
        ).model_dump(mode="json"),
    }
    assert _numeric_integrity(tampered, _active_registry(tampered)) == (True, False)
