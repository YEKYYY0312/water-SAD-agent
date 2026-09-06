"""RAG 图：retrieve → rerank → top_k 集成测试。"""
from __future__ import annotations

from pathlib import Path

from water_agent.domain.state import STATUS_SUCCEEDED, default_state
from water_agent.generation.mock import MockGenerator
from water_agent.graphs.rag import build_rag_graph
from water_agent.retrieval.in_memory import InMemoryRetriever
from water_agent.retrieval.loader import load_clauses_jsonl
from water_agent.retrieval.rerank import LocalReranker

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "data" / "fixtures" / "rag_fixture.jsonl"


def test_graph_with_reranker_returns_top_k_context():
    clauses = load_clauses_jsonl(FIXTURE_PATH)
    app = build_rag_graph(
        InMemoryRetriever(clauses),
        MockGenerator(),
        reranker=LocalReranker(),
        top_k=5,
        recall_top_k=20,
    )
    result = app.invoke(default_state(query="给水管道的设计秒流量"))
    assert result["status"] == STATUS_SUCCEEDED
    assert len(result["context"]) <= 5
    assert "FIX 1.1.1" in {c.source_id for c in result["context"]}


def test_graph_rerank_preserves_relevant_top_citation():
    clauses = load_clauses_jsonl(FIXTURE_PATH)
    app = build_rag_graph(
        InMemoryRetriever(clauses),
        MockGenerator(),
        reranker=LocalReranker(),
        top_k=5,
        recall_top_k=20,
    )
    result = app.invoke(default_state(query="排水管道最小管径"))
    assert result["status"] == STATUS_SUCCEEDED
    assert "FIX 2.1.2" in {c.source_id for c in result["context"]}
