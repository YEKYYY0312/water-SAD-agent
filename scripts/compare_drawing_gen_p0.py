"""`drawing_gen_p0` 门禁对比：HEAD 报告 vs 入仓基线（P6-S1 Step 7）。

做法照 `compare_drawing_p0.py`，另加两条针对出图的硬门：

1. **用例级 PASS_TO_PASS**：基线通过的用例在 HEAD 失败，或从 HEAD 里消失 → 红灯；
2. **三项链路指标只许持平**：``dn_match_rate``（管径读回）、``readback_rate``（read_drawing 读回）、
   ``review_honesty_rate``（复审未检查 ≥1 且不说"均未发现问题"）任何下降都红灯——这三项
   不是观测值，是生成图"能被本仓库审查器读懂、且不被读成通过"的硬约束；
3. ``case_pass_rate`` 下降超过 ``--threshold``（默认 5pt）红灯；
4. **拒绝手改报告**：用例明细重算出的聚合与报告里写的不一致 → 红灯。

DWG 状态（依赖本机是否装有 LibreDWG）与生成耗时只作观测，不参与判定。

用法（ci_gate.sh 内）::

    _venv/bin/python scripts/compare_drawing_gen_p0.py \\
        --head data/eval/reports/drawing_gen_p0_head.json \\
        --base data/eval/drawing_gen_p0_main.json

退出码：0 通过 / 1 对比失败 / 2 输入错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from water_agent.harness.drawing_gen_eval import REPORT_SCHEMA_VERSION, aggregate_generation_metrics

HARD_METRICS = ("dn_match_rate", "readback_rate", "review_honesty_rate")
_RECOMPUTED = ("case_pass_rate", "dn_match_rate", "readback_rate", "review_honesty_rate", "sheets_checked", "pipe_runs_checked")


def _load(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise TypeError(f"{path} 不是 drawing_gen_eval 报告（缺 cases 列表）")
    return payload


def _tampered(report: dict) -> list[str]:
    recomputed = aggregate_generation_metrics(report["cases"])
    written = report.get("aggregate") or {}
    return [key for key in _RECOMPUTED if recomputed.get(key) != written.get(key)]


def compare_reports(head: dict, base: dict | None, *, threshold_pt: float = 5.0) -> dict:
    violations: list[str] = []
    if head.get("schema_version") != REPORT_SCHEMA_VERSION:
        violations.append(f"HEAD 报告 schema_version={head.get('schema_version')!r}，期望 {REPORT_SCHEMA_VERSION}")
    if tampered := _tampered(head):
        violations.append(f"HEAD 报告的聚合与用例明细不一致（疑似手改）：{tampered}")
    head_cases = {str(c.get("case_id")): c for c in head["cases"]}
    head_passed = sorted(cid for cid, c in head_cases.items() if c.get("passed"))
    if base is None:
        return {
            "passed": not violations,
            "reason": "; ".join(violations) or "无基线报告（首次运行）；请把本次报告固化为基线",
            "violations": violations,
            "head_passed_case_ids": head_passed,
        }
    if tampered := _tampered(base):
        violations.append(f"基线报告的聚合与用例明细不一致（疑似手改）：{tampered}")
    base_cases = {str(c.get("case_id")): c for c in base["cases"]}
    if missing := sorted(set(base_cases) - set(head_cases)):
        violations.append(f"基线用例在 HEAD 里消失：{missing}")
    if regressions := sorted(
        cid for cid, c in base_cases.items() if c.get("passed") and cid in head_cases and not head_cases[cid].get("passed")
    ):
        violations.append(f"PASS_TO_PASS 违规：基线通过用例新失败 {regressions}")
    head_agg, base_agg = head.get("aggregate") or {}, base.get("aggregate") or {}
    for key in HARD_METRICS:
        base_value, head_value = base_agg.get(key), head_agg.get(key)
        if base_value is not None and (head_value is None or float(head_value) < float(base_value)):
            violations.append(f"{key} 下降：{base_value} → {head_value}（硬门，只许持平）")
    base_rate, head_rate = base_agg.get("case_pass_rate"), head_agg.get("case_pass_rate")
    if base_rate is not None and head_rate is not None:
        drop = (float(base_rate) - float(head_rate)) * 100
        if drop > threshold_pt:
            violations.append(f"case_pass_rate 下降 {drop:.2f}pt（>{threshold_pt}pt）")
    return {
        "passed": not violations,
        "reason": "; ".join(violations),
        "violations": violations,
        "head_passed_case_ids": head_passed,
        "base_passed_case_ids": sorted(cid for cid, c in base_cases.items() if c.get("passed")),
        "observed": {
            "head_dwg_statuses": head_agg.get("dwg_statuses"),
            "head_max_generation_ms": head_agg.get("max_generation_ms"),
        },
        "real_cad_verified": head.get("real_cad_verified"),
        "gate_note": head.get("gate_note", ""),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare drawing_gen_p0 reports (HEAD vs base)")
    parser.add_argument("--head", required=True)
    parser.add_argument("--base", default=None)
    parser.add_argument("--threshold", type=float, default=5.0, help="case_pass_rate 下降阈值（百分点）")
    args = parser.parse_args(argv)
    head_path = Path(args.head)
    if not head_path.is_file():
        print(json.dumps({"ok": False, "error": f"HEAD 报告不存在: {head_path}（先跑 harness.drawing_gen_eval）"}, ensure_ascii=False), file=sys.stderr)
        return 2
    try:
        head = _load(head_path)
        base = _load(Path(args.base)) if args.base else None
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    result = compare_reports(head, base, threshold_pt=args.threshold)
    print(json.dumps({"ok": result["passed"], **result}, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
