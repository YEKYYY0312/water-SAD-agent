"""Step 31 drawing executor — DrawingPlan → staging DXF + roundtrip verify (narrow adapter)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from water_agent.multi_agent.contracts import DrawingPlan

_FILE_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
DXF_VERSION = "R2010"


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
        txt = ent.text or ""
        exp_dn = parse_dn(txt)
        exp_mat = parse_material(txt)
        exp_sys = None
        low = txt.lower()
        if "fire_protection" in low:
            exp_sys = "fire_protection"
        elif "water_supply" in low:
            exp_sys = "water_supply"
        elif "drainage" in low:
            exp_sys = "drainage"
        exp_layer = ent.layer
        expected_by_seg[seg] = {"dn_mm": exp_dn, "material": exp_mat, "system": exp_sys, "layer": exp_layer, "text": txt, "entity_id": ent.entity_id}

    for ent in plan.entities:
        if ent.segment_id is None:
            continue
        seg = ent.segment_id
        if seg not in expected_by_seg:
            expected_by_seg[seg] = {"dn_mm": None, "material": None, "system": None, "layer": ent.layer, "text": "", "entity_id": ent.entity_id}

    extracted_texts: list[str] = []
    extracted_layers: list[str] = []
    if extraction is not None:
        for e in extraction.entities:
            if e.text:
                extracted_texts.append(e.text)
                extracted_layers.append(e.layer)

    mismatches: list[dict[str, Any]] = []
    recalled = 0
    total = len(expected_by_seg)

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
            actual_mat = parse_material(txt)
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
        if not has_mismatch:
            recalled += 1

    roundtrip_segment_recall = (recalled / total) if total > 0 else 1.0
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
        "review_summary": review.summary() if review is not None else "",
    }

    return roundtrip_review
