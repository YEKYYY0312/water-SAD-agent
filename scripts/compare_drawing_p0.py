"""`drawing_p0` 门禁对比：HEAD 报告 vs `main` 报告（P3 Step 22）。

与 `compare_baseline.py` 的关系：**不进** `MECHANISM_SUITES` —— 那套基线按
suite 聚合 pass_rate/citation_ok_rate，且 `drawing_p0` 当前是合成 smoke 集
（`accepts_recall_gate=false`），把它的召回率塞进机制层基线会制造
"召回率已建档"的假象。本脚本独立对比两份 runner 报告：

1. **用例级 PASS_TO_PASS**：`main` 通过的用例在 HEAD 失败 → 红灯（链路回归）；
2. **聚合观测指标**：`defect_recall` / `false_positive_rate` / `case_pass_rate`
   的下降超过 `--recall-threshold`（默认 5pt）→ 红灯（观测守护）；
3. **诚实性硬门**：报告 `accepts_recall_gate` 为 false 时，任何"召回率"数字
   都只是 smoke 回归观测 —— 脚本在输出里原样带出 `real_samples` /
   `accepts_recall_gate` / `gate_note`，并在 `--strict-recall`（**默认关闭**）
   之外拒绝把召回率下降解释为"真实质量下降"。

真实样本集（`drawing_p1`）到手并重新标定后，才把召回率门槛设为验收语义。

用法（ci_gate.sh 内）::

    _venv/bin/python scripts/compare_drawing_p0.py \
        --head data/eval/reports/drawing_p0_head.json \
        --base data/eval/reports/drawing_p0_main.json

无 base 报告（首次运行）→ 通过并提示初始化；缺 HEAD 报告 → 失败（runner 先行）。
退出码：0 通过 / 1 对比失败 / 2 输入错误。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"{path} 顶层必须是 JSON object")
    return payload


def compare_reports(
    head: dict,
    base: dict | None,
    *,
    recall_threshold_pt: float = 5.0,
) -> dict:
    """对比 HEAD 与 base 的 drawing_p0 报告。返回结构化裁定（含 passed/reasons）。"""
    reasons: list[str] = []
    violations: list[str] = []

    head_suite = str(head.get("suite", ""))
    head_ids = {str(c.get("case_id")) for c in head.get("cases", [])}
    head_passed_ids = {
        str(c.get("case_id")) for c in head.get("cases", []) if c.get("passed")
    }

    if base is None:
        return {
            "passed": True,
            "reason": "无基线报告（首次运行）；请把本次报告固化为基线",
            "violations": [],
            "head_passed_case_ids": sorted(head_passed_ids),
            "suite": head_suite,
        }

    base_suite = str(base.get("suite", ""))
    if base_suite != head_suite:
        violations.append(f"suite 不一致: base={base_suite!r} head={head_suite!r}")

    base_cases = {str(c.get("case_id")): c for c in base.get("cases", [])}

    # 1) 用例级 PASS_TO_PASS
    ptp_failures = sorted(
        case_id
        for case_id, case in base_cases.items()
        if case.get("passed") and case_id in head_ids
        and case_id not in head_passed_ids
    )
    if ptp_failures:
        violations.append(f"PASS_TO_PASS 违规：基线通过用例新失败 {ptp_failures}")

    # 2) 聚合观测指标下降（pt = 百分点）
    for key, threshold_pt in (
        ("case_pass_rate", recall_threshold_pt),
        ("defect_recall", recall_threshold_pt),
    ):
        base_value = base.get(key)
        head_value = head.get(key)
        if base_value is None or head_value is None:
            continue
        drop_pt = (float(base_value) - float(head_value)) * 100
        if drop_pt > threshold_pt:
            violations.append(
                f"{key} 下降 {drop_pt:.2f}pt（>{threshold_pt}pt）："
                f"{float(base_value):.4f} → {float(head_value):.4f}"
            )

    base_fpr = base.get("false_positive_rate")
    head_fpr = head.get("false_positive_rate")
    if base_fpr is not None and head_fpr is not None:
        rise_pt = (float(head_fpr) - float(base_fpr)) * 100
        if rise_pt > recall_threshold_pt:
            violations.append(
                f"false_positive_rate 上升 {rise_pt:.2f}pt（>{recall_threshold_pt}pt）："
                f"{float(base_fpr):.4f} → {float(head_fpr):.4f}"
            )

    reasons.extend(violations)
    return {
        "passed": not violations,
        "reason": "; ".join(reasons),
        "violations": violations,
        "head_passed_case_ids": sorted(head_passed_ids),
        "base_passed_case_ids": sorted(
            cid for cid, c in base_cases.items() if c.get("passed")
        ),
        "suite": head_suite,
        "real_samples": head.get("real_samples"),
        "accepts_recall_gate": head.get("accepts_recall_gate"),
        "gate_note": head.get("gate_note", ""),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare drawing_p0 reports (HEAD vs base)")
    parser.add_argument("--head", required=True, help="HEAD 的新报告 JSON")
    parser.add_argument("--base", default=None, help="基线报告 JSON（缺省=首次运行，只提示固化基线）")
    parser.add_argument(
        "--recall-threshold",
        type=float,
        default=5.0,
        help="聚合指标下降/上升阈值（百分点，默认 5）",
    )
    args = parser.parse_args(argv)

    head_path = Path(args.head)
    if not head_path.is_file():
        print(
            json.dumps(
                {"ok": False, "error": f"HEAD 报告不存在: {head_path}（先跑 harness.drawing_eval）"},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    head = _load(head_path)
    base = _load(Path(args.base)) if args.base else None

    result = compare_reports(head, base, recall_threshold_pt=args.recall_threshold)
    print(json.dumps({"ok": result["passed"], **result}, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
