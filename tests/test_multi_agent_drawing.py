"""Step 31 drawing agent — deterministic, offline, schematic only, roundtrip."""

from __future__ import annotations

import math
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from water_agent.multi_agent.agents.drawing import (
    build_drawing_package,
    build_drawing_plan,
    run_drawing_agent,
)
from water_agent.multi_agent.contracts import (
    ArtifactEnvelope,
    CalcCase,
    CalcPackage,
    DesignPackage,
    DesignPipeSegment,
    DrawingPlan,
    PlannedEntity,
    compute_payload_hash,
)
from water_agent.multi_agent.drawing_executor import (
    render_drawing_plan_to_staging,
    stage_drawing_package,
    verify_roundtrip,
    verify_staged_drawing_package,
)
from water_agent.multi_agent.provenance import stale_propagation, validate_provenance


def _payload_hash(payload: dict) -> str:
    return compute_payload_hash(payload)


def _calc_pkg(cases: list[CalcCase] | None = None) -> CalcPackage:
    if cases is None:
        cases = [
            CalcCase(calc_case_id="case-ws-1", formula="velocity", inputs={"flow_lps": 10, "diameter_mm": 150}, outputs={"velocity_mps": 0.5}, tool_invocation_id="hyd-aaa", assumptions=[]),
            CalcCase(calc_case_id="case-dr-1", formula="velocity", inputs={"flow_lps": 5, "diameter_mm": 200}, outputs={"velocity_mps": 0.3}, tool_invocation_id="hyd-bbb", assumptions=[]),
            CalcCase(calc_case_id="case-fp-1", formula="velocity", inputs={"flow_lps": 20, "diameter_mm": 100}, outputs={"velocity_mps": 0.8}, tool_invocation_id="hyd-ccc", assumptions=[]),
        ]
    return CalcPackage(cases=cases, missing_inputs=[], errors=[], supersedes_artifact_id=None)


def _design_pkg(
    calc_pkg: CalcPackage,
    materials: tuple[str, str, str] = ("PE", "钢筋混凝土", "steel"),
    systems: tuple[str, str, str] = ("water_supply", "drainage", "fire_protection"),
    hydraulic_statuses: tuple[str, str, str] = ("verified", "verified", "not_verified"),
) -> DesignPackage:
    calc_ids = [c.calc_case_id for c in calc_pkg.cases]
    segs = [
        DesignPipeSegment(segment_id="seg-ws-001", system=systems[0], start_node_id="n1", end_node_id="n2", length_m=10.0, dn_mm=150, material=materials[0], slope=None, calc_case_ids=[calc_ids[0]], source_ids=["SRC 1"], hydraulic_status=hydraulic_statuses[0]),
        DesignPipeSegment(segment_id="seg-dr-001", system=systems[1], start_node_id="n2", end_node_id="n3", length_m=20.0, dn_mm=200, material=materials[1], slope=0.005, calc_case_ids=[calc_ids[1]], source_ids=["SRC 2"], hydraulic_status=hydraulic_statuses[1]),
        DesignPipeSegment(segment_id="seg-fp-001", system=systems[2], start_node_id="n3", end_node_id="n4", length_m=15.0, dn_mm=100, material=materials[2], slope=None, calc_case_ids=[calc_ids[2]], source_ids=["SRC 3"], hydraulic_status=hydraulic_statuses[2]),
    ]
    return DesignPackage(segments=segs, decisions=[], alternatives=[], recalculation_requests=[], unresolved_items=[])


def _envelope(payload: dict, artifact_id: str, artifact_type: str, producer: str, parent_ids: list[str] | None = None, status: str = "ready") -> ArtifactEnvelope:
    h = _payload_hash(payload)
    return ArtifactEnvelope(
        artifact_id=artifact_id,
        artifact_type=artifact_type,  # type: ignore[arg-type]
        schema_version="p4.5-2026-09-10",
        producer=producer,  # type: ignore[arg-type]
        revision=1,
        parent_artifact_ids=parent_ids or [],
        payload_hash=h,
        payload=payload,
        citation_ids=[],
        assumptions=[],
        warnings=[],
        status=status,  # type: ignore[arg-type]
    )


