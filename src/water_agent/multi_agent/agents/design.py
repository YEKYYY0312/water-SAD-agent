"""Step 30 design agent — deterministic, offline, no hydraulic compute."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Literal, cast

from pydantic import ValidationError

from water_agent.domain.citation import CitationClaim, enrich_claims, validate_citations
from water_agent.multi_agent.contracts import (
    ArtifactEnvelope,
    CalcPackage,
    DesignCitation,
    DesignPackage,
    DesignPipeSegment,
    ProjectBrief,
    TopologyEdge,
    compute_payload_hash,
)

_SCHEMA_VERSION = "p4.5-2026-09-10"


def _stable_segment_id(project_id: str, start: str, end: str, system: str) -> str:
    raw = f"{project_id}:{start}:{end}:{system}"
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"seg-{h}"


def _infer_dn_mm(brief: ProjectBrief) -> int | None:
    for constraint in brief.spatial_constraints:
        if constraint.kind.lower() not in {"diameter_mm", "dn_mm"}:
            continue
        try:
            value = float(constraint.value)
        except (TypeError, ValueError):
            continue
        if value > 0 and value.is_integer():
            return int(value)
    for c in brief.hydraulic_cases:
        v = c.inputs.get("diameter_mm")
        if isinstance(v, (int, float)):
            try:
                iv = int(v)
                if iv > 0:
                    return iv
            except (ValueError, TypeError):
                continue
        v2 = c.inputs.get("diameter")
        if isinstance(v2, (int, float)):
            try:
                iv = int(v2)
                if iv > 0:
                    return iv
            except (ValueError, TypeError):
                continue
    for uc in brief.user_constraints:
        m = re.search(r"DN\s*(\d+)", uc, re.IGNORECASE)
        if m:
            try:
                iv = int(m.group(1))
                if iv > 0:
                    return iv
            except (ValueError, TypeError):
                continue
    return None


def _infer_material(brief: ProjectBrief) -> str | None:
    for sc in brief.spatial_constraints:
        if sc.kind.lower() == "material" and isinstance(sc.value, str) and sc.value.strip():
            return sc.value.strip()
    for uc in brief.user_constraints:
        low = uc.lower()
        if "material" in low:
            if ":" in uc:
                part = uc.split(":", 1)[1].strip()
                if part:
                    return part
            if "=" in uc:
                part = uc.split("=", 1)[1].strip()
                if part:
                    return part
    return None


def _infer_slope(brief: ProjectBrief) -> float | None:
    for c in brief.hydraulic_cases:
        v = c.inputs.get("slope")
        if isinstance(v, (int, float)):
            return float(v)
        v2 = c.inputs.get("fill_ratio")
        _ = v2
    return None


def _requested_source_ids(brief: ProjectBrief) -> list[str]:
    """Read user citation requests without treating them as trusted citations."""
    requested: list[str] = []
    for uc in brief.user_constraints:
        if uc.strip().lower().startswith("source_id:"):
            sid = uc.split(":", 1)[1].strip()
            if sid:
                requested.append(sid)
        elif uc.strip().lower().startswith("source_ids:"):
            rest = uc.split(":", 1)[1].strip()
            for part in re.split(r"[;,]", rest):
                p = part.strip()
                if p:
                    requested.append(p)
    return list(dict.fromkeys(requested))


def _citation_candidates(
    retrieved_source_ids: list[str] | None,
    validated_citations: list[CitationClaim] | None,
    *,
    version_registry: Any | None,
) -> dict[str, CitationClaim]:
    """Return only IDs made available by retrieval or a prior validator run."""
    candidates: dict[str, CitationClaim] = {}
    for source_id in retrieved_source_ids or []:
        cleaned = source_id.strip()
        if cleaned:
            candidates[cleaned] = CitationClaim(source_id=cleaned, declared_by="system")
    for claim in validated_citations or []:
        cleaned = claim.source_id.strip()
        if cleaned:
            candidates[cleaned] = claim.model_copy(update={"source_id": cleaned})

    if not candidates:
        return {}
    check = validate_citations(candidates, candidates, version_registry=version_registry)
    return {claim.source_id: claim for claim in enrich_claims(candidates.values(), check)}


def _select_citations(
    brief: ProjectBrief,
    candidates: dict[str, CitationClaim],
) -> tuple[list[str], list[str]]:
    requested = _requested_source_ids(brief)
    if not requested:
        return sorted(candidates), []
    selected = [source_id for source_id in requested if source_id in candidates]
    invalid = [source_id for source_id in requested if source_id not in candidates]
    return selected, invalid


def _infer_length(edge: TopologyEdge) -> float | None:
    return edge.length_m


def _suggested_formula(fields: set[str]) -> str:
    if fields & {"diameter_mm", "flow_lps", "velocity_mps"}:
        return "velocity"
    return "head_loss_darcy_weisbach"


def _add_recalculation_request(
    requests: list[dict[str, Any]],
    *,
    segment_id: str,
    reason: str,
    fields: set[str],
    formula: str | None = None,
    source_calc_case_id: str | None = None,
    input_updates: dict[str, float | str] | None = None,
) -> None:
    cleaned_fields = sorted({field.strip() for field in fields if field.strip()})
    if not cleaned_fields:
        return
    request: dict[str, Any] = {
        "reason": reason,
        "fields": cleaned_fields,
        "segment_id": segment_id,
    }
    if formula is not None and source_calc_case_id is not None and input_updates:
        request.update(
            {
                "formula": formula,
                "source_calc_case_id": source_calc_case_id,
                "input_updates": dict(input_updates),
            }
        )
    else:
        request["suggested_formula"] = _suggested_formula(set(cleaned_fields))
    if request not in requests:
        requests.append(request)


def build_design_package(
    brief: ProjectBrief,
    calc_package: CalcPackage,
    *,
    retrieved_source_ids: list[str] | None = None,
    validated_citations: list[CitationClaim] | None = None,
    version_registry: Any | None = None,
    prev_design: DesignPackage | None = None,
    active_calc_case_ids: set[str] | None = None,
    recalc_trigger_fields: set[str] | None = None,
) -> DesignPackage:
    # Failed/missing cases remain in CalcPackage for provenance, but cannot
    # support a verified design segment.
    verified_case_ids = (
        {
            c.calc_case_id
            for c in calc_package.cases
            if c.status == "verified" and c.outputs
        }
        if not calc_package.missing_inputs and not calc_package.errors
        else set()
    )
    active_ids = verified_case_ids if active_calc_case_ids is None else verified_case_ids & active_calc_case_ids
    citation_candidates = _citation_candidates(
        retrieved_source_ids, validated_citations, version_registry=version_registry
    )
    inferred_source_ids, invalid_citation_ids = _select_citations(brief, citation_candidates)

    dn_mm = _infer_dn_mm(brief)
    material = _infer_material(brief)
    slope_val = _infer_slope(brief)

    segments: list[DesignPipeSegment] = []
    unresolved: list[str] = []
    recalc_requests: list[dict[str, Any]] = []

    if not brief.topology:
        unresolved.append("missing topology: no edges, cannot derive segments")

    systems = brief.systems
    if not systems:
        unresolved.append("missing system: brief.systems empty")
    systems_for_gen = list(systems)

    for sys_val_raw in systems_for_gen:
        sys_val = cast(Literal["water_supply", "drainage", "fire_protection"], sys_val_raw)
        for edge in brief.topology:
            sid = _stable_segment_id(brief.project_id, edge.start_node_id, edge.end_node_id, sys_val)
            length_m = _infer_length(edge)
            calc_ids = sorted(active_ids) if active_ids else []

            seg_unresolved: list[str] = []
            if material is None:
                seg_unresolved.append(f"segment {sid}: missing material")
            if dn_mm is None:
                seg_unresolved.append(f"segment {sid}: missing dn_mm")
            if length_m is None:
                seg_unresolved.append(f"segment {sid}: missing length_m")
            if sys_val not in ("water_supply", "drainage", "fire_protection"):
                seg_unresolved.append(f"segment {sid}: invalid system {sys_val}")

            # fire_protection device unresolved per Q3
            if sys_val == "fire_protection":
                seg_unresolved.append(f"segment {sid}: fire hazard level / sprinkler selection unresolved")

            # A segment is verified only when every referenced case comes from
            # the active, successful calculation set and its explicit inputs
            # still match the selected design values.
            hydraulic_status: Literal["verified", "not_verified", "recalc_required"]
            if not calc_ids:
                hydraulic_status = "not_verified"
            else:
                inconsistent = False
                for cc in calc_package.cases:
                    if cc.calc_case_id in calc_ids:
                        input_updates: dict[str, float | str] = {}
                        d = cc.inputs.get("diameter_mm")
                        if d is not None and dn_mm is not None:
                            try:
                                if int(d) != int(dn_mm):
                                    inconsistent = True
                                    input_updates["diameter_mm"] = float(dn_mm)
                            except (ValueError, TypeError):
                                pass
                        if input_updates:
                            _add_recalculation_request(
                                recalc_requests,
                                segment_id=sid,
                                reason="hydraulic_input_changed",
                                fields=set(input_updates),
                                formula=cc.formula,
                                source_calc_case_id=cc.calc_case_id,
                                input_updates=input_updates,
                            )
                hydraulic_status = "recalc_required" if inconsistent else "verified"

            # unresolved for slope when drainage and slope missing
            if sys_val == "drainage" and slope_val is None:
                unresolved.append(f"segment {sid}: missing slope for drainage")

            for u in seg_unresolved:
                if u not in unresolved:
                    unresolved.append(u)

            segments.append(
                DesignPipeSegment(
                    segment_id=sid,
                    system=sys_val,
                    start_node_id=edge.start_node_id,
                    end_node_id=edge.end_node_id,
                    length_m=length_m,
                    dn_mm=dn_mm,
                    material=material,
                    slope=slope_val,
                    calc_case_ids=calc_ids,
                    source_ids=list(inferred_source_ids),
                    hydraulic_status=hydraulic_status,
                )
            )

    # recalc detection vs prev_design
    if prev_design is not None and segments:
        prev_map = {s.segment_id: s for s in prev_design.segments}
        for seg in segments:
            old = prev_map.get(seg.segment_id)
            if old is None:
                _add_recalculation_request(
                    recalc_requests,
                    segment_id=seg.segment_id,
                    reason="topology_changed",
                    fields={"topology"},
                )
                if seg.hydraulic_status == "verified":
                    seg.hydraulic_status = "recalc_required"
                continue
            fields: dict[str, float | str | None] = {}
            if old.dn_mm != seg.dn_mm:
                fields["diameter_mm"] = float(seg.dn_mm) if seg.dn_mm is not None else None
            if old.length_m != seg.length_m:
                fields["length_m"] = seg.length_m
            if old.slope != seg.slope:
                fields["slope"] = seg.slope
            if fields:
                existing_fields = {
                    field
                    for request in recalc_requests
                    if request.get("segment_id") == seg.segment_id
                    for field in request.get("fields", [])
                    if isinstance(field, str)
                }
                pending_fields = set(fields) - existing_fields
                if pending_fields:
                    source_cases = [
                        case
                        for case in calc_package.cases
                        if case.calc_case_id in seg.calc_case_ids
                    ]
                    updates = {
                        field: value
                        for field, value in fields.items()
                        if field in pending_fields and value is not None
                    }
                    if len(source_cases) == 1 and updates:
                        source = source_cases[0]
                        _add_recalculation_request(
                            recalc_requests,
                            segment_id=seg.segment_id,
                            reason="hydraulic_input_changed",
                            fields=pending_fields,
                            formula=source.formula,
                            source_calc_case_id=source.calc_case_id,
                            input_updates=updates,
                        )
                    else:
                        _add_recalculation_request(
                            recalc_requests,
                            segment_id=seg.segment_id,
                            reason="hydraulic_input_changed",
                            fields=pending_fields,
                        )
                if seg.hydraulic_status == "verified":
                    seg.hydraulic_status = "recalc_required"

    # Inputs newly supplied by a design revision (for example flow, roughness
    # or a revised slope) have no safe implicit mapping to an old calc result.
    explicit_recalc_fields = {
        field.strip() for field in recalc_trigger_fields or set() if field.strip()
    }
    if explicit_recalc_fields:
        for seg in segments:
            _add_recalculation_request(
                recalc_requests,
                segment_id=seg.segment_id,
                reason="hydraulic_input_changed",
                fields=explicit_recalc_fields,
            )
            if seg.hydraulic_status == "verified":
                seg.hydraulic_status = "recalc_required"

    decisions: list[str] = []
    alternatives: list[str] = []
    if segments:
        decisions.append(f"design for systems {','.join(systems_for_gen)} based on calc cases {sorted(active_ids)}")
        alternatives.append("alternative: keep current diameter vs upsized diameter (requires recalc)")

    # ensure unresolved dedup preserve order
    seen_u: set[str] = set()
    dedup_u: list[str] = []
    for u in unresolved:
        if u not in seen_u:
            seen_u.add(u)
            dedup_u.append(u)

    citations = [
        DesignCitation(
            source_id=source_id,
            spec_version=citation_candidates[source_id].spec_version,
            version_status=cast(
                Literal["current", "superseded", "deprecated", "unknown"] | None,
                citation_candidates[source_id].version_status,
            ),
            location=f"segment:{segment.segment_id}:source_ids",
        )
        for segment in segments
        for source_id in segment.source_ids
    ]

    pkg = DesignPackage(
        segments=segments,
        decisions=decisions,
        alternatives=alternatives,
        recalculation_requests=recalc_requests,
        unresolved_items=dedup_u,
        citations=citations,
        invalid_citation_ids=invalid_citation_ids,
    )
    return pkg


def run_design_agent(
    brief: ProjectBrief,
    calc_envelope: ArtifactEnvelope,
    *,
    retrieved_source_ids: list[str] | None = None,
    validated_citations: list[CitationClaim] | None = None,
    version_registry: Any | None = None,
    prev_design: DesignPackage | None = None,
    recalc_trigger_fields: set[str] | None = None,
    active_calc_case_ids: set[str] | None = None,
) -> ArtifactEnvelope:
    try:
        calc_pkg = CalcPackage.model_validate(calc_envelope.payload)
    except ValidationError as e:
        payload = DesignPackage(
            segments=[],
            decisions=[],
            alternatives=[],
            recalculation_requests=[],
            unresolved_items=[f"calc payload invalid: {e}"],
        ).model_dump()
        ph = compute_payload_hash(payload)
        raw = f"{brief.project_id}:{calc_envelope.artifact_id}:{ph}"
        aid = f"design-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"
        return ArtifactEnvelope(
            artifact_id=aid,
            artifact_type="design_package",
            schema_version=_SCHEMA_VERSION,
            producer="design",
            revision=2 if prev_design is not None else 1,
            parent_artifact_ids=[calc_envelope.artifact_id],
            payload_hash=ph,
            payload=payload,
            citation_ids=[],
            assumptions=[],
            warnings=["calc payload invalid"],
            status="blocked",
        )

    # A successful case is usable only from an active, successful CalcPackage.
    # Package-level failure/missing signals prevent an unrelated-looking case
    # from lending a false "verified" label to a design segment.
    current_case_ids: set[str] = set()
    if (
        calc_envelope.artifact_type == "calc_package"
        and calc_envelope.status == "ready"
        and not calc_pkg.missing_inputs
        and not calc_pkg.errors
    ):
        current_case_ids = {
            case.calc_case_id
            for case in calc_pkg.cases
            if case.status == "verified" and case.outputs
        }
    if active_calc_case_ids is not None:
        current_case_ids &= active_calc_case_ids

    design_pkg = build_design_package(
        brief,
        calc_pkg,
        retrieved_source_ids=retrieved_source_ids,
        validated_citations=validated_citations,
        version_registry=version_registry,
        prev_design=prev_design,
        active_calc_case_ids=current_case_ids,
        recalc_trigger_fields=recalc_trigger_fields,
    )

    payload = design_pkg.model_dump()
    payload_hash = compute_payload_hash(payload)
    raw = f"{brief.project_id}:{calc_envelope.artifact_id}:{payload_hash}"
    artifact_id = f"design-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"
    revision = 2 if prev_design is not None else 1

    has_invalid_source = bool(design_pkg.invalid_citation_ids)
    has_not_verified = any(s.hydraulic_status == "not_verified" for s in design_pkg.segments)
    has_recalculation = bool(design_pkg.recalculation_requests) or any(
        s.hydraulic_status == "recalc_required" for s in design_pkg.segments
    )
    has_unresolved = len(design_pkg.unresolved_items) > 0

    warnings: list[str] = []
    if has_invalid_source:
        warnings.append(
            "invalid citation source_id rejected: "
            + ", ".join(design_pkg.invalid_citation_ids)
        )
    if has_not_verified:
        warnings.append("hydraulic not_verified present")
    if has_recalculation:
        warnings.append("recalculation_requests pending")
    if has_unresolved:
        warnings.append("unresolved_items present")

    if has_invalid_source or has_recalculation or has_not_verified:
        status: str = "blocked"
    elif has_unresolved and any("missing" in u for u in design_pkg.unresolved_items):
        # missing required inputs -> missing_inputs
        # but if topology missing etc, treat as missing_inputs
        status = "missing_inputs"
    elif has_unresolved:
        status = "missing_inputs"
    else:
        status = "ready"

    if not design_pkg.segments and not brief.topology:
        status = "missing_inputs"

    # provenance invalid marker is via status blocked
    envelope = ArtifactEnvelope(
        artifact_id=artifact_id,
        artifact_type="design_package",
        schema_version=_SCHEMA_VERSION,
        producer="design",
        revision=revision,
        parent_artifact_ids=[calc_envelope.artifact_id],
        payload_hash=payload_hash,
        payload=payload,
        citation_ids=sorted({citation.source_id for citation in design_pkg.citations}),
        assumptions=[],
        warnings=warnings,
        status=status,  # type: ignore[arg-type]
    )
    return envelope
