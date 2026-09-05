"""Embedding Provider 配置契约：真实入库不得默认使用 mock embedding。"""
from __future__ import annotations

from urllib.error import URLError

import pytest

from water_agent.embedding import (
    BailianEmbeddingConfig,
    BailianEmbeddingProvider,
    build_embedding_provider,
)
from water_agent.runtime.governance.contracts import CODE_PROVIDER_UNAVAILABLE, GovernanceError


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
    """远程失败绝不回退本地向量。

    P2.5 Step 12：错误经 ``ProviderGateway`` 转为结构化 ``GovernanceError``
    （``PROVIDER_UNAVAILABLE``），原始异常保留在 ``__cause__`` 便于排障。
    RuntimeError 属"不可重试的永久错误" → 零重试。
    """
    calls = {"n": 0}
    provider = BailianEmbeddingProvider(BailianEmbeddingConfig(api_key="secret"))

    def fail(_text: str):
        calls["n"] += 1
        raise RuntimeError("remote unavailable")

    provider._request_embedding = fail
    with pytest.raises(GovernanceError) as exc_info:
        provider.embed("条款正文")
    assert exc_info.value.code == CODE_PROVIDER_UNAVAILABLE
    # 永久错误零重试
    assert calls["n"] == 1
    # 原始错误细节可追溯，但不外泄进用户可见 message
    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert "remote unavailable" in str(exc_info.value.__cause__)
    assert "remote unavailable" not in exc_info.value.message


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
    """瞬时错误耗尽重试后仍不回退本地向量。

    P2.5 Step 12：重试由 gateway 承担，``config.max_retries=2`` 映射为
    ``max_attempts=3``（含首次）；耗尽后抛 ``PROVIDER_UNAVAILABLE``，
    原始 ``URLError`` 保留在 ``__cause__``。
    """
    calls = {"n": 0}

    def transport(_payload):
        calls["n"] += 1
        raise URLError(TimeoutError("The handshake operation timed out"))

    provider = BailianEmbeddingProvider(
        BailianEmbeddingConfig(api_key="secret", max_retries=2, retry_backoff_seconds=0.0),
        transport=transport,
    )
    with pytest.raises(GovernanceError) as exc_info:
        provider.embed("条款正文")
    assert exc_info.value.code == CODE_PROVIDER_UNAVAILABLE
    # max_retries=2 → 首次 + 2 次重试 = 3 次真实尝试（config 字段真实生效，非死字段）
    assert calls["n"] == 3
    assert isinstance(exc_info.value.__cause__, URLError)
    assert "handshake" in str(exc_info.value.__cause__)
