"""P7-S8 Step 4–6：Streamable HTTP 协议客户端（``docs/design/16-p7-plugin-mcp.md`` §7 P7-S8-A03 / A04 / A05）。

对接**协议正确的本地假 server**（``tests/fixtures/fake_mcp_http_server.py``，自签 TLS）与按用例定制的脚本化
明文 server。验证：JSON 与 SSE 两种响应、会话头与 404 重建、新版无状态协议的识别、工具调用永不重发、
每个出站请求都有一笔 ``ProviderGateway`` 账、静态 Bearer 只进 ``Authorization`` 头。
"""
from __future__ import annotations

import json
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from water_agent.mcp.config import MCPServerConfig
from water_agent.mcp.credentials import write_slot
from water_agent.mcp.errors import (
    MCP_CONNECT_FAILED,
    MCP_CREDENTIAL_MISSING,
    MCP_NOT_INITIALIZED,
    MCP_PROTOCOL_ERROR,
    MCP_PROTOCOL_UNSUPPORTED,
    MCP_RESPONSE_TOO_LARGE,
    MCP_TIMEOUT,
    MCPError,
)
from water_agent.mcp.http_client import MCPHttpClient, mcp_request_scope
from water_agent.runtime.cancellation import RunCancelled, cancellation_scope
from water_agent.runtime.governance.contracts import (
    DEFAULT_LOCAL_CAPABILITIES,
    Capability,
    Principal,
    ProviderKind,
    RequestContext,
)
from water_agent.runtime.governance.gateway import ProviderGateway

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_mcp_http_server.py"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


class Fake:
    """假 HTTP MCP server 子进程 + 信任它证书的 SSL 上下文。"""

    def __init__(self, mode: str, tmp: Path, *flags: str) -> None:
        self.tmp = tmp
        subprocess.run([sys.executable, str(FAKE), "--make-cert", str(tmp / "tls")], check=True, capture_output=True)
        self.proc = subprocess.Popen(
            [sys.executable, str(FAKE), "--mode", mode, "--cert-dir", str(tmp / "tls"),
             "--log", str(tmp / "log.jsonl"), "--secrets", str(tmp / "secrets.json"), *flags],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        ports = json.loads(self.proc.stdout.readline())  # type: ignore[union-attr]
        self.port, self.auth_port = ports["mcp_port"], ports["auth_port"]
        self.context = ssl.create_default_context(cafile=str(tmp / "tls" / "cert.pem"))

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.port}/mcp"

    def log(self) -> list[dict[str, Any]]:
        path = self.tmp / "log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def wire(self) -> list[dict[str, Any]]:
        return [e for e in self.log() if e.get("server") == "mcp"]

    def processed_calls(self) -> int:
        return sum(1 for e in self.wire() if e.get("rpc_method") == "tools/call" and e.get("status") in (200, "cut"))

    def stop(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=3)
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream:
                stream.close()


@pytest.fixture
def fake(tmp_path: Path) -> Iterator[Callable[..., Fake]]:
    started: list[Fake] = []

    def start(mode: str, *flags: str) -> Fake:
        server = Fake(mode, tmp_path / f"srv{len(started)}", *flags)
        started.append(server)
        return server

    yield start
    for server in started:
        server.stop()


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WATER_AGENT_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        monkeypatch.delenv(key, raising=False)


def server_config(url: str, **overrides: Any) -> MCPServerConfig:
    payload: dict[str, Any] = {
        "server_id": "httpfake",
        "transport": "streamable_http",
        "url": url,
        "allow_private_network": True,
        "allow": ["echo_query", "session_probe"],
        "call_timeout_seconds": 3.0,
        "startup_timeout_seconds": 3.0,
    }
    payload.update(overrides)
    return MCPServerConfig.model_validate(payload)


def caller(name: str = "tester", *, invoke: bool = True) -> RequestContext:
    capabilities = DEFAULT_LOCAL_CAPABILITIES | ({Capability.MCP_INVOKE} if invoke else set())
    return RequestContext(principal=Principal.local(name, capabilities=capabilities), request_id=f"req-{name}")


