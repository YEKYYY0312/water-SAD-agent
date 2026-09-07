"""图纸审查缺陷召回率度量（P3 Step 22）。

规格 `docs/design/13-p3-drawing-review.md` §7。**不得复用
`harness/retrieval_metrics.py`** —— 那套是检索指标，三条理由（规格 §7.2）：

1. **`@k` 排名语义不适用**：`Recall@k` 度量"top-k 切片里有没有命中"，
   缺陷审查没有排名、没有 k；
2. **是 any-hit 而非集合覆盖率**：检索指标交集非空即记 1.0（一条 gold 命中就
   满分）；缺陷召回必须是**逐条覆盖**，否则「≥80%」无法度量 —— 分母是
   **人工标注的缺陷数**，不是"有缺陷的图数"；
3. **完全不度量误报**，而规格验收同时要求"误报率可控"。

两类指标混在一起会让缺陷漏判率被稀释：检索分母是"全部文档"，缺陷召回分母是
"人工标注的缺陷数"。本模块的分母只取后者。

口径（与 §7.3 对齐）
---------------------
- 逐条判分按 `(rule_id, handle)` 精确匹配：`handle` 是缺陷定位的主键（验收
  "能定位缺陷对象"）；同 rule_id 但 handle 对不上 = 漏掉那条 + 多报一条假的；
- 单条用例 pass/fail **仍精确匹配**，不用召回率替代 —— 只断言"报了缺陷"
  会让个别规则退化被平均值掩盖；
- `defect_recall` 的未知值记 `None` 不记 `0.0`（同 `UsageRecord.usage_known`
  纪律：分母为 0 时召回率没有定义，写 0 等于声称"一条 gold 都没有，完美召回"）。
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping

#: 缺陷条目的定位键。`handle` 与 rule_id 一起构成唯一标识。
_KEYS = ("rule_id", "handle")


def _pairs(entries: Iterable[Mapping[str, object]]) -> Counter[tuple[str, str]]:
    """把缺陷条目归一为 (rule_id, handle) 计数器。"""
    return Counter(
        (str(entry.get("rule_id") or ""), str(entry.get("handle") or ""))
        for entry in entries
    )


def _as_int(value: object) -> int:
    """report dict 字段 → int（json 报告里的数字反序列化后可能是 float/str）。"""
    return int(value) if isinstance(value, (int, float, str)) else 0


def case_drawing_metrics(
    gold_defects: Iterable[Mapping[str, object]],
    reported_findings: Iterable[Mapping[str, object]],
) -> dict[str, float | int | bool | None]:
    """单张图纸的逐条缺陷覆盖与误报度量。

    Args:
        gold_defects: 人工标注的缺陷，每条含 ``rule_id`` 与 ``handle``；
        reported_findings: 规则引擎实际报出的缺陷，同构。

    Returns:
        ``gold_defects``（分母）、``reported_findings``（实际报出条数）、
        ``matched_defects``、``missed_defects``、``false_positives``、
        ``defect_recall``（gold 为 0 时是 ``None`` 而非 0）、
        ``false_positive_rate``（无报出时为 0.0）、``case_passed``
        （逐条精确匹配：不漏报也不误报才为 True）。

    ``case_passed`` 同时要求 recall 满覆盖且零误报，两路都精确匹配 ——
    把"全标 bad"的退化（100% 召回 + 高误报）与"漏报"（低召回）都判失败。
    """
    gold = _pairs(gold_defects)
    reported = _pairs(reported_findings)
    gold_total = sum(gold.values())
    reported_total = sum(reported.values())

    matched = 0
    for key, count in gold.items():
        matched += min(count, reported.get(key, 0))
    missed = gold_total - matched
    false_positives = reported_total - matched

    recall: float | None = (matched / gold_total) if gold_total else None
    fpr = (false_positives / reported_total) if reported_total else 0.0
    return {
        "gold_defects": gold_total,
        "reported_findings": reported_total,
        "matched_defects": matched,
        "missed_defects": missed,
        "false_positives": false_positives,
        "defect_recall": round(recall, 4) if recall is not None else None,
        "false_positive_rate": round(fpr, 4),
        "case_passed": matched == gold_total and false_positives == 0,
    }


def aggregate_drawing_metrics(
    case_metrics: Iterable[Mapping[str, object]],
) -> dict[str, float | int | bool | None]:
    """对一批图纸的逐条覆盖做聚合。

    召回率分母是**全部用例的 gold 缺陷总数**（跨图累加），不是"有缺陷的图数"——
    一张图漏两条与两张图各漏一条，都必须反映为相同的召回下降。
    """
    rows = list(case_metrics)
    gold_total = sum(_as_int(row.get("gold_defects")) for row in rows)
    matched = sum(_as_int(row.get("matched_defects")) for row in rows)
    reported = sum(_as_int(row.get("reported_findings")) for row in rows)
    false_positives = sum(_as_int(row.get("false_positives")) for row in rows)
    cases_passed = sum(1 for row in rows if row.get("case_passed") is True)

    recall: float | None = (matched / gold_total) if gold_total else None
    fpr = (false_positives / reported) if reported else 0.0
    return {
        "cases_total": len(rows),
        "cases_passed": cases_passed,
        "case_pass_rate": round(cases_passed / len(rows), 4) if rows else 0.0,
        "gold_defects_total": gold_total,
        "reported_findings_total": reported,
        "matched_defects_total": matched,
        "missed_defects_total": gold_total - matched,
        "false_positives_total": false_positives,
        "defect_recall": round(recall, 4) if recall is not None else None,
        "false_positive_rate": round(fpr, 4),
    }
