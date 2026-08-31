"""Integration tests for the LangGraph RAG state machine (graphs/rag.py)."""
from __future__ import annotations

from pathlib import Path

import pytest

from water_agent.domain.state import (
    MAX_CITATION_RETRIES,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    WaterAgentState,
    default_state,
)
from water_agent.generation.base import Generator
from water_agent.generation.mock import MockGenerator
from water_agent.graphs.rag import build_rag_graph
from water_agent.retrieval.in_memory import InMemoryRetriever
from water_agent.retrieval.loader import load_clauses_jsonl

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "data" / "fixtures" / "rag_fixture.jsonl"


@pytest.fixture(scope="module")
def clauses():
    return load_clauses_jsonl(FIXTURE_PATH)


@pytest.fixture
def retriever(clauses):
    return InMemoryRetriever(clauses)


@pytest.fixture
def generator():
    return MockGenerator()


class BadCitationGenerator(Generator):
    """故意生成不在 context 中的引用，用于测试引用校验失败路径。"""

    def generate(self, query: str, context):
        from water_agent.domain.models import Citation, GeneratedAnswer

        bad = Citation(spec_id="FAKE", article="99.99", title="不存在的条款", text_snippet="编造")
        return GeneratedAnswer(
            answer="包含编造引用的回答。",
            citations=[bad],
            intent="rag",
        )


class TestRagGraph:
    def test_normal_hit(self, retriever, generator) -> None:
        app = build_rag_graph(retriever, generator)
        state = default_state(query="给水管道的设计秒流量")
        result = app.invoke(state)
        assert result["status"] == STATUS_SUCCEEDED, result.get("errors")
        assert result["answer"], "answer should not be empty"
        assert len(result["citations"]) >= 1
        assert "FIX 1.1.1" in [c.source_id for c in result["citations"]]

    def test_multi_article_hit(self, retriever, generator) -> None:
        app = build_rag_graph(retriever, generator)
        result = app.invoke(default_state(query="排水管道最小管径"))
        assert result["status"] == STATUS_SUCCEEDED
        assert len(result["citations"]) >= 1

    def test_no_result_fails(self, retriever, generator) -> None:
        app = build_rag_graph(retriever, generator)
        result = app.invoke(default_state(query="量子纠缠与宇宙常数"))
        assert result["status"] == STATUS_FAILED
        assert any("检索无结果" in e for e in result.get("errors", []))

    def test_bad_citation_retries_then_fails(self, retriever) -> None:
        """BadCitationGenerator 的引用不在 context 中 → 重试达上限后失败。"""
        app = build_rag_graph(retriever, BadCitationGenerator())
        result = app.invoke(default_state(query="给水管道的设计秒流量"))
        assert result["status"] == STATUS_FAILED
        assert any("引用校验" in e for e in result.get("errors", []))

    def test_retry_count_bounded(self, retriever) -> None:
        """达最大重试次数后停止，不无限循环。"""
        app = build_rag_graph(retriever, BadCitationGenerator())
        result = app.invoke(default_state(query="给水管道的设计秒流量"))
        assert result["retry_count"] <= MAX_CITATION_RETRIES, "retry must not exceed limit"
        assert result["step_count"] >= 1, "at least one step should have run"

    def test_trace_fields_present(self, retriever, generator) -> None:
        app = build_rag_graph(retriever, generator)
        result = app.invoke(default_state(query="排水管道坡度"))
        assert result["run_id"], "run_id should be set"
        assert result["trace_id"], "trace_id should be set"
        assert result["run_id"] != result["trace_id"] or (result["run_id"] and result["trace_id"])

    def test_status_initialized(self, retriever, generator) -> None:
        app = build_rag_graph(retriever, generator)
        result = app.invoke(default_state(query="消防用水量"))
        # The graph should have transitioned from running to success/fail
        assert result["status"] in (STATUS_SUCCEEDED, STATUS_FAILED), f"unexpected status: {result['status']}"

    def test_returns_water_agent_state(self, retriever, generator) -> None:
        app = build_rag_graph(retriever, generator)
        result = app.invoke(default_state(query="水泵扬程如何确定"))
        # Verify it's the full WaterAgentState shape
        assert isinstance(result, dict)
        assert "intent" in result
        assert "context" in result
        assert "citations" in result