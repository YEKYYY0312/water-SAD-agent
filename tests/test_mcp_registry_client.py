"""P7-S14 目录协议 v1 客户端（``mcp/registry.py``）：分页 / 字节 / 条目上限、整页严格校验、失败码、仅经 ``governed_get``。

目录用 ``tests/fixtures/fake_mcp_registry.py``（进程内线程）。条目地址指向 ``EntrySink``——只数连接与字节的 TCP 监听口。
"""
from __future__ import annotations

import socket
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from tests.fixtures.fake_mcp_registry import EntrySink, FakeMcpRegistry
from water_agent.mcp import registry as reg
from water_agent.mcp.errors import (
    MCP_ERROR_CODES,
    MCP_REGISTRY_AUTH,
    MCP_REGISTRY_BAD_RESPONSE,
    MCP_REGISTRY_NOT_CONFIGURED,
    MCP_REGISTRY_RATE_LIMITED,
    MCP_REGISTRY_UNAVAILABLE,
)
from water_agent.runtime.governance.gateway import ProviderGateway


@contextmanager
def registry(modes: str = "ok", *, allow_private: bool = True) -> Iterator[tuple[reg.RegistryCatalogClient, FakeMcpRegistry, EntrySink]]:
    with EntrySink() as sink, FakeMcpRegistry(modes, sink_url=sink.url) as server:
        config = reg.RegistryConfig(base_url=server.base_url, allow_private=allow_private)
        client = reg.RegistryCatalogClient(config, reg.registry_context("admin-1", request_id="req-client"), gateway=ProviderGateway())
        yield client, server, sink


def _fail(client: reg.RegistryCatalogClient, query: str = "") -> str:
    with pytest.raises(reg.McpRegistryError) as excinfo:
        client.search(query)
    return excinfo.value.code


def test_search_reads_one_page_and_never_touches_entry_addresses() -> None:
    """P7-S14-A01 良性目录：一页读完，请求只发往目录（路径 ``/servers``、``limit=20``）；条目地址零连接零字节。"""

    with registry("ok") as (client, server, sink):
        result = client.search()
        assert [entry.name for entry in result.entries] == ["weather-station", "pipe-calc", "local-cad", "legacy-sse"]
        assert result.pages == 1 and result.truncated is False
        assert [(item["path"], item["limit"]) for item in server.requests] == [("/servers", "20")]
        assert (sink.contacts, sink.bytes_received) == (0, 0)


def test_query_and_cursor_are_sent_as_given() -> None:
    """P7-S14-A01 搜索词与游标原样作为查询参数发送；搜索词在服务端过滤。"""

    with registry("ok") as (client, server, _sink):
        result = client.search("pipe")
        assert [entry.name for entry in result.entries] == ["pipe-calc"]
        assert server.requests[-1]["query"] == "pipe"


def test_paging_is_capped_at_five_pages() -> None:
    """P7-S14-A01 目录永远说"还有下一页"：至多读 5 页、标记 ``truncated``，总条数 ≤ 100。"""

    with registry("paging_many") as (client, server, sink):
        result = client.search()
        assert result.pages == reg.MAX_PAGES == 5
        assert result.truncated is True
        assert len(result.entries) == 100
        assert len(server.requests) == 5
        assert [item["cursor"] for item in server.requests] == ["", "p1", "p2", "p3", "p4"]
        assert sink.contacts == 0


def test_cumulative_size_across_pages_is_capped() -> None:
    """P7-S14-A01 总量上限 256 KiB 管的是**多页累计**：每页都在单页字节上限之内（~100 KiB），第 3 页时累计超限，整次失败。
    （单页超限由 ``oversize_total`` 覆盖；这条专门证明累计检查本身在起作用——它与单页上限互为冗余时不会被发现。）"""

    with registry("paging_heavy") as (client, server, sink):
        assert _fail(client) == MCP_REGISTRY_BAD_RESPONSE
        assert 2 <= len(server.requests) < reg.MAX_PAGES, "第 3 页读完就应中止，而不是读满 5 页"
        assert sink.contacts == 0


@pytest.mark.parametrize(
    "mode",
    ["junk", "bad_shape", "bad_entry_shape", "bad_cursor", "oversize_entry", "oversize_total", "homoglyph_names", "wrong_ctype", "redirect"],
)
def test_malformed_pages_are_rejected_whole(mode: str) -> None:
    """P7-S14-A01 整页严格校验：不是 JSON / 形状不对 / 版本号不是整数 / 游标含 HTML / 单条超 16 KiB / 总量超 256 KiB /
    名字含同形字 / Content-Type 不对 / 3xx 重定向——一律 ``MCP_REGISTRY_BAD_RESPONSE``，不部分采纳；重定向目标（条目地址）零连接。"""

    with registry(mode) as (client, _server, sink):
        assert _fail(client) == MCP_REGISTRY_BAD_RESPONSE
        assert (sink.contacts, sink.bytes_received) == (0, 0)


@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("status_401", MCP_REGISTRY_AUTH),
        ("status_403", MCP_REGISTRY_AUTH),
        ("status_429", MCP_REGISTRY_RATE_LIMITED),
        ("status_500", MCP_REGISTRY_UNAVAILABLE),
        ("status_503", MCP_REGISTRY_UNAVAILABLE),
    ],
)
def test_failure_codes_are_stable_and_never_echo_upstream(mode: str, code: str) -> None:
    """P7-S14-A01 状态码 → 稳定码；错误文本只含固定文案，不含上游正文 / 网址 / 密钥 / 本机回环地址。"""

    with registry(mode) as (client, _server, _sink), pytest.raises(reg.McpRegistryError) as excinfo:
        client.search()
    text = str(excinfo.value) + excinfo.value.message
    assert excinfo.value.code == code
    for marker in ("registry-upstream", "SECRET", "upstream.internal", "sk-live", "127.0.0.1", "://"):
        assert marker not in text