def client_for(server: Fake, gateway: ProviderGateway | None = None, **overrides: Any) -> MCPHttpClient:
    return MCPHttpClient(server_config(server.url, **overrides), gateway=gateway, ssl_context=server.context)


def echo(client: MCPHttpClient, text: str = "管径", context: RequestContext | None = None) -> dict[str, Any]:
    with mcp_request_scope(context or caller()):
        return client.call_tool("echo_query", {"query": text})


class Scripted:
    """脚本化明文 server：``script(request_json, handler)`` 自行写响应。"""

    def __init__(self, script: Callable[[dict[str, Any], BaseHTTPRequestHandler], None]) -> None:
        outer = self
        self.requests: list[dict[str, Any]] = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_: object) -> None:
                pass

            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    message = json.loads(raw) if raw else {}
                except ValueError:
                    message = {}
                outer.requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers),
                                       "message": message})
                script(message, self)

            do_POST = _serve
            do_DELETE = _serve
            do_GET = _serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/mcp"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def send(handler: BaseHTTPRequestHandler, status: int, body: object = None, *,
         headers: dict[str, str] | None = None, content_type: str = "application/json") -> None:
    raw = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    handler.send_response(status)
    if raw:
        handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Connection", "close")
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(raw)
    handler.close_connection = True


def init_result(message: dict[str, Any], version: str = "2025-11-25") -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message["id"], "result": {
        "protocolVersion": version, "capabilities": {}, "serverInfo": {"name": "scripted", "version": "0"}}}


@pytest.fixture
def scripted() -> Iterator[Callable[..., Scripted]]:
    started: list[Scripted] = []

    def start(script: Callable[[dict[str, Any], BaseHTTPRequestHandler], None]) -> Scripted:
        server = Scripted(script)
        started.append(server)
        return server

    yield start
    for server in started:
        server.stop()


def plain_client(server: Scripted, **overrides: Any) -> MCPHttpClient:
    return MCPHttpClient(server_config(server.url, **overrides))


# ---------------------------------------------------------------------------
# A03 协议
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["json", "sse"])
def test_json_and_sse_responses(fake: Callable[..., Fake], mode: str) -> None:
    """P7-S8-A03：JSON 与 SSE 两种响应都支持；SSE 的预热事件（空 data）被忽略。"""

    server = fake(mode)
    client = client_for(server)
    try:
        info = client.connect()
        assert info["name"] == f"fake-http-{mode}"
        assert client.connected and client.alive
        names = [tool.name for tool in client.list_tools()]
        assert names[:2] == ["echo_query", "lookup_project"] and "session_probe" in names
        result = echo(client)
        assert result["ok"] is True
        assert result["structured_content"] == {"echo": {"query": "管径"}}
        assert result["server_id"] == "httpfake"
    finally:
        client.close()
    accept = {entry["accept"] for entry in server.wire() if entry.get("rpc_method")}
    assert accept == {"application/json, text/event-stream"}


def test_session_header_and_expiry(fake: Callable[..., Fake]) -> None:
    """P7-S8-A03：initialize 不带会话号 / 版本头，之后每个请求都带；404 重建一次后继续；close 发 DELETE。"""

    server = fake("session_expire")
    client = client_for(server)
    client.connect()
    first = client.call_tool  # noqa: F841 — 防止误用：下面都在作用域里调用
    with mcp_request_scope(caller()):
        one = client.call_tool("session_probe", {})
        two = client.call_tool("session_probe", {})
    assert one["ok"] and two["ok"]
    assert one["structured_content"]["calls_in_session"] == 1
    assert two["structured_content"] == {**two["structured_content"], "calls_in_session": 1, "initialize_count": 2}
    assert client.session_rebuilds == 1
    wire = server.wire()
    init = [e for e in wire if e.get("rpc_method") == "initialize"]
    assert [e["session_id_present"] for e in init] == [False, False]
    assert all(e["protocol_header"] == "" for e in init)
    later = [e for e in wire if e.get("rpc_method") in ("tools/call", "notifications/initialized", "tools/list")]
    assert later and all(e["session_id_present"] and e["protocol_header"] == "2025-11-25" for e in later)
    assert [e["status"] for e in wire if e.get("rpc_method") == "tools/call"] == [200, 404, 200]
    assert server.processed_calls() == 2, "被 404 拒绝的那次请求没有被处理，重发一次不会重复执行"
    client.close()
    deletes = [e for e in server.wire() if e["method"] == "DELETE"]
    assert len(deletes) == 1 and deletes[0]["session_id_present"] is True
    client.close()  # 幂等：不再发第二次 DELETE
    assert len([e for e in server.wire() if e["method"] == "DELETE"]) == 1


