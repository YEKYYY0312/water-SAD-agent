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

    # 只对比指定套件（默认 MECHANISM_SUITES 全部四条机制层套件）
    python scripts/compare_baseline.py data/eval/baseline.json --suite rag_p0 --suite calc_p0

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

# 机制层默认对比套件（--init 与对比共用；--suite 可覆盖）。
# P2 收口后 docgen_p0/material_p0 正式纳入基线（docs/design/11-docgen-materials.md §5）。
# governance_p0 早就在 baseline.json 里却不参与对比：删掉两条治理用例整条门禁仍绿
# （review 2026-09-30），故一并纳入。
# agentic_p0（mock-agentic，生产 AgenticSession 装配）同理：新增即入基线。
MECHANISM_SUITES: list[str] = [
    "rag_p0", "calc_p0", "docgen_p0", "material_p0", "governance_p0", "agentic_p0",
]

# 单一权威阈值集（单位：pt=百分点）
# quality_pass_rate_decline_pt 只作用于真实质量层 suite（rag_p1，独立基线文件），
# 机制层 suite（rag_p0/calc_p0/docgen_p0/material_p0）不含 quality_pass_rate 字段，
# 不受该阈值影响。
# citation_ok_rate 对机制层统一口径：calc/material 恒 1.0（无引用要求不误伤）；
# docgen_p0 基线为 0.2727（RAG 口径"引用⊆检索上下文"回显——docgen 用例无检索上下文，
# 有 citations_required 的用例按 runner.citation_ok_for_result 语义计失败，确定性可回归）。
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
        # 要对比却没有基线 = 无法判定是否回归，不能当通过（首次接入先 --init 再入仓）。
        return {
            "passed": False,
            "reason": f"suite {suite_key} 无基线记录（先 --init 生成并入仓）",
            "deltas": {},
        }

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
    # 用例消失/被跳过同样是回归：只看"新报告里存在且失败"的话，数据集删行或字段改名
    # （用例变成加载问题）都会静默通过——calc_p0 少了 4 条仍 100%（review 2026-09-30）。
    present_ids = {
        c.get("case_id")
        for c in new_report.get("cases", [])
        if c.get("case_id") and not c.get("skipped", False)
    }
    expected_ids = baseline_passed_ids | set(baseline_suite.get("case_ids", []) or [])
    vanished = sorted(expected_ids - present_ids)

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
    if vanished:
        violations.append(f"基线用例在新报告中缺失或被跳过 {vanished}")

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
            "vanished_cases": vanished,
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
            # 全部用例（含基线里本就失败的）：用例被删时据此报"缺失"
            "case_ids": [
                c.get("case_id")
                for c in report.get("cases", [])
                if c.get("case_id") and not c.get("skipped", False)
            ],
            "source_report": latest.name,
        }
        # 真实质量层 suite（rag_p1 --judge）：仅当报告真实判定过（judged>0）才把质量指标
        # 带进独立基线文件；机制层报告 judged=0，不携带（compare 侧也以 judged>0 为前置，
        # 与基线/阈值说明保持一致——机制层 baseline 只表达确定性通过率/引用口径）。
        if int(report.get("quality_judged_count", 0) or 0) > 0:
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
    suite_keys = args.suite or MECHANISM_SUITES
    if args.report_path and len(suite_keys) != 1:
        # 此前一份 --report-path 会被套用到每个套件，彼此错比
        print(json.dumps({"ok": False, "error": "--report-path 必须配且只配一个 --suite"},
                         ensure_ascii=False), file=sys.stderr)
        return 2

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
            # 基线里有这个套件、这次却没有报告 = runner 没跑成，不能当通过
            missing_ok = key not in baseline_suites
            if not missing_ok:
                failed_any = True
            results.append({
                "suite": key,
                "passed": missing_ok,
                "reason": "无新报告，跳过" if missing_ok else "缺少新报告：基线有该套件，本次未产出报告",
                "deltas": {},
            })
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
