"""基线对比工具（Step 10 CI 门禁）。

单一、显式阈值集：``THRESHOLDS`` 是本工程唯一权威阈值来源（任何阈值调整必须
修改此处并更新文档），PR 门禁（ci_gate.sh）只跑离线机制层，本工具对比机制层
新报告与已入库基线，违反阈值 exit 1。

SKIP 语义：新报告全部用例 SKIP（真实变体无 Key）→ 不参与基线比较，视为通过
（exit 0）。真实评测失败（有 Key 但调用出错）由 runner 以非零退出码表达，与本
工具无涉。

用法：
    # 生成基线（首次，或机制层指标有重大变化时人工更新）
    python scripts/compare_baseline.py --init data/eval/baseline.json

    # 对比最新报告与基线（PR 门禁 / 定时任务）
    python scripts/compare_baseline.py data/eval/baseline.json
    python scripts/compare_baseline.py data/eval/baseline.json --report-path <report.json>

    # 真实质量层独立基线（rag_p1，--variant real --judge 产物；需 Key，不入 PR CI）
    python scripts/compare_baseline.py --init data/eval/rag_p1_baseline.json --suite rag_p1
    python scripts/compare_baseline.py data/eval/rag_p1_baseline.json --suite rag_p1
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = REPO_ROOT / "data" / "eval" / "baseline.json"
DEFAULT_REPORT_DIR = REPO_ROOT / "data" / "eval" / "reports"

# 单一权威阈值集（单位：pt=百分点）
# quality_pass_rate_decline_pt 只作用于真实质量层 suite（rag_p1，独立基线文件），
# 机制层 suite（rag_p0/calc_p0）不含 quality_pass_rate 字段，不受该阈值影响。
THRESHOLDS: dict[str, float] = {
    "pass_rate_decline_pt": 2.0,      # 通过率相对基线下降 ≤2pt
    "citation_ok_decline_pt": 3.0,    # 引用正确率相对基线下降 ≤3pt
    "quality_pass_rate_decline_pt": 5.0,  # 质量通过率相对基线下降 ≤5pt（真实质量层用）
}


def _load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise TypeError(f"{path} 顶层必须是 JSON object")
    return payload


def _find_latest_report(report_dir: Path, suite_key: str) -> Path | None:
    """在 report_dir 中查找 suite 对应的最新报告（按文件名时间戳排序）。"""
    candidates = sorted(report_dir.glob(f"{suite_key}_*.json"))
    return candidates[-1] if candidates else None


def _compute_new_metrics(new_report: dict) -> tuple[float, float]:
    """从新报告计算 pass_rate 与 citation_ok_rate（优先用报告已汇总字段）。"""
    pass_rate = float(new_report.get("pass_rate", 0.0))
    citation_ok = float(new_report.get("citation_ok_rate", 0.0))
    cases = new_report.get("cases", [])
    non_skipped = [c for c in cases if not c.get("skipped", False)]
    if not cases or len(non_skipped) == len(cases):
        return pass_rate, citation_ok
    # 报告缺少汇总字段时按 cases 兜底计算
    passed = sum(1 for c in non_skipped if c.get("passed", False))
    pass_rate = (passed / len(non_skipped)) if non_skipped else 0.0

    def _cit_ok(c: dict) -> bool:
        # 与 runner.citation_ok_for_result 同语义（P1/Step 10 评审）：零引用对存在
        # expected_articles 的 RAG 用例计 citation failure；无需引用的用例不误伤。
        ctx = set(c.get("context_articles", []) or [])
        citations = c.get("actual_citations", []) or []
        if not citations:
            return not bool(c.get("expected_articles") or [])
        return all(x in ctx for x in citations)

    citation_ok = (sum(1 for c in non_skipped if _cit_ok(c)) / len(non_skipped)) if non_skipped else 0.0
    return pass_rate, citation_ok


def compare_suite(
    suite_key: str,
    baseline: dict,
    new_report: dict,
    thresholds: dict[str, float],
) -> dict:
    """对比单个 suite 的新报告与基线。

    Args:
        suite_key: suite 名（如 rag_p0）
        baseline: 基线文件中的 suites 字典（suite_key -> 基线指标）
        new_report: 新报告 dict
        thresholds: 阈值集（默认 THRESHOLDS）

    Returns:
        {"passed": bool, "reason": str, "deltas": {...}}
    """
    baseline_suite = baseline.get(suite_key)
    if not baseline_suite:
        return {"passed": True, "reason": f"suite {suite_key} 无基线记录，跳过", "deltas": {}}

    # SKIP 语义：全部用例 SKIP（真实变体无 Key）→ 不参与基线比较
    total = int(new_report.get("total", 0))
    skipped = int(new_report.get("skipped", 0))
    if total > 0 and skipped == total:
        return {
            "passed": True,
            "reason": f"suite {suite_key} 全部 SKIP，不参与基线比较",
            "deltas": {},
        }

    new_pass_rate, new_cit_ok = _compute_new_metrics(new_report)
    base_pass_rate = float(baseline_suite.get("pass_rate", 0.0))
    base_cit_ok = float(baseline_suite.get("citation_ok_rate", 0.0))

    pass_delta_pt = (base_pass_rate - new_pass_rate) * 100
    cit_delta_pt = (base_cit_ok - new_cit_ok) * 100

    # PASS_TO_PASS：基线中通过的用例不得在新报告中失败
    baseline_passed_ids = set(baseline_suite.get("passed_case_ids", []) or [])
    new_failed_ids = {
        c.get("case_id")
        for c in new_report.get("cases", [])
        if c.get("case_id") and not c.get("passed", False) and not c.get("skipped", False)
    }
    new_core_failures = sorted(baseline_passed_ids & new_failed_ids)

    violations: list[str] = []
    if pass_delta_pt > thresholds["pass_rate_decline_pt"]:
        violations.append(
            f"pass_rate 下降 {pass_delta_pt:.2f}pt（>{thresholds['pass_rate_decline_pt']}pt）"
        )
    if cit_delta_pt > thresholds["citation_ok_decline_pt"]:
        violations.append(
            f"citation_ok_rate 下降 {cit_delta_pt:.2f}pt（>{thresholds['citation_ok_decline_pt']}pt）"
        )
    if new_core_failures:
        violations.append(f"PASS_TO_PASS 违规：基线通过用例新失败 {new_core_failures}")

    # 真实质量层（rag_p1 等）：基线含 quality_pass_rate 且新报告真实判定过（judged>0）
    # 才比较质量通过率；无 Key 的全 SKIP 报告在更早分支已返回，不会到这里。
    new_judged = int(new_report.get("quality_judged_count", 0) or 0)
    base_quality = baseline_suite.get("quality_pass_rate")
    new_quality = new_report.get("quality_pass_rate")
    if base_quality is not None and new_quality is not None and new_judged > 0:
        quality_delta_pt = (float(base_quality) - float(new_quality)) * 100
        if quality_delta_pt > thresholds.get("quality_pass_rate_decline_pt", 5.0):
            violations.append(
                f"quality_pass_rate 下降 {quality_delta_pt:.2f}pt"
                f"（>{thresholds.get('quality_pass_rate_decline_pt', 5.0)}pt）"
            )

    return {
        "passed": not violations,
        "reason": "; ".join(violations),
        "deltas": {
            "suite": suite_key,
            "base_pass_rate": base_pass_rate,
            "new_pass_rate": new_pass_rate,
            "pass_delta_pt": round(pass_delta_pt, 3),
            "base_citation_ok_rate": base_cit_ok,
            "new_citation_ok_rate": new_cit_ok,
            "citation_delta_pt": round(cit_delta_pt, 3),
            "new_core_failures": new_core_failures,
        },
    }


def build_baseline(report_dir: Path, suite_keys: list[str]) -> dict:
    """从 report_dir 的最新报告中提取各 suite 基线指标。"""
    suites: dict[str, dict] = {}
    for key in suite_keys:
        latest = _find_latest_report(report_dir, key)
        if latest is None:
            continue
        report = _load_json(latest)
        pass_rate, citation_ok = _compute_new_metrics(report)
        entry: dict = {
            "input_path": report.get("input_path", key),
            "variant": report.get("variant", "mock"),
            "total": report.get("total", 0),
            "passed": report.get("passed", 0),
            "failed": report.get("failed", 0),
            "skipped": report.get("skipped", 0),
            "pass_rate": pass_rate,
            "citation_ok_rate": citation_ok,
            "avg_latency_ms": report.get("avg_latency_ms", 0.0),
            "retrieval_metrics": report.get("retrieval_metrics", {}),
            "passed_case_ids": [
                c.get("case_id")
                for c in report.get("cases", [])
                if c.get("case_id") and c.get("passed", False) and not c.get("skipped", False)
            ],
            "source_report": latest.name,
        }
        # 真实质量层 suite（rag_p1 --judge）：把质量判定指标带进独立基线（版本化，
        # 不覆盖 rag_p0/calc_p0 基线文件）。机制层报告无这些字段，跳过。
        for quality_key in ("quality_pass_rate", "quality_low_score_count", "quality_judged_count", "judge_dimension_averages"):
            if quality_key in report:
                entry[quality_key] = report.get(quality_key)
        suites[key] = entry
    return {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "thresholds": THRESHOLDS,
        "suites": suites,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Compare harness reports against baseline (Step 10 CI gate)")
    p.add_argument("baseline_path", nargs="?", default=str(DEFAULT_BASELINE), help="基线 JSON 路径")
    p.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR), help="报告目录")
    p.add_argument("--report-path", default=None, help="指定新报告 JSON（默认取 report-dir 下最新报告）")
    p.add_argument("--init", action="store_true", help="从最新报告生成基线（不比较，直接写出基线文件）")
    p.add_argument("--suite", action="append", default=None, help="只对比指定 suite（可多次）；默认全部")
    args = p.parse_args(argv)

    baseline_path = Path(args.baseline_path)
    report_dir = Path(args.report_dir)
    suite_keys = args.suite or ["rag_p0", "calc_p0"]

    if args.init:
        baseline = build_baseline(report_dir, suite_keys)
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps({"ok": True, "init": True, "baseline_path": str(baseline_path),
                          "suites": list(baseline["suites"].keys())}, ensure_ascii=False, indent=2))
        return 0

    if not baseline_path.exists():
        print(json.dumps({"ok": False, "error": f"基线文件不存在：{baseline_path}（先运行 --init）"},
                         ensure_ascii=False), file=sys.stderr)
        return 2

    baseline = _load_json(baseline_path)
    baseline_suites = baseline.get("suites", {})
    results = []
    failed_any = False
    for key in suite_keys:
        report_path = Path(args.report_path) if args.report_path else _find_latest_report(report_dir, key)
        if report_path is None or not report_path.exists():
            results.append({"suite": key, "passed": True, "reason": "无新报告，跳过", "deltas": {}})
            continue
        new_report = _load_json(report_path)
        result = compare_suite(key, baseline_suites, new_report, THRESHOLDS)
        if not result["passed"]:
            failed_any = True
        results.append({"suite": key, "report": str(report_path), **result})

    print(json.dumps({"ok": not failed_any, "baseline": str(baseline_path),
                      "results": results}, ensure_ascii=False, indent=2))
    return 1 if failed_any else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