def test_drawing_plan_injects_design_fields() -> None:
    calc = _calc_pkg()
    design = _design_pkg(calc)
    calc_env = _envelope(calc.model_dump(), "calc-001", "calc_package", "hydraulic")
    design_env = _envelope(design.model_dump(), "design-001", "design_package", "design", parent_ids=["calc-001"])
    plan_env = run_drawing_agent(design_env, calc_env, drawing_id="drawing-001", insunits=4)
    plan = DrawingPlan.model_validate(plan_env.payload)
    for seg in design.segments:
        related = [e for e in plan.entities if e.segment_id == seg.segment_id]
        assert len(related) >= 2
        texts = [e for e in related if e.kind in ("text", "mtext")]
        assert texts
        txt = texts[0].text
        assert seg.segment_id in txt
        if seg.dn_mm is not None:
            assert f"DN{seg.dn_mm}" in txt
        if seg.material is not None and seg.material in ("PE", "钢筋混凝土"):
            assert seg.material in txt
        assert seg.system in txt
        for entity in related:
            assert entity.source_artifact_id == design_env.artifact_id
            assert entity.system == seg.system
            assert entity.dn_mm == seg.dn_mm
            assert entity.material == seg.material
        if seg.system == "fire_protection" and seg.hydraulic_status == "not_verified":
            assert "(NOT VERIFIED)" in txt
        for e in related:
            assert e.kind in ("line", "polyline", "text", "mtext")
        for e in related:
            for x, y in e.points:
                assert math.isfinite(x) and math.isfinite(y)
        if seg.system == "fire_protection":
            assert any(e.layer == "FP-PIPE" for e in related)


def test_schematic_when_no_topology() -> None:
    calc = _calc_pkg()
    design = _design_pkg(calc)
    calc_env = _envelope(calc.model_dump(), "calc-002", "calc_package", "hydraulic")
    design_env = _envelope(design.model_dump(), "design-002", "design_package", "design", parent_ids=["calc-002"])
    for hint in [None, "schematic", "project_coordinates"]:
        plan_env = run_drawing_agent(design_env, calc_env, drawing_id="drawing-schem-001", insunits=4, drawing_class_hint=hint)  # type: ignore[arg-type]
        plan = DrawingPlan.model_validate(plan_env.payload)
        assert plan.drawing_class == "schematic"
        all_text = " ".join(e.text for e in plan.entities)
        assert "SCHEMATIC" in all_text
        assert "NOT FOR CONSTRUCTION" in all_text


def test_insunits_zero_rejected() -> None:
    calc = _calc_pkg()
    design = _design_pkg(calc)
    calc_env = _envelope(calc.model_dump(), "calc-003", "calc_package", "hydraulic")
    design_env = _envelope(design.model_dump(), "design-003", "design_package", "design", parent_ids=["calc-003"])
    with pytest.raises((ValidationError, ValueError)):
        run_drawing_agent(design_env, calc_env, drawing_id="drawing-zero-001", insunits=0)
    with pytest.raises((ValidationError, ValueError)):
        build_drawing_plan(design, calc, drawing_id="drawing-zero-002", insunits=0)
    with pytest.raises(ValidationError):
        DrawingPlan(drawing_id="d1", drawing_class="schematic", insunits=0, entities=[], source_design_artifact_id="a", source_calc_artifact_id="b", integration_conflicts=[])


