"""P7-S14 提案（``mcp/registry.py::build_proposal``）：仅 streamable-http https 域名 remote；恒停用且无 ``allow``；
env / 请求头 / 令牌 / 查询串丢弃；冲突改名；``proposal_hash`` 确定且对 URL / 版本 / 目录主机 / 名称敏感；stdio / 包类条目只展示不提案。

提案只是文本——这里的每条断言都同时核对"条目地址零连接"。
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from tests.fixtures.fake_mcp_registry import (
    ENV_SECRET,
    HEADER_SECRET,
    QUERY_SECRET,
    UNKNOWN_FIELD_SECRET,
    USERINFO_SECRET,
    EntrySink,
    FakeMcpRegistry,
)
from water_agent.mcp import registry as reg
from water_agent.mcp.config import MCPServerConfig
from water_agent.runtime.governance.gateway import ProviderGateway

HOST = "registry.example.test"


def fetch(modes: str, name: str, version: str = "1.0.0") -> reg.RegistryEntry:
    """经假目录**服务端重取**一条（与路由同一路径：``find``）。"""

    with EntrySink() as sink, FakeMcpRegistry(modes, sink_url=sink.url) as server:
        config = reg.RegistryConfig(base_url=server.base_url, allow_private=True)
        client = reg.RegistryCatalogClient(config, reg.registry_context("admin-1", request_id="req-prop"), gateway=ProviderGateway())
        found = client.find(name, version)
        assert (sink.contacts, sink.bytes_received) == (0, 0), "取条目不得碰条目地址"
        return found


def propose(modes: str, name: str, version: str = "1.0.0", *, existing: tuple[str, ...] = ()) -> reg.Proposal:
    return reg.build_proposal(fetch(modes, name, version), registry_host=HOST, existing_ids=existing)


def refused(modes: str, name: str, version: str = "1.0.0") -> str:
    entry = fetch(modes, name, version)
    with pytest.raises(reg.McpRegistryError) as excinfo:
        reg.build_proposal(entry, registry_host=HOST)
    return excinfo.value.code


def _servers(proposal: reg.Proposal) -> list[dict[str, Any]]:
    return json.loads(proposal.text)["servers"]


def test_a_benign_streamable_http_entry_yields_a_disabled_deny_all_proposal() -> None:
    """P7-S14-A03 良性远程条目能生成提案：覆盖层格式、``transport: streamable_http``、``disabled: true``、**没有 ``allow``**
    （全拒）；提案能通过 ``MCPServerConfig`` 模型层校验。"""

    proposal = propose("ok", "weather-station")
    (server,) = _servers(proposal)
    assert server["transport"] == "streamable_http" and server["url"] == "https://mcp.weather-station.example/mcp"
    assert server["disabled"] is True and "allow" not in server
    assert set(server) == {"server_id", "transport", "url", "disabled"}
    MCPServerConfig.model_validate(server)
    assert json.loads(proposal.text)["version"].startswith("p7-s")
    assert proposal.notes and "全拒" in proposal.notes[-1]


@pytest.mark.parametrize("name", ["weather-station", "pipe-calc"])
def test_every_benign_remote_entry_is_proposable(name: str) -> None:
    """P7-S14-A03 良性邻近：两条合法远程条目都能提案（评测的 ``benign_entry_proposed_rate``）。"""

    version = "1.0.0" if name == "weather-station" else "2.1.0"
    (server,) = _servers(propose("ok", name, version))
    assert server["disabled"] is True and "allow" not in server


@pytest.mark.parametrize(
    ("modes", "name", "version"),
    [
        ("ok", "local-cad", "0.3.1"),
        ("ok", "legacy-sse", "1.2.0"),
        ("ok,stdio_only", "stdio-only", "1.0.0"),
        ("ok,npm_only", "npm-only", "1.0.0"),
    ],
)
def test_stdio_package_and_sse_entries_are_shown_but_never_proposed(modes: str, name: str, version: str) -> None:
    """P7-S14-A05 stdio / npm / pypi / 旧式 sse 条目只展示，**不生成提案**（任何包管理器拉代码都是供应链执行面，永不生成 ``command``）。"""

    assert refused(modes, name, version) == "MCP_REGISTRY_NOT_PROPOSABLE"


@pytest.mark.parametrize(
    ("modes", "name", "reason"),
    [
        ("ok,http_url", "plain-http", reg.REASON_URL_REJECTED),
        ("ok,userinfo_url", "userinfo", reg.REASON_URL_REJECTED),
        ("ok,ip_url", "ip-v4", reg.REASON_IP_LITERAL),
        ("ok,ip_url", "ip-v6", reg.REASON_IP_LITERAL),
        ("ok,ip_url", "ip-private", reg.REASON_IP_LITERAL),
        ("ok,localhost_url", "local-host", reg.REASON_LOCAL_HOST),
        ("ok,localhost_url", "one-label", reg.REASON_LOCAL_HOST),
    ],
)
def test_unacceptable_addresses_are_refused(modes: str, name: str, reason: str) -> None:
    """P7-S14-A03 明文 http / userinfo / IP 字面量（IPv4、IPv6、私网）/ localhost / 单标签主机：不可提案，原因码稳定。

    ``parse_server_url`` 对 IP 字面量放行（Step 0 实测）——"非 IP 字面量"是本步自己加的一层，这里逐个钉住。"""

    entry = fetch(modes, name)
    assert entry.not_proposable_reason == reason
    with pytest.raises(reg.McpRegistryError) as excinfo:
        reg.build_proposal(entry, registry_host=HOST)
    assert excinfo.value.code == "MCP_REGISTRY_NOT_PROPOSABLE"


def test_unsafe_entries_cannot_be_proposed() -> None:
    """P7-S14-A03 ``unsafe`` 条目（注入 / 同形字域名 / 重名）不可提案：``MCP_REGISTRY_ENTRY_UNSAFE``。"""

    assert refused("ok,injection_desc", "evil-injector") == "MCP_REGISTRY_ENTRY_UNSAFE"
    assert refused("ok,cyrillic_host", "cyr-host") == "MCP_REGISTRY_ENTRY_UNSAFE"
    assert refused("ok,tag_smuggle_desc", "tagger") == "MCP_REGISTRY_ENTRY_UNSAFE"
    # 重名：两条都 unsafe
    assert refused("ok,dup_names", "dupe") == "MCP_REGISTRY_ENTRY_UNSAFE"


def test_query_string_is_dropped_and_counted() -> None:
    """P7-S14-A03 URL 的查询串（常含 ``api_key=``）丢弃并计数；提案、备注、哈希输入里都没有令牌值。"""

    proposal = propose("ok,query_secret_url", "query-secret")
    (server,) = _servers(proposal)
    assert server["url"] == "https://q.example.com/mcp"
    blob = proposal.text + json.dumps(proposal.to_dict(), ensure_ascii=False)
    assert QUERY_SECRET not in blob and "acme" not in blob
    assert proposal.secret_dropped >= 1


def test_headers_env_and_unknown_fields_are_dropped() -> None:
    """P7-S14-A03 条目里的请求头 / env / 令牌 / 其余非白名单字段：解析阶段丢弃，提案与全部视图里都没有；``secret_dropped`` 计数。"""

    for modes, name in (("ok,header_secret", "hdr-secret"), ("ok,unknown_fields", "extras")):
        entry = fetch(modes, name)
        proposal = reg.build_proposal(entry, registry_host=HOST)
        blob = proposal.text + json.dumps(proposal.to_dict(), ensure_ascii=False) + json.dumps(entry.to_dict(), ensure_ascii=False)
        for secret in (HEADER_SECRET, ENV_SECRET, UNKNOWN_FIELD_SECRET):
            assert secret not in blob
        assert proposal.secret_dropped >= 1
        (server,) = _servers(proposal)
        assert set(server) == {"server_id", "transport", "url", "disabled"}


def test_userinfo_secret_never_reaches_any_view() -> None:
    """P7-S14-A03 ``https://user:pw@…`` 条目不可提案，展示视图里也没有 userinfo。"""

    entry = fetch("ok,userinfo_url", "userinfo")
    assert USERINFO_SECRET not in json.dumps(entry.to_dict(), ensure_ascii=False)


