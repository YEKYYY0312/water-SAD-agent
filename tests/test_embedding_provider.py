"""Embedding Provider 配置契约：真实入库不得默认使用 mock embedding。"""
from __future__ import annotations

from urllib.error import URLError

import pytest

from water_agent.embedding import BailianEmbeddingConfig, BailianEmbeddingProvider, build_embedding_provider


def test_config_is_explicit_and_has_fixed_index_version():
    config = BailianEmbeddingConfig(api_key="secret")
    assert config.model == "qwen3.7-text-embedding"
    assert config.dimensions == 512
    assert config.fallback_enabled is False
    assert config.index_version == "qwen3.7-text-embedding__512__curated-v1"


def test_missing_api_key_is_rejected_without_local_fallback(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="DASHSCOPE_API_KEY"):
        build_embedding_provider()


def test_provider_never_falls_back_when_remote_call_fails():
    provider = BailianEmbeddingProvider(BailianEmbeddingConfig(api_key="secret"))

    def fail(_text: str):
        raise RuntimeError("remote unavailable")

    provider._request_embedding = fail
    with pytest.raises(RuntimeError, match="remote unavailable"):
        provider.embed("条款正文")


def test_provider_rejects_wrong_dimension():
    provider = BailianEmbeddingProvider(BailianEmbeddingConfig(api_key="secret"))
    provider._request_embedding = lambda _text: [0.1] * 256
    with pytest.raises(ValueError, match="512"):
        provider.embed("条款正文")


def test_provider_retries_ssl_handshake_timeout_then_succeeds():
    calls = {"n": 0}

    def transport(_payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise URLError(TimeoutError("The handshake operation timed out"))
        return {"data": [{"embedding": [0.1] * 512}]}

    provider = BailianEmbeddingProvider(
        BailianEmbeddingConfig(api_key="secret", retry_backoff_seconds=0.0),
        transport=transport,
    )
    vector = provider.embed("条款正文")
    assert len(vector) == 512
    assert calls["n"] == 2


def test_provider_exhausted_timeout_retries_do_not_fallback():
    def transport(_payload):
        raise URLError(TimeoutError("The handshake operation timed out"))

    provider = BailianEmbeddingProvider(
        BailianEmbeddingConfig(api_key="secret", max_retries=2, retry_backoff_seconds=0.0),
        transport=transport,
    )
    with pytest.raises(URLError, match="handshake"):
        provider.embed("条款正文")