def test_close_tolerates_servers_that_refuse_session_deletion(fake: Callable[..., Fake]) -> None:
    server = fake("no_delete")
    client = client_for(server)
    client.connect()
    client.close()  # 405 视为正常，不抛
    assert not client.alive
    with pytest.raises(MCPError) as caught:
        client.list_tools()
    assert caught.value.code == MCP_CONNECT_FAILED


def test_modern_only_server_is_identified(fake: Callable[..., Fake], scripted: Callable[..., Scripted]) -> None:
    """P7-S8-A03：只讲 2026-07-28 的 server 记 MCP_PROTOCOL_UNSUPPORTED，不伪装成网络故障，也不回落 HTTP+SSE。"""

    server = fake("modern_only")
    client = client_for(server)
    with pytest.raises(MCPError) as caught:
        client.connect()
    assert caught.value.code == MCP_PROTOCOL_UNSUPPORTED
    assert "2026-07-28" in caught.value.message
    assert [e["method"] for e in server.wire()] == ["POST"], "只发了一次 initialize，没有回落去 GET 旧传输"

    def unsupported_version(message: dict[str, Any], handler: BaseHTTPRequestHandler) -> None:
        send(handler, 400, {"jsonrpc": "2.0", "id": message.get("id"), "error": {
            "code": -32022, "message": "Unsupported protocol version",
            "data": {"supported": ["2026-07-28"], "requested": "2025-11-25"}}})

    with pytest.raises(MCPError) as caught:
        plain_client(scripted(unsupported_version)).connect()
    assert caught.value.code == MCP_PROTOCOL_UNSUPPORTED

    def not_modern(message: dict[str, Any], handler: BaseHTTPRequestHandler) -> None:
        send(handler, 400, {"detail": "bad request"})

    with pytest.raises(MCPError) as caught:
        plain_client(scripted(not_modern)).connect()
    assert caught.value.code == MCP_PROTOCOL_ERROR, "不是可识别的新版错误体：按普通协议错误处理"


def test_protocol_version_and_session_id_are_validated(scripted: Callable[..., Scripted]) -> None:
    def old_but_compatible(message: dict[str, Any], handler: BaseHTTPRequestHandler) -> None:
        if message.get("method") == "initialize":
            send(handler, 200, init_result(message, "2025-06-18"), headers={"Mcp-Session-Id": "abc-123"})
        else:
            send(handler, 202)

    server = scripted(old_but_compatible)
    client = plain_client(server)
    client.connect()
    assert client._protocol_version == "2025-06-18"
    client.close()

    def future_version(message: dict[str, Any], handler: BaseHTTPRequestHandler) -> None:
        send(handler, 200, init_result(message, "2099-01-01"))

    with pytest.raises(MCPError) as caught:
        plain_client(scripted(future_version)).connect()
    assert caught.value.code == MCP_PROTOCOL_ERROR

    def bad_session(message: dict[str, Any], handler: BaseHTTPRequestHandler) -> None:
        send(handler, 200, init_result(message), headers={"Mcp-Session-Id": "has\x7fdel"})

    with pytest.raises(MCPError) as caught:
        plain_client(scripted(bad_session)).connect()
    assert caught.value.code == MCP_PROTOCOL_ERROR and "MCP-Session-Id" in caught.value.message