def test_integration_conflicts_blocks_formal() -> None:
    calc = _calc_pkg()
    calc_env = _envelope(calc.model_dump(), "calc-004", "calc_package", "hydraulic")
    design = _design_pkg(calc)
    design.segments[0].calc_case_ids = ["missing-case-id"]
    design_env = _envelope(design.model_dump(), "design-004", "design_package", "design", parent_ids=["calc-004"])
    plan_env = run_drawing_agent(design_env, calc_env, drawing_id="drawing-conf-001", insunits=4)
    plan = DrawingPlan.model_validate(plan_env.payload)
    assert plan.integration_conflicts
    assert any("superseded" in c or "missing-case-id" in c for c in plan.integration_conflicts)
    assert plan_env.status == "blocked"
    seg_entities = [e for e in plan.entities if e.segment_id is not None]
    assert len(seg_entities) == 0

    calc2 = _calc_pkg()
    calc_env2 = _envelope(calc2.model_dump(), "calc-005", "calc_package", "hydraulic")
    design2 = _design_pkg(calc2)
    design2.segments[0].dn_mm = 200
    design_env2 = _envelope(design2.model_dump(), "design-005", "design_package", "design", parent_ids=["calc-005"])
    plan_env2 = run_drawing_agent(design_env2, calc_env2, drawing_id="drawing-conf-002", insunits=4)
    plan2 = DrawingPlan.model_validate(plan_env2.payload)
    assert plan2.integration_conflicts
    assert any("DN mismatch" in c for c in plan2.integration_conflicts)
    assert plan_env2.status == "blocked"

    design3 = _design_pkg(calc2)
    design3.recalculation_requests = [{"reason": "hydraulic_input_changed", "fields": ["diameter_mm"]}]
    design_env3 = _envelope(design3.model_dump(), "design-006", "design_package", "design", parent_ids=["calc-005"])
    plan_env3 = run_drawing_agent(design_env3, calc_env2, drawing_id="drawing-conf-003", insunits=4)
    plan3 = DrawingPlan.model_validate(plan_env3.payload)
    assert any("recalculation_requests" in c for c in plan3.integration_conflicts)
    assert plan_env3.status == "blocked"


def test_missing_length_returns_missing_inputs_without_default_candidate() -> None:
    calc = _calc_pkg()
    design = _design_pkg(calc)
    design.segments[0].length_m = None
    calc_env = _envelope(calc.model_dump(), "calc-missing-length", "calc_package", "hydraulic")
    design_env = _envelope(
        design.model_dump(), "design-missing-length", "design_package", "design", parent_ids=[calc_env.artifact_id]
    )

    plan_env = run_drawing_agent(design_env, calc_env, drawing_id="drawing-missing-length", insunits=4)

    plan = DrawingPlan.model_validate(plan_env.payload)
    assert plan_env.status == "missing_inputs"
    assert "segment seg-ws-001: missing length_m" in plan.missing_inputs
    assert [entity for entity in plan.entities if entity.segment_id is not None] == []


def test_unknown_unit_keeps_mapped_lengths_none() -> None:
    calc = _calc_pkg()
    design = _design_pkg(calc)
    calc_env = _envelope(calc.model_dump(), "calc-unit-unknown", "calc_package", "hydraulic")
    design_env = _envelope(
        design.model_dump(), "design-unit-unknown", "design_package", "design", parent_ids=[calc_env.artifact_id]
    )
    plan_env = run_drawing_agent(design_env, calc_env, drawing_id="drawing-unit-unknown", insunits=4)
    plan = DrawingPlan.model_validate(plan_env.payload)
    with tempfile.TemporaryDirectory() as tmp:
        staged = render_drawing_plan_to_staging(plan, staging_dir=tmp, file_id="unit-unknown")
        import ezdxf

        doc = ezdxf.readfile(staged)
        doc.header["$INSUNITS"] = 0
        doc.saveas(staged)
        result = verify_roundtrip(staged, plan)
        pkg_env = verify_staged_drawing_package(
            plan_env, staged_path=staged, staged_file_id="unit-unknown"
        )

    assert result["unit_known"] is False
    assert result["mapped_lengths_m"]
    assert all(length is None for length in result["mapped_lengths_m"])
    assert pkg_env.status == "blocked"
    assert "staged DXF unit is unknown; mapped length_m remains None" in pkg_env.payload["unresolved_items"]