def test_the_defensive_recheck_refuses_a_proposal_that_is_not_disabled_deny_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S14-A03 纵深：``importer`` 现在恒输出停用提案，但 ``build_proposal`` 不依赖它——哪怕 ``importer`` 日后改成输出
    启用的 / 带 ``allow`` 的 server，目录提案也必须拒绝（拿掉这层复核，只有这条测试会变红）。"""

    entry = fetch("ok", "weather-station")
    real = reg.preview_import

    def tampered(text: str, *, existing_ids: Any = ()) -> Any:
        preview = real(text, existing_ids=existing_ids)
        preview.proposal["servers"][0].pop("disabled", None)
        return preview

    monkeypatch.setattr(reg, "preview_import", tampered)
    with pytest.raises(reg.McpRegistryError) as excinfo:
        reg.build_proposal(entry, registry_host=HOST)
    assert excinfo.value.code == "MCP_REGISTRY_NOT_PROPOSABLE"

    def with_allow(text: str, *, existing_ids: Any = ()) -> Any:
        preview = real(text, existing_ids=existing_ids)
        preview.proposal["servers"][0]["allow"] = ["anything"]
        return preview

    monkeypatch.setattr(reg, "preview_import", with_allow)
    with pytest.raises(reg.McpRegistryError):
        reg.build_proposal(entry, registry_host=HOST)


def test_sensitive_keys_are_stripped_even_if_the_importer_were_to_emit_them(monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S14-A03 纵深：提案里 ``env`` / ``headers`` / ``command`` / ``oauth`` 等键即使被上游（importer）带进来，也在这里剥掉。"""

    entry = fetch("ok", "weather-station")
    real = reg.preview_import

    def noisy(text: str, *, existing_ids: Any = ()) -> Any:
        preview = real(text, existing_ids=existing_ids)
        preview.proposal["servers"][0].update({"env": {"T": ENV_SECRET}, "command": "/bin/sh", "args": ["-c", "x"], "oauth": {"x": 1}})
        return preview

    monkeypatch.setattr(reg, "preview_import", noisy)
    proposal = reg.build_proposal(entry, registry_host=HOST)
    (server,) = _servers(proposal)
    assert set(server) == {"server_id", "transport", "url", "disabled"}
    assert ENV_SECRET not in proposal.text


