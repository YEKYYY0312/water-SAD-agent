"""Brave Search API 适配器（公网 API，需订阅令牌）。

**字段来源（官方原文，逐字，2026-10-20 读取 api-dashboard.search.brave.com 的 Web Search 文档，见决策记录 §0.2 Step 0）**：

- 端点：``https://api.search.brave.com/res/v1/web/search``，查询在 ``q``；令牌在请求头 ``X-Subscription-Token``；
- 参数：``q`` · ``count``（"Maximum number of results per page (max 20, default 20)"，实际返回可少于它）· ``offset``（0 起，最大 9）·
  ``country`` · ``search_lang`` · ``safesearch`` · ``freshness`` · ``extra_snippets`` …；
- 响应：``query.original`` / ``query.more_results_available``；``web.results[]``，条目字段 ``title`` · ``url`` ·
  ``description``（``extra_snippets`` 仅在请求时出现）。

文档页**没有**给出 ``type`` / ``age`` / ``page_age`` / ``language`` / ``family_friendly`` / ``profile`` / ``meta_url`` /
``thumbnail`` 的权威定义（它指向另一个 API reference，本机读不到），所以本适配器**只取** ``title`` / ``url`` /
``description`` 三个字段（官方示例里逐字出现的最小子集），其余一律忽略。``locations`` / ``rich`` 等附加块不取。

**未对真实 Brave 服务验证**（本机无网络与 Key）：``unverified_against_real_service``。数据出境：查询会发往 Brave，
合规评估由运维负责（SDD §13）。
"""
from __future__ import annotations

import json

from water_agent.web_search.adapters.base import (
    AdapterParseError,
    ProviderAdapter,
    RawHit,
    RequestSpec,
)

#: 官方文档写明 ``count`` 最大 20；本工具最多要 5 条（``web_search`` 的 ``count`` 上限），多要无益。
_MAX_COUNT = 20


def build_request(query: str, count: int, key: str) -> RequestSpec:
    """令牌只放进 ``X-Subscription-Token`` 头，不进路径 / 查询串（查询串会出现在代理与服务端访问日志里）。"""

    return RequestSpec(
        path="/res/v1/web/search",
        params={"q": query, "count": str(max(1, min(int(count), _MAX_COUNT)))},
        headers={"Accept": "application/json", "X-Subscription-Token": key},
    )


def parse_response(body: bytes) -> list[RawHit]:
    try:
        document = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise AdapterParseError("响应不是合法 JSON") from None
    if not isinstance(document, dict):
        raise AdapterParseError("响应顶层不是对象")
    web = document.get("web")
    if web is None:
        # 官方文档：没有网页结果时 web 块可以缺席——这是"零结果"，不是坏响应
        if isinstance(document.get("query"), dict):
            return []
        raise AdapterParseError("响应缺少 web 块")
    if not isinstance(web, dict) or not isinstance(web.get("results"), list):
        raise AdapterParseError("响应的 web.results 不是列表")
    hits: list[RawHit] = []
    for item in web["results"]:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url:
            continue
        title = item.get("title")
        description = item.get("description")
        hits.append(
            RawHit(
                title=title if isinstance(title, str) else "",
                snippet=description if isinstance(description, str) else "",
                url=url,
            )
        )
    return hits


ADAPTER = ProviderAdapter(provider_id="brave", needs_key=True, build_request=build_request, parse_response=parse_response)

__all__ = ["ADAPTER", "build_request", "parse_response"]
