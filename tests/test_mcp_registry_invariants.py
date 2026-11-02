"""P7-S14 不连接条目地址（SDD17-I13，AST 判定，带反证）+ 治理登记。

目录条目的 URL 是第三方目录写的：搜索与提案阶段对条目 URL 零字节、对条目主机零 DNS。结构上的保证是——目录模块与目录路由
**不导入任何 MCP 客户端**，也不直接调用 ``open_exchange``（出站只经 ``governed_get``，只访问运维写死的目录基址）。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from water_agent.runtime.governance import inventory
from water_agent.runtime.governance.contracts import (
    DANGEROUS_CAPABILITIES,
    DEFAULT_LOCAL_CAPABILITIES,
    CallCategory,
    Capability,
    ProviderKind,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "water_agent"
REGISTRY_MODULE = SRC / "mcp" / "registry.py"
ROUTES_MODULE = SRC / "webapp" / "mcp_registry_routes.py"

#: 目录模块 / 路由**不得**碰的名字：任何一个都意味着有路径能连接条目地址或起子进程。
FORBIDDEN_NAMES = frozenset(
    {
        "MCPStdioClient", "MCPHttpClient", "open_bridge", "open_gateway_sessions", "open_exchange", "McpConnectionPool",
        "bridge_server_tools", "MCPServerSession",
    }
)
FORBIDDEN_MODULES = frozenset(
    {
        "water_agent.mcp.client", "water_agent.mcp.http_client", "water_agent.mcp.bridge", "water_agent.mcp.pool",
        "water_agent.mcp.wiring", "water_agent.mcp.oauth",
    }
)
RAW_NETWORK_MODULES = frozenset({"socket", "http.client", "urllib.request", "ssl", "subprocess", "httpx", "requests", "aiohttp"})


def _violations(source: str) -> list[str]:
    """源码里导入了禁用模块 / 禁用名字、或使用了原始网络原语的位置（AST，不看注释与字符串）。"""

    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in FORBIDDEN_MODULES or alias.name.split(".")[0] in RAW_NETWORK_MODULES or alias.name in RAW_NETWORK_MODULES:
                    found.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in FORBIDDEN_MODULES or module in RAW_NETWORK_MODULES or module.split(".")[0] in RAW_NETWORK_MODULES:
                found.append(f"from {module}")
            for alias in node.names:
                if alias.name in FORBIDDEN_NAMES:
                    found.append(f"from {module} import {alias.name}")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            found.append(f"name {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
            found.append(f"attr {node.attr}")
    return found


@pytest.mark.parametrize("path", [REGISTRY_MODULE, ROUTES_MODULE], ids=["mcp/registry.py", "webapp/mcp_registry_routes.py"])
def test_registry_modules_import_no_mcp_client_and_no_raw_network(path: Path) -> None:
    """P7-S14-A04 SDD17-I13：``mcp/registry.py`` 与 ``webapp/mcp_registry_routes.py`` 不导入 ``MCPStdioClient`` / ``MCPHttpClient`` /
    ``open_bridge``（以及连接池、桥接、OAuth、装配），不直接调用 ``open_exchange``，不用原始套接字 / 子进程 / HTTP 客户端。"""

    assert _violations(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "snippet",
    [
        "from water_agent.mcp.client import MCPStdioClient",
        "from water_agent.mcp.http_client import MCPHttpClient",
        "from water_agent.mcp.wiring import open_gateway_sessions",
        "from water_agent.mcp.http_transport import open_exchange",
        "import socket",
        "import subprocess",
        "from water_agent.mcp import bridge\nbridge.open_bridge()",
    ],
)
def test_the_invariant_check_catches_each_forbidden_form(snippet: str) -> None:
    """P7-S14-A04 反证：检查本身有鉴别力——每一种禁用写法放进源码都会被报出。"""

    assert _violations(snippet) != []
    assert _violations("# from water_agent.mcp.client import MCPStdioClient\nx = 'open_exchange'") == []


def test_only_governed_get_is_the_egress_path() -> None:
    """P7-S14-A04 出站只经 ``governed_get``：目录模块调用它，不调用 ``open_exchange``；``open_exchange`` 的调用方登记（SDD17-I08）不变。"""

    source = REGISTRY_MODULE.read_text(encoding="utf-8")
    calls = {
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute))
    }
    assert "governed_get" in calls and "open_exchange" not in calls


def test_the_directory_capability_is_given_to_no_ordinary_principal() -> None:
    """P7-S14-A08 ``MCP_REGISTRY_SEARCH`` 不在默认本地能力集、Web 成员 / 管理员能力集、``GRANTABLE_CAPABILITIES``；
    不在危险集（它是目的地固定的只读外发，不是 ``NETWORK_FETCH``）；``NETWORK_FETCH`` 仍在危险集。"""

    from water_agent.plugins.base import GRANTABLE_CAPABILITIES
    from water_agent.webapp.auth.dependencies import _ADMIN_CAPABILITIES, _MEMBER_CAPABILITIES

    cap = Capability.MCP_REGISTRY_SEARCH
    assert cap not in DEFAULT_LOCAL_CAPABILITIES
    assert cap not in _MEMBER_CAPABILITIES and cap not in _ADMIN_CAPABILITIES
    assert cap not in GRANTABLE_CAPABILITIES
    assert GRANTABLE_CAPABILITIES == frozenset({Capability.MCP_INVOKE, Capability.WEB_SEARCH})
    assert cap not in DANGEROUS_CAPABILITIES and Capability.NETWORK_FETCH in DANGEROUS_CAPABILITIES


def test_the_directory_is_not_a_model_tool() -> None:
    """P7-S14-A08 目录搜索不是工具：没有 ``ToolSpec``、不在任何能力 → 工具登记表里，``tools`` / ``tools_guardrail_validated`` 不变。"""

    registered = {entry.tool_name for entry in (*inventory.TOOL_CAPABILITIES, *inventory.PLUGIN_GRANTED_TOOL_CAPABILITIES)}
    assert not any("registry" in name for name in registered)
    assert "mcp_registry" not in inventory.scan_tool_spec_names()
    coverage = inventory.governance_coverage()
    assert coverage["tools"] == coverage["tools_guardrail_validated"] == 7


def test_the_eleventh_egress_site_is_registered_like_web_search() -> None:
    """P7-S14-A08 第 10 个出站站点 ``mcp_registry``：逻辑调用点 ``mcp/registry.py``、出站原语在 ``mcp/http_transport.py``（经
    ``governed_get``）；画像 15 s、只对瞬时错误重试一次、每主体每分钟 ≤ 20；熔断按目录主机分桶；``GOVERNANCE_CONFIG_VERSION`` 已递进。"""

    from water_agent.mcp.registry import RegistryConfig, provider_call_for
    from water_agent.runtime.governance.config import (
        DEFAULT_GOVERNANCE_CONFIG,
        GOVERNANCE_CONFIG_VERSION,
    )
    from water_agent.runtime.governance.gateway import ProviderGateway

    sites = {site.site_id: site for site in inventory.PROVIDER_CALL_SITES}
    site = sites["mcp_registry"]
    assert len(inventory.PROVIDER_CALL_SITES) == 11
    assert site.module == "water_agent/mcp/registry.py" and site.egress_module == "water_agent/mcp/http_transport.py"
    assert site.provider is ProviderKind.MCP_REGISTRY and site.category is CallCategory.MCP_REGISTRY
    assert site.required_capability is Capability.MCP_REGISTRY_SEARCH
    assert site.governed is True and site.cache_eligible is False
    profile = DEFAULT_GOVERNANCE_CONFIG.profile_for(ProviderKind.MCP_REGISTRY)
    assert profile.timeout_seconds == 15.0 and profile.retry.max_attempts == 2
    assert profile.rate_limit.max_requests_per_minute == 20
    assert GOVERNANCE_CONFIG_VERSION == "p7-s14-2026-11-02"
    call_a = provider_call_for(RegistryConfig(base_url="https://a.example/"))
    call_b = provider_call_for(RegistryConfig(base_url="https://b.example/"))
    assert ProviderGateway.breaker_key(call_a) != ProviderGateway.breaker_key(call_b)
    assert inventory.ungoverned_egress_modules() == frozenset()


def test_directory_reads_are_recorded_with_unknown_usage() -> None:
    """P7-S14-A08 目录读取经网关落账，usage 记**未知**（不估算 token / 成本）。"""

    from tests.fixtures.fake_mcp_registry import EntrySink, FakeMcpRegistry
    from water_agent.mcp import registry as reg
    from water_agent.runtime.governance.gateway import ProviderGateway

    records: list[object] = []
    gateway = ProviderGateway(on_usage=records.append)
    with EntrySink() as sink, FakeMcpRegistry("ok", sink_url=sink.url) as server:
        config = reg.RegistryConfig(base_url=server.base_url, allow_private=True)
        reg.RegistryCatalogClient(config, reg.registry_context("admin-1", request_id="r"), gateway=gateway).search()
    assert records, "目录读取必须落账"
    assert all(getattr(record, "usage_known", True) is False for record in records)
    assert {getattr(record, "provider", None) for record in records} == {ProviderKind.MCP_REGISTRY}