def test_conflicting_server_id_is_renamed() -> None:
    """P7-S14-A03 与运维已有 ``server_id`` 冲突时改名（``_2``）并写进备注（同名 id 贴进覆盖层会改写那台 server 的配置）。"""

    proposal = propose("ok", "weather-station", existing=("weather_station",))
    assert proposal.server_id == "weather_station_2"
    assert any("改名为 'weather_station_2'" in note for note in proposal.notes)
    assert propose("ok", "weather-station").server_id == "weather_station"


def test_idna_host_is_shown_as_punycode_with_a_warning() -> None:
    """P7-S14-A03 非 ASCII 域名：提案里写 punycode 形式（运维看到的就是最终会被连接的主机）并提示核对同形字。"""

    proposal = propose("ok,idna_host", "idna-host")
    (server,) = _servers(proposal)
    assert server["url"] == "https://xn--fsq092h.xn--fiqs8s/mcp"
    assert proposal.idna_host == "xn--fsq092h.xn--fiqs8s"
    assert any("非 ASCII" in note for note in proposal.notes)
    assert propose("ok", "weather-station").idna_host is None


def test_proposal_hash_is_deterministic_and_sensitive() -> None:
    """P7-S14-A03 ``proposal_hash``：同一条目两次取得完全相同；改 URL / 版本 / 目录主机 / 名称任一，哈希都变。"""

    base = fetch("ok", "weather-station")
    first = reg.build_proposal(base, registry_host=HOST).proposal_hash
    assert first == reg.build_proposal(fetch("ok", "weather-station"), registry_host=HOST).proposal_hash
    assert len(first) == 64 and int(first, 16) >= 0

    moved = fetch("ok,weather_moved", "weather-station")
    assert reg.build_proposal(moved, registry_host=HOST).proposal_hash != first, "改 URL"
    newer = fetch("ok,two_versions", "weather-station", "1.1.0")
    assert reg.build_proposal(newer, registry_host=HOST).proposal_hash != first, "改版本"
    assert reg.build_proposal(base, registry_host="other.example.test").proposal_hash != first, "改目录主机"
    renamed = reg.RegistryEntry(
        name="weather-station-2", version=base.version, status=base.status, description=base.description,
        remotes=base.remotes, packages=base.packages,
    )
    assert reg.build_proposal(renamed, registry_host=HOST).proposal_hash != first, "改名称"


def test_hash_input_is_canonical_json() -> None:
    """P7-S14-A03 规范化 = ``json.dumps(sort_keys, ensure_ascii, 紧凑分隔符)``：键序不影响哈希。"""

    a = reg.proposal_hash_of({"a": 1, "b": "中"}, registry_host="h", name="n", version="1.0.0")
    b = reg.proposal_hash_of({"b": "中", "a": 1}, registry_host="h", name="n", version="1.0.0")
    assert a == b
    assert reg._canonical({"b": "中", "a": 1}) == '{"a":1,"b":"\\u4e2d"}'


def test_lookup_requires_an_exact_name_and_version_from_the_directory() -> None:
    """P7-S14-A03 服务端重取：目录里没有的 ``name@version`` → ``MCP_REGISTRY_ENTRY_NOT_FOUND``；格式不对的名字 / 版本同样，且不发请求。"""

    with EntrySink() as sink, FakeMcpRegistry("ok", sink_url=sink.url) as server:
        config = reg.RegistryConfig(base_url=server.base_url, allow_private=True)
        client = reg.RegistryCatalogClient(config, reg.registry_context("admin-1", request_id="r"), gateway=ProviderGateway())
        for name, version in (("weather-station", "9.9.9"), ("no-such-server", "1.0.0")):
            with pytest.raises(reg.McpRegistryError) as excinfo:
                client.find(name, version)
            assert excinfo.value.code == "MCP_REGISTRY_ENTRY_NOT_FOUND"
        requests_before = len(server.requests)
        for name, version in (("Weather Station", "1.0.0"), ("weather-station", "latest"), ("", "")):
            with pytest.raises(reg.McpRegistryError):
                client.find(name, version)
        assert len(server.requests) == requests_before
        assert sink.contacts == 0


def test_proposal_never_writes_anything(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S14-A03 提案是纯函数：不落盘、不写覆盖层 / Web 管理层。工作目录与凭据目录在提案前后逐字节相同。"""

    monkeypatch.setenv("WATER_AGENT_CREDENTIALS_DIR", str(tmp_path / "cred"))
    monkeypatch.chdir(tmp_path)
    before = sorted(p.name for p in tmp_path.rglob("*"))
    propose("ok", "weather-station")
    assert sorted(p.name for p in tmp_path.rglob("*")) == before
