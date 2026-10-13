"""`retrieval_colloquial_p0` 门禁对比：HEAD 报告 vs 入仓基线（P7-S10，规格 P7-S10-A07）。

只比 **text 模式**（关键词检索，不要求任何 Key，CI 离线可跑）；hybrid 模式需要百炼 Key，不进门禁。做法照
`compare_skill_p0.py`：

1. **拒绝人工编写的报告**：``generated_by`` / ``suite`` / ``report_format`` 不对，``mode`` 不是 text，
   ``mechanism`` 不是 present（absent 是 S10 Step 1 在没有 text 模式的代码上跑的"改之前"报告），``status``
   不是 ok，发生了 embedding 调用，或用例明细重算出的指标与报告里写的不一致 → 红灯；
2. **数据集与语料**：数据集摘要变了 → 红灯（S10 Step 1 冻结；有意修改必须同时重新固化基线）；受控语料摘要变了
   → 红灯（检索结果随语料变，要重新固化基线并写明原因）；
3. **指标不降**：两条管线（retriever / local_rerank）的 Recall@1/3/5、MRR 与各问法类别的 Recall@5 都不得低于
   基线；用例执行出错数必须为 0；
4. **用例级**：基线用例在 HEAD 里消失 → 红灯；基线前 5 命中的用例在 HEAD 未命中 → 红灯（PASS_TO_PASS）。

**不比较**检索到的条款列表本身：TriviumDB 对近乎同分的条目排序不固定（并行求和的浮点抖动，见
``docs/design/p7-step-10-decision.md``），gold 的名次与全部指标连跑 10 次稳定，非 gold 的尾部条目会变。

用法（ci_gate.sh 内）::

    _venv/bin/python scripts/compare_retrieval_colloquial_p0.py \\
        --head data/eval/reports/retrieval_colloquial_p0_head.json \\
        --base data/eval/retrieval_colloquial_p0_main.json

退出码：0 通过 / 1 对比失败 / 2 输入错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from water_agent.harness.retrieval_mode_eval import (
    GENERATED_BY,
    PIPELINES,
    REPORT_FORMAT,
    metrics_from_case_dicts,
)

SUITE = "retrieval_colloquial_p0"
_RATE_KEYS = ("recall_at_1", "recall_at_3", "recall_at_5", "mrr")


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise TypeError(f"{path} 不是 retrieval_mode_eval 报告（缺 cases 列表）")
    return payload


def _provenance(report: dict[str, Any], label: str) -> list[str]:
    problems: list[str] = []
    if report.get("generated_by") != GENERATED_BY or report.get("suite") != SUITE:
        problems.append(f"{label} 报告不是 {GENERATED_BY} 生成的 {SUITE} 报告")
    if report.get("report_format") != REPORT_FORMAT:
        problems.append(f"{label} 报告 report_format={report.get('report_format')!r}，期望 {REPORT_FORMAT}")
    if report.get("mode") != "text":
        problems.append(f"{label} 报告 mode={report.get('mode')!r}，门禁只比 text 模式")
    if report.get("mechanism") != "present":
        problems.append(f"{label} 报告 mechanism={report.get('mechanism')!r}，门禁只认 present")
    if report.get("status") != "ok":
        problems.append(f"{label} 报告 status={report.get('status')!r}")
    if report.get("embedding_calls") != 0:
        problems.append(f"{label} 报告 embedding_calls={report.get('embedding_calls')!r}：关键词检索不得调用 embedding")
    recomputed = metrics_from_case_dicts(report["cases"])
    if recomputed != report.get("metrics"):
        problems.append(f"{label} 报告的指标与用例明细不一致（疑似手改）")
    passed = sum(1 for case in report["cases"] if case.get("passed"))
    if report.get("passed") != passed or report.get("total") != len(report["cases"]):
        problems.append(f"{label} 报告的 passed/total 与用例明细不一致（疑似手改）")
    if recomputed.get("harness_error_count"):
        problems.append(f"{label} 报告有 {recomputed['harness_error_count']} 条用例执行出错")
    return problems


def _metric_regressions(base: dict[str, Any], head: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for pipeline in PIPELINES:
        base_values = base.get(pipeline) or {}
        head_values = head.get(pipeline) or {}
        for key in _RATE_KEYS:
            if float(head_values.get(key, 0.0)) < float(base_values.get(key, 0.0)):
                problems.append(f"{pipeline}.{key} 变差：{base_values.get(key)} → {head_values.get(key)}")
        for phrasing, item in (base_values.get("by_phrasing") or {}).items():
            head_item = (head_values.get("by_phrasing") or {}).get(phrasing) or {}
            if float(head_item.get("recall_at_5", 0.0)) < float(item.get("recall_at_5", 0.0)):
                problems.append(
                    f"{pipeline}.by_phrasing.{phrasing}.recall_at_5 变差："
                    f"{item.get('recall_at_5')} → {head_item.get('recall_at_5')}"
                )
    return problems


def compare_reports(head: dict[str, Any], base: dict[str, Any] | None) -> dict[str, Any]:
    violations = _provenance(head, "HEAD")
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
        violations.append(
            "数据集与基线不同（retrieval_colloquial_p0.jsonl 已冻结；有意修改须同时重新固化基线并写明原因）"
        )
    if head.get("corpus_sha256") != base.get("corpus_sha256"):
        violations.append("受控语料与基线不同（检索结果随语料变；须重新固化基线并写明原因）")
    base_cases = {str(case.get("id")): case for case in base["cases"]}
    if missing := sorted(set(base_cases) - set(head_cases)):
        violations.append(f"基线用例在 HEAD 里消失：{missing}")
    if regressions := sorted(
        cid
        for cid, case in base_cases.items()
        if case.get("passed") and cid in head_cases and not head_cases[cid].get("passed")
    ):
        violations.append(f"PASS_TO_PASS 违规：基线前 5 命中的用例新未命中 {regressions}")
    violations.extend(_metric_regressions(base.get("metrics") or {}, head.get("metrics") or {}))
    return {
        "passed": not violations,
        "reason": "; ".join(violations),
        "violations": violations,
        "head_passed_case_ids": head_passed,
        "base_passed_case_ids": sorted(cid for cid, case in base_cases.items() if case.get("passed")),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare retrieval_colloquial_p0 text-mode reports (HEAD vs base)")
    parser.add_argument("--head", required=True)
    parser.add_argument("--base", default=None)
    args = parser.parse_args(argv)
    head_path = Path(args.head)
    if not head_path.is_file():
        print(
            json.dumps(
                {"ok": False, "error": f"HEAD 报告不存在: {head_path}（先跑 harness.retrieval_mode_eval --mode text）"},
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