def test_nonexistent_staged_file_is_blocked() -> None:
    calc = _calc_pkg()
    design = _design_pkg(calc)
    calc_env = _envelope(calc.model_dump(), "calc-no-file", "calc_package", "hydraulic")
    design_env = _envelope(
        design.model_dump(), "design-no-file", "design_package", "design", parent_ids=[calc_env.artifact_id]
    )
    plan_env = run_drawing_agent(design_env, calc_env, drawing_id="drawing-no-file", insunits=4)
    with tempfile.TemporaryDirectory() as tmp:
        pkg_env = verify_staged_drawing_package(
            plan_env, staged_path=Path(tmp) / "does-not-exist.dxf", staged_file_id="does-not-exist"
        )

    assert pkg_env.status == "blocked"
    assert pkg_env.payload["staged_file_id"] is None
    assert pkg_env.payload["staged_file_verified"] is False
    assert pkg_env.payload["roundtrip_review"]["errors"] == ["staged_file_missing"]


def test_roundtrip_failure_cannot_be_ready() -> None:
    calc = _calc_pkg()
    design = _design_pkg(calc)
    calc_env = _envelope(calc.model_dump(), "calc-rt-fail", "calc_package", "hydraulic")
    design_env = _envelope(
        design.model_dump(), "design-rt-fail", "design_package", "design", parent_ids=[calc_env.artifact_id]
    )
    plan_env = run_drawing_agent(design_env, calc_env, drawing_id="drawing-rt-fail", insunits=4)
    plan = DrawingPlan.model_validate(plan_env.payload)
    with tempfile.TemporaryDirectory() as tmp:
        staged = render_drawing_plan_to_staging(plan, staging_dir=tmp, file_id="roundtrip-fail")
        import ezdxf

        doc = ezdxf.readfile(staged)
        for entity in doc.modelspace():
            if entity.dxftype() == "TEXT" and "DN150" in str(entity.dxf.text or ""):
                entity.dxf.text = str(entity.dxf.text).replace("DN150", "DN999")
                break
        doc.saveas(staged)
        pkg_env = verify_staged_drawing_package(
            plan_env, staged_path=staged, staged_file_id="roundtrip-fail"
        )

    assert pkg_env.status == "blocked"
    assert pkg_env.payload["staged_file_id"] is None
    assert pkg_env.payload["roundtrip_review"]["roundtrip_segment_recall"] != 1.0


def test_staged_plan_checks_all_design_fields() -> None:
    calc = _calc_pkg()
    design = _design_pkg(calc)
    calc_env = _envelope(calc.model_dump(), "calc-fields", "calc_package", "hydraulic")
    design_env = _envelope(
        design.model_dump(), "design-fields", "design_package", "design", parent_ids=[calc_env.artifact_id]
    )
    plan_env = run_drawing_agent(design_env, calc_env, drawing_id="drawing-fields", insunits=4)
    plan = DrawingPlan.model_validate(plan_env.payload)
    label = next(entity for entity in plan.entities if entity.segment_id == "seg-ws-001" and entity.kind == "text")
    label.text = "seg-ws-001 DN999 PVC-U drainage"
    mutated_payload = plan.model_dump()
    mutated_env = plan_env.model_copy(
        update={"payload": mutated_payload, "payload_hash": compute_payload_hash(mutated_payload)}
    )
    with tempfile.TemporaryDirectory() as tmp:
        pkg_env = stage_drawing_package(mutated_env, staging_dir=tmp, file_id="field-mismatch")

    assert pkg_env.status == "blocked"
    fields = {item["field"] for item in pkg_env.payload["roundtrip_review"]["mismatches"]}
    assert {"DN", "material", "system"} <= fields


def test_upstream_conflict_does_not_render_formal_candidate() -> None:
    calc = _calc_pkg()
    design = _design_pkg(calc)
    design.segments[0].calc_case_ids = ["obsolete-case"]
    calc_env = _envelope(calc.model_dump(), "calc-upstream-conflict", "calc_package", "hydraulic")
    design_env = _envelope(
        design.model_dump(), "design-upstream-conflict", "design_package", "design", parent_ids=[calc_env.artifact_id]
    )
    plan_env = run_drawing_agent(design_env, calc_env, drawing_id="drawing-upstream-conflict", insunits=4)
    with tempfile.TemporaryDirectory() as tmp:
        pkg_env = stage_drawing_package(plan_env, staging_dir=tmp, file_id="should-not-exist")
        assert not (Path(tmp) / "should-not-exist.dxf").exists()

    assert plan_env.status == "blocked"
    assert pkg_env.status == "blocked"
    assert pkg_env.payload["staged_file_id"] is None


