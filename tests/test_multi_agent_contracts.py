"""Step 28 contracts/provenance — schema, hash, provenance, stale, approval."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from water_agent.multi_agent.contracts import (
    MULTI_PROVENANCE_INVALID,
    ApprovalRecord,
    ArtifactEnvelope,
    CalcCase,
    CalcPackage,
    DesignPackage,
    DesignPipeSegment,
    DrawingPackage,
    DrawingPlan,
    PlannedEntity,
    ProjectBrief,
    ReviewIssue,
    ReviewPackage,
    compute_payload_hash,
    envelope_hash,
    verify_envelope_hash,
)
from water_agent.multi_agent.provenance import (
    is_approval_stale,
    stale_propagation,
    superseded_chain,
    validate_provenance,
)


def _payload_hash(payload: dict) -> str:
    return compute_payload_hash(payload)


def _envelope(
    payload: dict,
    *,
    artifact_id: str = "test-artifact-001",
    artifact_type: str = "calc_package",
    producer: str = "hydraulic",
    revision: int = 1,
    parent_ids: list[str] | None = None,
    status: str = "ready",
) -> ArtifactEnvelope:
    h = _payload_hash(payload)
    return ArtifactEnvelope(
        artifact_id=artifact_id,
        artifact_type=artifact_type,  # type: ignore[arg-type]
        schema_version="p4.5-2026-09-10",
        producer=producer,  # type: ignore[arg-type]
        revision=revision,
        parent_artifact_ids=parent_ids or [],
        payload_hash=h,
        payload=payload,
        citation_ids=[],
        assumptions=[],
        warnings=[],
        status=status,  # type: ignore[arg-type]
    )


def _calc_registry() -> tuple[ArtifactEnvelope, dict[str, ArtifactEnvelope]]:
    pkg = CalcPackage(
        cases=[
            CalcCase(
                calc_case_id="case-001",
                formula="velocity",
                inputs={"flow_lps": 1.0},
                outputs={"velocity_mps": 0.5},
                tool_invocation_id="tool-1",
                assumptions=[],
            )
        ],
        missing_inputs=[],
        errors=[],
        supersedes_artifact_id=None,
    )
    env = _envelope(pkg.model_dump(), artifact_id="calc-001", artifact_type="calc_package")
    return env, {"calc-001": env}


# ---------------------------------------------------------------------------
# extra="forbid"
# ---------------------------------------------------------------------------


def test_extra_forbid_project_brief() -> None:
    with pytest.raises(ValidationError):
        ProjectBrief(  # type: ignore[call-arg]
            project_id="proj-1",
            request="req",
            systems=["water_supply"],
            unit_system="SI",
            hydraulic_cases=[],
            topology=[],
            spatial_constraints=[],
            required_outputs=[],
            user_constraints=[],
            unknown_field="oops",
        )


def test_extra_forbid_artifact_envelope() -> None:
    payload: dict = {}
    h = _payload_hash(payload)
    with pytest.raises(ValidationError):
        ArtifactEnvelope(  # type: ignore[call-arg]
            artifact_id="test-001",
            artifact_type="calc_package",
            schema_version="v1",
            producer="hydraulic",
            revision=1,
            parent_artifact_ids=[],
            payload_hash=h,
            payload=payload,
            citation_ids=[],
            assumptions=[],
            warnings=[],
            status="ready",
            extra_field="bad",
        )


def test_extra_forbid_calc_package() -> None:
    with pytest.raises(ValidationError):
        CalcPackage(cases=[], missing_inputs=[], errors=[], supersedes_artifact_id=None, bad="x")  # type: ignore[call-arg]


def test_extra_forbid_design_package() -> None:
    with pytest.raises(ValidationError):
        DesignPackage(segments=[], decisions=[], alternatives=[], recalculation_requests=[], unresolved_items=[], extra="x")  # type: ignore[call-arg]


def test_extra_forbid_drawing_plan() -> None:
    with pytest.raises(ValidationError):
        DrawingPlan(  # type: ignore[call-arg]
            drawing_id="d1",
            drawing_class="schematic",
            insunits=4,
            entities=[],
            source_design_artifact_id="a",
            source_calc_artifact_id="b",
            integration_conflicts=[],
            unknown="x",
        )


def test_extra_forbid_review_package() -> None:
    with pytest.raises(ValidationError):
        ReviewPackage(decision="pass", issues=[], not_checked=[], reviewed_artifact_hashes=[], foo="x")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Finite checks
# ---------------------------------------------------------------------------


def test_calc_case_outputs_nan_rejected() -> None:
    with pytest.raises(ValidationError):
        CalcCase(
            calc_case_id="c1",
            formula="velocity",
            inputs={"x": 1.0},
            outputs={"v": float("nan")},
            tool_invocation_id="t1",
        )


def test_calc_case_outputs_inf_rejected() -> None:
    with pytest.raises(ValidationError):
        CalcCase(
            calc_case_id="c1",
            formula="velocity",
            inputs={"x": 1.0},
            outputs={"v": float("inf")},
            tool_invocation_id="t1",
        )


def test_design_segment_slope_nan_rejected() -> None:
    with pytest.raises(ValidationError):
        DesignPipeSegment(
            segment_id="seg-1",
            system="water_supply",
            start_node_id="n1",
            end_node_id="n2",
            slope=float("nan"),
            calc_case_ids=[],
            source_ids=[],
            hydraulic_status="verified",
        )


def test_design_segment_slope_inf_rejected() -> None:
    with pytest.raises(ValidationError):
        DesignPipeSegment(
            segment_id="seg-1",
            system="water_supply",
            start_node_id="n1",
            end_node_id="n2",
            slope=float("-inf"),
            calc_case_ids=[],
            source_ids=[],
            hydraulic_status="verified",
        )


def test_planned_entity_points_nan_rejected() -> None:
    with pytest.raises(ValidationError):
        PlannedEntity(entity_id="e1", kind="line", layer="WATER", points=[(float("nan"), 0.0)], text="")


def test_planned_entity_points_inf_rejected() -> None:
    with pytest.raises(ValidationError):
        PlannedEntity(entity_id="e1", kind="line", layer="WATER", points=[(0.0, float("inf"))], text="")


def test_hydraulic_case_inputs_nan_rejected() -> None:
    from water_agent.multi_agent.contracts import HydraulicCaseRequest

    with pytest.raises(ValidationError):
        HydraulicCaseRequest(case_id="c1", formula="velocity", inputs={"flow_lps": float("nan")})


# ---------------------------------------------------------------------------
# Enum loud failure
# ---------------------------------------------------------------------------


def test_system_unknown_rejected() -> None:
    with pytest.raises(ValidationError):
        DesignPipeSegment(
            segment_id="seg-1",
            system="gas",  # type: ignore[arg-type]
            start_node_id="n1",
            end_node_id="n2",
            calc_case_ids=[],
            source_ids=[],
            hydraulic_status="verified",
        )


def test_drawing_class_unknown_rejected() -> None:
    with pytest.raises(ValidationError):
        DrawingPlan(
            drawing_id="d1",
            drawing_class="isometric",  # type: ignore[arg-type]
            insunits=4,
            entities=[],
            source_design_artifact_id="a",
            source_calc_artifact_id="b",
            integration_conflicts=[],
        )


def test_owner_unknown_rejected() -> None:
    with pytest.raises(ValidationError):
        ReviewIssue(
            issue_id="i1",
            rule_id="r1",
            severity="error",
            owner="ai",  # type: ignore[arg-type]
            artifact_id="a1",
            message="msg",
            evidence_ids=[],
        )


def test_status_unknown_rejected() -> None:
    with pytest.raises(ValidationError):
        ArtifactEnvelope(
            artifact_id="test-001",
            artifact_type="calc_package",
            schema_version="v1",
            producer="hydraulic",
            revision=1,
            parent_artifact_ids=[],
            payload_hash="a" * 64,
            payload={},
            citation_ids=[],
            assumptions=[],
            warnings=[],
            status="done",  # type: ignore[arg-type]
        )


def test_insunits_zero_rejected() -> None:
    with pytest.raises(ValidationError):
        DrawingPlan(
            drawing_id="d1",
            drawing_class="schematic",
            insunits=0,
            entities=[],
            source_design_artifact_id="a",
            source_calc_artifact_id="b",
            integration_conflicts=[],
        )


# ---------------------------------------------------------------------------
# payload_hash determinism
# ---------------------------------------------------------------------------


def test_payload_hash_deterministic_same_payload_same_hash() -> None:
    payload = {"cases": [{"calc_case_id": "c1"}], "missing_inputs": []}
    assert compute_payload_hash(payload) == compute_payload_hash(payload)


def test_payload_hash_changes_when_field_changes() -> None:
    p1 = {"a": 1, "b": 2}
    p2 = {"a": 1, "b": 3}
    assert compute_payload_hash(p1) != compute_payload_hash(p2)


def test_compute_vs_envelope_hash_consistent() -> None:
    payload = {"hello": "world", "num": 42}
    env = _envelope(payload, artifact_type="calc_package")
    assert compute_payload_hash(payload) == envelope_hash(env)
    assert verify_envelope_hash(env) is True


def test_same_payload_100_times_hash_same() -> None:
    payload = {"x": 1, "y": [1, 2, 3]}
    h = compute_payload_hash(payload)
    for _ in range(100):
        assert compute_payload_hash(payload) == h


def test_verify_envelope_hash_fails_when_tampered() -> None:
    payload = {"a": 1}
    env = _envelope(payload, artifact_type="calc_package")
    tampered = env.model_copy(update={"payload": {"a": 2}})
    assert verify_envelope_hash(tampered) is False


# ---------------------------------------------------------------------------
# Model cannot forge: artifact_id / payload_hash format, uppercase, etc.
# ---------------------------------------------------------------------------


def test_artifact_id_uppercase_rejected() -> None:
    payload: dict = {}
    h = _payload_hash(payload)
    with pytest.raises(ValidationError):
        ArtifactEnvelope(
            artifact_id="UPPERCASE-001",
            artifact_type="calc_package",
            schema_version="v1",
            producer="hydraulic",
            revision=1,
            parent_artifact_ids=[],
            payload_hash=h,
            payload=payload,
            citation_ids=[],
            assumptions=[],
            warnings=[],
            status="ready",
        )


def test_payload_hash_invalid_format_rejected() -> None:
    with pytest.raises(ValidationError):
        ArtifactEnvelope(
            artifact_id="test-001",
            artifact_type="calc_package",
            schema_version="v1",
            producer="hydraulic",
            revision=1,
            parent_artifact_ids=[],
            payload_hash="not-a-hash",
            payload={},
            citation_ids=[],
            assumptions=[],
            warnings=[],
            status="ready",
        )


def test_fire_protection_system_allowed() -> None:
    seg = DesignPipeSegment(
        segment_id="seg-fire-1",
        system="fire_protection",
        start_node_id="n1",
        end_node_id="n2",
        calc_case_ids=[],
        source_ids=[],
        hydraulic_status="not_verified",
    )
    assert seg.system == "fire_protection"


# ---------------------------------------------------------------------------
# Provenance broken chain
# ---------------------------------------------------------------------------


def test_parent_not_exist_provenance_invalid() -> None:
    _, registry = _calc_registry()
    pkg = CalcPackage(cases=[], missing_inputs=[], errors=[], supersedes_artifact_id=None)
    env = _envelope(pkg.model_dump(), artifact_id="calc-002", artifact_type="calc_package", parent_ids=["nonexistent"])
    errors = validate_provenance(env, registry)
    assert MULTI_PROVENANCE_INVALID in errors


def test_calc_case_id_not_in_active_provenance_invalid() -> None:
    _, registry = _calc_registry()
    design = DesignPackage(
        segments=[
            DesignPipeSegment(
                segment_id="seg-1",
                system="water_supply",
                start_node_id="n1",
                end_node_id="n2",
                calc_case_ids=["missing-case-id"],
                source_ids=[],
                hydraulic_status="verified",
            )
        ],
        decisions=[],
        alternatives=[],
        recalculation_requests=[],
        unresolved_items=[],
    )
    env = _envelope(design.model_dump(), artifact_id="design-001", artifact_type="design_package")
    errors = validate_provenance(env, registry)
    assert MULTI_PROVENANCE_INVALID in errors


def test_source_id_empty_provenance_invalid() -> None:
    calc_env, registry = _calc_registry()
    design = DesignPackage(
        segments=[
            DesignPipeSegment(
                segment_id="seg-1",
                system="water_supply",
                start_node_id="n1",
                end_node_id="n2",
                calc_case_ids=["case-001"],
                source_ids=[""],
                hydraulic_status="verified",
            )
        ],
        decisions=[],
        alternatives=[],
        recalculation_requests=[],
        unresolved_items=[],
    )
    env = _envelope(design.model_dump(), artifact_id="design-001", artifact_type="design_package")
    registry_with_calc = dict(registry)
    errors = validate_provenance(env, registry_with_calc)
    assert MULTI_PROVENANCE_INVALID in errors
    _ = calc_env


def test_segment_id_mismatch_in_drawing_provenance_invalid() -> None:
    calc_env, registry = _calc_registry()
    # Create design with seg-1
    design = DesignPackage(
        segments=[
            DesignPipeSegment(
                segment_id="seg-1",
                system="water_supply",
                start_node_id="n1",
                end_node_id="n2",
                calc_case_ids=["case-001"],
                source_ids=["SRC 1"],
                hydraulic_status="verified",
            )
        ],
        decisions=[],
        alternatives=[],
        recalculation_requests=[],
        unresolved_items=[],
    )
    design_env = _envelope(design.model_dump(), artifact_id="design-001", artifact_type="design_package")
    registry2: dict[str, ArtifactEnvelope] = {"calc-001": calc_env, "design-001": design_env}
    plan = DrawingPlan(
        drawing_id="d1",
        drawing_class="schematic",
        insunits=4,
        entities=[PlannedEntity(entity_id="e1", segment_id="nonexistent-seg", kind="line", layer="WATER", points=[(0, 0), (1, 1)], text="")],
        source_design_artifact_id="design-001",
        source_calc_artifact_id="calc-001",
        integration_conflicts=[],
    )
    plan_env = _envelope(plan.model_dump(), artifact_id="plan-001", artifact_type="drawing_plan")
    errors = validate_provenance(plan_env, registry2)
    assert MULTI_PROVENANCE_INVALID in errors


# ---------------------------------------------------------------------------
# Stale propagation: three rules
# ---------------------------------------------------------------------------


def test_new_calc_stales_all_downstream() -> None:
    # active has each downstream type
    active: dict[str, ArtifactEnvelope] = {}
    for atype, aid in [
        ("design_package", "design-001"),
        ("drawing_plan", "plan-001"),
        ("drawing_package", "pkg-001"),
        ("document", "doc-001"),
        ("review_package", "rev-001"),
    ]:
        active[atype] = _envelope({}, artifact_id=aid, artifact_type=atype, producer="design")  # type: ignore[arg-type]

    new_calc = _envelope(CalcPackage(cases=[], missing_inputs=[], errors=[]).model_dump(), artifact_id="calc-new", artifact_type="calc_package")
    result = stale_propagation(new_calc, active)
    assert set(result.keys()) == {"design_package", "drawing_plan", "drawing_package", "document", "review_package"}
    for v in result.values():
        assert len(v) == 1


def test_new_design_stales_drawing_doc_review() -> None:
    active: dict[str, ArtifactEnvelope] = {}
    for atype, aid in [
        ("drawing_plan", "plan-001"),
        ("drawing_package", "pkg-001"),
        ("document", "doc-001"),
        ("review_package", "rev-001"),
        ("calc_package", "calc-001"),
    ]:
        active[atype] = _envelope({}, artifact_id=aid, artifact_type=atype)

    new_design = _envelope(DesignPackage(segments=[]).model_dump(), artifact_id="design-new", artifact_type="design_package")
    result = stale_propagation(new_design, active)
    assert set(result.keys()) == {"drawing_plan", "drawing_package", "document", "review_package"}
    assert "calc_package" not in result


def test_new_drawing_plan_stales_only_two() -> None:
    active: dict[str, ArtifactEnvelope] = {}
    for atype, aid in [
        ("design_package", "design-001"),
        ("drawing_package", "pkg-001"),
        ("review_package", "rev-001"),
        ("document", "doc-001"),
    ]:
        active[atype] = _envelope({}, artifact_id=aid, artifact_type=atype)

    plan = DrawingPlan(
        drawing_id="d1",
        drawing_class="schematic",
        insunits=4,
        entities=[],
        source_design_artifact_id="design-001",
        source_calc_artifact_id="calc-001",
        integration_conflicts=[],
    )
    new_plan = _envelope(plan.model_dump(), artifact_id="plan-new", artifact_type="drawing_plan")
    result = stale_propagation(new_plan, active)
    assert set(result.keys()) == {"drawing_package", "review_package"}


# ---------------------------------------------------------------------------
# Approval binding
# ---------------------------------------------------------------------------


def test_approval_not_stale_when_same_hashes() -> None:
    h1 = "a" * 64
    h2 = "b" * 64
    approval = ApprovalRecord(approved=True, reason="ok", artifact_hashes=[h1, h2], approved_at="2026-09-10T00:00:00Z")
    assert is_approval_stale(approval, [h1, h2]) is False
    assert is_approval_stale(approval, [h2, h1]) is False


def test_approval_stale_when_any_hash_changed() -> None:
    h1 = "a" * 64
    h2 = "b" * 64
    h3 = "c" * 64
    approval = ApprovalRecord(approved=True, reason="ok", artifact_hashes=[h1, h2], approved_at="2026-09-10T00:00:00Z")
    assert is_approval_stale(approval, [h1, h3]) is True


def test_approval_stale_when_length_diff() -> None:
    h1 = "a" * 64
    approval = ApprovalRecord(approved=True, reason="ok", artifact_hashes=[h1], approved_at="2026-09-10T00:00:00Z")
    assert is_approval_stale(approval, [h1, "b" * 64]) is True


def test_approved_false_does_not_imply_not_stale() -> None:
    h1 = "a" * 64
    approval = ApprovalRecord(approved=False, reason="rejected", artifact_hashes=[h1], approved_at="2026-09-10T00:00:00Z")
    assert approval.approved is False
    # Publish logic must not allow approved=False; this binding is for hash check still works
    assert is_approval_stale(approval, [h1]) is False
    assert is_approval_stale(approval, ["b" * 64]) is True


def test_superseded_chain_and_cycle_detection() -> None:
    pkg_a = CalcPackage(cases=[], missing_inputs=[], errors=[], supersedes_artifact_id=None)
    env_a = _envelope(pkg_a.model_dump(), artifact_id="calc-a", artifact_type="calc_package")
    pkg_b = CalcPackage(cases=[], missing_inputs=[], errors=[], supersedes_artifact_id="calc-a")
    env_b = _envelope(pkg_b.model_dump(), artifact_id="calc-b", artifact_type="calc_package")
    registry = {"calc-a": env_a, "calc-b": env_b}
    chain = superseded_chain(pkg_b, registry)
    assert chain == ["calc-a"]
    # Cycle: a supersedes b, b supersedes a
    pkg_cycle = CalcPackage(cases=[], missing_inputs=[], errors=[], supersedes_artifact_id="calc-b")
    env_a_cycle = _envelope(pkg_cycle.model_dump(), artifact_id="calc-a", artifact_type="calc_package")
    registry_cycle = {"calc-a": env_a_cycle, "calc-b": env_b}
    with pytest.raises(ValueError, match="cycle"):
        superseded_chain(pkg_cycle, registry_cycle)
