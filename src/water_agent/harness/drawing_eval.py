"""`drawing_p0` 评测集 runner（P3 Step 22）。

跑 `tests/eval/drawing_p0` 的图纸审查 smoke 集：每条用例走完整审查子图
（`graphs/drawing_review.run_drawing_review`），按 gold 逐条判分，聚合
缺陷召回率 / 误报率，写结构化 JSON 报告（供 `scripts/compare_drawing_p0.py`
做 HEAD vs main 基线对比）。

判分口径（规格 `13-p3-drawing-review.md` §7.3）：

- 单条用例 pass/fail **精确匹配**：gold 缺陷按 `(rule_id, handle)` 全命中、
  零误报，且 status / summary / not_checked / missing_inputs 的 gold 期望
  全部满足；
- `defect_recall` / `false_positive_rate` 是**报告级观测指标**（度量在
  `harness/drawing_metrics.py`，分母是 gold 缺陷数），本 runner 不用它们
  替代逐条判分。

前置诚实性：套件元数据带 `real_samples=false` / `accepts_recall_gate=false`
（合成 smoke 集）。本 runner **不读取** gold 来调整执行（先跑管线后判分，
与 `HarnessRunner` 的评测标签纪律同源），报告里原样带出这两个标记，
防止下游把 smoke 通过误读为召回率达标。

用法::

    _venv/bin/python -m water_agent.harness.drawing_eval tests/eval/drawing_p0
    _venv/bin/python -m water_agent.harness.drawing_eval tests/eval/drawing_p0 \
        --report-path data/eval/reports/drawing_p0_head.json

退出码：0 全部通过；1 有用例失败；2 套件/用例加载问题。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from water_agent.drawing.dxf_reader import read_drawing
from water_agent.graphs.drawing_review import run_drawing_review
from water_agent.harness.drawing_metrics import (
    aggregate_drawing_metrics,
    case_drawing_metrics,
)
from water_agent.runtime.governance.pipeline import ToolPolicy

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_REPORT_DIR = REPO_ROOT / "data" / "eval" / "reports"

#: gold 里的 handle 必须能在该图抽取结果中找到 —— 找不到说明 gold 与图纸
#: 不同步（改了 case 没重新生成），判用例失败而不是悄悄降级成规则错配。
GOLD_HANDLE_OUT_OF_DRAWING = "gold_handle_not_in_drawing"


def load_suite(suite_dir: str | Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """加载套件元数据与全部用例（cases/*.json）。返回 (suite_meta, cases)。"""
    root = Path(suite_dir)
    suite_path = root / "suite.json"
    if not suite_path.is_file():
        raise FileNotFoundError(f"suite.json 不存在: {suite_path}")
    meta = json.loads(suite_path.read_text(encoding="utf-8"))
    cases_dir = root / "cases"
    if not cases_dir.is_dir():
        raise FileNotFoundError(f"cases/ 目录不存在: {cases_dir}")
    cases = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(cases_dir.glob("*.json"))
    ]
    if not cases:
        raise ValueError(f"cases/ 下没有用例（*.json）: {cases_dir}")
    return meta, cases


def _contains_all(needles: list[str], haystack: list[str]) -> list[str]:
    """返回 haystack 中缺失的 needle（保持 gold 顺序，便于报告定位）。"""
    joined = "\n".join(haystack)
    return [needle for needle in needles if needle not in joined]


def run_case(case: dict[str, Any], *, cases_dir: Path, policy: ToolPolicy) -> dict[str, Any]:
    """跑单条用例并判分。返回可 JSON 序列化的用例结果。"""
    drawing_id = str(case["drawing"]).removesuffix(".dxf")
    drawing_path = cases_dir / str(case["drawing"])

    result: dict[str, Any] = {
        "case_id": case["case_id"],
        "drawing": case["drawing"],
        "description": case.get("description", ""),
        "passed": False,
        "failure_reasons": [],
        "expected_status": case.get("expected_status", "succeeded"),
        "actual_status": "",
        "gold_defects": case.get("defects", []),
        "reported_findings": [],
        "pipes_reviewed": 0,
    }
    reasons: list[str] = result["failure_reasons"]

    if not drawing_path.is_file():
        reasons.append(f"图纸文件缺失: {drawing_path.name}")
        return result

    report = run_drawing_review(drawing_id, root=cases_dir, policy=policy)
    result["actual_status"] = str(report.get("status", ""))
    result["pipes_reviewed"] = int(report.get("pipes_reviewed", 0))
    result["summary"] = report.get("summary", "")
    result["reported_findings"] = [
        {"rule_id": f.get("rule_id", ""), "handle": f.get("handle", "")}
        for f in report.get("findings", [])
    ]
    result["reported_rendered"] = list(report.get("rendered_findings", []))
    result["not_checked"] = list(report.get("not_checked", []))
    result["missing_inputs"] = list(report.get("missing_inputs", []))
    result["report_text"] = report.get("report_text", "")

    metrics = case_drawing_metrics(
        case.get("defects", []),
        result["reported_findings"],
    )
    result["defect_metrics"] = metrics

    # 期望 1：状态（succeeded = 审查对象成功抽取；failed = 解析失败收口）。
    if result["actual_status"] != result["expected_status"]:
        reasons.append(
            f"status 不符: 期望 {result['expected_status']}, 实际 {result['actual_status']}"
            f"（extraction_errors={report.get('extraction_errors', [])}）"
        )

    # 期望 2：缺陷逐条精确匹配（gold handle 必须真实存在于图中）。
    if metrics["case_passed"] is not True:
        reasons.append(
            "缺陷判定不符: gold="
            f"{[(d['rule_id'], d['handle']) for d in case.get('defects', [])]}, "
            f"reported={[(f['rule_id'], f['handle']) for f in result['reported_findings']]}"
        )

    if result["expected_status"] == "succeeded":
        gold_handles = [str(d["handle"]) for d in case.get("defects", []) if d.get("handle")]
        if gold_handles:
            extraction = read_drawing(drawing_id, root=cases_dir, policy=policy)
            entity_handles = {e.handle for e in extraction.entities}
            missing = [h for h in gold_handles if h not in entity_handles]
            if missing:
                reasons.append(
                    f"{GOLD_HANDLE_OUT_OF_DRAWING}: {missing} —— gold 与图纸不同步，"
                    "请重跑 tests/eval/drawing_p0/build_cases.py"
                )

    # 期望 3：summary / not_checked / missing_inputs 的 gold 逐条出现。
    summary = [str(result["summary"])]
    for missing_needle in _contains_all(
        list(case.get("expected_summary_contains", [])), summary
    ):
        reasons.append(f"summary 缺少期望片段: {missing_needle!r}")
    for missing_needle in _contains_all(
        list(case.get("expected_not_checked_contains", [])), result["not_checked"]
    ):
        reasons.append(
            f"not_checked 缺少期望项: {missing_needle!r}（未检查项必须如实进报告）"
        )
    for missing_needle in _contains_all(
        list(case.get("expected_missing_inputs_contains", [])), result["missing_inputs"]
    ):
        reasons.append(f"missing_inputs 缺少期望项: {missing_needle!r}")

    result["passed"] = not reasons
    return result


def run_suite(
    suite_dir: str | Path,
    *,
    report_dir: str | Path = DEFAULT_REPORT_DIR,
    report_path: str | Path | None = None,
) -> tuple[dict[str, Any], Path]:
    """跑完整套件，返回 (报告 dict, 报告路径)。"""
    meta, cases = load_suite(suite_dir)
    cases_dir = Path(suite_dir) / "cases"
    policy = ToolPolicy(allowed_read_dirs=frozenset({cases_dir.resolve()}))

    case_results = [run_case(case, cases_dir=cases_dir, policy=policy) for case in cases]
    aggregate = aggregate_drawing_metrics(
        [case["defect_metrics"] for case in case_results]
    )

    failed = sum(1 for c in case_results if not c["passed"])
    payload: dict[str, Any] = {
        "suite": meta.get("suite", Path(suite_dir).name),
        "suite_dir": str(suite_dir),
        "real_samples": bool(meta.get("real_samples", False)),
        "accepts_recall_gate": bool(meta.get("accepts_recall_gate", False)),
        "created_at": datetime.now(UTC).isoformat(),
        "total": len(case_results),
        "passed": len(case_results) - failed,
        "failed": failed,
        "case_pass_rate": aggregate.get("case_pass_rate"),
        "defect_recall": aggregate.get("defect_recall"),
        "false_positive_rate": aggregate.get("false_positive_rate"),
        "aggregate": aggregate,
        "cases": case_results,
        # 门禁警示字段：compare_drawing_p0.py 据此拒绝把本套件当真实召回率证据。
        "gate_note": (
            "合成 smoke 集：仅守护链路回归，不构成真实缺陷召回率验收证据"
            if not meta.get("accepts_recall_gate", False)
            else ""
        ),
    }

    out_path = Path(report_path) if report_path else None
    if out_path is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        report_dir = Path(report_dir)
        report_dir.mkdir(parents=True, exist_ok=True)
        out_path = report_dir / f"{payload['suite']}_{stamp}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return payload, out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the drawing_p0 smoke eval suite")
    parser.add_argument("suite_dir", help="评测集目录（含 suite.json 与 cases/）")
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    parser.add_argument("--report-path", default=None, help="显式报告路径（门禁用固定名便于 diff）")
    args = parser.parse_args(argv)

    try:
        payload, report_path = run_suite(
            args.suite_dir, report_dir=args.report_dir, report_path=args.report_path
        )
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    summary = {
        "ok": payload["failed"] == 0,
        "suite": payload["suite"],
        "real_samples": payload["real_samples"],
        "accepts_recall_gate": payload["accepts_recall_gate"],
        "total": payload["total"],
        "passed": payload["passed"],
        "failed": payload["failed"],
        "defect_recall": payload["defect_recall"],
        "false_positive_rate": payload["false_positive_rate"],
        "gate_note": payload["gate_note"],
        "report_path": str(report_path),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if payload["failed"] == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
