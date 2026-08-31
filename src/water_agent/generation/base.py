"""生成器接口。"""
from __future__ import annotations

from abc import ABC, abstractmethod

from water_agent.domain.models import GeneratedAnswer, RetrievedClause


class Generator(ABC):
    """生成器接口：基于检索上下文产出带引用的回答。"""

    @abstractmethod
    def generate(self, query: str, context: list[RetrievedClause]) -> GeneratedAnswer:
        """生成回答；引用必须只来自 context，不得编造。"""
        raise NotImplementedError
