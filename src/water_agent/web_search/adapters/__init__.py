"""联网搜索 provider 适配器（P7-S12，规格 §5.3）。

适配器是**纯函数**：``build_request(query, count) -> RequestSpec``（路径 + 查询参数 + 头）与
``parse_response(body) -> list[RawHit]``。一律 GET；响应上限与媒体类型由 :class:`ProviderAdapter` 声明、由
``governed_get`` 强制。适配器**不 import 任何网络原语**（SDD17-I10，``tests/test_web_search_adapters.py`` 用 AST 判定）——
只能被 ``governed_get`` 驱动，没有自己的连接、重试或回退。

``get_adapter(provider)`` 对未知 provider id 抛 ``KeyError``（装配期把它转成 ``WEB_SEARCH_CONFIG_INVALID``）：
**没有"找不到就换一家"**——显式配置的 provider 失败不回退到别的 provider。
"""
from __future__ import annotations

from water_agent.web_search.adapters.base import (
    JSON_MEDIA_TYPE,
    MAX_RESPONSE_BYTES,
    AdapterParseError,
    ProviderAdapter,
    RawHit,
    RequestSpec,
)
from water_agent.web_search.adapters.brave import ADAPTER as BRAVE
from water_agent.web_search.adapters.searxng import ADAPTER as SEARXNG

_ADAPTERS: dict[str, ProviderAdapter] = {SEARXNG.provider_id: SEARXNG, BRAVE.provider_id: BRAVE}


def get_adapter(provider: str) -> ProviderAdapter:
    try:
        return _ADAPTERS[provider]
    except KeyError:
        raise KeyError(f"未知的联网搜索 provider：{provider!r}（支持：{sorted(_ADAPTERS)}）") from None


def adapter_ids() -> frozenset[str]:
    return frozenset(_ADAPTERS)


__all__ = [
    "BRAVE",
    "JSON_MEDIA_TYPE",
    "MAX_RESPONSE_BYTES",
    "SEARXNG",
    "AdapterParseError",
    "ProviderAdapter",
    "RawHit",
    "RequestSpec",
    "adapter_ids",
    "get_adapter",
]
