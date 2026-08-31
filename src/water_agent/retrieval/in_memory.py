"""本地确定性检索替身：无向量、无外部依赖，用于 P0 测试与离线回放。"""
from __future__ import annotations

from collections.abc import Iterable

from water_agent.domain.models import RetrievedClause
from water_agent.retrieval.base import Retriever


class InMemoryRetriever(Retriever):
    """基于查询与条款文本的二元组（bigram）重叠打分，结果确定可复现。"""

    def __init__(self, clauses: Iterable[RetrievedClause] | None = None) -> None:
        self._clauses: list[RetrievedClause] = list(clauses or [])

    def add(self, clause: RetrievedClause) -> None:
        self._clauses.append(clause)

    def retrieve(self, query: str, top_k: int = 5) -> list[RetrievedClause]:
        scored = [(self._score(query, c), c) for c in self._clauses]
        scored = [(s, c) for s, c in scored if s > 0]
        scored.sort(key=lambda t: t[0], reverse=True)
        return [c for _, c in scored[:top_k]]

    @staticmethod
    def _score(query: str, clause: RetrievedClause) -> int:
        q = query.lower().strip()
        if not q:
            return 0
        text = f"{clause.title} {clause.text} {clause.article}".lower()
        q_grams = {q[i : i + 2] for i in range(len(q) - 1)}
        t_grams = {text[i : i + 2] for i in range(len(text) - 1)}
        overlap = len(q_grams & t_grams)
        bonus = 5 if q in text else 0
        return overlap + bonus
