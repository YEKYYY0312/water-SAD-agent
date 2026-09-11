"""Step 28 provenance and approval binding behavior tests."""

from __future__ import annotations

import pytest

from water_agent.multi_agent.contracts import (
    MULTI_PROVENANCE_INVALID,
    ApprovalRecord,
    ArtifactEnvelope,
    CalcCase,
    CalcPackage,
    DesignPackage,
    DesignPipeSegment,
    DrawingPackage,
    compute_payload_hash,
)
from water_agent.multi_agent.provenance import (
    assert_approval_binding,
    stale_propagation,
    validate_provenance,
)


def _env(
    artifact_id: str,
    artifact_type: str,
    *,
    parent_ids: list[str] | None = None,
    payload: dict | None = None,
    revision: int = 1,
) -> ArtifactEnvelope:
    body = payload or {"artifact": artifact_id}
    return ArtifactEnvelope(
        artifact_id=artifact_id,
        artifact_type=artifact_type,  # type: ignore[arg-type]
        schema_version="p4.5-test",
        producer="orchestrator",
        revision=revision,
        parent_artifact_ids=parent_ids or [],
        payload_hash=compute_payload_hash(body),
        payload=body,
        status="ready",
    )


def test_stale_propagation_recurses_through_all_descendants() -> None:
    calc = _env("calc-1", "calc_package")
    design = _env("design-1", "design_package", parent_ids=["calc-1"])
    plan = _env("plan-1", "drawing_plan", parent_ids=["design-1"])
    package = _env("package-1", "drawing_package", parent_ids=["plan-1"])
    review = _env("review-1", "review_package", parent_ids=["package-1"])
    new_calc = _env("calc-2", "calc_package", parent_ids=["calc-1"], revision=2)

    result = stale_propagation(new_calc, {e.artifact_id: e for e in (calc, design, plan, package, review)})

    assert result == {
        "design_package": ["design-1"],
        "drawing_plan": ["plan-1"],
        "drawing_package": ["package-1"],
        "review_package": ["review-1"],
    }


def test_design_calc_case_must_come_from_its_explicit_calc_parent() -> None:
    old_calc = CalcPackage(
        cases=[
            CalcCase(
                calc_case_id="case-old",
                formula="velocity",
                inputs={"flow_lps": 10.0, "diameter_mm": 150.0},
                outputs={"velocity_mps": 0.565884},
                tool_invocation_id="tool-old",
            )
        ]
    )
    new_calc = CalcPackage(
        cases=[
            CalcCase(
                calc_case_id="case-new",
                formula="velocity",
                inputs={"flow_lps": 10.0, "diameter_mm": 200.0},
                outputs={"velocity_mps": 0.31831},
                tool_invocation_id="tool-new",
            )
        ],
        supersedes_artifact_id="calc-old",
    )
    old_env = _env("calc-old", "calc_package", payload=old_calc.model_dump(mode="python"))
    new_env = _env(
        "calc-new",
        "calc_package",
        parent_ids=[old_env.artifact_id],
        payload=new_calc.model_dump(mode="python"),
        revision=2,
    )
    design = DesignPackage(
        segments=[
            DesignPipeSegment(
                segment_id="seg-1",
                system="water_supply",
                start_node_id="n1",
                end_node_id="n2",
                length_m=10.0,
                dn_mm=200,
                material="PE",
                calc_case_ids=["case-old"],
                source_ids=[],
                hydraulic_status="verified",
            )
        ]
    )
    design_env = _env(
        "design-new",
        "design_package",
        parent_ids=[new_env.artifact_id],
        payload=design.model_dump(mode="python"),
    )
    registry = {
        envelope.artifact_id: envelope
        for envelope in (old_env, new_env, design_env)
    }

    assert MULTI_PROVENANCE_INVALID in validate_provenance(design_env, registry)


def test_drawing_package_parent_must_match_payload_plan_reference() -> None:
    calc_env = _env("calc-parent", "calc_package")
    plan_env = _env("plan-parent", "drawing_plan")
    package = DrawingPackage(
        plan_artifact_id=plan_env.artifact_id,
        preview_status="blocked",
    )
    package_env = _env(
        "drawing-package-parent",
        "drawing_package",
        parent_ids=[calc_env.artifact_id],
        payload=package.model_dump(mode="python"),
    )
    registry = {
        envelope.artifact_id: envelope
        for envelope in (calc_env, plan_env, package_env)
    }

    assert MULTI_PROVENANCE_INVALID in validate_provenance(package_env, registry)


def test_assert_approval_binding_rejects_changed_payload() -> None:
    env = _env("doc-1", "document", payload={"text": "v1"})
    approval = ApprovalRecord(
        approved=True,
        reason="approved",
        artifact_ids=[env.artifact_id],
        artifact_hashes=[env.payload_hash],
        artifact_revisions=[env.revision],
        artifact_parent_artifact_ids=[env.parent_artifact_ids],
        approved_at="2026-09-11T00:00:00Z",
    )
    changed = env.model_copy(update={"payload": {"text": "v2"}})
    with pytest.raises(ValueError, match="MULTI_APPROVAL_STALE"):
        assert_approval_binding(approval, [changed])


@pytest.mark.parametrize(
    "changed",
    [
        _env("doc-1", "document", payload={"text": "v1"}, revision=2),
        _env(
            "doc-1",
            "document",
            payload={"text": "v1"},
            parent_ids=["design-new"],
        ),
    ],
    ids=["revision-only", "parent-only"],
)
def test_assert_approval_binding_rejects_envelope_metadata_drift(
    changed: ArtifactEnvelope,
) -> None:
    original = _env("doc-1", "document", payload={"text": "v1"})
    approval = ApprovalRecord(
        approved=True,
        reason="approved",
        artifact_ids=[original.artifact_id],
        artifact_hashes=[original.payload_hash],
        artifact_revisions=[original.revision],
        artifact_parent_artifact_ids=[original.parent_artifact_ids],
        approved_at="2026-09-11T00:00:00Z",
    )

    assert changed.payload_hash == original.payload_hash
    with pytest.raises(ValueError, match="MULTI_APPROVAL_STALE"):
        assert_approval_binding(approval, [changed])
