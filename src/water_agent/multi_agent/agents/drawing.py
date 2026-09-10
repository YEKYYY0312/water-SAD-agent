"""Step 31 drawing agent — deterministic, offline, no hydraulic recompute."""

from __future__ import annotations

import hashlib
import math
import re
from typing import Literal

from water_agent.multi_agent.contracts import (
    ArtifactEnvelope,
    CalcPackage,
    DesignPackage,
    DrawingPackage,
    DrawingPlan,
    PlannedEntity,
    compute_payload_hash,
)

_SCHEMA_VERSION = "p4.5-2026-09-10"
_DRAWING_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
_FILE_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")

_SYSTEM_LAYER: dict[str, str] = {
    "water_supply": "WS-PIPE",
    "drainage": "DR-PIPE",
    "fire_protection": "FP-PIPE",
}


def _validate_drawing_id(drawing_id: str) -> None:
    if not _DRAWING_ID_RE.match(drawing_id):
        raise ValueError(f"drawing_id must match {_DRAWING_ID_RE.pattern!r}, got {drawing_id!r}")


def _finite_check_points(points: list[tuple[float, float]]) -> None:
    for x, y in points:
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError(f"points must be finite, got {(x, y)!r}")


def _entity_id(drawing_id: str, segment_id: str, kind: str, idx: int) -> str:
    raw = f"{drawing_id}:{segment_id}:{kind}:{idx}"
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{drawing_id}-{h}"


def _detect_conflicts(
    design_pkg: DesignPackage,
    calc_pkg: CalcPackage,
) -> list[str]:
    conflicts: list[str] = []
    active_calc_ids = {c.calc_case_id for c in calc_pkg.cases}
    calc_dn_map: dict[str, int | None] = {}
    for c in calc_pkg.cases:
        v = c.inputs.get("diameter_mm")
        if v is None:
            v = c.inputs.get("diameter")
        dn = None
        if isinstance(v, (int, float)) and math.isfinite(float(v)):
            try:
                dn = int(v)
            except Exception:  # noqa: BLE001
                dn = None
        calc_dn_map[c.calc_case_id] = dn

    for seg in design_pkg.segments:
        for cid in seg.calc_case_ids:
            if cid not in active_calc_ids:
                conflicts.append(f"segment {seg.segment_id} references superseded calc_case_id {cid}")
            else:
                calc_dn = calc_dn_map.get(cid)
                if calc_dn is not None and seg.dn_mm is not None and calc_dn != seg.dn_mm:
                    conflicts.append(
                        f"DN mismatch: design {seg.dn_mm} vs calc {calc_dn} for case {cid} segment {seg.segment_id}"
                    )

    if design_pkg.recalculation_requests:
        conflicts.append(f"design has recalculation_requests: {len(design_pkg.recalculation_requests)} pending")

    return conflicts


def build_drawing_plan(
    design_package: DesignPackage,
    calc_package: CalcPackage,
    *,
    drawing_id: str = "drawing-001",
    insunits: int = 4,
    drawing_class_hint: Literal["schematic", "project_coordinates"] | None = None,
    source_design_artifact_id: str = "design-unknown",
    source_calc_artifact_id: str = "calc-unknown",
) -> DrawingPlan:
    _validate_drawing_id(drawing_id)
    if insunits == 0:
        raise ValueError("insunits must be explicit, 0 (unitless) is forbidden")

    drawing_class: Literal["schematic", "project_coordinates"] = "schematic"
    if drawing_class_hint == "project_coordinates":
        drawing_class = "schematic"
    elif drawing_class_hint == "schematic":
        drawing_class = "schematic"
    else:
        drawing_class = "schematic"

    conflicts = _detect_conflicts(design_package, calc_package)

    entities: list[PlannedEntity] = []

    if drawing_class == "schematic":
        entities.append(
            PlannedEntity(
                entity_id=_entity_id(drawing_id, "_label", "mtext", 0),
                segment_id=None,
                kind="mtext",
                layer="0",
                points=[(0.0, -10.0)],
                text="SCHEMATIC — NOT FOR CONSTRUCTION",
            )
        )

    if conflicts:
        pass
    else:
        for idx, seg in enumerate(design_package.segments):
            layer = _SYSTEM_LAYER.get(seg.system, "0")
            base_y = float(idx * 20.0)
            length = float(seg.length_m) if seg.length_m is not None and math.isfinite(seg.length_m) and seg.length_m > 0 else 10.0
            line_points = [(0.0, base_y), (length, base_y)]
            _finite_check_points(line_points)
            entities.append(
                PlannedEntity(
                    entity_id=_entity_id(drawing_id, seg.segment_id, "line", idx * 2),
                    segment_id=seg.segment_id,
                    kind="line",
                    layer=layer,
                    points=line_points,  # type: ignore[arg-type]
                    text="",
                )
            )
            dn_str = f"DN{seg.dn_mm}" if seg.dn_mm is not None else "DN?"
            mat_str = seg.material if seg.material is not None else "?"
            sys_str = seg.system
            label = f"{seg.segment_id} {dn_str} {mat_str} {sys_str}"
            if seg.hydraulic_status == "not_verified":
                label += " (NOT VERIFIED)"
            text_points = [(length / 2.0, base_y + 3.0)]
            _finite_check_points(text_points)
            entities.append(
                PlannedEntity(
                    entity_id=_entity_id(drawing_id, seg.segment_id, "text", idx * 2 + 1),
                    segment_id=seg.segment_id,
                    kind="text",
                    layer=layer,
                    points=text_points,  # type: ignore[arg-type]
                    text=label,
                )
            )

    plan = DrawingPlan(
        drawing_id=drawing_id,
        drawing_class=drawing_class,
        insunits=insunits,
        entities=entities,
        source_design_artifact_id=source_design_artifact_id,
        source_calc_artifact_id=source_calc_artifact_id,
        integration_conflicts=conflicts,
    )
    return plan


