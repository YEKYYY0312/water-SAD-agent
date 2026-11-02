"""P7-S12 目的地与传输：基址由运维写死，经 ``governed_get``；禁止目的地零字节、3xx 不跟随、响应上限、Content-Type。"""
from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

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

SRC = Path(__file__).resolve().parents[1] / "src" / "water_agent"
QUERY = "给水管道经济流速"


def _context() -> RequestContext:
    principal = Principal.local("ws-transport", capabilities=DEFAULT_LOCAL_CAPABILITIES | {Capability.WEB_SEARCH})
    return RequestContext(principal=principal, request_id="ws-transport", thread_id="ws-transport")


def _execute(base_url: str, *, allow_private: bool = True, provider: str = "searxng", api_key: str = "") -> dict[str, Any]:
    config = WebSearchConfig(status="ready", provider=provider, base_url=base_url, allow_private=allow_private, api_key=api_key)
    gateway = ProviderGateway()
    ctx = _context()
    spec = build_web_search_tool(config, WebSearchRuntime(request_context=lambda: ctx, gateway=lambda: gateway))
    return spec.execute(spec.args_model(query=QUERY, count=3))


@pytest.fixture()
def server() -> Iterator[FakeSearchServer]:
    with FakeSearchServer("ok") as running:
        yield running


def test_loopback_needs_the_operator_flag_and_sends_zero_bytes_without_it(server: FakeSearchServer) -> None:
    """P7-S12-A03 回环 / 明文 http 基址只在运维显式 ``ALLOW_PRIVATE=1`` 时接受；没开时 ``WEB_SEARCH_UNAVAILABLE`` 且零请求。"""

    refused = _execute(server.base_url, allow_private=False)
    assert refused["code"] == "WEB_SEARCH_UNAVAILABLE"
    assert server.requests == []
    allowed = _execute(server.base_url, allow_private=True)
    assert allowed["ok"] is True and allowed["count"] == 3
    assert len(server.requests) == 1


@pytest.mark.parametrize(
    "base_url",
    [
        "http://169.254.169.254/",  # 云元数据：即使 allow_private 也永远拒绝
        "http://2130706433/",  # 十进制整数主机名
        "http://0177.0.0.1/",  # 八进制
        "https://user:pw@search.example/",  # userinfo
        "ftp://search.example/",  # 非 http(s)
    ],
)
def test_forbidden_destinations_are_refused_before_any_byte(base_url: str) -> None:
    """P7-S12-A03 禁止目的地在发出任何字节之前被拒，返回稳定码（不泄漏网段分类与内核码）。"""

    result = _execute(base_url, allow_private=True)
    assert result["error"] is True and result["code"] == "WEB_SEARCH_UNAVAILABLE"
    assert "EXTERNAL_" not in result["message"] and "MCP_" not in result["message"]


def test_dns_rebinding_to_metadata_is_refused_with_zero_requests(server: FakeSearchServer, monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S12-A03 主机名解析到 [回环, 云元数据]：整体分类、任一禁止即拒绝，假服务零请求（钉死 IP 的内核语义）。"""

    from water_agent.web_search import tool as tool_module

    original = tool_module.governed_get

    def rebinding(url: str, **kwargs: Any) -> Any:
        kwargs["resolver"] = lambda host, port: ["127.0.0.1", "169.254.169.254"]
        return original(url, **kwargs)

    monkeypatch.setattr(tool_module, "governed_get", rebinding)
    result = _execute(f"http://rebind.test:{server.port}/", allow_private=True)
    assert result["code"] == "WEB_SEARCH_UNAVAILABLE"
    assert server.requests == []


def test_redirects_are_not_followed() -> None:
    """P7-S12-A03 3xx 不跟随：返回 ``WEB_SEARCH_BAD_RESPONSE``，重定向的落点零请求。"""

    with FakeSearchServer("ok") as landing, FakeSearchServer("redirect_to_loopback", redirect_to=landing.base_url + "/landed") as hop:
        result = _execute(hop.base_url)
        assert result["code"] == "WEB_SEARCH_BAD_RESPONSE"
        assert len(hop.requests) == 1 and landing.requests == []


@pytest.mark.parametrize("mode", ["oversize", "wrong_ctype", "junk_json", "wrong_shape"])
def test_unusable_responses_are_bad_responses(mode: str) -> None:
    """P7-S12-A03 响应超过 256 KiB 即中止；``Content-Type`` 不是 JSON、不是合法 JSON、形状不对 → ``WEB_SEARCH_BAD_RESPONSE``；
    错误文本不含响应内容。"""

    with FakeSearchServer(mode) as server:
        result = _execute(server.base_url)
    assert result["code"] == "WEB_SEARCH_BAD_RESPONSE"
    assert "x" * 20 not in str(result) and "hello" not in str(result)


def test_successful_request_shape(server: FakeSearchServer) -> None:
    """P7-S12-A03 成功请求：``GET /search?q=…&format=json``、``Accept: application/json``，查询原样作为 ``q``；只发一次。"""

    result = _execute(server.base_url)
    assert result["ok"] is True
    (request,) = server.requests
    assert request["path"] == "/search"
    assert request["query"] == QUERY
    assert "format=json" in str(request["raw_query"])
    assert request["accept"] == "application/json"


def _imported_modules(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


_RAW_NETWORK = ("socket", "ssl", "http.client", "urllib.request", "urllib3", "requests", "httpx", "aiohttp")


def test_web_search_package_has_no_raw_network_primitive_and_never_calls_open_exchange() -> None:
    """P7-S12-A03 ``web_search/`` 不 import 原始网络原语，也不直接调用 ``open_exchange``（出站只经 ``governed_get``，
    SDD17-I08 的登记表因此不需要新增调用方）。"""

    modules = sorted(path for path in (SRC / "web_search").rglob("*.py") if "__pycache__" not in path.parts)
    assert modules, "没有扫描到任何模块"
    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = _imported_modules(tree)
        bad = sorted(name for name in imported if any(name == raw or name.startswith(f"{raw}.") for raw in _RAW_NETWORK))
        assert not bad, f"{path.name} import 了网络原语：{bad}"
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and ((isinstance(node.func, ast.Name) and node.func.id == "open_exchange")
                 or (isinstance(node.func, ast.Attribute) and node.func.attr == "open_exchange"))
        ]
        assert not calls, f"{path.name} 直接调用了 open_exchange"


def test_raw_network_check_catches_a_socket_import() -> None:
    """P7-S12-A03 反证：同一个判定对一段 ``import socket`` 的源码必须报出它。"""

    imported = _imported_modules(ast.parse("import socket\nfrom http.client import HTTPSConnection\n"))
    assert {"socket", "http.client"} <= imported