def test_server_to_client_requests_are_refused_and_notifications_recorded(fake: Callable[..., Fake]) -> None:
    server = fake("server_request")
    client = client_for(server)
    client.connect()
    result = echo(client)
    client.close()
    assert result["ok"] is True
    replies = [e for e in server.wire() if e.get("client_response_for") == "srv-1"]
    assert len(replies) == 1 and replies[0]["client_response_error_code"] == -32601
    assert any(m.method == "notifications/message" for m in client.notifications)


def test_tool_calls_are_never_replayed(fake: Callable[..., Fake], scripted: Callable[..., Scripted]) -> None:
    """P7-S8-A03：流在收到响应前断开、超时、5xx 都直接报错，tools/call 在线上只出现一次。"""

    cut = fake("sse_cut")
    client = client_for(cut)
    client.connect()
    with pytest.raises(MCPError) as caught:
        echo(client)
    assert caught.value.code == MCP_CONNECT_FAILED
    assert cut.processed_calls() == 1
    client.close()

    slow = fake("slow")
    client = client_for(slow, call_timeout_seconds=1.0)
    client.connect()
    started = time.monotonic()
    with pytest.raises(MCPError) as caught:
        echo(client)
    assert caught.value.code == MCP_TIMEOUT and time.monotonic() - started < 1.8
    time.sleep(1.5)
    assert slow.processed_calls() == 1
    client.close()

    def flaky(message: dict[str, Any], handler: BaseHTTPRequestHandler) -> None:
        method = message.get("method")
        if method == "initialize":
            send(handler, 200, init_result(message), headers={"Mcp-Session-Id": "s1"})
        elif method == "tools/call":
            send(handler, 503)
        else:
            send(handler, 202)

    server = scripted(flaky)
    client = plain_client(server)
    client.connect()
    with pytest.raises(MCPError) as caught:
        echo(client)
    assert caught.value.code == MCP_CONNECT_FAILED and "503" in caught.value.message
    assert sum(1 for r in server.requests if r["message"].get("method") == "tools/call") == 1
    client.close()


def test_responses_are_bounded_and_typed(fake: Callable[..., Fake]) -> None:
    flood = client_for(fake("flood"), max_response_bytes=65536)
    flood.connect()
    with pytest.raises(MCPError) as caught:
        flood.list_tools()
    assert caught.value.code == MCP_RESPONSE_TOO_LARGE
    flood.close()
    bad = client_for(fake("bad_content_type"))
    bad.connect()
    with pytest.raises(MCPError) as caught:
        bad.list_tools()
    assert caught.value.code == MCP_PROTOCOL_ERROR and "Content-Type" in caught.value.message
    bad.close()
    redirect_server = fake("redirect")
    redirected = client_for(redirect_server)
    with pytest.raises(MCPError) as caught:
        redirected.connect()
    assert caught.value.code == MCP_PROTOCOL_ERROR
    assert not [e for e in redirect_server.wire() if e.get("path") == "/elsewhere"]


def test_requests_on_one_session_are_serialized(fake: Callable[..., Fake]) -> None:
    """同一会话上的请求串行；等锁计入 deadline，后来的调用按自己的 deadline 超时并说明 server 正忙。"""

    server = fake("slow")
    client = client_for(server, call_timeout_seconds=5.0)
    client.connect()
    first_done = threading.Event()

    def first() -> None:
        echo(client, "first")
        first_done.set()

    thread = threading.Thread(target=first)
    thread.start()
    time.sleep(0.4)
    started = time.monotonic()
    with pytest.raises(MCPError) as caught, mcp_request_scope(caller()):
        client.call_tool("echo_query", {"query": "second"}, deadline_seconds=0.3)
    assert caught.value.code == MCP_TIMEOUT and "正忙" in caught.value.message
    assert time.monotonic() - started < 1.0
    thread.join(timeout=5)
    assert first_done.is_set()
    client.close()


