"""P7-S12 provider 适配器：纯函数（构造请求 / 解析响应），不 import 任何网络原语（SDD17-I10），显式 provider 不回退。

字段取自官方原文（见 ``web_search/adapters/{searxng,brave}.py`` 模块文档），**未对真实服务验证**（unverified_against_real_service）。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from water_agent.web_search.adapters import (
    BRAVE,
    SEARXNG,
    AdapterParseError,
    adapter_ids,
    brave,
    get_adapter,
    searxng,
)

ADAPTERS_DIR = Path(__file__).resolve().parents[1] / "src" / "water_agent" / "web_search" / "adapters"
SECRET = "brave-secret-key-0123456789abcdef"
_RAW_NETWORK = (
    "socket", "ssl", "http", "http.client", "urllib.request", "urllib3", "requests", "httpx", "aiohttp",
    "water_agent.mcp.http_transport", "water_agent.external.http_get",
)


def _network_imports(source: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        found.extend(name for name in names if any(name == raw or name.startswith(f"{raw}.") for raw in _RAW_NETWORK))
    return found


def test_adapters_import_no_network_primitive() -> None:
    """P7-S12-A02 / SDD17-I10 适配器模块不 import 任何网络原语（AST 判定）：只能被 ``governed_get`` 驱动。"""

    modules = sorted(ADAPTERS_DIR.glob("*.py"))
    assert {path.name for path in modules} >= {"__init__.py", "base.py", "searxng.py", "brave.py"}
    for path in modules:
        assert _network_imports(path.read_text(encoding="utf-8")) == [], path.name


def test_network_import_check_catches_a_raw_primitive() -> None:
    """P7-S12-A02 / SDD17-I10 反证：同一判定对 ``import socket`` / ``from http.client import …`` 必须报出。"""

    assert _network_imports("import socket\nfrom http.client import HTTPSConnection\n") == ["socket", "http.client"]
    assert _network_imports("from water_agent.external.http_get import governed_get\n") == ["water_agent.external.http_get"]


def test_searxng_builds_a_keyless_json_query() -> None:
    """P7-S12-A02 searxng：``GET /search?q=…&format=json``；``count`` 无对应参数（由结果流水线裁剪）；``key`` 不出现在任何地方。"""

    spec = searxng.build_request("给水管道", 3, SECRET)
    assert spec.path == "/search"
    assert dict(spec.params) == {"q": "给水管道", "format": "json"}
    assert dict(spec.headers) == {"Accept": "application/json"}
    assert SECRET not in json.dumps([spec.path, dict(spec.params), dict(spec.headers)])
    assert SEARXNG.needs_key is False


def test_searxng_parses_only_title_url_content() -> None:
    """P7-S12-A02 searxng 只取 ``results[].title / url / content``；``answers`` / ``infoboxes``（实例合成的内容）不取；
    非对象条目、缺网址条目跳过。"""

    body = json.dumps(
        {
            "query": "q",
            "results": [
                {"title": "t1", "url": "https://a.example/1", "content": "c1", "engine": "x", "score": 1.0},
                "not-a-dict",
                {"title": "no url"},
                {"url": "https://a.example/2"},
            ],
            "answers": [{"answer": "合成的答案"}],
            "infoboxes": [{"content": "infobox"}],
        }
    ).encode()
    hits = searxng.parse_response(body)
    assert [(hit.title, hit.snippet, hit.url) for hit in hits] == [("t1", "c1", "https://a.example/1"), ("", "", "https://a.example/2")]


@pytest.mark.parametrize("body", [b"{ not json", b"[1, 2]", b'{"query": "q"}', b'{"results": "nope"}', b"\xff\xfe"])
def test_searxng_rejects_bad_shapes_without_echoing(body: bytes) -> None:
    """P7-S12-A02 不是合法 JSON / 顶层不是对象 / 缺 ``results`` 列表 → ``AdapterParseError``，消息不含响应原文。"""

    with pytest.raises(AdapterParseError) as excinfo:
        searxng.parse_response(body)
    assert "nope" not in str(excinfo.value) and "not json" not in str(excinfo.value)


def test_brave_puts_the_token_only_in_its_header() -> None:
    """P7-S12-A02 brave：``/res/v1/web/search``，``count`` 夹到 1–20；令牌只在 ``X-Subscription-Token`` 头里，不进路径与查询串；
    ``RequestSpec`` 的 ``repr`` 不含令牌。"""

    spec = brave.build_request("给水管道", 3, SECRET)
    assert spec.path == "/res/v1/web/search"
    assert dict(spec.params) == {"q": "给水管道", "count": "3"}
    assert spec.headers["X-Subscription-Token"] == SECRET
    assert SECRET not in spec.path and SECRET not in json.dumps(dict(spec.params))
    assert SECRET not in repr(spec)
    assert brave.build_request("q", 99, SECRET).params["count"] == "20"
    assert brave.build_request("q", 0, SECRET).params["count"] == "1"
    assert BRAVE.needs_key is True


def test_brave_parses_web_results_and_tolerates_a_missing_web_block() -> None:
    """P7-S12-A02 brave 取 ``web.results[].title / url / description``；没有网页结果时 ``web`` 块可以缺席（零结果，不是坏响应）。"""

    body = json.dumps(
        {"type": "search", "query": {"original": "q"},
         "web": {"type": "search", "results": [{"title": "t", "url": "https://b.example/", "description": "d", "age": "x"}]}}
    ).encode()
    assert [(hit.title, hit.snippet, hit.url) for hit in brave.parse_response(body)] == [("t", "d", "https://b.example/")]
    assert brave.parse_response(json.dumps({"query": {"original": "q"}}).encode()) == []
    with pytest.raises(AdapterParseError):
        brave.parse_response(json.dumps({"web": {"results": "nope"}}).encode())
    with pytest.raises(AdapterParseError):
        brave.parse_response(json.dumps({"hello": "world"}).encode())


def test_unknown_provider_ids_fail_and_there_is_no_fallback() -> None:
    """P7-S12-A02 provider 拼错 → ``KeyError``（装配期转成 ``WEB_SEARCH_CONFIG_INVALID``）；没有"找不到就换一家"。"""

    assert adapter_ids() == frozenset({"searxng", "brave"})
    assert get_adapter("searxng") is SEARXNG and get_adapter("brave") is BRAVE
    with pytest.raises(KeyError):
        get_adapter("searxn")
    with pytest.raises(KeyError):
        get_adapter("google")


def test_a_failing_explicit_provider_is_not_retried_elsewhere(tmp_path: Path) -> None:
    """P7-S12-A02 显式配置的 provider 失败不回退：searxng 返回 500 时，即使环境里有 brave 的密钥也不会去试 brave（只有 ``/search`` 被请求）。"""

    from tests.fixtures.fake_search_server import FakeSearchServer
    from water_agent.runtime.governance.contracts import (
        DEFAULT_LOCAL_CAPABILITIES,
        Capability,
        Principal,
        RequestContext,
    )
    from water_agent.runtime.governance.gateway import ProviderGateway
    from water_agent.web_search.config import WebSearchConfig
    from water_agent.web_search.runtime import WebSearchRuntime
    from water_agent.web_search.tool import build_web_search_tool

    ctx = RequestContext(
        principal=Principal.local("ws-adapter", capabilities=DEFAULT_LOCAL_CAPABILITIES | {Capability.WEB_SEARCH}),
        request_id="ws-adapter", thread_id="ws-adapter",
    )
    gateway = ProviderGateway()
    with FakeSearchServer("status_500") as server:
        config = WebSearchConfig(status="ready", provider="searxng", base_url=server.base_url, allow_private=True, api_key="")
        spec = build_web_search_tool(config, WebSearchRuntime(request_context=lambda: ctx, gateway=lambda: gateway))
        result = spec.execute(spec.args_model(query="给水管道经济流速"))
        paths = {str(item["path"]) for item in server.requests}
    assert result["code"] == "WEB_SEARCH_UNAVAILABLE"
    assert paths == {"/search"}
