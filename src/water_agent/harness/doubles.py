"""Harness 用测试替身。"""
from __future__ import annotations

from water_agent.domain.models import Citation, GeneratedAnswer, RetrievedClause
from water_agent.generation.base import Generator


class BadCitationGenerator(Generator):
    """故意返回不在检索上下文中的引用，用于触发引用校验失败。"""

    def generate(self, query: str, context: list[RetrievedClause]) -> GeneratedAnswer:
        citations = [
            Citation(
                spec_id="FAKE",
                article="0.0.0",
                title="伪造条款",
                text_snippet="This citation does not exist in context.",
            )
        ]
        return GeneratedAnswer(
            answer=f"这是一个包含无效引用的回答：{query}",
            citations=citations,
            intent="rag",
        )
