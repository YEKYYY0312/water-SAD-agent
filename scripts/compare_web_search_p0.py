"""`web_search_p0` 门禁对比：HEAD 报告 vs 入仓基线（P7-S12，规格 P7-S12-A13）。

做法照 `compare_external_content_p0.py`：

1. **拒绝人工编写的报告**：``generated_by`` / ``suite`` / ``report_format`` 不对，``mechanism`` 不是 ``present``，或用例明细
   重算出的指标与报告里写的不一致 → 红灯；
2. **数据集**：数据集摘要变了 → 红灯（S12 Step 1 冻结，有意修改必须同时重新固化基线并写明原因）；
3. **硬指标**：HEAD 必须全部达到 harness 的目标值，且不比基线差；
4. **用例级**：基线用例在 HEAD 里消失 → 红灯；基线通过的用例在 HEAD 失败 → 红灯（PASS_TO_PASS）。

用法（ci_gate.sh 内）::

    _venv/bin/python scripts/compare_web_search_p0.py \\
        --head data/eval/reports/web_search_p0_head.json \\
        --base data/eval/web_search_p0_main.json

退出码：0 通过 / 1 对比失败 / 2 输入错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from water_agent.harness.web_search_eval import (
    GENERATED_BY,
    HARD_TARGETS,
    REPORT_FORMAT,
    SUITE,
    hard_violations,
    metrics_from_case_dicts,
)

#: 计数类与误拦类硬指标"越小越好"，其余（速率）"越大越好"。
_LOWER_IS_BETTER = frozenset(
    {
        "instruction_bearing_leak_count",
        "query_false_block_rate",
        "citation_integrity_violations",
        "key_leak_count",
        "error_leak_count",
        "harness_error_count",
    }
)


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise TypeError(f"{path} 不是 web_search_eval 报告（缺 cases 列表）")
    return payload


def _provenance(report: dict[str, Any], label: str) -> list[str]:
    problems: list[str] = []
    if report.get("generated_by") != GENERATED_BY or report.get("suite") != SUITE:
        problems.append(f"{label} 报告不是 {GENERATED_BY} 生成的 {SUITE} 报告")
    if report.get("report_format") != REPORT_FORMAT:
        problems.append(f"{label} 报告 report_format={report.get('report_format')!r}，期望 {REPORT_FORMAT}")
    if report.get("mechanism") != "present":
        problems.append(f"{label} 报告 mechanism={report.get('mechanism')!r}：机制不在时的报告不能作为门禁对象")
    recomputed = metrics_from_case_dicts(report["cases"])
    written = report.get("metrics") or {}
    if tampered := sorted(key for key in recomputed if recomputed[key] != written.get(key)):
        problems.append(f"{label} 报告的指标与用例明细不一致（疑似手改）：{tampered}")
    passed = sum(1 for case in report["cases"] if case.get("passed"))
    if report.get("passed") != passed or report.get("total") != len(report["cases"]):
        problems.append(f"{label} 报告的 passed/total 与用例明细不一致（疑似手改）")
    return problems


def _worse(name: str, base: Any, head: Any) -> bool:
    if base is None:
        return False
    if head is None:
        return True
    return float(head) > float(base) if name in _LOWER_IS_BETTER else float(head) < float(base)


def compare_reports(head: dict[str, Any], base: dict[str, Any] | None) -> dict[str, Any]:
    violations = _provenance(head, "HEAD")
    head_metrics = head.get("metrics") or {}
    violations.extend(f"HEAD 硬指标不达标：{item}" for item in hard_violations(head_metrics))
    head_cases = {str(case.get("id")): case for case in head["cases"]}
    head_passed = sorted(cid for cid, case in head_cases.items() if case.get("passed"))
    if base is None:
        return {
            "passed": not violations,
            "reason": "; ".join(violations) or "无基线报告（首次运行）；请把本次报告固化为基线",
            "violations": violations,
            "head_passed_case_ids": head_passed,
        }
    violations.extend(_provenance(base, "基线"))
    if head.get("dataset_sha256") != base.get("dataset_sha256"):
        violations.append("数据集与基线不同（web_search_p0.jsonl 已冻结；有意修改须同时重新固化基线并写明原因）")
    base_cases = {str(case.get("id")): case for case in base["cases"]}
    if missing := sorted(set(base_cases) - set(head_cases)):
        violations.append(f"基线用例在 HEAD 里消失：{missing}")
    if regressions := sorted(
        cid
        for cid, case in base_cases.items()
        if case.get("passed") and cid in head_cases and not head_cases[cid].get("passed")
    ):
        violations.append(f"PASS_TO_PASS 违规：基线通过用例新失败 {regressions}")
    base_metrics = base.get("metrics") or {}
    for name in HARD_TARGETS:
        if _worse(name, base_metrics.get(name), head_metrics.get(name)):
            violations.append(f"{name} 变差：{base_metrics.get(name)} → {head_metrics.get(name)}")
    return {
        "passed": not violations,
        "reason": "; ".join(violations),
        "violations": violations,
        "head_passed_case_ids": head_passed,
        "base_passed_case_ids": sorted(cid for cid, case in base_cases.items() if case.get("passed")),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare web_search_p0 reports (HEAD vs base)")
    parser.add_argument("--head", required=True)
    parser.add_argument("--base", default=None)
    args = parser.parse_args(argv)
    head_path = Path(args.head)
    if not head_path.is_file():
        print(
            json.dumps(
                {"ok": False, "error": f"HEAD 报告不存在: {head_path}（先跑 harness.web_search_eval）"},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    try:
        head = _load(head_path)
        base = _load(Path(args.base)) if args.base else None
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    result = compare_reports(head, base)
    print(json.dumps({"ok": result["passed"], **result}, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
