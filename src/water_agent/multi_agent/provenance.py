"""Provenance, stale propagation and approval binding (pure functions)."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from water_agent.multi_agent.contracts import (
    MULTI_PROVENANCE_INVALID,
    ApprovalRecord,
    ArtifactEnvelope,
    CalcPackage,
    DesignPackage,
    DrawingPlan,
    compute_payload_hash,
)


def envelope_hash(envelope: ArtifactEnvelope) -> str:
    return compute_payload_hash(envelope.payload)


def verify_envelope_hash(envelope: ArtifactEnvelope) -> bool:
    return envelope.payload_hash == compute_payload_hash(envelope.payload)


def _collect_calc_case_ids(registry: dict[str, ArtifactEnvelope]) -> set[str]:
    ids: set[str] = set()
    for env in registry.values():
        if env.artifact_type != "calc_package":
            continue
        try:
            pkg = CalcPackage.model_validate(env.payload)
        except (ValidationError, ValueError, TypeError, KeyError):
            continue
        for c in pkg.cases:
            ids.add(c.calc_case_id)
    return ids


def _collect_segment_ids(registry: dict[str, ArtifactEnvelope]) -> set[str]:
    ids: set[str] = set()
    for env in registry.values():
        if env.artifact_type != "design_package":
            continue
        try:
            pkg = DesignPackage.model_validate(env.payload)
        except (ValidationError, ValueError, TypeError, KeyError):
            continue
        for s in pkg.segments:
            ids.add(s.segment_id)
    return ids


def validate_provenance(
    envelope: ArtifactEnvelope,
    registry: dict[str, ArtifactEnvelope],
) -> list[str]:
    """Return list of error codes; empty means valid."""
    errors: set[str] = set()
    for pid in envelope.parent_artifact_ids:
        if pid not in registry:
            errors.add(MULTI_PROVENANCE_INVALID)
            break
    payload: dict[str, Any] = envelope.payload
    if "segments" in payload and isinstance(payload["segments"], list):
        try:
            pkg = DesignPackage.model_validate(payload)
            calc_ids = _collect_calc_case_ids(registry)
            for seg in pkg.segments:
                for cid in seg.calc_case_ids:
                    if cid not in calc_ids:
                        errors.add(MULTI_PROVENANCE_INVALID)
                        break
                for sid in seg.source_ids:
                    if not isinstance(sid, str) or not sid.strip():
                        errors.add(MULTI_PROVENANCE_INVALID)
                        break
        except (ValidationError, ValueError, TypeError, KeyError):
            if envelope.artifact_type == "design_package":
                errors.add(MULTI_PROVENANCE_INVALID)
    if "entities" in payload and isinstance(payload["entities"], list):
        try:
            plan = DrawingPlan.model_validate(payload)
            segment_ids = _collect_segment_ids(registry)
            for ent in plan.entities:
                if ent.segment_id is not None and ent.segment_id not in segment_ids:
                    errors.add(MULTI_PROVENANCE_INVALID)
                    break
                if ent.source_artifact_id not in registry:
                    errors.add(MULTI_PROVENANCE_INVALID)
                    break
            for key in ("source_design_artifact_id", "source_calc_artifact_id"):
                ref = payload.get(key)
                if isinstance(ref, str) and ref and ref not in registry:
                    errors.add(MULTI_PROVENANCE_INVALID)
                    break
        except (ValidationError, ValueError, TypeError, KeyError):
            if envelope.artifact_type == "drawing_plan":
                errors.add(MULTI_PROVENANCE_INVALID)
    if "plan_artifact_id" in payload and isinstance(payload["plan_artifact_id"], str):
        ref = payload["plan_artifact_id"]
        if ref and ref not in registry and envelope.artifact_type == "drawing_package":
            errors.add(MULTI_PROVENANCE_INVALID)
    if errors:
        return sorted(errors)
    return []


_STALE_RULES: dict[str, list[str]] = {
    "calc_package": ["design_package", "drawing_plan", "drawing_package", "document", "review_package"],
    "design_package": ["drawing_plan", "drawing_package", "document", "review_package"],
    "drawing_plan": ["drawing_package", "review_package"],
}


def stale_propagation(
    new_artifact: ArtifactEnvelope,
    active: dict[str, ArtifactEnvelope],
) -> dict[str, list[str]]:
    affected = _STALE_RULES.get(new_artifact.artifact_type, [])
    result: dict[str, list[str]] = {}
    for atype in affected:
        env = active.get(atype)
        if env is not None:
            result[atype] = [env.artifact_id]
    return result


def is_approval_stale(
    approval: ApprovalRecord,
    current_hashes: list[str],
) -> bool:
    approved_hashes: list[str] = list(approval.artifact_hashes)
    if len(approved_hashes) != len(current_hashes):
        return True
    if set(approved_hashes) != set(current_hashes):
        return True
    return sorted(approved_hashes) != sorted(current_hashes)


def superseded_chain(
    pkg: CalcPackage,
    registry: dict[str, ArtifactEnvelope],
) -> list[str]:
    chain: list[str] = []
    visited: set[str] = set()
    cur: str | None = pkg.supersedes_artifact_id
    while cur is not None:
        if cur in visited:
            raise ValueError(f"superseded chain cycle detected at {cur!r}")
        visited.add(cur)
        chain.append(cur)
        env = registry.get(cur)
        if env is None:
            break
        try:
            parent_pkg = CalcPackage.model_validate(env.payload)
        except (ValidationError, ValueError, TypeError, KeyError):
            break
        cur = parent_pkg.supersedes_artifact_id
    return chain
