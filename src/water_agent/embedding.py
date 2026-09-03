"""阿里云百炼 Embedding Provider（OpenAI compatible-mode）。

默认只提供显式远程调用能力，不提供本地 embedding 兜底。
真实调用需要用户配置 ``DASHSCOPE_API_KEY``；本模块不在导入或构造时发起网络请求。
瞬时网络/SSL 超时会有上限重试；耗尽后仍抛原错误，绝不改用本地向量。
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError

DEFAULT_MODEL = "qwen3.7-text-embedding"
DEFAULT_DIMENSIONS = 512
DEFAULT_INDEX_VERSION = "qwen3.7-text-embedding__512__curated-v1"
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
TRANSIENT_HTTP_CODES = {408, 429, 500, 502, 503, 504}


def _is_transient_embed_error(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, HTTPError):
        return exc.code in TRANSIENT_HTTP_CODES
    if isinstance(exc, URLError):
        return True
    reason = getattr(exc, "reason", None)
    return isinstance(reason, TimeoutError)


@dataclass(frozen=True, slots=True)
class BailianEmbeddingConfig:
    api_key: str
    model: str = DEFAULT_MODEL
    dimensions: int = DEFAULT_DIMENSIONS
    index_version: str = DEFAULT_INDEX_VERSION
    base_url: str = DEFAULT_BASE_URL
    fallback_enabled: bool = False
    timeout_seconds: float = 60.0
    max_retries: int = 6
    retry_backoff_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise ValueError("DASHSCOPE_API_KEY must not be empty")
        if self.dimensions not in {256, 512, 768, 1024}:
            raise ValueError("dimensions must be one of 256, 512, 768, 1024")
        if self.fallback_enabled:
            raise ValueError("automatic local embedding fallback is disabled")
        expected = f"{self.model}__{self.dimensions}__curated-v1"
        if self.index_version != expected:
            raise ValueError(f"index_version must be {expected}")


class BailianEmbeddingProvider:
    def __init__(self, config: BailianEmbeddingConfig, *, transport: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> None:
        self.config = config
        self._transport = transport

    def _request_embedding(self, text: str) -> list[float]:
        payload = {
            "model": self.config.model,
            "input": text,
            "dimensions": self.config.dimensions,
        }
        if self._transport is not None:
            response = self._transport(payload)
        else:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = request.Request(
                f"{self.config.base_url.rstrip('/')}/embeddings",
                data=body,
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with request.urlopen(req, timeout=self.config.timeout_seconds) as raw:
                response = json.loads(raw.read().decode("utf-8"))
        return self._extract_vector(response)

    @staticmethod
    def _extract_vector(response: dict[str, Any]) -> list[float]:
        data = response.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict) and isinstance(data[0].get("embedding"), list):
            return [float(value) for value in data[0]["embedding"]]
        output = response.get("output")
        if isinstance(output, dict) and isinstance(output.get("embeddings"), list):
            embeddings = output["embeddings"]
            if embeddings and isinstance(embeddings[0], dict) and isinstance(embeddings[0].get("embedding"), list):
                return [float(value) for value in embeddings[0]["embedding"]]
        raise ValueError("Bailian embedding response does not contain a vector")

    def embed(self, text: str) -> list[float]:
        if not text.strip():
            raise ValueError("embedding text must not be empty")
        attempts = self.config.max_retries + 1
        last_error: BaseException = RuntimeError("embedding failed without an exception")
        for attempt in range(attempts):
            try:
                vector = self._request_embedding(text)
                if len(vector) != self.config.dimensions:
                    raise ValueError(
                        f"embedding dimension mismatch: expected {self.config.dimensions}, got {len(vector)}"
                    )
                return vector
            except Exception as exc:
                last_error = exc
                if not _is_transient_embed_error(exc) or attempt == attempts - 1:
                    raise
                delay = self.config.retry_backoff_seconds * (2 ** attempt)
                if delay > 0:
                    time.sleep(delay)
        raise last_error  # pragma: no cover


def build_embedding_provider(*, api_key: str | None = None, dimensions: int = DEFAULT_DIMENSIONS) -> BailianEmbeddingProvider:
    key = api_key if api_key is not None else os.environ.get("DASHSCOPE_API_KEY", "")
    return BailianEmbeddingProvider(BailianEmbeddingConfig(
        api_key=key,
        model=DEFAULT_MODEL,
        dimensions=dimensions,
        index_version=f"{DEFAULT_MODEL}__{dimensions}__curated-v1",
    ))