def test_unconfigured_registry_fails_before_any_request() -> None:
    """P7-S14-A01 没配基址 = 功能关闭：``MCP_REGISTRY_NOT_CONFIGURED``，不发请求。"""

    client = reg.RegistryCatalogClient(reg.RegistryConfig(base_url=""), reg.registry_context("admin-1", request_id="r"))
    with pytest.raises(reg.McpRegistryError) as excinfo:
        client.search()
    assert excinfo.value.code == MCP_REGISTRY_NOT_CONFIGURED


def test_plain_http_and_loopback_need_the_operator_flag_and_send_zero_bytes_without_it() -> None:
    """P7-S14-A01 运维没放行私网时，回环 / 明文 http 目录一律禁止且**零字节发出**（``EXTERNAL_DESTINATION_FORBIDDEN`` →
    ``MCP_REGISTRY_UNAVAILABLE``）；只数连接尝试，不看目录收到几个请求。"""

    attempts: list[Any] = []
    real_connect = socket.socket.connect

    def spy(sock: socket.socket, address: Any) -> Any:
        attempts.append(address)
        return real_connect(sock, address)

    with registry("ok", allow_private=False) as (client, server, _sink):
        socket.socket.connect = spy  # type: ignore[method-assign]
        try:
            assert _fail(client) == MCP_REGISTRY_UNAVAILABLE
        finally:
            socket.socket.connect = real_connect  # type: ignore[method-assign]
        assert server.requests == []
        assert [item for item in attempts if isinstance(item, tuple) and item[1] == server.port] == []


@pytest.mark.parametrize("query", ["x" * 65, "line1\nline2", "tab\there", "bad\x07bell", "tag" + chr(0xE0069), "zero\u200bwidth"])
def test_bad_queries_are_refused_before_any_request(query: str) -> None:
    """P7-S14-A01 搜索词：≤ 64 字符、拒绝控制字符 / 换行 / 不可见字符；不合格不发请求。"""

    with registry("ok") as (client, server, _sink):
        assert _fail(client, query) == MCP_REGISTRY_BAD_RESPONSE
        assert server.requests == []


@pytest.mark.parametrize("cursor", ["<script>", "a b", "x" * 129, "../etc"])
def test_bad_cursors_are_refused_before_any_request(cursor: str) -> None:
    """P7-S14-A01 游标：不透明串，≤ 128 字符、``[A-Za-z0-9_\\-.=]``；不合格不发请求。"""

    with registry("ok") as (client, server, _sink):
        with pytest.raises(reg.McpRegistryError):
            client.search("", cursor)
        assert server.requests == []


def test_registry_is_a_registered_governed_site() -> None:
    """P7-S14-A01 目录读取经 ``ProviderGateway``：缺 ``MCP_REGISTRY_SEARCH`` 能力的主体被网关拒绝（零请求）。"""

    from water_agent.runtime.governance.contracts import Principal, RequestContext

    with registry("ok") as (client, server, _sink):
        bare = RequestContext(principal=Principal.local("no-cap", capabilities=frozenset()), request_id="r")
        unprivileged = reg.RegistryCatalogClient(client.config, bare, gateway=ProviderGateway())
        with pytest.raises(reg.McpRegistryError):
            unprivileged.search()
        assert server.requests == []


def test_new_error_codes_are_registered() -> None:
    """P7-S14-A01 新增的 8 个 ``MCP_REGISTRY_*`` 稳定码都在 ``MCP_ERROR_CODES``（只增不减）。"""

    new = {code for code in MCP_ERROR_CODES if code.startswith("MCP_REGISTRY_")}
    assert new == {
        "MCP_REGISTRY_NOT_CONFIGURED", "MCP_REGISTRY_UNAVAILABLE", "MCP_REGISTRY_AUTH", "MCP_REGISTRY_RATE_LIMITED",
        "MCP_REGISTRY_BAD_RESPONSE", "MCP_REGISTRY_ENTRY_NOT_FOUND", "MCP_REGISTRY_ENTRY_UNSAFE", "MCP_REGISTRY_NOT_PROPOSABLE",
    }
    assert len(MCP_ERROR_CODES) == 28


def test_config_reads_only_the_operator_environment() -> None:
    """P7-S14-A01 配置：未设 / 空白 = 关闭；``ALLOW_PRIVATE`` 只认字面 ``1``；``repr`` 不泄漏基址。"""

    assert reg.resolve_registry_config({}).configured is False
    assert reg.resolve_registry_config({reg.ENV_URL: "   "}).configured is False
    on = reg.resolve_registry_config({reg.ENV_URL: "https://registry.example.com/v1", reg.ENV_ALLOW_PRIVATE: "1"})
    assert on.configured and on.allow_private and on.host == "registry.example.com"
    assert reg.resolve_registry_config({reg.ENV_URL: "https://r.example.com", reg.ENV_ALLOW_PRIVATE: "true"}).allow_private is False
    assert "registry.example.com" not in repr(on)
