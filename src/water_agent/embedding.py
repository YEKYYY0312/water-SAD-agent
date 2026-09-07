"""阿里云百炼 Embedding Provider（OpenAI compatible-mode）。

默认只提供显式远程调用能力，不提供本地 embedding 兜底。
真实调用需要用户配置 ``DASHSCOPE_API_KEY``；本模块不在导入或构造时发起网络请求。

P2.5（Step 12）：远程调用经 ``ProviderGateway`` —— 限流、预算预留、有界重试
（仅瞬时错误）、429 退避、熔断与 usage 对账统一在 gateway 内完成。
本模块不再自行 sleep 重试；瞬时错误耗尽后抛结构化 ``GovernanceError``，
**绝不改用本地向量**。
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib import request

from water_agent.runtime.governance.binding import (
    gateway_for_profile_overrides,
    local_request_context,
)
from water_agent.runtime.governance.config import (
    INGEST_RATE_LIMIT,
    ProviderProfile,
    RetryPolicy,
)
from water_agent.runtime.governance.contracts import (
    CallCategory,
    Capability,
    ProviderCall,
    ProviderKind,
    RequestContext,
)
from water_agent.runtime.governance.gateway import ProviderGateway, ProviderUsage

DEFAULT_MODEL = "qwen3.7-text-embedding"
DEFAULT_DIMENSIONS = 512
DEFAULT_INDEX_VERSION = "qwen3.7-text-embedding__512__curated-v1"
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# P2.5 Step 12：瞬时错误判定与重试已统一由 ProviderGateway._classify_error 承担。
# 原 _is_transient_embed_error / TRANSIENT_HTTP_CODES 已删除（避免两套判定漂移，
# 也避免留下看似生效的死代码）。gateway 侧的等价常量见
# runtime/governance/gateway.py::TRANSIENT_HTTP_CODES。


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
    """百炼 embedding。P2.5 起远程调用经 ``ProviderGateway``（限流/预算/重试/熔断）。

    ``gateway``/``request_context`` 均可选；为 ``None`` 时取进程级默认 gateway 与
    本地主体上下文 —— **不存在"不传 gateway 即绕过治理"的路径**（规格 §1.2）。
    重试语义统一由 gateway 承担：本类不再自行 sleep 重试。
    """

    def __init__(
        self,
        config: BailianEmbeddingConfig,
        *,
        transport: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        gateway: ProviderGateway | None = None,
        request_context: RequestContext | None = None,
    ) -> None:
        self.config = config
        self._transport = transport
        self._gateway = gateway
        self._request_context = request_context
        self.last_usage: ProviderUsage = ProviderUsage.unknown()

    def _resolve_gateway(self) -> ProviderGateway:
        """取 gateway：注入优先，否则用进程级默认 gateway。

        ``config.max_retries`` / ``retry_backoff_seconds`` 是**调用方显式声明的
        本地重试口径**（入库脚本会调大）；未注入 gateway 时按它派生 provider 画像，
        以免这两个字段沦为死字段（规格 §4 禁止保留看似生效的死字段）。
        """
        if self._gateway is not None:
            return self._gateway
        return default_gateway_for_embedding(self.config)

    def _resolve_context(self) -> RequestContext:
        if self._request_context is not None:
            return self._request_context
        return local_request_context(thread_id="embedding")

    def _request_embedding(self, text: str) -> list[float]:
        """发一次 embedding 请求并返回向量（**单一注入缝**：测试可替换本方法）。

        **不含重试**：重试/退避/熔断统一由 ``ProviderGateway`` 承担（Step 12）。
        provider 用量写入 :attr:`last_usage`，缺失时为 ``unknown``（不估算）。
        """
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
            with request.urlopen(req, timeout=self._resolve_timeout()) as raw:
                response = json.loads(raw.read().decode("utf-8"))
        self.last_usage = self._extract_usage(response)
        return self._extract_vector(response)

    def _resolve_timeout(self) -> float:
        """超时口径单一来源：优先 gateway 的 provider 画像，回落本地 config。"""
        try:
            return self._resolve_gateway().timeout_for(ProviderKind.BAILIAN_EMBEDDING)
        except KeyError:
            return self.config.timeout_seconds

    @staticmethod
    def _extract_usage(response: dict[str, Any]) -> ProviderUsage:
        """从响应提取 usage；缺失或形态不符 → ``unknown``（禁止估算充当账单）。"""
        usage = response.get("usage")
        if not isinstance(usage, dict):
            return ProviderUsage.unknown()
        total = usage.get("total_tokens")
        prompt = usage.get("prompt_tokens", total)
        if not isinstance(prompt, int):
            return ProviderUsage.unknown()
        return ProviderUsage.of(input_tokens=prompt, output_tokens=0)

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
        """向量化一段文本。

        P2.5：经 ``ProviderGateway`` —— 限流、预算预留、有界重试（仅瞬时错误）、
        429 退避、熔断与 usage 对账全部在 gateway 内完成。**永不回退本地向量**：
        失败抛 ``GovernanceError``（结构化），维度不符抛 ``ValueError``。
        """
        if not text.strip():
            raise ValueError("embedding text must not be empty")

        call = ProviderCall(
            provider=ProviderKind.BAILIAN_EMBEDDING,
            category=CallCategory.EMBEDDING_QUERY,
            model=self.config.model,
            temperature=0.0,
            estimated_input_tokens=_estimate_tokens(text),
            required_capability=Capability.EMBEDDING,
        )

        def executor() -> tuple[list[float], ProviderUsage]:
            self.last_usage = ProviderUsage.unknown()
            vector = self._request_embedding(text)
            return vector, self.last_usage

        result = self._resolve_gateway().call(self._resolve_context(), call, executor)
        vector = result.value
        # 维度校验在 gateway 之外：它是"库与模型配置不匹配"，不是 provider 不可用，
        # 既不该被重试，也不该被包装成 PROVIDER_UNAVAILABLE。
        if len(vector) != self.config.dimensions:
            raise ValueError(
                f"embedding dimension mismatch: expected {self.config.dimensions}, got {len(vector)}"
            )
        return vector


def _estimate_tokens(text: str) -> int:
    """粗略 token 估算（中文约 1 字 1 token，英文约 4 字符 1 token）。

    只用于**限流与预算预留**的额度预估，绝不作为账单依据 ——
    真实用量由 provider 返回后对账（``usage_known``）。
    """
    return max(1, len(text))


_embedding_gateways: dict[tuple[float, int, float], ProviderGateway] = {}


def default_gateway_for_embedding(config: BailianEmbeddingConfig) -> ProviderGateway:
    """按 config 的本地重试口径派生 gateway（同口径复用同一实例）。

    ``BailianEmbeddingConfig`` 的 ``timeout_seconds`` / ``max_retries`` /
    ``retry_backoff_seconds`` 是调用方显式声明的意图（入库脚本会调大重试）。
    把它们映射成 provider 画像，保证这些字段**真实生效**而不是死字段。
    复用同一实例是为了让限流桶与预算账本在同口径调用间共享，
    否则每次 embed 都新建 gateway，限流形同虚设。
    """
    key = (config.timeout_seconds, config.max_retries, config.retry_backoff_seconds)
    gateway = _embedding_gateways.get(key)
    if gateway is None:
        gateway = gateway_for_profile_overrides(
            {
                ProviderKind.BAILIAN_EMBEDDING: ProviderProfile(
                    timeout_seconds=config.timeout_seconds,
                    retry=RetryPolicy(
                        # config.max_retries 是"重试次数"，画像的 max_attempts 含首次
                        max_attempts=min(10, config.max_retries + 1),
                        base_backoff_seconds=config.retry_backoff_seconds,
                        max_backoff_seconds=max(config.retry_backoff_seconds * 32, 1.0),
                    ),
                    # 沿用批量入库额度：逐块嵌入整个语料库属合法批量作业
                    rate_limit=INGEST_RATE_LIMIT,
                )
            }
        )
        _embedding_gateways[key] = gateway
    return gateway


def build_embedding_provider(
    *,
    api_key: str | None = None,
    dimensions: int = DEFAULT_DIMENSIONS,
    request_context: RequestContext | None = None,
    gateway: ProviderGateway | None = None,
) -> BailianEmbeddingProvider:
    key = api_key if api_key is not None else os.environ.get("DASHSCOPE_API_KEY", "")
    return BailianEmbeddingProvider(
        BailianEmbeddingConfig(
            api_key=key,
            model=DEFAULT_MODEL,
            dimensions=dimensions,
            index_version=f"{DEFAULT_MODEL}__{dimensions}__curated-v1",
        ),
        request_context=request_context,
        gateway=gateway,
    )
