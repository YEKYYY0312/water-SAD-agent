"""SearXNG 适配器（自托管，无密钥；运维控制出口与引擎）。

**字段来源（官方原文，逐字，2026-10-20 读取，见决策记录 §0.2 Step 0）**：

- API：``GET /search?q=<query>&format=json``（docs.searxng.org/dev/search_api.html：端点 ``GET /search``，参数 ``q`` 必填、
  ``format`` 可选 ``json|csv|rss`` 且 "Must be activated in ``search:``"，未启用则 403 Forbidden）；
- 部署前提（searx/settings.yml）：``search.formats`` 默认只有 ``html``，要用本适配器须在实例里加 ``- json``；
- 响应顶层键（searx/webutils.py ``get_json_response``）：``query`` · ``results`` · ``answers`` · ``corrections`` ·
  ``infoboxes`` · ``suggestions`` · ``unresponsive_engines``（**没有** ``number_of_results``）；
- 结果条目由 ``MainResult.as_dict()`` 产生（searx/result_types/_base.py）：``url`` · ``title`` · ``content`` · ``engine`` ·
  ``engines`` · ``positions`` · ``score`` · ``category`` · ``template`` …

本适配器**只取** ``results[].title`` / ``url`` / ``content`` 三个字段（最小子集），其余一律忽略；``answers`` / ``infoboxes``
是 SearXNG 自己合成的内容，来源不可追溯，**不取**（SDD 非目标：LLM 合成 / 聚合答案）。

**未对真实 SearXNG 实例验证**（本机无网络与实例）：``unverified_against_real_service``。字段名取自上面的源码原文，
形状只与本仓库的进程内假服务对过。
"""
from __future__ import annotations

import json

from water_agent.web_search.adapters.base import (
    AdapterParseError,
    ProviderAdapter,
    RawHit,
    RequestSpec,
)


def build_request(query: str, count: int, key: str) -> RequestSpec:
    """``count`` 在 SearXNG 里没有对应参数（每页条数由实例决定）：多取的条目由结果流水线裁到 ``count``。``key`` 不使用。"""

    del count, key
    return RequestSpec(path="/search", params={"q": query, "format": "json"}, headers={"Accept": "application/json"})


def parse_response(body: bytes) -> list[RawHit]:
    try:
        document = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise AdapterParseError("响应不是合法 JSON") from None
    if not isinstance(document, dict):
        raise AdapterParseError("响应顶层不是对象")
    results = document.get("results")
    if not isinstance(results, list):
        raise AdapterParseError("响应缺少 results 列表")
    hits: list[RawHit] = []
    for item in results:
        if not isinstance(item, dict):
            continue  # 条目形状不对就跳过这一条，不让一条坏数据毁掉整次搜索
        url = item.get("url")
        if not isinstance(url, str) or not url:
            continue
        title = item.get("title")
        content = item.get("content")
        hits.append(
            RawHit(
                title=title if isinstance(title, str) else "",
                snippet=content if isinstance(content, str) else "",
                url=url,
            )
        )
    return hits


ADAPTER = ProviderAdapter(provider_id="searxng", needs_key=False, build_request=build_request, parse_response=parse_response)

__all__ = ["ADAPTER", "build_request", "parse_response"]
