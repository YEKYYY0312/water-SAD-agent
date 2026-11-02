"""P7-S12 能力与开关：部署配置的解析（缺项 skipped、取值非法装配期失败），工具契约，``NETWORK_FETCH`` 仍被拒。"""
from __future__ import annotations

from pathlib import Path

import pytest

from water_agent.runtime.governance.contracts import (
    DANGEROUS_CAPABILITIES,
    DEFAULT_LOCAL_CAPABILITIES,
    Capability,
    Principal,
    SourceTag,
)
from water_agent.web_search.config import (
    WEB_SEARCH_CONFIG_INVALID,
    WebSearchConfigError,
    resolve_switch,
    resolve_web_search_config,
)

SECRET = "brave-secret-key-0123456789abcdef"
SEARXNG = {"WATER_AGENT_WEB_SEARCH": "on", "WATER_AGENT_WEB_SEARCH_PROVIDER": "searxng",
           "WATER_AGENT_WEB_SEARCH_BASE_URL": "https://search.example/"}


@pytest.fixture(autouse=True)
def _no_credentials_volume(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WATER_AGENT_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    monkeypatch.delenv("WATER_AGENT_WEB_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("WATER_AGENT_WEB_SEARCH_API_KEY_FILE", raising=False)


@pytest.mark.parametrize("value", [None, "", "off", " OFF "])
def test_switch_defaults_to_off(value: str | None) -> None:
    """P7-S12-A01 开关缺省 / 空串 / ``off`` → ``off``（外发默认关）。"""

    environ = {} if value is None else {"WATER_AGENT_WEB_SEARCH": value}
    assert resolve_switch(environ) == "off"
    assert resolve_web_search_config(environ).status == "off"


@pytest.mark.parametrize("value", ["yes", "1", "true", "enabled"])
def test_invalid_switch_values_fail_loudly(value: str) -> None:
    """P7-S12-A01 开关取值非法 → 装配期失败（``WEB_SEARCH_CONFIG_INVALID``），不静默当成关闭或打开。"""

    with pytest.raises(WebSearchConfigError) as excinfo:
        resolve_web_search_config({"WATER_AGENT_WEB_SEARCH": value})
    assert excinfo.value.code == WEB_SEARCH_CONFIG_INVALID


def test_misspelled_provider_fails_loudly() -> None:
    """P7-S12-A01 provider id 拼错 → 装配期失败；不回退到别的 provider。"""

    with pytest.raises(WebSearchConfigError):
        resolve_web_search_config({**SEARXNG, "WATER_AGENT_WEB_SEARCH_PROVIDER": "searxn"})


@pytest.mark.parametrize(
    ("missing", "detail"),
    [("WATER_AGENT_WEB_SEARCH_PROVIDER", "provider_missing"), ("WATER_AGENT_WEB_SEARCH_BASE_URL", "base_url_missing")],
)
def test_missing_items_mean_skipped(missing: str, detail: str) -> None:
    """P7-S12-A01 开关 on 但缺 provider / 基址 → ``skipped``（能力不可用就不暴露），并写明缺什么。"""

    environ = {key: value for key, value in SEARXNG.items() if key != missing}
    config = resolve_web_search_config(environ)
    assert config.status == "skipped" and config.detail == detail and not config.ready


def test_base_url_shape_is_judged_at_request_time_not_here() -> None:
    """P7-S12-A01 基址的形状与目的地**不在配置阶段**判定：只在每次请求时由 ``governed_get`` 判定（单一真源；配错的 URL 不拖垮
    没勾选联网的会话）。配置照样 ready，``host`` 对解析不了的基址返回空串。"""

    for base_url in ("ftp://x", "https://user:pw@x/", "http://example.com/"):
        assert resolve_web_search_config({**SEARXNG, "WATER_AGENT_WEB_SEARCH_BASE_URL": base_url}).ready
    assert resolve_web_search_config({**SEARXNG, "WATER_AGENT_WEB_SEARCH_BASE_URL": "http://[::1"}).host == ""


def test_allow_private_is_an_explicit_operator_flag() -> None:
    """P7-S12-A01 私网 / 明文放行只认运维显式的 ``1``。"""

    assert resolve_web_search_config({**SEARXNG, "WATER_AGENT_WEB_SEARCH_ALLOW_PRIVATE": "1"}).allow_private is True
    for value in ("", "0", "true", "yes"):
        assert resolve_web_search_config({**SEARXNG, "WATER_AGENT_WEB_SEARCH_ALLOW_PRIVATE": value}).allow_private is False


def test_brave_needs_its_own_key_and_searxng_never_reads_one() -> None:
    """P7-S12-A01 brave 缺密钥 → ``skipped / api_key_missing``；有密钥 → ready 并带上；searxng 即使环境里有密钥也不读
    （无密钥的 provider 不因别家有密钥而被选用，密钥也不发给不需要它的服务）。"""

    brave = {**SEARXNG, "WATER_AGENT_WEB_SEARCH_PROVIDER": "brave"}
    missing = resolve_web_search_config(brave, getenv=lambda name: None)
    assert missing.status == "skipped" and missing.detail == "api_key_missing"
    ready = resolve_web_search_config(brave, getenv=lambda name: SECRET if name == "WATER_AGENT_WEB_SEARCH_API_KEY" else None)
    assert ready.ready and ready.api_key == SECRET
    searxng = resolve_web_search_config(SEARXNG, getenv=lambda name: SECRET)
    assert searxng.ready and searxng.api_key == ""


def test_repr_and_host_leak_nothing() -> None:
    """P7-S12-A01 ``repr`` 不含密钥与基址；``host`` 只有主机名（审计 / 熔断键 / 面板用）。"""

    config = resolve_web_search_config(
        {**SEARXNG, "WATER_AGENT_WEB_SEARCH_PROVIDER": "brave", "WATER_AGENT_WEB_SEARCH_BASE_URL": "https://API.Search.example:8443/v1/"},
        getenv=lambda name: SECRET,
    )
    assert config.host == "api.search.example"
    assert SECRET not in repr(config) and "8443" not in repr(config) and "/v1/" not in repr(config)


def test_capability_surface() -> None:
    """P7-S12-A01 ``WEB_SEARCH`` 不在默认能力集与 Web 成员能力集；``NETWORK_FETCH`` 仍在危险集、构造期仍被拒；
    插件可授予的能力扩为 ``{MCP_INVOKE, WEB_SEARCH}``（P7-I06）。"""

    from water_agent.plugins import GRANTABLE_CAPABILITIES
    from water_agent.webapp.auth.dependencies import _MEMBER_CAPABILITIES

    assert Capability.WEB_SEARCH not in DEFAULT_LOCAL_CAPABILITIES
    assert Capability.WEB_SEARCH not in _MEMBER_CAPABILITIES
    assert Capability.WEB_SEARCH not in DANGEROUS_CAPABILITIES
    assert Capability.NETWORK_FETCH in DANGEROUS_CAPABILITIES
    with pytest.raises(ValueError):
        Principal.local("ws-tool", capabilities=frozenset({Capability.NETWORK_FETCH}))
    assert GRANTABLE_CAPABILITIES == frozenset({Capability.MCP_INVOKE, Capability.WEB_SEARCH})


def test_tool_contract_is_two_parameters_and_marked_as_web_content() -> None:
    """P7-S12-A01 工具契约：只有 ``query``（1–128）与 ``count``（1–5，缺省 3）；结果来源 ``WEB_CONTENT``；超时 15 s；
    多余参数（尤其 ``url``）直接 schema 失败。"""

    from pydantic import ValidationError

    from water_agent.web_search.config import WebSearchConfig
    from water_agent.web_search.runtime import WebSearchRuntime
    from water_agent.web_search.tool import WebSearchArgs, build_web_search_tool

    assert sorted(WebSearchArgs.model_fields) == ["count", "query"]
    assert WebSearchArgs(query="q").count == 3
    for bad in ({"query": ""}, {"query": "x" * 129}, {"query": "q", "count": 0}, {"query": "q", "count": 6},
                {"query": "q", "url": "https://evil.example/"}):
        with pytest.raises(ValidationError):
            WebSearchArgs(**bad)
    spec = build_web_search_tool(WebSearchConfig(status="ready", provider="searxng", base_url="https://s.example/"), WebSearchRuntime())
    assert spec.name == "web_search" and spec.result_source is SourceTag.WEB_CONTENT and spec.timeout_seconds == 15.0
    with pytest.raises(ValueError):
        build_web_search_tool(WebSearchConfig(status="skipped", detail="provider_missing"), WebSearchRuntime())


def test_no_request_context_means_no_request() -> None:
    """P7-S12-A01 没有调用者主体可对账就不发请求（fail closed，返回稳定码）。"""

    from water_agent.web_search.config import WebSearchConfig
    from water_agent.web_search.runtime import WebSearchRuntime
    from water_agent.web_search.tool import build_web_search_tool

    spec = build_web_search_tool(WebSearchConfig(status="ready", provider="searxng", base_url="https://s.example/"), WebSearchRuntime())
    assert spec.execute(spec.args_model(query="给水管道"))["code"] == "WEB_SEARCH_UNAVAILABLE"
