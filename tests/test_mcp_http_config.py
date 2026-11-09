"""P7-S8 Step 2：Streamable HTTP server 的配置（``docs/design/16-p7-plugin-mcp.md`` §7 P7-S8-A01）。

配置形状只是出口的第一道闸：``url`` 是运维写死的出口，必须在加载期就把非 https、userinfo、fragment 与
各种让解析器把名字当成回环地址的写法挡在外面；混写 stdio 与 HTTP 字段一律 ``MCP_CONFIG_INVALID``。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from water_agent.mcp.config import (
    WEB_LAYER_SERVER_KEYS,
    MCPGatewayConfig,
    MCPOAuthConfig,
    MCPServerConfig,
    ServerTransport,
    child_env,
    load_gateway_config,
    merge_overlay,
    merge_web_layer,
)
from water_agent.mcp.errors import (
    MCP_CONFIG_INVALID,
    MCP_ERROR_CODES,
    MCPError,
)


def http_server(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "server_id": "remote",
        "transport": "streamable_http",
        "url": "https://mcp.example.com/mcp",
        "allow": ["search"],
    }
    payload.update(overrides)
    return payload


def build(**overrides: Any) -> MCPServerConfig:
    return MCPServerConfig.model_validate(http_server(**overrides))


def test_transport_fields_are_mutually_exclusive() -> None:
    """P7-S8-A01：stdio 与 HTTP 的字段混写即错，不静默忽略。"""

    assert build().transport is ServerTransport.STREAMABLE_HTTP
    # HTTP 不能带 stdio 字段
    for extra in (
        {"command": "/usr/bin/true"},
        {"args": ["-m", "x"]},
        {"env": {"A": "b"}},
        {"env_file": "/tmp/x.env"},
        {"cwd": "/tmp"},
    ):
        with pytest.raises(ValidationError, match="stdio 专用"):
            build(**extra)
    with pytest.raises(ValidationError, match="必须配置 url"):
        MCPServerConfig.model_validate({"server_id": "remote", "transport": "streamable_http"})
    # stdio 不能带 HTTP 字段，也不能缺 command
    stdio: dict[str, Any] = {"server_id": "local", "command": "/usr/bin/true"}
    assert MCPServerConfig.model_validate(stdio).transport is ServerTransport.STDIO
    for extra in (
        {"url": "https://mcp.example.com/mcp"},
        {"allow_private_network": True},
        {"bearer_credential": "API_KEY", "credential_env": ["API_KEY"]},
        {"oauth": {"client_id": "x"}},
    ):
        with pytest.raises(ValidationError, match="HTTP 专用"):
            MCPServerConfig.model_validate({**stdio, **extra})
    with pytest.raises(ValidationError, match="必须配置 command"):
        MCPServerConfig.model_validate({"server_id": "local"})
    # 一台 server 只能有一种认证方式
    with pytest.raises(ValidationError, match="互斥"):
        build(
            bearer_credential="API_KEY",
            credential_env=["API_KEY"],
            oauth={"client_id": "x"},
        )
    # 槽位必须先声明，也不能声明了却没人用（HTTP 的槽位不会注入任何环境变量）
    with pytest.raises(ValidationError, match="必须先在本 server 的 credential_env 里声明"):
        build(bearer_credential="API_KEY")
    with pytest.raises(ValidationError, match="没有被 bearer_credential"):
        build(credential_env=["API_KEY"])
    # HTTP server 没有子进程环境
    with pytest.raises(MCPError) as caught:
        child_env(build())
    assert caught.value.code == MCP_CONFIG_INVALID


@pytest.mark.parametrize(
    "url",
    [
        "http://mcp.example.com/mcp",          # 明文，且没开 allow_private_network
        "ftp://mcp.example.com/mcp",
        "mcp.example.com/mcp",
        "https://user:pass@mcp.example.com/mcp",
        "https://user@mcp.example.com/mcp",
        "https://mcp.example.com/mcp#frag",
        "https://mcp.example.com/mcp#",
        "https://mcp.example.com:0/mcp",
        "https://mcp.example.com:99999/mcp",
        "https:///mcp",
        "https://mcp.example.com/mcp ",
        " https://mcp.example.com/mcp",
        "https://mcp.example.com/m\ncp",
        "https://2130706433/mcp",              # 十进制整数 = 127.0.0.1
        "https://0177.0.0.1/mcp",              # 八进制写法
        "https://127.1/mcp",                   # 短写
        "https://0x7f.0.0.1/mcp",
        "https://1.2.3/mcp",
        "https://256.1.1.1/mcp",
        "https://" + "a" * 300 + ".com/mcp",
        "https://" + "x" * 2100,
    ],
)
def test_url_is_validated_at_load_time(url: str) -> None:
    """P7-S8-A01：URL 在加载期校验——只 https（私网开关才收 http），拒 userinfo / fragment / 非规范 IPv4。"""

    with pytest.raises(ValidationError):
        build(url=url)


def test_url_is_validated_at_load_time_accepts_the_normal_forms() -> None:
    assert build(url="https://MCP.Example.COM:8443/a/b?tenant=1").url == "https://mcp.example.com:8443/a/b?tenant=1"
    assert build(url="https://mcp.example.com:443/mcp").url == "https://mcp.example.com/mcp"
    assert build(url="https://[2001:db8::1]:8443/mcp").url == "https://[2001:db8::1]:8443/mcp"
    assert build(url="https://93.184.216.34/mcp").url == "https://93.184.216.34/mcp"
    assert build(url="https://例え.jp/mcp").url == "https://xn--r8jz45g.jp/mcp"
    # 明文 http 只在 allow_private_network=true 时接受
    assert build(url="http://127.0.0.1:8080/mcp", allow_private_network=True).url == "http://127.0.0.1:8080/mcp"
    with pytest.raises(ValidationError):
        build(url="http://127.0.0.1:8080/mcp", allow_private_network=False)


def test_oauth_registration_and_secret_rules() -> None:
    """oauth 必须二选一（预注册 / 动态注册）；公共客户端不带密钥；需要密钥的认证方式必须有槽位。"""

    ok = build(oauth={"client_id": "wa", "allowed_issuers": ["https://auth.example.com/"]})
    assert ok.oauth is not None and ok.oauth.allowed_issuers == ("https://auth.example.com",)
    assert build(oauth={"dynamic_registration": True}).oauth is not None
    secret = build(
        credential_env=["CLIENT_SECRET"],
        oauth={"client_id": "wa", "client_secret_credential": "CLIENT_SECRET",
               "token_endpoint_auth_method": "client_secret_post",
               "allowed_issuers": ["https://auth.example.com"]},
    )
    assert secret.oauth is not None and secret.oauth.client_secret_credential == "CLIENT_SECRET"
    for bad in (
        {},                                                      # 既没预注册也没动态注册
        {"client_id": "wa", "dynamic_registration": True},       # 二者同时
        {"dynamic_registration": True, "token_endpoint_auth_method": "client_secret_basic"},
        {"client_id": "wa", "token_endpoint_auth_method": "client_secret_basic"},   # 缺密钥槽位
        {"client_id": "wa", "client_secret_credential": "S1234567"},                 # none 不能配密钥
        {"client_id": "wa", "allowed_issuers": ["http://auth.example.com"]},         # issuer 必须 https
        {"client_id": "wa", "allowed_issuers": ["https://a.example.com", "https://a.example.com/"]},
        {"client_id": "wa", "scopes": ["has space"]},
        {"client_id": "wa", "unknown_key": 1},                                       # extra=forbid
    ):
        with pytest.raises(ValidationError):
            MCPOAuthConfig.model_validate(bad)


def test_gateway_config_loads_http_servers_from_a_file(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": [http_server(), {"server_id": "local", "command": "/usr/bin/true"}]}))
    config = load_gateway_config(path)
    assert config is not None
    assert [(s.server_id, s.transport.value) for s in config.enabled_servers()] == [
        ("remote", "streamable_http"), ("local", "stdio"),
    ]
    path.write_text(json.dumps({"servers": [http_server(url="https://u:p@x.example.com/mcp")]}))
    with pytest.raises(MCPError) as caught:
        load_gateway_config(path)
    assert caught.value.code == MCP_CONFIG_INVALID and "userinfo" in caught.value.message


def test_overlay_replaces_url_and_oauth_as_a_whole() -> None:
    base = {"servers": [http_server(url="https://a.example.com/mcp", oauth={"client_id": "wa"})]}
    merged = merge_overlay(base, {"servers": [{"server_id": "remote", "url": "https://b.example.com/mcp"}]})
    config = MCPGatewayConfig.model_validate(merged)
    assert config.servers[0].url == "https://b.example.com/mcp"
    assert config.servers[0].oauth is not None, "字段逐个覆盖：没写的 oauth 保持下层的值"


def test_web_layer_cannot_write_transport_fields() -> None:
    """P7-S8-A01：Web 管理层的可写键不变——面板改不了 URL、授权服务器与传输方式。"""

    assert WEB_LAYER_SERVER_KEYS == frozenset({"server_id", "disabled", "allow"})
    base = {"servers": [http_server()]}
    for key, value in (
        ("url", "https://evil.example.com/mcp"),
        ("transport", "stdio"),
        ("oauth", {"client_id": "x"}),
        ("bearer_credential", "API_KEY"),
        ("allow_private_network", True),
    ):
        with pytest.raises(ValueError, match="只能写"):
            merge_web_layer(base, [{"server_id": "remote", key: value}])
    merged, orphans = merge_web_layer(base, [{"server_id": "remote", "disabled": True, "allow": ["a"]}])
    assert orphans == () and merged["servers"][0]["disabled"] is True


def test_new_error_codes_are_registered_and_stable() -> None:
    """MCP_ERROR_CODES 只增不减：本步新增 5 个。P7-S14 又新增 8 个 ``MCP_REGISTRY_*``（总数 20 → 28，见
    ``tests/test_mcp_registry_client.py::test_new_error_codes_are_registered``）。"""

    new = {
        "MCP_DESTINATION_FORBIDDEN", "MCP_PROTOCOL_UNSUPPORTED", "MCP_OAUTH_REQUIRED",
        "MCP_OAUTH_UNAVAILABLE", "MCP_OAUTH_INSUFFICIENT_SCOPE",
    }
    assert new <= MCP_ERROR_CODES
    assert len(MCP_ERROR_CODES) == 28
    for code in new:
        assert MCPError(code, "x").code == code
