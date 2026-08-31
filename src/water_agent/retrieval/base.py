"""Retriever 接口。"""
from __future__ import annotations

from abc import ABC, abstractmethod

from water_agent.domain.models import RetrievedClause


class Retriever(ABC):
    """检索器接口：输入查询，返回相关规范条款（相关度降序）。"""

    @abstractmethod
    def retrieve(self, query: str, top_k: int = 5) -> list[RetrievedClause]:
        """返回与 query 相关的规范条款。无结果时返回空列表。"""
        raise NotImplementedError
