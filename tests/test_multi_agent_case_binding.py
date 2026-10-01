"""管段 ↔ 计算工况 / 系统的显式绑定（review 2026-09-30 缺陷 1 回归）。

此前 Design Agent 取全项目第一个 DN 套给所有管段、每段管引用全部工况，系统 × 拓扑做
笛卡尔积；两工况两 DN 时编排器自动重算，把用户的第二个工况改写成第一个工况的 DN，全程
无人参与并以 succeeded 收口。重复管段则直接从 ``app.invoke`` 抛裸 ValidationError。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from water_agent.multi_agent.agents.design import run_design_agent
from water_agent.multi_agent.agents.hydraulic import run_hydraulic_agent
from water_agent.multi_agent.contracts import (
    MULTI_INVALID_BRIEF,
    CalcPackage,
    DesignPackage,
    DesignPipeSegment,
    DrawingPlan,
    ProjectBrief,
)
from water_agent.multi_agent.orchestrator import build_orchestrator_graph, default_multi_agent_state
from water_agent.session import default_checkpoint_serde

_SOURCE_IDS = ["GB50015-2019 3.2.1"]


def _brief(**overrides: Any) -> dict[str, Any]:
    brief: dict[str, Any] = {
        "project_id": "proj-case-binding",
        "request": "给水两段管，两个流速工况",
        "systems": ["water_supply"],
        "unit_system": "SI",
        "hydraulic_cases": [
            {"case_id": "c1", "formula": "velocity", "inputs": {"flow_lps": 2, "diameter_mm": 100}},
            {"case_id": "c2", "formula": "velocity", "inputs": {"flow_lps": 10, "diameter_mm": 150}},
        ],
        "topology": [
            {"start_node_id": "n1", "end_node_id": "n2", "length_m": 10.0},
            {"start_node_id": "n2", "end_node_id": "n3", "length_m": 8.0},
        ],
        "spatial_constraints": [{"kind": "material", "value": "PE"}],
        "required_outputs": ["drawing"],
        "user_constraints": [],
    }
    brief.update(overrides)
    return brief


def _two_system_brief(**overrides: Any) -> dict[str, Any]:
    brief = _brief(
        systems=["water_supply", "drainage"],
        hydraulic_cases=[
            {"case_id": "c-ws", "formula": "velocity", "inputs": {"flow_lps": 10, "diameter_mm": 150}},
            {
                "case_id": "c-dr",
                "formula": "velocity",
                "inputs": {"flow_lps": 5, "diameter_mm": 200, "slope": 0.005},
            },
        ],
        topology=[
            {
                "start_node_id": "n1",
                "end_node_id": "n2",
                "length_m": 10.0,
                "system": "water_supply",
                "calc_case_id": "c-ws",
            },
            {
                "start_node_id": "n2",
                "end_node_id": "n3",
                "length_m": 20.0,
                "system": "drainage",
                "calc_case_id": "c-dr",
            },
        ],
    )
    brief.update(overrides)
    return brief


def _run(tmp_path: Path, brief: dict[str, Any], *, approve: bool | None = None) -> dict[str, Any]:
    conn = sqlite3.connect(tmp_path / "state.sqlite", check_same_thread=False)
    try:
        app = build_orchestrator_graph(checkpointer=SqliteSaver(conn, serde=default_checkpoint_serde()))
        config = {"configurable": {"thread_id": "case-binding"}}
        result = dict(
            app.invoke(
                default_multi_agent_state(
                    project_brief=brief,
                    retrieved_source_ids=list(_SOURCE_IDS),
                    staging_dir=str(tmp_path / "staging"),
                    final_dir=str(tmp_path / "final"),
                    output_file_id="case-binding",
                ),
                config=config,
            )
        )
        if result.get("__interrupt__") and approve is not None:
            result = dict(
                app.invoke(Command(resume={"approved": approve, "reason": "checked"}), config=config)
            )
        return result
    finally:
        conn.close()


def _active_design(result: dict[str, Any]) -> DesignPackage:
    design_id = result["active_artifact_ids"]["design"]
    return DesignPackage.model_validate(result["artifacts"][design_id]["payload"])


def _by_edge(segments: list[DesignPipeSegment]) -> dict[tuple[str, str, str], DesignPipeSegment]:
    return {(seg.start_node_id, seg.end_node_id, seg.system): seg for seg in segments}


def _brief_unchanged(result: dict[str, Any], brief: dict[str, Any]) -> bool:
    """The persisted brief (user input) still equals what the user supplied."""

    def normalized(raw: dict[str, Any]) -> dict[str, Any]:
        return ProjectBrief.model_validate(raw).model_dump(mode="json")

    return normalized(result["project_brief"]) == normalized(brief)


def _calc_artifacts(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [env for env in result["artifacts"].values() if env["artifact_type"] == "calc_package"]


def _design_env(brief: dict[str, Any]) -> Any:
    model = ProjectBrief.model_validate(brief)
    return run_design_agent(model, run_hydraulic_agent(model), retrieved_source_ids=list(_SOURCE_IDS))


# ---------------------------------------------------------------------------
# 多工况：管段只引用自己绑定的那一个工况，DN 不跨工况
# ---------------------------------------------------------------------------


def test_unbound_edges_with_two_cases_neither_bind_every_case_nor_rewrite_inputs(tmp_path: Path) -> None:
    brief = _brief()
    result = _run(tmp_path, brief, approve=True)

    # 不得自动重算改写用户输入，也不得以 verified 收口 succeeded
    assert result["status"] == "missing_inputs"
    assert result["revision_counts"]["hydraulic"] == 0
    assert _brief_unchanged(result, brief)
    calcs = _calc_artifacts(result)
    assert len(calcs) == 1
    assert CalcPackage.model_validate(calcs[0]["payload"]).supersedes_artifact_id is None
    assert result.get("exported_file_path") is None
    assert "__interrupt__" not in result

    segments = _active_design(result).segments
    assert len(segments) == 2
    for segment in segments:
        assert segment.calc_case_ids == []
        assert segment.hydraulic_status == "not_verified"
        assert segment.dn_mm is None  # 不把某一个工况的 DN 套给它
    assert any("calc_case_id" in item for item in result["missing_inputs"])


def test_explicitly_bound_edges_keep_their_own_case_dn_and_succeed(tmp_path: Path) -> None:
    brief = _brief(
        topology=[
            {"start_node_id": "n1", "end_node_id": "n2", "length_m": 10.0, "calc_case_id": "c1"},
            {"start_node_id": "n2", "end_node_id": "n3", "length_m": 8.0, "calc_case_id": "c2"},
        ]
    )
    result = _run(tmp_path, brief, approve=True)

    assert result["status"] == "succeeded"
    assert result["revision_counts"]["hydraulic"] == 0
    assert _brief_unchanged(result, brief)
    segments = _by_edge(_active_design(result).segments)
    first = segments[("n1", "n2", "water_supply")]
    second = segments[("n2", "n3", "water_supply")]
    assert (first.dn_mm, first.calc_case_ids, first.hydraulic_status) == (100, ["c1"], "verified")
    assert (second.dn_mm, second.calc_case_ids, second.hydraulic_status) == (150, ["c2"], "verified")

    plan = DrawingPlan.model_validate(
        result["artifacts"][result["active_artifact_ids"]["drawing_plan"]]["payload"]
    )
    drawn = {entity.segment_id: entity.dn_mm for entity in plan.entities if entity.segment_id}
    assert drawn == {first.segment_id: 100, second.segment_id: 150}


def test_project_level_dn_constraint_is_not_imposed_across_cases(tmp_path: Path) -> None:
    brief = _brief(
        topology=[
            {"start_node_id": "n1", "end_node_id": "n2", "length_m": 10.0, "calc_case_id": "c1"},
            {"start_node_id": "n2", "end_node_id": "n3", "length_m": 8.0, "calc_case_id": "c2"},
        ],
        spatial_constraints=[{"kind": "material", "value": "PE"}, {"kind": "diameter_mm", "value": 200}],
    )
    env = _design_env(brief)
    package = DesignPackage.model_validate(env.payload)

    # 项目级 DN200 归属不到唯一一个工况：不重算、不改写任何工况，转人工
    assert package.recalculation_requests == []
    assert env.status == "missing_inputs"
    segments = _by_edge(package.segments)
    assert segments[("n1", "n2", "water_supply")].dn_mm == 100
    assert segments[("n2", "n3", "water_supply")].dn_mm == 150
    assert all(seg.hydraulic_status == "not_verified" for seg in package.segments)
    assert any("DN200" in item for item in package.unresolved_items)

    result = _run(tmp_path, brief, approve=True)
    assert result["status"] == "missing_inputs"
    assert result["revision_counts"]["hydraulic"] == 0
    assert _brief_unchanged(result, brief)


def test_explicit_dn_restating_each_bound_case_is_consistent() -> None:
    brief = _brief(
        topology=[
            {"start_node_id": "n1", "end_node_id": "n2", "length_m": 10.0, "calc_case_id": "c1"},
            {"start_node_id": "n2", "end_node_id": "n3", "length_m": 8.0, "calc_case_id": "c2"},
        ],
        user_constraints=["DN100", "DN150"],
    )
    env = _design_env(brief)
    package = DesignPackage.model_validate(env.payload)
    assert env.status == "ready"
    assert package.recalculation_requests == []
    assert all(seg.hydraulic_status == "verified" for seg in package.segments)


def test_single_case_explicit_user_dn_differing_from_case_requests_recalculation() -> None:
    # 显式设计约束（user_constraints 里的 DN）与绑定工况不同 → 按新 DN 建新工况重算（§7.3）。
    # 此前 user_constraints 的 DN 排在工况 DN 之后，等于被静默忽略。
    brief = _brief(
        hydraulic_cases=[{"case_id": "c1", "formula": "velocity", "inputs": {"flow_lps": 10, "diameter_mm": 150}}],
        topology=[{"start_node_id": "n1", "end_node_id": "n2", "length_m": 10.0}],
        user_constraints=["DN200"],
    )
    env = _design_env(brief)
    package = DesignPackage.model_validate(env.payload)
    segment = package.segments[0]
    assert segment.dn_mm == 200
    assert segment.hydraulic_status == "recalc_required"
    assert package.recalculation_requests == [
        {
            "reason": "hydraulic_input_changed",
            "fields": ["diameter_mm"],
            "segment_id": segment.segment_id,
            "formula": "velocity",
            "source_calc_case_id": "c1",
            "input_updates": {"diameter_mm": 200.0},
        }
    ]


def test_conflicting_explicit_dn_constraints_go_to_human_without_recalculation() -> None:
    brief = _brief(
        hydraulic_cases=[{"case_id": "c1", "formula": "velocity", "inputs": {"flow_lps": 10, "diameter_mm": 150}}],
        topology=[{"start_node_id": "n1", "end_node_id": "n2", "length_m": 10.0}],
        user_constraints=["DN150", "DN200"],
    )
    env = _design_env(brief)
    package = DesignPackage.model_validate(env.payload)
    assert package.recalculation_requests == []
    assert package.segments[0].hydraulic_status == "not_verified"
    assert env.status == "missing_inputs"
    assert any("DN150" in item and "DN200" in item for item in package.unresolved_items)


def test_structured_spatial_dn_outranks_restated_user_dn() -> None:
    # 既有优先级保持不变：结构化 spatial_constraints 的管径是设计修订值，自由文本里复述的
    # "DN150" 不与之冲突（test_structured_diameter_revision_* 依赖这一点）。
    brief = _brief(
        hydraulic_cases=[{"case_id": "c1", "formula": "velocity", "inputs": {"flow_lps": 10, "diameter_mm": 150}}],
        topology=[{"start_node_id": "n1", "end_node_id": "n2", "length_m": 10.0}],
        spatial_constraints=[{"kind": "material", "value": "PE"}, {"kind": "diameter_mm", "value": 200}],
        user_constraints=["DN150"],
    )
    package = DesignPackage.model_validate(_design_env(brief).payload)
    assert package.segments[0].dn_mm == 200
    assert [request["input_updates"] for request in package.recalculation_requests] == [{"diameter_mm": 200.0}]


# ---------------------------------------------------------------------------
# 多系统：不做系统 × 拓扑笛卡尔积
# ---------------------------------------------------------------------------


def test_edge_without_system_in_two_system_brief_yields_no_verified_pipe(tmp_path: Path) -> None:
    brief = _brief(
        systems=["water_supply", "drainage"],
        hydraulic_cases=[
            {"case_id": "c1", "formula": "velocity", "inputs": {"flow_lps": 5, "diameter_mm": 200, "slope": 0.003}}
        ],
        topology=[{"start_node_id": "n1", "end_node_id": "n2", "length_m": 10.0}],
    )
    result = _run(tmp_path, brief, approve=True)

    assert result["status"] == "missing_inputs"
    segments = _active_design(result).segments
    assert {seg.system for seg in segments} == {"water_supply", "drainage"}
    assert all(seg.hydraulic_status != "verified" for seg in segments)
    assert any("system" in item for item in result["missing_inputs"])
    assert "__interrupt__" not in result


def test_declared_system_and_case_bind_one_pipe_per_edge() -> None:
    env = _design_env(_two_system_brief())
    package = DesignPackage.model_validate(env.payload)

    assert env.status == "ready"
    assert package.recalculation_requests == []
    segments = _by_edge(package.segments)
    assert set(segments) == {("n1", "n2", "water_supply"), ("n2", "n3", "drainage")}
    supply = segments[("n1", "n2", "water_supply")]
    drain = segments[("n2", "n3", "drainage")]
    assert (supply.dn_mm, supply.calc_case_ids, supply.slope, supply.hydraulic_status) == (
        150,
        ["c-ws"],
        None,  # 坡度同样只取绑定工况的输入，不把排水工况的坡度套给给水管
        "verified",
    )
    assert (drain.dn_mm, drain.calc_case_ids, drain.slope, drain.hydraulic_status) == (
        200,
        ["c-dr"],
        0.005,
        "verified",
    )


def test_parallel_edges_in_different_declared_systems_are_distinct_pipes() -> None:
    brief = _two_system_brief()
    brief["topology"][1].update({"start_node_id": "n1", "end_node_id": "n2"})
    env = _design_env(brief)
    package = DesignPackage.model_validate(env.payload)

    assert env.status == "ready"
    assert len({seg.segment_id for seg in package.segments}) == 2
    assert {seg.system for seg in package.segments} == {"water_supply", "drainage"}


def test_length_recalculation_updates_only_the_edge_of_the_segment_system() -> None:
    from water_agent.multi_agent.contracts import ArtifactEnvelope, compute_payload_hash
    from water_agent.multi_agent.orchestrator import _brief_with_recalculation_updates

    brief = _two_system_brief()
    brief["topology"][1].update({"start_node_id": "n1", "end_node_id": "n2"})
    model = ProjectBrief.model_validate(brief)
    calc = run_hydraulic_agent(model)
    design = run_design_agent(model, calc, retrieved_source_ids=list(_SOURCE_IDS))
    payload = design.model_dump(mode="python")["payload"]
    supply = next(seg for seg in payload["segments"] if seg["system"] == "water_supply")
    payload["recalculation_requests"] = [
        {
            "reason": "hydraulic_input_changed",
            "fields": ["length_m"],
            "segment_id": supply["segment_id"],
            "formula": "velocity",
            "source_calc_case_id": "c-ws",
            "input_updates": {"length_m": 12.5},
        }
    ]
    design = design.model_copy(update={"payload": payload, "payload_hash": compute_payload_hash(payload)})
    registry: dict[str, ArtifactEnvelope] = {calc.artifact_id: calc, design.artifact_id: design}
    state: dict[str, Any] = {"project_brief": model.model_dump(mode="json")}

    updated, missing = _brief_with_recalculation_updates(
        state,  # type: ignore[arg-type]
        registry,
        {"calc": calc.artifact_id, "design": design.artifact_id},
    )

    assert missing == []
    assert updated is not None
    lengths = {edge.system: edge.length_m for edge in updated.topology}
    assert lengths == {"water_supply": 12.5, "drainage": 20.0}


# ---------------------------------------------------------------------------
# 重复管段 / 字段冲突：intake 结构化拒绝，不抛裸 ValidationError
# ---------------------------------------------------------------------------


def _single_case_brief(**overrides: Any) -> dict[str, Any]:
    brief = _brief(
        hydraulic_cases=[{"case_id": "c1", "formula": "velocity", "inputs": {"flow_lps": 10, "diameter_mm": 150}}],
        topology=[{"start_node_id": "n1", "end_node_id": "n2", "length_m": 10.0}],
    )
    brief.update(overrides)
    return brief


_EDGE = {"start_node_id": "n1", "end_node_id": "n2", "length_m": 10.0}

_INVALID_BRIEFS: dict[str, dict[str, Any]] = {
    "parallel_edges_same_system": _single_case_brief(topology=[_EDGE, {**_EDGE, "length_m": 12.0}]),
    "duplicate_systems_entry": _single_case_brief(systems=["water_supply", "water_supply"]),
    "same_declared_system_twice": _two_system_brief(
        topology=[
            {**_EDGE, "system": "drainage", "calc_case_id": "c-dr"},
            {**_EDGE, "system": "drainage", "calc_case_id": "c-dr", "length_m": 12.0},
        ]
    ),
    "undeclared_edge_overlaps_declared_edge": _two_system_brief(
        topology=[{**_EDGE, "system": "drainage", "calc_case_id": "c-dr"}, {**_EDGE, "calc_case_id": "c-ws"}]
    ),
    "edge_case_not_in_brief": _single_case_brief(topology=[{**_EDGE, "calc_case_id": "c9"}]),
    "edge_system_not_in_brief": _single_case_brief(topology=[{**_EDGE, "system": "drainage"}]),
    "duplicate_case_id": _single_case_brief(
        hydraulic_cases=[
            {"case_id": "c1", "formula": "velocity", "inputs": {"flow_lps": 10, "diameter_mm": 150}},
            {"case_id": "c1", "formula": "velocity", "inputs": {"flow_lps": 5, "diameter_mm": 100}},
        ]
    ),
}


@pytest.mark.parametrize("name", sorted(_INVALID_BRIEFS))
def test_conflicting_brief_is_rejected_at_intake_with_invalid_brief_code(tmp_path: Path, name: str) -> None:
    result = _run(tmp_path, _INVALID_BRIEFS[name], approve=True)

    assert result["status"] == "missing_inputs"
    assert MULTI_INVALID_BRIEF in result["errors"]
    assert result["artifacts"] == {}  # 没有任何 Agent 被调用
    events = [event["event"] for event in result["trace_events"]]
    assert events[0] == "run_started"
    assert events[-1] == "run_finished"
    assert "agent_call" not in events
    assert result["trace_events"][-1]["run_status"] == "missing_inputs"


def test_design_agent_called_directly_with_duplicate_edges_returns_blocked_package() -> None:
    brief = ProjectBrief.model_validate(_INVALID_BRIEFS["parallel_edges_same_system"])
    calc_env = run_hydraulic_agent(brief)

    env = run_design_agent(brief, calc_env, retrieved_source_ids=list(_SOURCE_IDS))

    package = DesignPackage.model_validate(env.payload)
    assert env.status == "blocked"
    assert package.segments == []
    assert any("topology[0]" in item and "topology[1]" in item for item in package.unresolved_items)