def test_cancellation_propagates_out_of_the_event_stream(scripted: Callable[..., Scripted]) -> None:
    def slow_stream(message: dict[str, Any], handler: BaseHTTPRequestHandler) -> None:
        method = message.get("method")
        if method == "initialize":
            send(handler, 200, init_result(message), headers={"Mcp-Session-Id": "s1"})
        elif method == "tools/call":
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(b"id: 0\ndata:\n\n")
            handler.wfile.flush()
            time.sleep(1.0)
            handler.close_connection = True
        else:
            send(handler, 202)

    client = plain_client(scripted(slow_stream))
    client.connect()
    with cancellation_scope(lambda: True), pytest.raises(RunCancelled), mcp_request_scope(caller()):
        client.call_tool("echo_query", {"query": "x"})
    client.close()


# ---------------------------------------------------------------------------
# A04 治理
# ---------------------------------------------------------------------------


def test_remote_calls_go_through_the_gateway(fake: Callable[..., Fake]) -> None:
    """P7-S8-A04：每个出站请求（initialize / 通知 / tools/list / tools/call / 回绝 / DELETE）都有一笔
    ``ProviderGateway`` 账；账本里 usage 记未知（MCP 没有 token 概念，不估算）；零重试。"""

    server = fake("server_request")
    records: list[Any] = []
    gateway = ProviderGateway(on_usage=records.append)
    client = client_for(server, gateway)
    client.connect()
    client.list_tools()
    echo(client)
    client.close()
    wire = [e for e in server.wire() if e.get("method") in ("POST", "DELETE")]
    assert len(wire) == 6  # initialize, initialized, tools/list, tools/call, 回绝, DELETE
    assert len(records) == len(wire)
    assert gateway.provider_call_count == len(wire), "每个请求恰好一次尝试：没有重试"
    for record in records:
        assert record.provider is ProviderKind.MCP_REMOTE
        assert record.model == "mcp:httpfake"
        assert record.usage_known is False and record.input_tokens is None and record.cost_usd is None
        assert record.result_status == "USAGE_UNKNOWN", "usage 未知如实记账，不估算"


def test_tool_calls_require_a_caller_with_the_invoke_capability(fake: Callable[..., Fake]) -> None:
    """tools/call 按调用者的主体做能力校验；没有作用域 / 没有 MCP_INVOKE 一律拒绝，不借服务主体放行。"""

    server = fake("json")
    gateway = ProviderGateway()
    client = client_for(server, gateway)
    client.connect()
    before = len(server.wire())
    with pytest.raises(MCPError) as caught:
        client.call_tool("echo_query", {"query": "x"})
    assert caught.value.code == MCP_NOT_INITIALIZED and "调用者主体" in caught.value.message
    with pytest.raises(MCPError) as denied, mcp_request_scope(caller(invoke=False)):
        client.call_tool("echo_query", {"query": "x"})
    assert "mcp_invoke" in str(denied.value) or "CAPABILITY_DENIED" in str(denied.value)
    assert len(server.wire()) == before, "被拒绝的调用一个字节都没发出"
    client.close()


def test_the_breaker_opens_per_server_after_repeated_server_errors(scripted: Callable[..., Scripted]) -> None:
    def down(message: dict[str, Any], handler: BaseHTTPRequestHandler) -> None:
        send(handler, 503)

    server = scripted(down)
    gateway = ProviderGateway()
    client = plain_client(server)
    client._gateway = gateway
    for _ in range(5):
        with pytest.raises(MCPError):
            client.connect()
    sent = len(server.requests)
    with pytest.raises(MCPError) as caught:
        client.connect()
    assert "CIRCUIT_OPEN" in str(caught.value) or "熔断" in str(caught.value)
    assert len(server.requests) == sent, "熔断打开后不再向这台 server 发请求"


