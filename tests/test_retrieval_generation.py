"""Tests for Retriever / InMemoryRetriever and MockGenerator."""
from __future__ import annotations

from pathlib import Path

import pytest

from water_agent.domain.models import RetrievedClause
from water_agent.generation.base import Generator
from water_agent.generation.mock import MockGenerator
from water_agent.retrieval.base import Retriever
from water_agent.retrieval.in_memory import InMemoryRetriever
from water_agent.retrieval.loader import load_clauses_jsonl

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "data" / "fixtures" / "rag_fixture.jsonl"


@pytest.fixture(scope="module")
def clauses() -> list[RetrievedClause]:
    return load_clauses_jsonl(FIXTURE_PATH)


class TestLoader:
    def test_loads_fixture(self, clauses: list[RetrievedClause]) -> None:
        assert len(clauses) == 10
        assert all(isinstance(c, RetrievedClause) for c in clauses)
        assert clauses[0].source_id == "FIX 1.1.1"

    def test_loader_missing_file_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_clauses_jsonl(REPO_ROOT / "data" / "fixtures" / "does_not_exist.jsonl")


class TestRetrieverInterface:
    def test_implements_retrieve(self, clauses: list[RetrievedClause]) -> None:
        r: Retriever = InMemoryRetriever(clauses)
        assert callable(r.retrieve)


class TestInMemoryRetriever:
    def test_hit_by_keyword(self, clauses: list[RetrievedClause]) -> None:
        r = InMemoryRetriever(clauses)
        hits = r.retrieve("给水管道的设计秒流量", top_k=3)
        assert hits, "should return hits"
        assert hits[0].article == "1.1.1"

    def test_hit_min_diameter(self, clauses: list[RetrievedClause]) -> None:
        r = InMemoryRetriever(clauses)
        hits = r.retrieve("排水管道最小管径", top_k=3)
        assert hits[0].article == "2.1.2"

    def test_top_k_respected(self, clauses: list[RetrievedClause]) -> None:
        r = InMemoryRetriever(clauses)
        hits = r.retrieve("给水管道", top_k=2)
        assert len(hits) <= 2

    def test_no_match_returns_empty(self, clauses: list[RetrievedClause]) -> None:
        r = InMemoryRetriever(clauses)
        hits = r.retrieve("量子纠缠与宇宙常数", top_k=3)
        assert hits == []

    def test_empty_store(self) -> None:
        r = InMemoryRetriever()
        assert r.retrieve("anything", top_k=5) == []

    def test_add_then_retrieve(self) -> None:
        r = InMemoryRetriever()
        r.add(RetrievedClause(spec_id="FIX", article="9.9.9", title="专用条款", text="专用条款用于测试新增"))
        hits = r.retrieve("专用条款", top_k=1)
        assert hits[0].article == "9.9.9"

    def test_returns_retrieved_clause_objects(self, clauses: list[RetrievedClause]) -> None:
        r = InMemoryRetriever(clauses)
        hits = r.retrieve("水泵扬程", top_k=1)
        assert isinstance(hits[0], RetrievedClause)
        assert hits[0].source_id == "FIX 3.1.1"


class TestMockGenerator:
    def test_citations_only_from_context(self, clauses: list[RetrievedClause]) -> None:
        g = MockGenerator()
        context = [c for c in clauses if c.article in {"1.1.1", "2.1.2"}]
        ans = g.generate("给水管道设计", context)
        assert len(ans.citations) >= 1
        valid = {c.source_id for c in context}
        for cit in ans.citations:
            assert cit.source_id in valid, "citation must come from context"

    def test_deterministic(self, clauses: list[RetrievedClause]) -> None:
        g = MockGenerator()
        a = g.generate("给水管道", clauses[:3])
        b = g.generate("给水管道", clauses[:3])
        assert a.answer == b.answer
        assert a.citations == b.citations

    def test_empty_context_graceful(self) -> None:
        g = MockGenerator()
        ans = g.generate("不存在的东西", [])
        assert ans.citations == []
        assert "无法" in ans.answer

    def test_intent_is_rag(self, clauses: list[RetrievedClause]) -> None:
        g = MockGenerator()
        assert g.generate("给水管道", clauses[:2]).intent == "rag"

    def test_answer_mentions_citations(self, clauses: list[RetrievedClause]) -> None:
        g = MockGenerator()
        ans = g.generate("给水管道", clauses[:1])
        assert "FIX 1.1.1" in ans.answer