def test_path_not_in_schema() -> None:
    for name in ["staging", "final", "path", "file_path", "output_path", "staging_dir"]:
        assert name not in DrawingPlan.model_fields
        assert name not in PlannedEntity.model_fields
    src = Path("src/water_agent/multi_agent/agents/drawing.py").read_text(encoding="utf-8")
    assert "staging" not in src
    exec_src = Path("src/water_agent/multi_agent/drawing_executor.py").read_text(encoding="utf-8")
    assert "staging_dir" in exec_src


def test_roundtrip_recall_one() -> None:
    calc = _calc_pkg()
    design = _design_pkg(calc)
    calc_env = _envelope(calc.model_dump(), "calc-010", "calc_package", "hydraulic")
    design_env = _envelope(design.model_dump(), "design-010", "design_package", "design", parent_ids=["calc-010"])
    plan_env = run_drawing_agent(design_env, calc_env, drawing_id="drawing-rt-001", insunits=4)
    plan = DrawingPlan.model_validate(plan_env.payload)
    with tempfile.TemporaryDirectory() as tmp:
        staged = render_drawing_plan_to_staging(plan, staging_dir=tmp, file_id="staging-drawing-rt-001")
        assert Path(staged).exists()
        result = verify_roundtrip(staged, plan)
        assert result["roundtrip_segment_recall"] == 1.00
        assert result["mismatches"] == []
        assert result["total_segments"] == 3
        assert result["recalled_segments"] == 3


def test_roundtrip_mismatch_detected() -> None:
    calc = _calc_pkg()
    design = _design_pkg(calc)
    calc_env = _envelope(calc.model_dump(), "calc-011", "calc_package", "hydraulic")
    design_env = _envelope(design.model_dump(), "design-011", "design_package", "design", parent_ids=["calc-011"])
    plan_env = run_drawing_agent(design_env, calc_env, drawing_id="drawing-rt-002", insunits=4)
    plan = DrawingPlan.model_validate(plan_env.payload)
    with tempfile.TemporaryDirectory() as tmp:
        staged = render_drawing_plan_to_staging(plan, staging_dir=tmp, file_id="staging-drawing-rt-002")
        import ezdxf

        doc = ezdxf.readfile(staged)
        for e in doc.modelspace():
            if e.dxftype() == "TEXT":
                txt = str(e.dxf.text or "")
                if "DN150" in txt:
                    e.dxf.text = txt.replace("DN150", "DN999")
        doc.saveas(staged)
        result = verify_roundtrip(staged, plan)
        assert result["roundtrip_segment_recall"] != 1.00
        assert len(result["mismatches"]) > 0
        assert any(m["field"] == "DN" for m in result["mismatches"])


def test_provenance_parent_chain() -> None:
    calc = _calc_pkg()
    design = _design_pkg(calc)
    calc_env = _envelope(calc.model_dump(), "calc-020", "calc_package", "hydraulic")
    design_env = _envelope(design.model_dump(), "design-020", "design_package", "design", parent_ids=["calc-020"])
    plan_env = run_drawing_agent(design_env, calc_env, drawing_id="drawing-prov-001", insunits=4)
    plan = DrawingPlan.model_validate(plan_env.payload)
    assert plan_env.parent_artifact_ids == ["design-020", "calc-020"]
    assert plan.source_design_artifact_id == "design-020"
    assert plan.source_calc_artifact_id == "calc-020"
    registry: dict[str, ArtifactEnvelope] = {"calc-020": calc_env, "design-020": design_env, plan_env.artifact_id: plan_env}
    errors = validate_provenance(plan_env, registry)
    assert errors == []
    from water_agent.multi_agent.contracts import MULTI_PROVENANCE_INVALID

    assert MULTI_PROVENANCE_INVALID not in errors
    pkg_env = build_drawing_package(plan_env, staged_file_id="staging-drawing-prov-001")
    assert pkg_env.parent_artifact_ids == [plan_env.artifact_id]
    registry2 = dict(registry)
    registry2[pkg_env.artifact_id] = pkg_env
    errors2 = validate_provenance(pkg_env, registry2)
    assert errors2 == []


