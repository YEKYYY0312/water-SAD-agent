"""受治理的 GET 内核 ``external/http_get.py::governed_get``（P7-S11-A03）。

进程内假对端（线程，不起子进程）记录收到的请求数与路径；"禁止目的地零字节"靠请求数为 0 断言。
S11 没有任何生产调用方，所以网关调用方式由测试传入（现有 ``MCP_REMOTE`` / ``MCP_TOOL_CALL``）。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.fixtures.fake_http_peer import UPSTREAM_SECRET_BODY, FakeHttpPeer
from water_agent.external.http_get import (
    EXTERNAL_AUTH,
    EXTERNAL_BAD_RESPONSE,
    EXTERNAL_DESTINATION_FORBIDDEN,
    EXTERNAL_RATE_LIMITED,
    EXTERNAL_TIMEOUT,
    EXTERNAL_TOO_LARGE,
    EXTERNAL_UNAVAILABLE,
    ExternalGetError,
    governed_get,
)
from water_agent.runtime.governance.contracts import (
    CallCategory,
    Principal,
    ProviderCall,
    ProviderKind,
    RequestContext,
)
from water_agent.runtime.governance.gateway import ProviderGateway

SRC = Path(__file__).resolve().parents[1] / "src" / "water_agent" / "external" / "http_get.py"
KEY = "sk-live-abcdef0123456789"


def _ctx() -> RequestContext:
    return RequestContext(principal=Principal.local("ext-get"), request_id="ext-get", thread_id="ext")


def _call(model: str = "external:test") -> ProviderCall:
    return ProviderCall(provider=ProviderKind.MCP_REMOTE, category=CallCategory.MCP_TOOL_CALL, model=model)


def _get(url: str, **kwargs):
    defaults = {
        "headers": {"Authorization": f"Bearer {KEY}", "Accept": "application/json"},
        "max_bytes": 64 * 1024,
        "timeout": 3.0,
        "allow_private_network": True,
        "ctx": _ctx(),
        "call": _call(),
        "gateway": ProviderGateway(),
    }
    defaults.update(kwargs)
    return governed_get(url, **defaults)


def test_ok_returns_the_body_and_records_the_request() -> None:
    """P7-S11-A03 正常路径：回环仅在 ``allow_private_network`` 时放行；返回字节与媒体类型。"""

    with FakeHttpPeer("ok") as peer:
        result = _get(peer.base_url + "/search?q=1")
    assert result.status == 200 and result.media_type == "application/json"
    assert b'"ok"' in result.body
    assert peer.requests == ["/search?q=1"]


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:9/x",            # 回环，且没开 allow_private_network
        "https://169.254.169.254/latest",  # 云元数据，永远拒
        "https://10.0.0.5/x",              # 私有网段
        "https://[::1]/x",
        "https://2130706433/x",            # 十进制 IPv4
        "https://user:pw@example.com/x",   # userinfo
    ],
)
def test_forbidden_destination_sends_zero_bytes(url: str) -> None:
    """P7-S11-A03 禁止目的地在发出第一个字节之前拒绝，错误码是本规格的稳定码（不是 ``MCP_*``）。"""

    with FakeHttpPeer("ok") as peer:
        with pytest.raises(ExternalGetError) as caught:
            _get(url, allow_private_network=False)
        assert peer.requests == []
    assert caught.value.code == EXTERNAL_DESTINATION_FORBIDDEN
    assert not caught.value.code.startswith("MCP_")


def test_metadata_address_is_refused_even_with_allow_private_network() -> None:
    """P7-S11-A03 ``allow_private_network`` 只放行回环与私有，链路本地 / 云元数据永远拒。"""

    with pytest.raises(ExternalGetError) as caught:
        _get("https://169.254.169.254/latest", allow_private_network=True)
    assert caught.value.code == EXTERNAL_DESTINATION_FORBIDDEN


def test_dns_rebinding_is_pinned_to_the_validated_address() -> None:
    """P7-S11-A03 解析器注入：应答里混一个禁止地址整体拒绝；全部合法时只连这些地址（不二次解析）。"""

    calls: list[tuple[str, int]] = []

    def mixed(host: str, port: int) -> list[str]:
        calls.append((host, port))
        return ["127.0.0.1", "169.254.169.254"]

    with FakeHttpPeer("ok") as peer:
        with pytest.raises(ExternalGetError) as caught:
            _get(f"http://rebind.test:{peer.port}/x", resolver=mixed)
        assert peer.requests == []
        assert caught.value.code == EXTERNAL_DESTINATION_FORBIDDEN

        def pinned(host: str, port: int) -> list[str]:
            calls.append((host, port))
            return ["127.0.0.1"]

        calls.clear()
        result = _get(f"http://rebind.test:{peer.port}/x", resolver=pinned)
        assert result.status == 200 and peer.requests == ["/x"]
        assert len(calls) == 1, "只解析一次，连接钉死在校验过的地址上"


def test_redirects_are_never_followed() -> None:
    """P7-S11-A03 3xx 一律不跟随：重定向目标对端零请求；错误码是 ``EXTERNAL_BAD_RESPONSE``。"""

    with FakeHttpPeer("ok") as target, FakeHttpPeer("redirect", redirect_to=target.base_url + "/landed") as hop:
        with pytest.raises(ExternalGetError) as caught:
            _get(hop.base_url + "/start")
        assert hop.requests == ["/start"]
        assert target.requests == []
    assert caught.value.code == EXTERNAL_BAD_RESPONSE


def test_oversize_response_is_aborted() -> None:
    """P7-S11-A03 响应体超过 ``max_bytes`` 即中止，不把超大内容读进内存。"""

    with FakeHttpPeer("oversize", oversize_bytes=2_000_000) as peer, pytest.raises(ExternalGetError) as caught:
        _get(peer.base_url + "/big", max_bytes=1024)
    assert caught.value.code == EXTERNAL_TOO_LARGE


def test_media_type_must_match_when_required() -> None:
    """P7-S11-A03 调用方可要求 ``Content-Type``（provider 适配器要求 JSON）；不符即 ``EXTERNAL_BAD_RESPONSE``。"""

    with FakeHttpPeer("wrong_ctype") as peer, pytest.raises(ExternalGetError) as caught:
        _get(peer.base_url + "/x", require_media_type="application/json")
    assert caught.value.code == EXTERNAL_BAD_RESPONSE


def test_deadline_is_enforced() -> None:
    """P7-S11-A03 慢响应在 ``timeout`` 内超时，错误码 ``EXTERNAL_TIMEOUT``。"""

    with FakeHttpPeer("slow", delay=3.0) as peer, pytest.raises(ExternalGetError) as caught:
        _get(peer.base_url + "/x", timeout=0.5)
    assert caught.value.code == EXTERNAL_TIMEOUT


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, EXTERNAL_AUTH), (403, EXTERNAL_AUTH), (429, EXTERNAL_RATE_LIMITED), (500, EXTERNAL_UNAVAILABLE), (503, EXTERNAL_UNAVAILABLE), (404, EXTERNAL_BAD_RESPONSE)],
)
def test_http_errors_map_to_stable_codes_without_echoing_anything(status: int, code: str) -> None:
    """P7-S11-A03 HTTP 错误映射成稳定码；错误文本只含码与一句话，**不含**上游正文 / 网址 / 密钥。"""

    with FakeHttpPeer(f"status_{status}") as peer, pytest.raises(ExternalGetError) as caught:
        _get(peer.base_url + "/secret-path?token=abc")
    error = caught.value
    assert error.code == code
    text = f"{error} {error.message} {error.code}"
    for leaked in (UPSTREAM_SECRET_BODY, "upstream.internal", "SECRET-TOKEN", KEY, "secret-path", "token=abc", peer.base_url, "127.0.0.1"):
        assert leaked not in text, leaked


def test_the_request_goes_through_the_provider_gateway() -> None:
    """P7-S11-A03 每次出站都经 ``ProviderGateway.call``：``provider_call_count`` 加一、账本里有一条 usage 记录，
    且 usage **未知**（HTTP 交换没有 token 用量，不估算、不填 0 当成已知）。"""

    records: list = []
    gateway = ProviderGateway(on_usage=records.append)
    with FakeHttpPeer("ok") as peer:
        result = _get(peer.base_url + "/x", gateway=gateway)
    assert result.usage_known is False
    assert gateway.provider_call_count == 1
    assert len(records) == 1
    assert records[0].usage_known is False
    assert records[0].input_tokens is None or records[0].input_tokens == 0


def test_a_forbidden_destination_never_reaches_the_gateway_peer_and_is_not_retried() -> None:
    """P7-S11-A03 目的地拒绝是策略拒绝，不是瞬时错误：网关不重试（对端零请求，``provider_call_count`` 不超过一次尝试）。"""

    gateway = ProviderGateway()
    with FakeHttpPeer("ok") as peer:
        with pytest.raises(ExternalGetError):
            _get("https://169.254.169.254/x", gateway=gateway, allow_private_network=False)
        assert peer.requests == []
    assert gateway.provider_call_count <= 1


def test_gateway_rate_limit_is_mapped_without_leaking_bucket_details() -> None:
    """P7-S11-A03 网关自己的拒绝（限流）映射成 ``EXTERNAL_RATE_LIMITED``，文案不含 bucket / 主体细节。"""

    class _AlwaysLimited(ProviderGateway):
        def call(self, ctx, call, executor, **kwargs):  # type: ignore[override]
            from water_agent.runtime.governance.contracts import CODE_RATE_LIMITED, GovernanceError

            raise GovernanceError(CODE_RATE_LIMITED, "bucket=abc123|principal=deadbeef 超过额度")

    with pytest.raises(ExternalGetError) as caught:
        _get("https://example.com/x", gateway=_AlwaysLimited(), allow_private_network=False)
    assert caught.value.code == EXTERNAL_RATE_LIMITED
    assert "bucket" not in str(caught.value) and "deadbeef" not in str(caught.value)


def test_module_has_no_raw_network_primitive() -> None:
    """P7-S11-A03 / 设计约束：``external/http_get.py`` 不含任何原始网络原语，只调用 ``open_exchange``。"""

    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not imported & {"socket", "http", "urllib", "requests", "httpx", "aiohttp", "ssl"}, imported
    called = {
        node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert "open_exchange" in called
