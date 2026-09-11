"""Step 28 provenance and approval binding behavior tests."""

from __future__ import annotations

import pytest

from water_agent.multi_agent.contracts import (
    ApprovalRecord,
    ArtifactEnvelope,
    compute_payload_hash,
)
from water_agent.multi_agent.provenance import (
    assert_approval_binding,
    stale_propagation,
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


def test_assert_approval_binding_rejects_changed_payload() -> None:
    env = _env("doc-1", "document", payload={"text": "v1"})
    approval = ApprovalRecord(
        approved=True,
        reason="approved",
        artifact_ids=[env.artifact_id],
        artifact_hashes=[env.payload_hash],
        approved_at="2026-09-11T00:00:00Z",
    )
    changed = env.model_copy(update={"payload": {"text": "v2"}})
    with pytest.raises(ValueError, match="MULTI_APPROVAL_STALE"):
        assert_approval_binding(approval, [changed])
