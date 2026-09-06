"""evaluate_retrieval CLI 的 rerank 选择逻辑。"""
from __future__ import annotations

from argparse import Namespace

from scripts.evaluate_retrieval import _build_reranker
from water_agent.retrieval.rerank import BailianReranker, LocalReranker


def test_build_local_reranker():
    args = Namespace(rerank="local", rerank_model=None, rerank_base_url=None, rerank_api_key=None)
    rr = _build_reranker(args)
    assert isinstance(rr, LocalReranker)


def test_build_bailian_reranker_uses_env_or_args(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-env")
    args = Namespace(
        rerank="bailian",
        rerank_model="gte-rerank",
        rerank_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        rerank_api_key=None,
    )
    rr = _build_reranker(args)
    assert isinstance(rr, BailianReranker)
    assert rr.model == "gte-rerank"


def test_build_none_reranker():
    args = Namespace(rerank="none", rerank_model=None, rerank_base_url=None, rerank_api_key=None)
    assert _build_reranker(args) is None