def run_drawing_agent(
    design_envelope: ArtifactEnvelope,
    calc_envelope: ArtifactEnvelope,
    *,
    drawing_id: str | None = None,
    insunits: int = 4,
    drawing_class_hint: Literal["schematic", "project_coordinates"] | None = None,
) -> ArtifactEnvelope:
    design_pkg = DesignPackage.model_validate(design_envelope.payload)
    calc_pkg = CalcPackage.model_validate(calc_envelope.payload)

    did = drawing_id or f"drawing-{design_envelope.artifact_id[:8]}"
    did = did.lower().replace(".", "-").replace(" ", "-")

    plan = build_drawing_plan(
        design_pkg,
        calc_pkg,
        drawing_id=did,
        insunits=insunits,
        drawing_class_hint=drawing_class_hint,
        source_design_artifact_id=design_envelope.artifact_id,
        source_calc_artifact_id=calc_envelope.artifact_id,
    )

    payload = plan.model_dump()
    payload_hash = compute_payload_hash(payload)
    raw = f"{design_envelope.artifact_id}:{calc_envelope.artifact_id}:{did}:{insunits}:{payload_hash}"
    artifact_id = f"plan-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"

    if plan.integration_conflicts:
        status: Literal["ready", "missing_inputs", "blocked", "failed"] = "blocked"  # type: ignore[assignment]
    else:
        status = "ready"  # type: ignore[assignment]

    envelope = ArtifactEnvelope(
        artifact_id=artifact_id,
        artifact_type="drawing_plan",
        schema_version=_SCHEMA_VERSION,
        producer="drawing",
        revision=1,
        parent_artifact_ids=[design_envelope.artifact_id, calc_envelope.artifact_id],
        payload_hash=payload_hash,
        payload=payload,
        citation_ids=[],
        assumptions=[],
        warnings=[],
        status=status,  # type: ignore[arg-type]
    )
    return envelope


def build_drawing_package(
    plan_envelope: ArtifactEnvelope,
    *,
    staged_file_id: str | None = None,
    preview_status: str = "staged",
) -> ArtifactEnvelope:
    plan = DrawingPlan.model_validate(plan_envelope.payload)
    _ = plan
    fid = staged_file_id
    if fid is not None and not _FILE_ID_RE.match(fid):
        raise ValueError(f"staged_file_id must match {_FILE_ID_RE.pattern!r}, got {fid!r}")

    pkg = DrawingPackage(
        plan_artifact_id=plan_envelope.artifact_id,
        preview_status=preview_status,
        staged_file_id=fid,
        final_file_id=None,
        roundtrip_review=None,
    )
    payload = pkg.model_dump()
    payload_hash = compute_payload_hash(payload)
    raw = f"{plan_envelope.artifact_id}:{fid or ''}:{payload_hash}"
    artifact_id = f"dwpkg-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"

    envelope = ArtifactEnvelope(
        artifact_id=artifact_id,
        artifact_type="drawing_package",
        schema_version=_SCHEMA_VERSION,
        producer="drawing",
        revision=1,
        parent_artifact_ids=[plan_envelope.artifact_id],
        payload_hash=payload_hash,
        payload=payload,
        citation_ids=[],
        assumptions=[],
        warnings=[],
        status="ready",  # type: ignore[arg-type]
    )
    return envelope
