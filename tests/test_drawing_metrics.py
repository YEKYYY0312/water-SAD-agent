"""P3 Step 22：缺陷召回度量（harness/drawing_metrics.py）测试。

规格 `docs/design/13-p3-drawing-review.md` §7.2 三条理由的反例化：

1. **逐条覆盖而非 any-hit**：同 rule_id 多张 gold 只命中一张 → recall 0.5
   （检索指标的"交集非空即 1.0"在这里会错误地给满分）；
2. **分母是 gold 缺陷数**：一张图两个缺陷漏一个 → recall 0.5，
   不是"这张图算没召回"或"算召回"；
3. **度量误报**：`case_passed` 同时要求不漏报、不误报（全标 bad 的退化被拦）。

另有静态断言：本模块**不得 import retrieval_metrics**（任务书硬约束，
两类指标分母不同，混用会把缺陷漏判率稀释）。
"""
from __future__ import annotations

from pathlib import Path

from water_agent.harness.drawing_metrics import (
    aggregate_drawing_metrics,
    case_drawing_metrics,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "water_agent" / "harness" / "drawing_metrics.py"


def _defects(rule_id: str, handle: str) -> dict[str, str]:
    return {"rule_id": rule_id, "handle": handle}


class TestDenominatorAndCoverage:
    def test_recall_is_per_defect_not_any_hit(self) -> None:
        """检索指标"交集非空即 1.0"在此是错的：同 rule_id 两个缺陷漏一个 = 0.5。"""
        gold = [_defects("drain.min_diameter", "A1"), _defects("drain.min_diameter", "A2")]
        reported = [_defects("drain.min_diameter", "A1")]
        metrics = case_drawing_metrics(gold, reported)
        assert metrics["gold_defects"] == 2
        assert metrics["defect_recall"] == 0.5
        assert metrics["missed_defects"] == 1

    def test_full_match_is_recall_1_and_no_false_positive(self) -> None:
        metrics = case_drawing_metrics(
            [_defects("drain.min_diameter", "A1")],
            [_defects("drain.min_diameter", "A1")],
        )
        assert metrics["defect_recall"] == 1.0
        assert metrics["false_positive_rate"] == 0.0
        assert metrics["case_passed"] is True

    def test_empty_gold_recall_is_none_not_zero(self) -> None:
        """分母为 0 时召回率没有定义 —— 记 None 不记 0（usage_known 同一条纪律）。"""
        metrics = case_drawing_metrics([], [])
        assert metrics["gold_defects"] == 0
        assert metrics["defect_recall"] is None
        assert metrics["case_passed"] is True  # 期望无缺陷且确实无报出

    def test_false_positive_on_clean_drawing_fails_case(self) -> None:
        """合规图被报缺陷 → recall 无定义但误报率 1.0 → case 失败。"""
        metrics = case_drawing_metrics([], [_defects("drain.min_diameter", "A1")])
        assert metrics["false_positive_rate"] == 1.0
        assert metrics["case_passed"] is False


class TestLocationMatching:
    def test_rule_id_match_with_wrong_handle_is_miss_plus_false_positive(
        self,
    ) -> None:
        """handle 是缺陷定位主键：报对了规则但定位错对象 = 漏掉真缺陷 + 多一条假缺陷。"""
        metrics = case_drawing_metrics(
            [_defects("drain.min_diameter", "A1")],
            [_defects("drain.min_diameter", "B9")],
        )
        assert metrics["matched_defects"] == 0
        assert metrics["missed_defects"] == 1
        assert metrics["false_positives"] == 1
        assert metrics["case_passed"] is False

    def test_wrong_rule_same_handle_is_not_a_match(self) -> None:
        metrics = case_drawing_metrics(
            [_defects("drain.horizontal_slope_range", "A1")],
            [_defects("drain.min_diameter", "A1")],
        )
        assert metrics["matched_defects"] == 0
        assert metrics["defect_recall"] == 0.0

    def test_duplicate_reported_same_defect_counts_one_false_positive(self) -> None:
        """重复报同一缺陷：matched 不超 gold 数，多报的算误报。"""
        gold = [_defects("drain.min_diameter", "A1")]
        reported = [
            _defects("drain.min_diameter", "A1"),
            _defects("drain.min_diameter", "A1"),
        ]
        metrics = case_drawing_metrics(gold, reported)
        assert metrics["matched_defects"] == 1
        assert metrics["defect_recall"] == 1.0
        assert metrics["false_positives"] == 1
        assert metrics["case_passed"] is False


class TestAggregate:
    def test_aggregate_recall_sums_gold_defects_across_cases(self) -> None:
        """跨图聚合按 gold 缺陷总数计（不是"有缺陷的图数"）。"""
        rows = [
            case_drawing_metrics(
                [_defects("drain.min_diameter", "A1"), _defects("drain.min_diameter", "A2")],
                [_defects("drain.min_diameter", "A1")],
            ),
            case_drawing_metrics(
                [_defects("drain.min_diameter", "B1")],
                [_defects("drain.min_diameter", "B1")],
            ),
            case_drawing_metrics([], []),
        ]
        agg = aggregate_drawing_metrics(rows)
        assert agg["gold_defects_total"] == 3
        assert agg["matched_defects_total"] == 2
        assert agg["defect_recall"] == round(2 / 3, 4)
        assert agg["cases_total"] == 3
        assert agg["cases_passed"] == 2  # 全标 bad 或漏报的那张图不计通过

    def test_aggregate_with_no_gold_defects_reports_none_recall(self) -> None:
        agg = aggregate_drawing_metrics(
            [case_drawing_metrics([], []), case_drawing_metrics([], [])]
        )
        assert agg["defect_recall"] is None
        assert agg["case_pass_rate"] == 1.0

    def test_mixed_recall_rate_is_denominator_of_gold_only(self) -> None:
        """两张图各一条 gold：只命中一张 → recall 0.5（分母=gold 数，不是图数）。"""
        rows = [
            case_drawing_metrics(
                [_defects("drain.min_diameter", "A1")], [_defects("drain.min_diameter", "A1")]
            ),
            case_drawing_metrics([_defects("drain.min_diameter", "B1")], []),
        ]
        agg = aggregate_drawing_metrics(rows)
        assert agg["defect_recall"] == 0.5


class TestNoRetrievalMetricsReuse:
    def test_module_never_imports_retrieval_metrics(self) -> None:
        """硬约束：缺陷召回与检索指标分母不同，模块间不得有任何 import/符号引用。"""
        source = SRC.read_text(encoding="utf-8")
        for banned in ("import retrieval_metrics", "from water_agent.harness.retrieval_metrics"):
            assert banned not in source
        assert "recall_at_" not in source
        assert "reciprocal_rank" not in source
