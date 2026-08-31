"""MockGenerator：确定性生成器，引用只来自检索上下文，不调用真实云端模型。"""
from __future__ import annotations

from water_agent.domain.models import Citation, GeneratedAnswer, RetrievedClause
from water_agent.generation.base import Generator


class MockGenerator(Generator):
    """P0 测试替身：取检索结果前 3 条作为引用，确定性组装回答。"""

    MAX_CITATIONS = 3

    def generate(self, query: str, context: list[RetrievedClause]) -> GeneratedAnswer:
        if not context:
            return GeneratedAnswer(
                answer="无法在语料中定位相关条款，请补充或核对查询。",
                citations=[],
                intent="rag",
            )
        citations = [
            Citation(
                spec_id=c.spec_id,
                article=c.article,
                title=c.title,
                text_snippet=c.text[:60],
            )
            for c in context[: self.MAX_CITATIONS]
        ]
        sources = "；".join(c.source_id for c in citations)
        answer = f"根据检索结果（{sources}），针对“{query}”的相关规定摘要如下，详见所引条款原文。"
        return GeneratedAnswer(answer=answer, citations=citations, intent="rag")
