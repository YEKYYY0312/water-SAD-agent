"""Step 30 design agent — deterministic, offline, no hydraulic compute."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Literal, cast

from pydantic import ValidationError

from water_agent.multi_agent.contracts import (
    ArtifactEnvelope,
    CalcPackage,
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
    for c in brief.hydraulic_cases:
        v = c.inputs.get("diameter_mm")
        if isinstance(v, (int, float)):
            try:
                iv = int(v)
                if iv > 0:
                    return iv
            except (ValueError, TypeError):  # noqa: S112
                continue
        v2 = c.inputs.get("diameter")
        if isinstance(v2, (int, float)):
            try:
                iv = int(v2)
                if iv > 0:
                    return iv
            except (ValueError, TypeError):  # noqa: S112
                continue
    for uc in brief.user_constraints:
        m = re.search(r"DN\s*(\d+)", uc, re.IGNORECASE)
        if m:
            try:
                iv = int(m.group(1))
                if iv > 0:
                    return iv
            except (ValueError, TypeError):  # noqa: S112
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


def _infer_source_ids(brief: ProjectBrief, retrieved: list[str] | None) -> list[str]:
    override: list[str] = []
    for uc in brief.user_constraints:
        if uc.strip().lower().startswith("source_id:"):
            sid = uc.split(":", 1)[1].strip()
            if sid:
                override.append(sid)
        elif uc.strip().lower().startswith("source_ids:"):
            rest = uc.split(":", 1)[1].strip()
            for part in re.split(r"[;,]", rest):
                p = part.strip()
                if p:
                    override.append(p)
    if override:
        return override
    if retrieved is None:
        return []
    return sorted(set(retrieved))


def _infer_length(edge: TopologyEdge) -> float | None:
    return edge.length_m


def build_design_package(
    brief: ProjectBrief,
    calc_package: CalcPackage,
    *,
    retrieved_source_ids: list[str] | None = None,
    prev_design: DesignPackage | None = None,
) -> DesignPackage:
    active_ids = {c.calc_case_id for c in calc_package.cases}
    inferred_source_ids = _infer_source_ids(brief, retrieved_source_ids)

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
        systems_for_gen: list[str] = ["water_supply"]
    else:
        systems_for_gen = list(systems)

    for edge in brief.topology:
        sys_val_raw = systems_for_gen[0]
        sys_val = cast(Literal["water_supply", "drainage", "fire_protection"], sys_val_raw)
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

        # decide hydraulic status before recalc check
        hydraulic_status: Literal["verified", "not_verified", "recalc_required"]
        if not calc_ids or any(cid not in active_ids for cid in calc_ids):
            hydraulic_status = "not_verified"
        else:
            # check semantic consistency: if dn_mm mismatches any calc input diameter
            inconsistent = False
            for cc in calc_package.cases:
                if cc.calc_case_id in calc_ids:
                    d = cc.inputs.get("diameter_mm")
                    if d is not None and dn_mm is not None:
                        try:
                            if int(d) != int(dn_mm):
                                inconsistent = True
                        except (ValueError, TypeError):  # noqa: S110
                            pass
            if inconsistent:
                hydraulic_status = "recalc_required"
            else:
                hydraulic_status = "verified"

        # unresolved for slope when drainage and slope missing
        if sys_val == "drainage" and slope_val is None:
            unresolved.append(f"segment {sid}: missing slope for drainage")

        for u in seg_unresolved:
            if u not in unresolved:
                unresolved.append(u)

        seg = DesignPipeSegment(
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
        segments.append(seg)

    # recalc detection vs prev_design
    if prev_design is not None and segments:
        prev_map = {s.segment_id: s for s in prev_design.segments}
        for seg in segments:
            old = prev_map.get(seg.segment_id)
            if old is None:
                recalc_requests.append(
                    {
                        "reason": "topology_changed",
                        "fields": ["topology"],
                        "segment_id": seg.segment_id,
                        "suggested_formula": "velocity",
                    }
                )
                if seg.hydraulic_status == "verified":
                    seg.hydraulic_status = "recalc_required"
                continue
            fields: list[str] = []
            if old.dn_mm != seg.dn_mm:
                fields.extend(["diameter_mm", "velocity_mps"])
            if old.length_m != seg.length_m:
                fields.append("length_m")
            if old.slope != seg.slope:
                fields.append("slope")
            if old.material != seg.material:
                fields.append("material")
            if fields:
                uniq = sorted(set(fields))
                recalc_requests.append(
                    {
                        "reason": "hydraulic_input_changed",
                        "fields": uniq,
                        "segment_id": seg.segment_id,
                        "suggested_formula": "velocity" if "diameter_mm" in uniq else "head_loss_darcy_weisbach",
                    }
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

    pkg = DesignPackage(
        segments=segments,
        decisions=decisions,
        alternatives=alternatives,
        recalculation_requests=recalc_requests,
        unresolved_items=dedup_u,
    )
    return pkg


def run_design_agent(
    brief: ProjectBrief,
    calc_envelope: ArtifactEnvelope,
    *,
    retrieved_source_ids: list[str] | None = None,
    prev_design: DesignPackage | None = None,
    recalc_trigger_fields: set[str] | None = None,
) -> ArtifactEnvelope:
    _ = recalc_trigger_fields
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
            citation_ids=list(retrieved_source_ids or []),
            assumptions=[],
            warnings=["calc payload invalid"],
            status="blocked",
        )

    design_pkg = build_design_package(
        brief, calc_pkg, retrieved_source_ids=retrieved_source_ids, prev_design=prev_design
    )

    payload = design_pkg.model_dump()
    payload_hash = compute_payload_hash(payload)
    raw = f"{brief.project_id}:{calc_envelope.artifact_id}:{payload_hash}"
    artifact_id = f"design-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"
    revision = 2 if prev_design is not None else 1

    retrieved_set = set(retrieved_source_ids or [])
    has_invalid_source = False
    for seg in design_pkg.segments:
        for sid in seg.source_ids:
            if sid not in retrieved_set:
                has_invalid_source = True
                break
            if not sid.strip():
                has_invalid_source = True
                break

    has_not_verified = any(s.hydraulic_status == "not_verified" for s in design_pkg.segments)
    has_unresolved = len(design_pkg.unresolved_items) > 0

    warnings: list[str] = []
    errors: list[str] = []
    if has_invalid_source:
        errors.append("source_id not in retrieved set")
        warnings.append("source_id validation failed")
    if has_not_verified:
        warnings.append("hydraulic not_verified present")
    if has_unresolved:
        warnings.append("unresolved_items present")

    if has_invalid_source:
        status: str = "blocked"
    elif has_not_verified:
        status = "blocked"
    elif has_unresolved and any("missing" in u for u in design_pkg.unresolved_items):
        # missing required inputs -> missing_inputs
        # but if topology missing etc, treat as missing_inputs
        status = "missing_inputs"
    elif has_unresolved:
        status = "missing_inputs"
    else:
        status = "ready"

    # if recalc requests present, keep status ready unless other block
    if design_pkg.recalculation_requests and status == "ready":
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
        citation_ids=list(retrieved_source_ids or []),
        assumptions=[],
        warnings=warnings,
        status=status,  # type: ignore[arg-type]
    )
    return envelope
