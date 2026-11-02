"""`drawing_gen_p0` 评测集 runner（P6-S1 Step 7）。

跑 `tests/eval/drawing_gen_p0`：每条合法用例走完整出图服务
（`drawing/generation_service.generate_drawing_set`，产物写进临时导出目录），
再**独立**读回判分；非法用例只判错误码与字段路径。结构化报告供
`scripts/compare_drawing_gen_p0.py` 做 HEAD vs 入仓基线对比。

判分口径（执行 prompt §2.3）：每条合法用例都要满足——

- 流速：与用例里按 ``v = Q / (π d² / 4)`` 独立算出的期望值一致，建议区间判定一致；
- 三张图：``read_drawing`` 能读回；``WS-*`` 标准图层都在，期望图层都有图元；图元数不低于下限；
  ``WS-PIPE-*`` 上的管段数与期望一致，且**全部**管径读回一致（服务端按 handle 核对 +
  这里按管径多重集再核一次）；
- 系统图：立管段数 = 层数（每层一根，读回的竖直主管段逐一计数）、支管组数 = 层数；
- S50 复审跑通：报告以固定计数头开头，未检查 ≥1，结论里不出现"审查通过 / 0 缺陷 / 均未发现问题"。

本 runner 不读 gold 来调整执行（先跑后判），报告带 ``real_cad_verified=false``：
机制层证据，不是"可在 AutoCAD / 天正中编辑"的证据。

用法::

    _venv/bin/python -m water_agent.harness.drawing_gen_eval tests/eval/drawing_gen_p0
    _venv/bin/python -m water_agent.harness.drawing_gen_eval tests/eval/drawing_gen_p0 \\
        --report-path data/eval/reports/drawing_gen_p0_head.json

退出码：0 全部通过；1 有用例失败；2 套件/用例加载问题。
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from water_agent.drawing.building_rules import load_building_rules
from water_agent.drawing.generation_models import (
    PIPE_LAYERS,
    STANDARD_LAYERS,
    GenerationError,
    SheetPlan,
    parse_generation_request,
    resolve_riser_position,
)
from water_agent.drawing.generation_service import GenerationResult, generate_drawing_set
from water_agent.drawing.generator import read_back
from water_agent.drawing.mapping import map_pipes
from water_agent.drawing.models import DrawingEntityKind

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_REPORT_DIR = REPO_ROOT / "data" / "eval" / "reports"
REPORT_SCHEMA_VERSION = 1
_PIPE_KINDS = (DrawingEntityKind.LINE, DrawingEntityKind.POLYLINE)
_REVIEW_HEADER = "【DXF 程序化审查报告】"


def load_suite(suite_dir: str | Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    root = Path(suite_dir)
    suite_path = root / "suite.json"
    if not suite_path.is_file():
        raise FileNotFoundError(f"suite.json 不存在: {suite_path}")
    meta = json.loads(suite_path.read_text(encoding="utf-8"))
    cases_dir = root / "cases"
    if not cases_dir.is_dir():
        raise FileNotFoundError(f"cases/ 目录不存在: {cases_dir}")
    cases = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(cases_dir.glob("*.json"))]
    if not cases:
        raise ValueError(f"cases/ 下没有用例（*.json）: {cases_dir}")
    for case in cases:
        if not {"case_id", "request", "expect"} <= set(case):
            raise ValueError(f"用例缺字段（case_id / request / expect）: {case.get('case_id')!r}")
    return meta, cases


def _invalid_case(case: dict[str, Any], reasons: list[str]) -> dict[str, Any]:
    expect = case["expect"]
    try:
        request = parse_generation_request(case["request"])
        resolve_riser_position(request, load_building_rules())
    except GenerationError as exc:
        locs = sorted(field["loc"] for field in exc.fields)
        if exc.code != expect["error_code"]:
            reasons.append(f"错误码 {exc.code} ≠ 期望 {expect['error_code']}")
        if locs != sorted(expect["error_fields"]):
            reasons.append(f"错误字段 {locs} ≠ 期望 {sorted(expect['error_fields'])}")
        return {"error_code": exc.code, "error_fields": locs}
    reasons.append(f"期望 {expect['error_code']}，但参数被接受了")
    return {}


def _sheet_checks(result: GenerationResult, case: dict[str, Any], reasons: list[str]) -> dict[str, Any]:
    expect = case["expect"]
    plans: dict[str, SheetPlan] = {sheet.sheet_id: sheet for sheet in result.plan.sheets()}
    sheets: dict[str, Any] = {}
    # 以期望的图纸清单为准：只遍历实际产出会让"少生成一张图"整体通过（review 2026-09-30）。
    generated = {sheet.sheet_id for sheet in result.sheets}
    not_generated = sorted(set(expect["sheets"]) - generated)
    if not_generated:
        reasons.append(f"期望的图纸未生成：{not_generated}")
    for sheet in result.sheets:
        want = expect["sheets"][sheet.sheet_id]
        plan = plans[sheet.sheet_id]
        data = (result.directory / sheet.filename).read_bytes()
        extraction = read_back(data)
        used_layers = sorted({e.layer for e in extraction.entities})
        pipes = [e for e in extraction.entities if e.layer in PIPE_LAYERS and e.kind in _PIPE_KINDS]
        read_dn = Counter(p.dn_mm for p in map_pipes(extraction).pipes if p.layer in PIPE_LAYERS)
        planned_dn = Counter(run.dn.mm for run in plan.pipes)
        info = {
            "readback_ok": extraction.ok and not extraction.truncated,
            "entity_count": len(extraction.entities),
            "pipe_runs": len(pipes),
            "planned_runs": len(plan.pipes),
            "dn_matched": sheet.readback.dn_matched,
            "used_layers": used_layers,
            "review_pipes_reviewed": sheet.review.pipes_reviewed,
            "review_not_checked": sheet.review.not_checked,
            "review_summary": sheet.review.summary,
            "dwg_status": None if sheet.dwg is None else sheet.dwg.status,
        }
        sheets[sheet.sheet_id] = info
        label = sheet.filename
        if not info["readback_ok"]:
            reasons.append(f"{label}：read_drawing 读回失败 {extraction.errors}")
        if missing := sorted(set(STANDARD_LAYERS) - set(extraction.layers)):
            reasons.append(f"{label}：图层表缺 {missing}")
        if missing := sorted(set(want["layers"]) - set(used_layers)):
            reasons.append(f"{label}：期望图层上没有图元 {missing}")
        if info["entity_count"] < want["min_entities"]:
            reasons.append(f"{label}：图元 {info['entity_count']} < 下限 {want['min_entities']}")
        if info["pipe_runs"] != want["pipe_runs"]:
            reasons.append(f"{label}：管段 {info['pipe_runs']} ≠ 期望 {want['pipe_runs']}")
        if expect["all_pipe_dn_matched"] and (
            sheet.readback.dn_matched != len(plan.pipes) or read_dn != planned_dn
        ):
            reasons.append(f"{label}：管径读回不一致（按 handle {sheet.readback.dn_matched}/{len(plan.pipes)}）")
        review_text = sheet.review.text
        if not review_text.startswith(_REVIEW_HEADER):
            reasons.append(f"{label}：S50 复审没有跑出报告")
        if sheet.review.not_checked < expect["review"]["min_not_checked"]:
            reasons.append(f"{label}：未检查 {sheet.review.not_checked} 项，少于期望")
        for claim in expect["review"]["summary_must_not_contain"]:
            if claim in sheet.review.summary or claim in review_text:
                reasons.append(f"{label}：复审里出现了不该有的说法 {claim!r}")
        if sheet.sheet_id == "schematic":
            risers = [
                e for e in pipes
                if e.layer == "WS-PIPE-MAIN" and len(e.points) == 2 and e.points[0][0] == e.points[1][0] == 0.0
            ]
            info["riser_segments_read_back"] = len(risers)
            if len(risers) != expect["riser_segments"] or len(result.plan.schematic.riser_run_ids) != expect["riser_segments"]:
                reasons.append(f"系统图：立管段 {len(risers)} ≠ 层数 {expect['riser_segments']}（每层一根）")
            if result.plan.schematic.branch_groups != expect["branch_groups"]:
                reasons.append(f"系统图：支管组 {result.plan.schematic.branch_groups} ≠ 层数 {expect['branch_groups']}")
    return sheets


def _valid_case(case: dict[str, Any], export_root: Path, reasons: list[str]) -> dict[str, Any]:
    expect = case["expect"]
    try:
        result = generate_drawing_set(case["request"], export_root=export_root, owner_user_id="drawing-gen-eval")
    except GenerationError as exc:
        reasons.append(f"生成失败 {exc.code}: {exc.message}")
        return {}
    plan = result.plan
    if (plan.riser_position, plan.riser_position_defaulted) != (
        expect["riser_position"],
        expect["riser_position_defaulted"],
    ):
        reasons.append(f"立管位置 {plan.riser_position}/{plan.riser_position_defaulted} 与期望不符")
    main, branch = plan.calculations
    tolerance = expect["velocity_tolerance"]
    main_v = main.velocity_mps
    if main_v is None or not math.isclose(main_v, expect["main_velocity_mps"], abs_tol=tolerance):
        reasons.append(f"主管流速 {main_v} ≠ 期望 {expect['main_velocity_mps']}")
    if main.advisory != expect["main_advisory"]:
        reasons.append(f"主管建议区间判定 {main.advisory} ≠ 期望 {expect['main_advisory']}")
    if expect["branch_velocity_mps"] is None:
        if branch.ok:
            reasons.append("支管没给流量却算出了流速（不得补默认值）")
    elif branch.velocity_mps is None or not math.isclose(branch.velocity_mps, expect["branch_velocity_mps"], abs_tol=tolerance):
        reasons.append(f"支管流速 {branch.velocity_mps} ≠ 期望 {expect['branch_velocity_mps']}")
    for needle in expect["warnings_contains"]:
        if not any(needle in warning for warning in plan.warnings):
            reasons.append(f"warnings 缺少 {needle!r}")
    for needle in expect["missing_inputs_contains"]:
        if not any(needle in item for item in plan.missing_inputs):
            reasons.append(f"missing_inputs 缺少 {needle!r}")
    sheets = _sheet_checks(result, case, reasons)
    return {
        "generation_ms": result.timings_ms.get("total"),
        "main_velocity_mps": main_v,
        "main_advisory": main.advisory,
        "warnings": list(plan.warnings),
        "missing_inputs": list(plan.missing_inputs),
        "sheets": sheets,
    }


def run_case(case: dict[str, Any], *, export_root: Path) -> dict[str, Any]:
    reasons: list[str] = []
    expect_ok = bool(case["expect"]["ok"])
    detail = _valid_case(case, export_root, reasons) if expect_ok else _invalid_case(case, reasons)
    return {
        "case_id": case["case_id"],
        "description": case.get("description", ""),
        "expect_ok": expect_ok,
        "passed": not reasons,
        "failure_reasons": reasons,
        **detail,
    }


def aggregate_generation_metrics(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """用例明细 → 聚合指标。比较器用它重算，拒绝聚合与明细对不上的报告。"""

    sheets = [sheet for case in cases for sheet in case.get("sheets", {}).values()]
    planned = sum(sheet["planned_runs"] for sheet in sheets)
    matched = sum(sheet["dn_matched"] for sheet in sheets)
    honest = sum(
        1 for sheet in sheets if sheet["review_not_checked"] >= 1 and "均未发现问题" not in sheet["review_summary"]
    )
    timings = [case["generation_ms"] for case in cases if case.get("generation_ms") is not None]
    return {
        "case_pass_rate": round(sum(1 for c in cases if c["passed"]) / len(cases), 4) if cases else None,
        "dn_match_rate": round(matched / planned, 4) if planned else None,
        "readback_rate": round(sum(1 for s in sheets if s["readback_ok"]) / len(sheets), 4) if sheets else None,
        "review_honesty_rate": round(honest / len(sheets), 4) if sheets else None,
        "sheets_checked": len(sheets),
        "pipe_runs_checked": planned,
        "max_generation_ms": max(timings) if timings else None,
        "dwg_statuses": dict(Counter(s["dwg_status"] for s in sheets if s["dwg_status"] is not None)),
    }


def run_suite(
    suite_dir: str | Path,
    *,
    report_dir: str | Path = DEFAULT_REPORT_DIR,
    report_path: str | Path | None = None,
) -> tuple[dict[str, Any], Path]:
    meta, cases = load_suite(suite_dir)
    export_root = Path(tempfile.mkdtemp(prefix="drawing-gen-eval-"))
    try:
        results = [run_case(case, export_root=export_root) for case in cases]
    finally:
        shutil.rmtree(export_root, ignore_errors=True)
    failed = sum(1 for case in results if not case["passed"])
    payload: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "suite": meta.get("suite", Path(suite_dir).name),
        "suite_dir": str(suite_dir),
        "real_cad_verified": bool(meta.get("real_cad_verified", False)),
        "created_at": datetime.now(UTC).isoformat(),
        "total": len(results),
        "passed": len(results) - failed,
        "failed": failed,
        "aggregate": aggregate_generation_metrics(results),
        "cases": results,
        "gate_note": "机制层评测：证明出图链路可读回、管径可匹配、复审如实；不构成可在 AutoCAD/天正中编辑的证据",
    }
    out_path = Path(report_path) if report_path else None
    if out_path is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        Path(report_dir).mkdir(parents=True, exist_ok=True)
        out_path = Path(report_dir) / f"{payload['suite']}_{stamp}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload, out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the drawing_gen_p0 eval suite")
    parser.add_argument("suite_dir", help="评测集目录（含 suite.json 与 cases/）")
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    parser.add_argument("--report-path", default=None, help="显式报告路径（门禁用固定名便于 diff）")
    args = parser.parse_args(argv)
    try:
        payload, report_path = run_suite(args.suite_dir, report_dir=args.report_dir, report_path=args.report_path)
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    summary = {
        "ok": payload["failed"] == 0,
        "suite": payload["suite"],
        "real_cad_verified": payload["real_cad_verified"],
        "total": payload["total"],
        "passed": payload["passed"],
        "failed": payload["failed"],
        **{key: payload["aggregate"][key] for key in ("dn_match_rate", "readback_rate", "review_honesty_rate", "max_generation_ms", "dwg_statuses")},
        "failures": {case["case_id"]: case["failure_reasons"] for case in payload["cases"] if not case["passed"]},
        "gate_note": payload["gate_note"],
        "report_path": str(report_path),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if payload["failed"] == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