def test_stale_propagation_on_new_design() -> None:
    calc = _calc_pkg()
    design = _design_pkg(calc)
    calc_env = _envelope(calc.model_dump(), "calc-030", "calc_package", "hydraulic")
    design_env = _envelope(design.model_dump(), "design-030", "design_package", "design", parent_ids=["calc-030"])
    plan_env = run_drawing_agent(design_env, calc_env, drawing_id="drawing-stale-001", insunits=4)
    pkg_env = build_drawing_package(plan_env, staged_file_id="staging-drawing-stale-001")
    active: dict[str, ArtifactEnvelope] = {
        "calc_package": calc_env,
        "design_package": design_env,
        "drawing_plan": plan_env,
        "drawing_package": pkg_env,
    }
    design2 = _design_pkg(calc)
    design2.segments[0].dn_mm = 200
    new_design_env = _envelope(design2.model_dump(), "design-031", "design_package", "design", parent_ids=["calc-030"])
    result = stale_propagation(new_design_env, active)
    assert "drawing_plan" in result
    assert "drawing_package" in result
    assert result["drawing_plan"] == [plan_env.artifact_id]
    assert result["drawing_package"] == [pkg_env.artifact_id]


def test_deterministic_plan_hash() -> None:
    calc = _calc_pkg()
    design = _design_pkg(calc)
    calc_env = _envelope(calc.model_dump(), "calc-040", "calc_package", "hydraulic")
    design_env = _envelope(design.model_dump(), "design-040", "design_package", "design", parent_ids=["calc-040"])
    hashes: set[str] = set()
    aids: set[str] = set()
    for _ in range(100):
        env = run_drawing_agent(design_env, calc_env, drawing_id="drawing-determ-001", insunits=4)
        hashes.add(env.payload_hash)
        aids.add(env.artifact_id)
        assert env.payload_hash == compute_payload_hash(env.payload)
        plan = DrawingPlan.model_validate(env.payload)
        for e in plan.entities:
            for x, y in e.points:
                assert math.isfinite(x) and math.isfinite(y)
    assert len(hashes) == 1
    assert len(aids) == 1
    with pytest.raises(ValidationError):
        DrawingPlan(  # type: ignore[call-arg]
            drawing_id="d1",
            drawing_class="schematic",
            insunits=4,
            entities=[],
            source_design_artifact_id="a",
            source_calc_artifact_id="b",
            integration_conflicts=[],
            extra_field="bad",
        )
    with pytest.raises(ValidationError):
        PlannedEntity(entity_id="e1", kind="line", layer="W", points=[(0, 0)], text="", extra="x")  # type: ignore[call-arg]


def test_tool_view_no_calc_mcp() -> None:
    src = Path("src/water_agent/multi_agent/agents/drawing.py").read_text(encoding="utf-8")
    assert "calc_velocity" not in src
    assert "hydraulics.run_calc" not in src
    assert "run_calc" not in src
    assert "MCP" not in src
    assert "promote_staging_to_final" not in src
    assert "ToolRegistry" not in src


def test_offline_no_llm_no_cad() -> None:
    calc = _calc_pkg()
    design = _design_pkg(calc)
    calc_env = _envelope(calc.model_dump(), "calc-050", "calc_package", "hydraulic")
    design_env = _envelope(design.model_dump(), "design-050", "design_package", "design", parent_ids=["calc-050"])
    env = run_drawing_agent(design_env, calc_env, drawing_id="drawing-offline-001", insunits=4)
    assert env.status == "ready"
    plan = DrawingPlan.model_validate(env.payload)
    with tempfile.TemporaryDirectory() as tmp:
        staged = render_drawing_plan_to_staging(plan, staging_dir=tmp, file_id="staging-offline-001")
        assert Path(staged).exists()
    src = Path("src/water_agent/multi_agent/agents/drawing.py").read_text(encoding="utf-8")
    assert "ProviderGateway" not in src
    assert "GovernedChatModel" not in src
