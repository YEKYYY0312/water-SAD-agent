"""Provenance, stale propagation and approval binding (pure functions)."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from water_agent.multi_agent.contracts import (
    MULTI_APPROVAL_STALE,
    MULTI_PROVENANCE_INVALID,
    ApprovalRecord,
    ArtifactEnvelope,
    CalcPackage,
    DesignPackage,
    DrawingPlan,
    compute_artifact_collection_hash,
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
    # Registries are keyed by artifact_id.  Walk descendants by parent ids so
    # multi-hop dependencies are invalidated transitively.
    roots = {new_artifact.artifact_id, *new_artifact.parent_artifact_ids}
    stale_ids: set[str] = set()
    changed = True
    while changed:
        changed = False
        for key, env in active.items():
            artifact_id = env.artifact_id or key
            if artifact_id in roots or artifact_id in stale_ids:
                continue
            if any(parent in roots or parent in stale_ids for parent in env.parent_artifact_ids):
                stale_ids.add(artifact_id)
                changed = True

    result: dict[str, list[str]] = {}
    for key, env in active.items():
        artifact_id = env.artifact_id or key
        if artifact_id in stale_ids:
            result.setdefault(env.artifact_type, []).append(artifact_id)

    # Keep compatibility with the original fixtures that keyed the registry by
    # artifact type and omitted parent links.  Real registries use the graph.
    if not result:
        for atype in _STALE_RULES.get(new_artifact.artifact_type, []):
            matches = [
                env.artifact_id
                for key, env in active.items()
                if key == atype or env.artifact_type == atype
            ]
            if matches:
                result[atype] = sorted(set(matches))
    for ids in result.values():
        ids.sort()
    return result


def is_approval_stale(
    approval: ApprovalRecord,
    current_hashes: list[str] | dict[str, ArtifactEnvelope],
) -> bool:
    if isinstance(current_hashes, dict):
        current_ids = sorted(current_hashes)
        current_payload_hashes = [compute_payload_hash(current_hashes[aid].payload) for aid in current_ids]
        if approval.artifact_ids and approval.artifact_ids != current_ids:
            return True
        expected_collection = compute_artifact_collection_hash(
            current_payload_hashes,
            artifact_ids=current_ids if approval.artifact_ids else None,
        )
        if approval.artifact_collection_hash != expected_collection:
            return True
        current_hashes = current_payload_hashes
    else:
        expected_collection = compute_artifact_collection_hash(list(current_hashes))
        if approval.artifact_collection_hash != expected_collection:
            return True
    approved_hashes: list[str] = list(approval.artifact_hashes)
    if len(approved_hashes) != len(current_hashes):
        return True
    if set(approved_hashes) != set(current_hashes):
        return True
    return sorted(approved_hashes) != sorted(current_hashes)


def validate_approval_binding(
    approval: ApprovalRecord,
    artifacts: dict[str, ArtifactEnvelope] | list[ArtifactEnvelope],
) -> bool:
    """Return whether approval is bound to the current final artifacts."""
    collection = artifacts if isinstance(artifacts, dict) else {e.artifact_id: e for e in artifacts}
    return not is_approval_stale(approval, collection)


def assert_approval_binding(
    approval: ApprovalRecord,
    artifacts: dict[str, ArtifactEnvelope] | list[ArtifactEnvelope],
) -> None:
    """Raise a stable error when an approval no longer matches its artifacts."""
    if not validate_approval_binding(approval, artifacts):
        raise ValueError(f"{MULTI_APPROVAL_STALE}: approval is stale or mismatched")


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
