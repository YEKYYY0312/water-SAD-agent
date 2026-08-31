"""Recall@k 与 MRR 的确定性指标测试。"""
from __future__ import annotations

from water_agent.harness.retrieval_metrics import evaluate_retrieval, reciprocal_rank


def test_reciprocal_rank_uses_first_gold_hit():
    assert reciprocal_rank(["A", "B", "C"], {"B", "C"}) == 0.5
    assert reciprocal_rank(["A", "B"], {"X"}) == 0.0


def test_evaluate_retrieval_returns_recall_and_mrr():
    result = evaluate_retrieval([
        {"retrieved": ["A", "B"], "gold": ["B"]},
        {"retrieved": ["C"], "gold": ["B"]},
    ])
    assert result["queries"] == 2
    assert result["recall_at_1"] == 0.0
    assert result["recall_at_5"] == 0.5
    assert result["recall_at_10"] == 0.5
    assert result["mrr"] == 0.25
