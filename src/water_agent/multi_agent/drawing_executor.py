"""Step 31 drawing executor — DrawingPlan → staging DXF + roundtrip verify (narrow adapter)."""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Any

from water_agent.multi_agent.contracts import (
    ArtifactEnvelope,
    DrawingPackage,
    DrawingPlan,
    compute_payload_hash,
)

_FILE_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
DXF_VERSION = "R2010"
_SCHEMA_VERSION = "p4.5-2026-09-10"

# P3 serializes mapped metre lengths to six decimal places.  One micrometre
# therefore absorbs DXF floating-point/unit-conversion noise without accepting
# a meaningful engineering length change.
ROUNDTRIP_LENGTH_ABS_TOL_M = 1e-6
_FILE_HASH_CHUNK_BYTES = 1024 * 1024


def compute_file_sha256(path: str | Path) -> str:
    """Return the observed SHA-256 of a staged/final file without exposing its path."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(_FILE_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def render_drawing_plan_to_staging(plan: DrawingPlan, *, staging_dir: str, file_id: str) -> str:
    """Render DrawingPlan to staging DXF via ezdxf. Returns real path string."""
    if not _FILE_ID_RE.match(file_id):
        raise ValueError(f"file_id must match {_FILE_ID_RE.pattern!r}, got {file_id!r}")
    staging_path = Path(staging_dir)
    staging_path.mkdir(parents=True, exist_ok=True)
    real_path = staging_path / f"{file_id}.dxf"

    try:
        import ezdxf
    except ImportError as exc:
        raise RuntimeError(f"ezdxf not available: {exc}") from exc

    doc = ezdxf.new(DXF_VERSION)
    doc.header["$INSUNITS"] = int(plan.insunits)
    layers_needed = {e.layer for e in plan.entities}
    for layer_name in layers_needed:
        if layer_name not in doc.layers:
            try:
                doc.layers.add(layer_name)
            except Exception:  # noqa: BLE001, S110
                pass

    msp = doc.modelspace()
    for ent in plan.entities:
        layer = ent.layer
        kind = ent.kind
        pts = ent.points
        txt = ent.text
        if kind == "line":
            if len(pts) >= 2:
                msp.add_line((float(pts[0][0]), float(pts[0][1])), (float(pts[1][0]), float(pts[1][1])), dxfattribs={"layer": layer})
            elif len(pts) == 1:
                msp.add_line((float(pts[0][0]), float(pts[0][1])), (float(pts[0][0]) + 1, float(pts[0][1])), dxfattribs={"layer": layer})
        elif kind == "polyline":
            if len(pts) >= 2:
                msp.add_lwpolyline([(float(x), float(y)) for x, y in pts], dxfattribs={"layer": layer})
            elif len(pts) == 1:
                msp.add_line((float(pts[0][0]), float(pts[0][1])), (float(pts[0][0]) + 1, float(pts[0][1])), dxfattribs={"layer": layer})
        elif kind == "text":
            pos = (float(pts[0][0]), float(pts[0][1])) if pts else (0.0, 0.0)
            t = msp.add_text(txt or "", dxfattribs={"layer": layer, "height": 2.5})
            t.set_placement(pos)
        elif kind == "mtext":
            pos = (float(pts[0][0]), float(pts[0][1])) if pts else (0.0, 0.0)
            mt = msp.add_mtext(txt or "", dxfattribs={"layer": layer})
            mt.set_location(pos)
        else:
            raise ValueError(f"unsupported kind {kind!r}")

    doc.saveas(str(real_path))
    return str(real_path)


def verify_roundtrip(staged_path: str, plan: DrawingPlan) -> dict[str, Any]:
    """Verify staged DXF against DrawingPlan via read_drawing→map_pipes→review_drawing."""
    from pathlib import Path as _P

    from water_agent.drawing.dxf_reader import read_drawing
    from water_agent.drawing.mapping import map_pipes, parse_dn, parse_material
    from water_agent.drawing.rules import review_drawing
    from water_agent.runtime.governance.pipeline import ToolPolicy

    staged = _P(staged_path)
    if not staged.is_file():
        return {
            "roundtrip_segment_recall": 0.0,
            "mismatches": [],
            "total_segments": 0,
            "recalled_segments": 0,
            "extraction_ok": False,
            "insunits_match": False,
            "extracted_entity_count": 0,
            "p3_pipeline_executed": False,
            "unit_known": False,
            "mapped_lengths_m": [],
            "review_summary": "",
            "errors": ["staged_file_missing"],
        }
    try:
        with staged.open("rb") as handle:
            if not handle.read(1):
                raise ValueError("DXF file is empty")
    except OSError as exc:
        return {
            "roundtrip_segment_recall": 0.0,
            "mismatches": [],
            "total_segments": 0,
            "recalled_segments": 0,
            "extraction_ok": False,
            "insunits_match": False,
            "extracted_entity_count": 0,
            "p3_pipeline_executed": False,
            "unit_known": False,
            "mapped_lengths_m": [],
            "review_summary": "",
            "errors": [f"staged_file_unreadable:{type(exc).__name__}"],
        }
    except ValueError as exc:
        return {
            "roundtrip_segment_recall": 0.0,
            "mismatches": [],
            "total_segments": 0,
            "recalled_segments": 0,
            "extraction_ok": False,
            "insunits_match": False,
            "extracted_entity_count": 0,
            "p3_pipeline_executed": False,
            "unit_known": False,
            "mapped_lengths_m": [],
            "review_summary": "",
            "errors": [f"staged_file_invalid:{exc}"],
        }
    root = staged.parent
    drawing_id = staged.stem

    policy = ToolPolicy(allowed_read_dirs=frozenset({root.resolve()}))
    extraction = read_drawing(drawing_id, root=root, policy=policy)
    mapping = map_pipes(extraction) if extraction is not None else None
    review = review_drawing(mapping) if mapping is not None else None

    expected_by_seg: dict[str, dict[str, Any]] = {}
    for ent in plan.entities:
        if ent.segment_id is None:
            continue
        if ent.kind not in ("text", "mtext"):
            continue
        seg = ent.segment_id
        if seg in expected_by_seg:
            continue
        exp_layer = ent.layer
        expected_by_seg[seg] = {
            "dn_mm": ent.dn_mm,
            "material": ent.material,
            "system": ent.system,
            "layer": exp_layer,
            "text": ent.text or "",
            "entity_id": ent.entity_id,
            "length_m": ent.length_m,
            "length_entity_id": ent.entity_id,
        }

    for ent in plan.entities:
        if ent.segment_id is None:
            continue
        seg = ent.segment_id
        if seg not in expected_by_seg:
            expected_by_seg[seg] = {
                "dn_mm": ent.dn_mm,
                "material": ent.material,
                "system": ent.system,
                "layer": ent.layer,
                "text": "",
                "entity_id": ent.entity_id,
                "length_m": ent.length_m,
                "length_entity_id": ent.entity_id,
            }
        elif ent.kind in ("line", "polyline"):
            expected_by_seg[seg]["length_m"] = ent.length_m
            expected_by_seg[seg]["length_entity_id"] = ent.entity_id

    extracted_texts: list[str] = []
    extracted_layers: list[str] = []
    if extraction is not None:
        for e in extraction.entities:
            if e.text:
                extracted_texts.append(e.text)
                extracted_layers.append(e.layer)

    text_segment_by_handle: dict[str, str] = {}
    expected_segment_ids = set(expected_by_seg)
    if extraction is not None:
        for entity in extraction.entities:
            if not entity.text:
                continue
            tokens = set(entity.text.split())
            matches = sorted(expected_segment_ids & tokens)
            if len(matches) == 1:
                text_segment_by_handle[entity.handle] = matches[0]

    mapped_by_segment: dict[str, list[Any]] = {}
    unmatched_pipes: list[Any] = []
    if mapping is not None:
        for pipe in mapping.pipes:
            candidates = {
                text_segment_by_handle[handle]
                for handle in pipe.evidence_handles
                if handle in text_segment_by_handle
            }
            if len(candidates) == 1:
                mapped_by_segment.setdefault(next(iter(candidates)), []).append(pipe)
            else:
                unmatched_pipes.append(pipe)

    unmatched_segments = {
        segment_id
        for segment_id in expected_by_seg
        if segment_id not in mapped_by_segment
    }
    for layer in sorted({pipe.layer for pipe in unmatched_pipes}):
        layer_pipes = [pipe for pipe in unmatched_pipes if pipe.layer == layer]
        layer_segments = [
            segment_id
            for segment_id in sorted(unmatched_segments)
            if expected_by_seg[segment_id].get("layer") == layer
        ]
        if len(layer_pipes) == 1 and len(layer_segments) == 1:
            segment_id = layer_segments[0]
            mapped_by_segment[segment_id] = layer_pipes
            unmatched_segments.remove(segment_id)

    mismatches: list[dict[str, Any]] = []
    recalled = 0
    total = len(expected_by_seg)
    unit_known = bool(mapping.unit_known) if mapping is not None else False
    mapped_lengths_m = [pipe.length_m for pipe in mapping.pipes] if mapping is not None else []

    for seg_id, exp in expected_by_seg.items():
        found = False
        actual_dn = None
        actual_mat = None
        actual_sys = None
        actual_text = None
        for txt, layer in zip(extracted_texts, extracted_layers):
            if seg_id not in txt:
                continue
            actual_dn = parse_dn(txt)
            expected_material = exp.get("material")
            actual_mat = (
                expected_material
                if isinstance(expected_material, str) and expected_material in txt
                else parse_material(txt)
            )
            low = txt.lower()
            if "fire_protection" in low:
                actual_sys = "fire_protection"
            elif "water_supply" in low:
                actual_sys = "water_supply"
            elif "drainage" in low:
                actual_sys = "drainage"
            actual_text = txt
            found = True
            break

        if not found:
            for field in ("dn_mm", "material", "system", "segment"):
                mismatches.append(
                    {
                        "entity_id": exp["entity_id"],
                        "segment_id": seg_id,
                        "field": field,
                        "expected": exp.get(field, exp.get("text", "")),
                        "actual": None,
                    }
                )
            continue

        has_mismatch = False
        for field, key in [("DN", "dn_mm"), ("material", "material"), ("system", "system")]:
            exp_val = exp.get(key)
            act_val = actual_dn if key == "dn_mm" else (actual_mat if key == "material" else actual_sys)
            if exp_val is None and act_val is None:
                continue
            if exp_val != act_val:
                has_mismatch = True
                mismatches.append(
                    {
                        "entity_id": exp["entity_id"],
                        "segment_id": seg_id,
                        "field": field,
                        "expected": exp_val,
                        "actual": act_val,
                    }
                )
        if exp.get("layer") is not None:
            actual_layer = None
            for txt, layer in zip(extracted_texts, extracted_layers):
                if txt == actual_text:
                    actual_layer = layer
                    break
            if actual_layer is not None and actual_layer != exp["layer"]:
                has_mismatch = True
                mismatches.append(
                    {
                        "entity_id": exp["entity_id"],
                        "segment_id": seg_id,
                        "field": "layer",
                        "expected": exp["layer"],
                        "actual": actual_layer,
                    }
                )
        expected_length = exp.get("length_m")
        if unit_known and isinstance(expected_length, (int, float)):
            mapped_pipes = mapped_by_segment.get(seg_id, [])
            actual_lengths = [
                pipe.length_m
                for pipe in mapped_pipes
                if pipe.length_m is not None
            ]
            actual_length = sum(actual_lengths) if actual_lengths else None
            if actual_length is None or not math.isclose(
                float(actual_length),
                float(expected_length),
                rel_tol=0.0,
                abs_tol=ROUNDTRIP_LENGTH_ABS_TOL_M,
            ):
                has_mismatch = True
                mismatches.append(
                    {
                        "entity_id": exp.get("length_entity_id", exp["entity_id"]),
                        "segment_id": seg_id,
                        "field": "length_m",
                        "expected": float(expected_length),
                        "actual": None if actual_length is None else float(actual_length),
                    }
                )
        if not has_mismatch:
            recalled += 1

    roundtrip_segment_recall = (recalled / total) if total > 0 else 0.0
    insunits_ok = extraction.insunits == plan.insunits if extraction is not None else False
    entity_count_match = len(extraction.entities) if extraction is not None else 0

    roundtrip_review: dict[str, Any] = {
        "roundtrip_segment_recall": roundtrip_segment_recall,
        "mismatches": mismatches,
        "total_segments": total,
        "recalled_segments": recalled,
        "extraction_ok": bool(extraction.ok) if extraction is not None else False,
        "insunits_match": insunits_ok,
        "extracted_entity_count": entity_count_match,
        "p3_pipeline_executed": extraction is not None and mapping is not None and review is not None,
        "unit_known": unit_known,
        "mapped_lengths_m": mapped_lengths_m,
        "review_summary": review.summary() if review is not None else "",
        # P3 审查的结构化结论原样保留：此前只存 summary() 一句话，findings / not_checked /
        # pipes_reviewed 全部丢掉，多 Agent 审查对 P3 查出的缺陷视而不见（review 2026-09-30）。
        "pipes_reviewed": review.pipes_reviewed if review is not None else 0,
        "water_supply_pipes": review.water_supply_pipes if review is not None else 0,
        "p3_findings": [finding.to_dict() for finding in review.findings] if review is not None else [],
        "p3_not_checked": list(review.not_checked) if review is not None else [],
        "errors": list(extraction.errors) if extraction is not None else [],
    }

    return roundtrip_review


def _package_envelope(
    plan_envelope: ArtifactEnvelope,
    *,
    staged_file_id: str | None,
    staged_content_sha256: str | None,
    preview_status: str,
    roundtrip_review: dict[str, Any] | None,
    staged_file_verified: bool,
    p3_pipeline_executed: bool,
    unresolved_items: list[str],
    status: str,
) -> ArtifactEnvelope:
    package = DrawingPackage(
        plan_artifact_id=plan_envelope.artifact_id,
        preview_status=preview_status,
        staged_file_id=staged_file_id,
        staged_content_sha256=staged_content_sha256,
        final_file_id=None,
        roundtrip_review=roundtrip_review,
        staged_file_verified=staged_file_verified,
        p3_pipeline_executed=p3_pipeline_executed,
        unresolved_items=unresolved_items,
    )
    payload = package.model_dump()
    payload_hash = compute_payload_hash(payload)
    raw = f"{plan_envelope.artifact_id}:{staged_file_id or ''}:{payload_hash}"
    artifact_id = f"dwpkg-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"
    return ArtifactEnvelope(
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
        status=status,  # type: ignore[arg-type]
    )


def verify_staged_drawing_package(
    plan_envelope: ArtifactEnvelope,
    *,
    staged_path: str | Path,
    staged_file_id: str,
) -> ArtifactEnvelope:
    """Validate an already-rendered staging file and package only observed evidence.

    ``staged_path`` is an assembly-time argument and is deliberately absent
    from every Agent-visible schema and returned artifact payload.
    """
    if not _FILE_ID_RE.match(staged_file_id):
        raise ValueError(f"staged_file_id must match {_FILE_ID_RE.pattern!r}, got {staged_file_id!r}")
    plan = DrawingPlan.model_validate(plan_envelope.payload)
    blocked_reasons: list[str] = []
    if plan_envelope.status != "ready":
        blocked_reasons.append(f"drawing plan status is {plan_envelope.status}")
    if plan.integration_conflicts:
        blocked_reasons.append("drawing plan has integration_conflicts")
    if plan.missing_inputs:
        blocked_reasons.extend(plan.missing_inputs)
    if blocked_reasons:
        return _package_envelope(
            plan_envelope,
            staged_file_id=None,
            staged_content_sha256=None,
            preview_status="blocked",
            roundtrip_review=None,
            staged_file_verified=False,
            p3_pipeline_executed=False,
            unresolved_items=blocked_reasons,
            status="blocked" if plan.integration_conflicts or plan_envelope.status == "blocked" else "missing_inputs",
        )

    staged = Path(staged_path)
    digest_before: str | None = None
    if staged.is_file():
        try:
            digest_before = compute_file_sha256(staged)
        except OSError:
            digest_before = None

    report = verify_roundtrip(str(staged), plan)
    unresolved: list[str] = []
    if not staged.is_file():
        unresolved.append("staged DXF file does not exist")
    if not report.get("extraction_ok"):
        unresolved.append("staged DXF could not be read by P3 read_drawing")
    if not report.get("p3_pipeline_executed"):
        unresolved.append("P3 read_drawing → map_pipes → review_drawing pipeline did not complete")
    if not report.get("insunits_match"):
        unresolved.append("staged DXF $INSUNITS does not match DrawingPlan")
    if report.get("unit_known") is False:
        unresolved.append("staged DXF unit is unknown; mapped length_m remains None")
    recall = report.get("roundtrip_segment_recall")
    if not isinstance(recall, (int, float)) or isinstance(recall, bool) or float(recall) != 1.0:
        unresolved.append("roundtrip_segment_recall is not exactly 1.0")
    if report.get("mismatches"):
        unresolved.append("staged DXF differs from DrawingPlan")

    digest_after: str | None = None
    if staged.is_file():
        try:
            digest_after = compute_file_sha256(staged)
        except OSError:
            unresolved.append("staged DXF content hash could not be computed")
    if digest_before is not None and digest_after is not None and digest_before != digest_after:
        unresolved.append("staged DXF changed during verification")
    if digest_after is None:
        unresolved.append("staged DXF content hash is unavailable")

    return _package_envelope(
        plan_envelope,
        staged_file_id=staged_file_id if not unresolved else None,
        staged_content_sha256=digest_after if not unresolved else None,
        preview_status="staged" if not unresolved else "blocked",
        roundtrip_review=report,
        staged_file_verified=not unresolved,
        p3_pipeline_executed=bool(report.get("p3_pipeline_executed")),
        unresolved_items=unresolved,
        status="ready" if not unresolved else "blocked",
    )


def stage_drawing_package(
    plan_envelope: ArtifactEnvelope,
    *,
    staging_dir: str | Path,
    file_id: str,
) -> ArtifactEnvelope:
    """Render then verify a P0 DXF in one controlled assembly-time operation."""
    plan = DrawingPlan.model_validate(plan_envelope.payload)
    if plan_envelope.status != "ready" or plan.integration_conflicts or plan.missing_inputs:
        return verify_staged_drawing_package(
            plan_envelope,
            staged_path=Path(staging_dir) / f"{file_id}.dxf",
            staged_file_id=file_id,
        )
    try:
        staged_path = render_drawing_plan_to_staging(
            plan, staging_dir=str(staging_dir), file_id=file_id
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return _package_envelope(
            plan_envelope,
            staged_file_id=None,
            staged_content_sha256=None,
            preview_status="failed",
            roundtrip_review=None,
            staged_file_verified=False,
            p3_pipeline_executed=False,
            unresolved_items=[f"staging render failed: {type(exc).__name__}"],
            status="failed",
        )
    return verify_staged_drawing_package(
        plan_envelope, staged_path=staged_path, staged_file_id=file_id
    )
