"""检索质量指标：Recall@k 与 MRR（Step 10 接入 RAG 用例评测）。

Step 10 前该模块只服务纯检索矩阵评测（row 含 retrieved/gold 字段）；
本版新增两个入口：

- ``case_retrieval_metrics(retrieved, gold_any)``：单个 RAG 用例的检索指标
  （以 ``context_articles`` 的召回顺序作为检索排名，gold 取 ``gold_articles_any``）；
- ``aggregate_retrieval_metrics(case_metrics)``：对一批用例做聚合。

两种判分口径互补：引用判分（scorer）是"最终引用正确"的确定性判分；
检索指标衡量"检索环节是否把该召回的条款召回进上下文"，二者独立记录。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping


def reciprocal_rank(retrieved: Iterable[str], gold: set[str]) -> float:
    for rank, article in enumerate(retrieved, start=1):
        if article in gold:
            return 1.0 / rank
    return 0.0


def _as_article_list(values: Iterable[str]) -> list[str]:
    """去重并保留顺序的 source_id 列表。"""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        cleaned = (value or "").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def case_retrieval_metrics(retrieved: Iterable[str], gold_any: Iterable[str]) -> dict[str, float]:
    """单个用例的检索指标。

    Args:
        retrieved: 检索结果（按相关性从高到低排序）的 source_id 列表；
        gold_any: 期望召回（任意命中即可）的 source_id 列表。

    Returns:
        {"recall_at_1", "recall_at_3", "recall_at_5", "mrr"}；gold 为空时四项全 0。
    """
    retrieved_ids = _as_article_list(retrieved)
    gold_ids = _as_article_list(gold_any)
    gold_set = set(gold_ids)
    if not gold_set:
        return {"recall_at_1": 0.0, "recall_at_3": 0.0, "recall_at_5": 0.0, "mrr": 0.0}

    hits_at: dict[int, bool] = {}
    for k in (1, 3, 5):
        hits_at[k] = bool(set(retrieved_ids[:k]) & gold_set)
    return {
        "recall_at_1": 1.0 if hits_at[1] else 0.0,
        "recall_at_3": 1.0 if hits_at[3] else 0.0,
        "recall_at_5": 1.0 if hits_at[5] else 0.0,
        "mrr": reciprocal_rank(retrieved_ids, gold_set),
    }


def aggregate_retrieval_metrics(
    case_metrics: Iterable[Mapping[str, float]],
) -> dict[str, float]:
    """对一批用例的检索指标做平均聚合。"""
    rows = list(case_metrics)
    total = len(rows)
    if not total:
        return {"queries": 0, "recall_at_1": 0.0, "recall_at_3": 0.0, "recall_at_5": 0.0, "mrr": 0.0}
    keys = ["recall_at_1", "recall_at_3", "recall_at_5", "mrr"]
    result: dict[str, float] = {"queries": total}
    for key in keys:
        result[key] = round(
            sum(float(row.get(key, 0.0)) for row in rows) / total,
            4,
        )
    return result


def evaluate_retrieval(cases: Iterable[Mapping[str, object]]) -> dict[str, float | int]:
    """纯检索矩阵评测（保留，供 evaluate_retrieval_matrix 使用）。"""
    rows = list(cases)
    total = len(rows)
    if not total:
        return {"queries": 0, "recall_at_1": 0.0, "recall_at_5": 0.0, "recall_at_10": 0.0, "mrr": 0.0}

    def recall_at(k: int) -> float:
        hits = 0
        for row in rows:
            retrieved = [str(value) for value in _as_article_list(row.get("retrieved", []))]  # type: ignore[arg-type]
            gold = set(_as_article_list(row.get("gold", [])))  # type: ignore[arg-type]
            if set(retrieved[:k]) & gold:
                hits += 1
        return hits / total

    mrr_values = [
        reciprocal_rank(
            _as_article_list(row.get("retrieved", [])),  # type: ignore[arg-type]
            set(_as_article_list(row.get("gold", []))),  # type: ignore[arg-type]
        )
        for row in rows
    ]
    mrr = sum(mrr_values) / total
    return {
        "queries": total,
        "recall_at_1": recall_at(1),
        "recall_at_5": recall_at(5),
        "recall_at_10": recall_at(10),
        "mrr": mrr,
    }