# ---------------------------------------------------------------------------
# A05 静态 Bearer
# ---------------------------------------------------------------------------


def test_bearer_slot_is_sent_only_as_a_header(fake: Callable[..., Fake]) -> None:
    """P7-S8-A05：值来自槽位、每个请求都带 Authorization、不进 URL；缺值 MCP_CREDENTIAL_MISSING 且不发请求；
    面板改值立即生效；server 拒绝时 MCP_CONNECT_FAILED。"""

    server = fake("bearer")
    config_overrides = {"bearer_credential": "SERVER_API_KEY", "credential_env": ["SERVER_API_KEY"]}
    client = client_for(server, **config_overrides)
    with pytest.raises(MCPError) as caught:
        client.connect()
    assert caught.value.code == MCP_CREDENTIAL_MISSING and server.wire() == []

    write_slot("httpfake", "SERVER_API_KEY", "wrong-bearer-token-9999")
    with pytest.raises(MCPError) as rejected:
        client.connect()
    assert rejected.value.code == MCP_CONNECT_FAILED and "静态 Bearer" in rejected.value.message

    write_slot("httpfake", "SERVER_API_KEY", "static-bearer-token-0001")
    client = client_for(server, **config_overrides)
    client.connect()
    assert echo(client)["ok"] is True
    client.close()
    mcp_requests = [e for e in server.wire() if e.get("rpc_method") or e["method"] == "DELETE"]
    ok_requests = [e for e in mcp_requests if e.get("status") != "auth_rejected"]
    assert ok_requests and all(e["has_authorization"] and e["authorization_scheme"] == "Bearer" for e in ok_requests)
    assert not any("static-bearer-token-0001" in json.dumps(entry) for entry in server.log())


def test_bearer_value_never_appears_in_the_url_or_errors(scripted: Callable[..., Scripted]) -> None:
    secret = "secret-value-for-the-wire-123"
    write_slot("httpfake", "SERVER_API_KEY", secret)

    def deny(message: dict[str, Any], handler: BaseHTTPRequestHandler) -> None:
        send(handler, 401)

    server = scripted(deny)
    client = plain_client(server, bearer_credential="SERVER_API_KEY", credential_env=["SERVER_API_KEY"])
    with pytest.raises(MCPError) as caught:
        client.connect()
    assert secret not in str(caught.value) and secret not in repr(caught.value.__cause__)
    request = server.requests[0]
    assert request["headers"]["Authorization"] == f"Bearer {secret}"
    assert secret not in request["path"]


def test_bridged_descriptions_name_the_transport(
    fake: Callable[..., Fake], monkeypatch: pytest.MonkeyPatch
) -> None:
    """桥接层与传输无关，只在工具描述里如实写明来源：stdio 保持原文（不动既有快照），HTTP 写 Streamable HTTP。"""

    from water_agent.mcp.bridge import open_bridge
    from water_agent.mcp.config import MCPGatewayConfig

    server = fake("json")
    # open_bridge 用系统默认信任库建连（和生产一致）：让它信任假 server 的自签证书
    monkeypatch.setenv("SSL_CERT_FILE", str(server.tmp / "tls" / "cert.pem"))
    config = server_config(server.url, allow=["echo_query"])
    session = open_bridge(config, MCPGatewayConfig(servers=(config,)))
    try:
        assert session.client is not None and session.result.exposed_names == ("mcp_httpfake_echo_query",)
        assert "Streamable HTTP 集成" in session.result.tools[0].description
    finally:
        session.close()
    stdio = MCPServerConfig.model_validate({"server_id": "stdiofake", "command": sys.executable, "args": [
        str(Path(__file__).resolve().parent / "fixtures" / "fake_mcp_server.py"), "--mode", "ok"], "allow": ["echo_query"]})
    stdio_session = open_bridge(stdio, MCPGatewayConfig(servers=(stdio,)))
    try:
        assert "（stdio 集成，能力 mcp_invoke）" in stdio_session.result.tools[0].description
    finally:
        stdio_session.close()
