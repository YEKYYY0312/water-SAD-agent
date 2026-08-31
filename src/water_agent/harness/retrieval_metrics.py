"""检索质量指标：Recall@k 与 MRR。"""
from __future__ import annotations

from collections.abc import Iterable, Mapping


def reciprocal_rank(retrieved: Iterable[str], gold: set[str]) -> float:
    for rank, article in enumerate(retrieved, start=1):
        if article in gold:
            return 1.0 / rank
    return 0.0


def evaluate_retrieval(cases: Iterable[Mapping[str, object]]) -> dict[str, float | int]:
    rows = list(cases)
    total = len(rows)
    if not total:
        return {"queries": 0, "recall_at_1": 0.0, "recall_at_5": 0.0, "recall_at_10": 0.0, "mrr": 0.0}

    def recall_at(k: int) -> float:
        hits = 0
        for row in rows:
            retrieved = [str(value) for value in row.get("retrieved", [])]  # type: ignore[union-attr]
            gold = {str(value) for value in row.get("gold", [])}  # type: ignore[union-attr]
            if set(retrieved[:k]) & gold:
                hits += 1
        return hits / total

    mrr = sum(
        reciprocal_rank(
            [str(value) for value in row.get("retrieved", [])],  # type: ignore[union-attr]
            {str(value) for value in row.get("gold", [])},  # type: ignore[union-attr]
        )
        for row in rows
    ) / total
    return {
        "queries": total,
        "recall_at_1": recall_at(1),
        "recall_at_5": recall_at(5),
        "recall_at_10": recall_at(10),
        "mrr": mrr,
    }
