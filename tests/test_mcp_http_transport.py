"""P7-S8 Step 3：MCP 远程出口与 SSRF（``docs/design/16-p7-plugin-mcp.md`` §7 P7-S8-A02）。

出口由运维写死的 URL 决定，但解析结果不是：一个公网域名可以解析到内网，DNS 可以在校验之后换答案，
远端可以用 3xx 把客户端引向别处。这里逐条验证：每次建连都分类并整体拒绝（零字节发出）、连接只去校验过的
IP、重定向不跟随、响应字节上限与 Content-Type 约束。
"""
from __future__ import annotations

import json
import socket
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from water_agent.mcp.errors import (
    MCP_DESTINATION_FORBIDDEN,
    MCP_PROTOCOL_ERROR,
    MCP_RESPONSE_TOO_LARGE,
    MCPError,
)
from water_agent.mcp.http_transport import (
    TransportConnectionError,
    TransportTimeout,
    classify_address,
    open_exchange,
    parse_server_url,
    resolve_destination,
    valid_session_id,
)

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_mcp_http_server.py"


# ---------------------------------------------------------------------------
# 地址分类
# ---------------------------------------------------------------------------

#: (地址, 默认应归入的类别)
CLASSIFICATIONS = [
    ("127.0.0.1", "loopback"), ("127.255.255.254", "loopback"), ("::1", "loopback"),
    ("10.0.0.1", "private"), ("172.16.5.4", "private"), ("172.31.255.255", "private"),
    ("192.168.1.1", "private"), ("fc00::1", "private"), ("fd12:3456::1", "private"),
    ("100.64.0.1", "private"), ("100.127.255.254", "private"),
    ("169.254.1.1", "link_local"), ("fe80::1", "link_local"),
    ("169.254.169.254", "metadata"), ("169.254.170.2", "metadata"), ("100.100.100.200", "metadata"),
    ("fd00:ec2::254", "metadata"),
    ("0.0.0.0", "unspecified"), ("::", "unspecified"),
    ("224.0.0.1", "multicast"), ("ff02::1", "multicast"),
    ("240.0.0.1", "reserved"), ("255.255.255.255", "reserved"), ("192.0.2.1", "reserved"),
    ("198.51.100.7", "reserved"), ("203.0.113.9", "reserved"), ("198.18.0.1", "reserved"),
    ("2001:db8::1", "reserved"),
    # 内嵌 IPv4 的 IPv6 形态按内嵌地址分类
    ("::ffff:127.0.0.1", "loopback"), ("::ffff:10.1.2.3", "private"),
    ("::ffff:169.254.169.254", "metadata"), ("::ffff:0.0.0.0", "unspecified"),
    ("2002:7f00:1::", "loopback"), ("2002:a9fe:a9fe::", "metadata"), ("2002:a9fe:101::", "link_local"),
    ("64:ff9b::7f00:1", "loopback"), ("64:ff9b::a9fe:a9fe", "metadata"),
    # 公网
    ("93.184.216.34", "public"), ("8.8.8.8", "public"), ("2606:4700:4700::1111", "public"),
    ("::ffff:8.8.8.8", "public"),
]


@pytest.mark.parametrize(("address", "kind"), CLASSIFICATIONS)
def test_address_classification(address: str, kind: str) -> None:
    assert classify_address(address) == kind


def resolver_for(*addresses: str) -> tuple[Callable[[str, int], list[str]], list[tuple[str, int]]]:
    calls: list[tuple[str, int]] = []

    def resolve(host: str, port: int) -> list[str]:
        calls.append((host, port))
        return list(addresses)

    return resolve, calls


@pytest.fixture
def no_connections(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """任何 TCP 连接尝试都是失败：用来证明"一个字节都没发出"。"""

    attempts: list[object] = []

    def refuse(self: socket.socket, address: object) -> None:
        attempts.append(address)
        raise AssertionError(f"不该发生的连接：{address!r}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    return attempts


@pytest.mark.parametrize(("address", "kind"), CLASSIFICATIONS)
def test_reserved_destinations_are_refused_before_sending(
    address: str, kind: str, no_connections: list[object]
) -> None:
    """P7-S8-A02：每次建连都解析并分类；非公网段整体拒绝，零连接。``allow_private_network`` 只放行回环与
    私有网段——链路本地、云元数据、未指定、组播、保留段永远拒绝。"""

    url = parse_server_url("https://mcp.example.com/mcp")
    resolver, _ = resolver_for(address)
    if kind == "public":
        assert resolve_destination(url, allow_private_network=False, resolver=resolver) == (address,)
        return
    with pytest.raises(MCPError) as caught:
        resolve_destination(url, allow_private_network=False, resolver=resolver)
    assert caught.value.code == MCP_DESTINATION_FORBIDDEN
    # 经 open_exchange 同样零连接
    with pytest.raises(MCPError):
        open_exchange(url, "POST", headers={}, body=b"{}", timeout=1, allow_private_network=False, resolver=resolver)
    if kind in ("loopback", "private"):
        assert resolve_destination(url, allow_private_network=True, resolver=resolver) == (address,)
    else:
        with pytest.raises(MCPError) as always:
            resolve_destination(url, allow_private_network=True, resolver=resolver)
        assert always.value.code == MCP_DESTINATION_FORBIDDEN
    assert no_connections == []


def test_one_bad_answer_refuses_the_whole_resolution(no_connections: list[object]) -> None:
    """解析出多个地址、其中一个落在禁止段：整体拒绝，不"挑安全的那个"。"""

    url = parse_server_url("https://mcp.example.com/mcp")
    resolver, _ = resolver_for("93.184.216.34", "10.0.0.5")
    with pytest.raises(MCPError) as caught:
        open_exchange(url, "POST", headers={}, body=b"{}", timeout=1, allow_private_network=False, resolver=resolver)
    assert caught.value.code == MCP_DESTINATION_FORBIDDEN
    assert no_connections == []


def test_ip_literals_are_classified_without_the_resolver(no_connections: list[object]) -> None:
    url = parse_server_url("https://169.254.169.254/latest/meta-data")
    resolver, calls = resolver_for("93.184.216.34")
    with pytest.raises(MCPError) as caught:
        resolve_destination(url, allow_private_network=True, resolver=resolver)
    assert caught.value.code == MCP_DESTINATION_FORBIDDEN and calls == [], "字面量不经解析器"
    v6 = parse_server_url("https://[::ffff:169.254.169.254]/mcp")
    with pytest.raises(MCPError):
        resolve_destination(v6, allow_private_network=True, resolver=resolver)


def test_unresolvable_host_fails_closed(no_connections: list[object]) -> None:
    def boom(host: str, port: int) -> list[str]:
        raise socket.gaierror("nope")

    with pytest.raises(MCPError) as caught:
        resolve_destination(parse_server_url("https://nx.example.com/mcp"), allow_private_network=False, resolver=boom)
    assert caught.value.code == MCP_DESTINATION_FORBIDDEN
    empty, _ = resolver_for()
    with pytest.raises(MCPError):
        resolve_destination(parse_server_url("https://nx.example.com/mcp"), allow_private_network=False, resolver=empty)


# ---------------------------------------------------------------------------
# 本地测试服务器
# ---------------------------------------------------------------------------


class Serve:
    """起一个按用例定制行为的明文 HTTP 服务（回环，配合 ``allow_private_network=True``）。"""

    def __init__(self, behave: Callable[[BaseHTTPRequestHandler], None]) -> None:
        outer = self
        self.requests: list[tuple[str, str]] = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_: object) -> None:
                pass

            def do_POST(self) -> None:
                outer.requests.append(("POST", self.path))
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                behave(self)

            do_GET = do_POST

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def url(self, path: str = "/mcp") -> object:
        return parse_server_url(f"http://127.0.0.1:{self.port}{path}", allow_private_network=True)

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def serve() -> Iterator[Callable[[Callable[[BaseHTTPRequestHandler], None]], Serve]]:
    started: list[Serve] = []

    def start(behave: Callable[[BaseHTTPRequestHandler], None]) -> Serve:
        server = Serve(behave)
        started.append(server)
        return server

    yield start
    for server in started:
        server.stop()


def exchange(server: Serve, *, timeout: float = 3.0, path: str = "/mcp") -> object:
    return open_exchange(
        server.url(path), "POST", headers={"Content-Type": "application/json"}, body=b"{}",  # type: ignore[arg-type]
        timeout=timeout, allow_private_network=True,
    )


def reply(handler: BaseHTTPRequestHandler, status: int, body: bytes, content_type: str = "application/json",
          extra: dict[str, str] | None = None) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Connection", "close")
    for key, value in (extra or {}).items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)
    handler.close_connection = True


# ---------------------------------------------------------------------------
# 钉死 IP、TLS、重定向
# ---------------------------------------------------------------------------


def test_connection_is_pinned_to_the_checked_address(
    serve: Callable[..., Serve], monkeypatch: pytest.MonkeyPatch
) -> None:
    """P7-S8-A02：校验时解析一次、连接只去那个 IP，不再二次解析（防 DNS 重绑定）。"""

    server = serve(lambda h: reply(h, 200, b'{"ok":true}'))
    answers = [["127.0.0.1"], ["169.254.169.254"]]
    lookups: list[str] = []
    connects: list[tuple[str, int]] = []

    def resolver(host: str, port: int) -> list[str]:
        lookups.append(host)
        return answers[min(len(lookups) - 1, 1)]

    real_connect = socket.socket.connect

    def spy(self: socket.socket, address: tuple[str, int]) -> None:
        connects.append((address[0], address[1]))
        real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", spy)
    url = parse_server_url(f"http://rebind.example:{server.port}/mcp", allow_private_network=True)
    first = open_exchange(url, "POST", headers={}, body=b"{}", timeout=3, allow_private_network=True, resolver=resolver)
    try:
        assert first.status == 200 and first.read_body(1024) == b'{"ok":true}'
    finally:
        first.close()
    assert lookups == ["rebind.example"], "一次建连只解析一次"
    assert connects == [("127.0.0.1", server.port)], "连接的是校验过的那个 IP"
    # 下一次建连重新解析并重新校验：这一次答案是元数据地址 → 拒绝，零连接
    with pytest.raises(MCPError) as caught:
        open_exchange(url, "POST", headers={}, body=b"{}", timeout=3, allow_private_network=True, resolver=resolver)
    assert caught.value.code == MCP_DESTINATION_FORBIDDEN
    assert len(connects) == 1 and len(lookups) == 2


def test_connection_tries_each_checked_address_in_order(
    serve: Callable[..., Serve], monkeypatch: pytest.MonkeyPatch
) -> None:
    """::1 没人监听时回落到下一个校验过的地址（localhost 常见的双栈解析）。"""

    server = serve(lambda h: reply(h, 200, b"{}"))
    resolver, _ = resolver_for("::1", "127.0.0.1")
    url = parse_server_url(f"http://localhost:{server.port}/mcp", allow_private_network=True)
    result = open_exchange(url, "POST", headers={}, body=b"{}", timeout=3, allow_private_network=True, resolver=resolver)
    result.close()
    assert result.status == 200


@pytest.fixture(scope="module")
def tls_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[int, Path]]:
    tmp = tmp_path_factory.mktemp("tls")
    subprocess.run([sys.executable, str(FAKE), "--make-cert", str(tmp / "tls")], check=True, capture_output=True)
    proc = subprocess.Popen(
        [sys.executable, str(FAKE), "--mode", "json", "--cert-dir", str(tmp / "tls"), "--log", str(tmp / "log.jsonl")],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    port = json.loads(proc.stdout.readline())["mcp_port"]  # type: ignore[union-attr]
    yield port, tmp / "tls" / "cert.pem"
    proc.terminate()
    proc.wait(timeout=3)
    for stream in (proc.stdout, proc.stderr):
        if stream:
            stream.close()


def test_tls_uses_the_hostname_for_sni_and_certificate_checks(tls_server: tuple[int, Path]) -> None:
    """连接钉死到 IP，但证书校验仍按主机名：名字在 SAN 里放行，不在就失败。"""

    port, cert = tls_server
    context = ssl.create_default_context(cafile=str(cert))
    resolver, _ = resolver_for("127.0.0.1")
    good = parse_server_url(f"https://rebind.test:{port}/mcp", allow_private_network=True)
    ok = open_exchange(good, "GET", headers={}, body=None, timeout=3, allow_private_network=True,
                       resolver=resolver, ssl_context=context)
    ok.close()
    assert ok.status in (405, 404, 200)
    bad = parse_server_url(f"https://not-in-the-certificate.test:{port}/mcp", allow_private_network=True)
    with pytest.raises(TransportConnectionError) as caught:
        open_exchange(bad, "GET", headers={}, body=None, timeout=3, allow_private_network=True,
                      resolver=resolver, ssl_context=context)
    assert "证书校验失败" in caught.value.message
    # 系统信任库里没有测试证书：默认上下文也必须拒绝（不会因为是自签就放行）
    with pytest.raises(TransportConnectionError):
        open_exchange(good, "GET", headers={}, body=None, timeout=3, allow_private_network=True, resolver=resolver)


def test_redirects_are_never_followed(serve: Callable[..., Serve]) -> None:
    """P7-S8-A02：3xx 一律不跟随；detail 只写状态码，不回显 Location。"""

    hits: list[str] = []

    def behave(handler: BaseHTTPRequestHandler) -> None:
        if handler.path == "/mcp":
            reply(handler, 307, b"", extra={"Location": "http://127.0.0.1:1/secret-redirect-target"})
        else:
            hits.append(handler.path)
            reply(handler, 200, b"{}")

    server = serve(behave)
    for status in (301, 302, 303, 307, 308):
        def redirect(handler: BaseHTTPRequestHandler, status: int = status) -> None:
            reply(handler, status, b"", extra={"Location": f"http://127.0.0.1:{server.port}/elsewhere"})

        target = serve(redirect)
        with pytest.raises(MCPError) as caught:
            exchange(target)
        assert caught.value.code == MCP_PROTOCOL_ERROR
        assert str(status) in caught.value.message and "elsewhere" not in caught.value.message
    assert hits == []


# ---------------------------------------------------------------------------
# 响应上限与 Content-Type
# ---------------------------------------------------------------------------


def test_response_limits_and_content_type(serve: Callable[..., Serve]) -> None:
    """P7-S8-A02：声明的 Content-Length 超限在读正文前就拒绝；未声明长度的流式正文读到超限即断；
    SSE 单事件与整条流都有上限；media_type 如实解析（调用方据此拒绝非 JSON / SSE）。"""

    big = serve(lambda h: reply(h, 200, b"x" * 5000))
    ex = exchange(big)
    with pytest.raises(MCPError) as caught:
        ex.read_body(1000)  # type: ignore[attr-defined]
    assert caught.value.code == MCP_RESPONSE_TOO_LARGE
    ex.close()  # type: ignore[attr-defined]
    ex = exchange(big)
    assert len(ex.read_body(5000)) == 5000  # type: ignore[attr-defined]
    ex.close()  # type: ignore[attr-defined]

    def chunked(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Connection", "close")
        handler.end_headers()
        for _ in range(100):
            handler.wfile.write(b"y" * 100)
        handler.close_connection = True

    ex = exchange(serve(chunked))
    with pytest.raises(MCPError) as caught:
        ex.read_body(1000)  # type: ignore[attr-defined]
    assert caught.value.code == MCP_RESPONSE_TOO_LARGE
    ex.close()  # type: ignore[attr-defined]

    def sse(events: list[bytes]) -> Callable[[BaseHTTPRequestHandler], None]:
        def behave(handler: BaseHTTPRequestHandler) -> None:
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
            handler.send_header("Connection", "close")
            handler.end_headers()
            for item in events:
                handler.wfile.write(item)
            handler.close_connection = True

        return behave

    ex = exchange(serve(sse([b"data: " + b"z" * 5000 + b"\n\n"])))
    assert ex.media_type == "text/event-stream"  # type: ignore[attr-defined]
    with pytest.raises(MCPError) as caught:
        list(ex.events(max_event_bytes=1000, max_total_bytes=100000))  # type: ignore[attr-defined]
    assert caught.value.code == MCP_RESPONSE_TOO_LARGE
    ex.close()  # type: ignore[attr-defined]
    many = [f"data: {'a' * 200}\n\n".encode() for _ in range(50)]
    ex = exchange(serve(sse(many)))
    with pytest.raises(MCPError) as caught:
        list(ex.events(max_event_bytes=1000, max_total_bytes=2000))  # type: ignore[attr-defined]
    assert caught.value.code == MCP_RESPONSE_TOO_LARGE
    ex.close()  # type: ignore[attr-defined]
    ex = exchange(serve(lambda h: reply(h, 200, b"hi", content_type="text/plain")))
    assert ex.media_type == "text/plain"  # type: ignore[attr-defined]
    ex.close()  # type: ignore[attr-defined]


def test_sse_parsing_follows_the_event_stream_rules(serve: Callable[..., Serve]) -> None:
    stream = (
        b": comment\n\n"
        b"id: 0\ndata:\n\n"                       # 带 id 的空 data 事件（预热）：产出空 data 事件
        b"id: 7\nevent: message\ndata: {\"a\":\n"
        b"data: 1}\n\n"                           # 多行 data 以换行拼接
        b"event: ping\ndata: x\r\n\r\n"           # CRLF 行尾
        b"retry: 1000\n\n"
        b"data: tail-without-blank-line"          # 缺空行的残事件不产出
    )

    def behave(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(stream)
        handler.close_connection = True

    ex = exchange(serve(behave))
    events = list(ex.events(max_event_bytes=1000, max_total_bytes=10000))  # type: ignore[attr-defined]
    ex.close()  # type: ignore[attr-defined]
    assert [(e.event, e.data, e.event_id) for e in events] == [
        ("message", "", "0"),                    # 预热事件原样产出（空 data），由协议层忽略
        ("message", '{"a":\n1}', "7"),
        ("ping", "x", None),
    ]


def test_slow_servers_time_out_instead_of_hanging(serve: Callable[..., Serve]) -> None:
    def hang(handler: BaseHTTPRequestHandler) -> None:
        time.sleep(1.5)
        reply(handler, 200, b"{}")

    server = serve(hang)
    started = time.monotonic()
    with pytest.raises(TransportTimeout):
        exchange(server, timeout=0.4)
    assert time.monotonic() - started < 1.2

    def drip(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.flush()
        time.sleep(1.5)

    ex = exchange(serve(drip), timeout=0.5)
    with pytest.raises(TransportTimeout):
        list(ex.events(max_event_bytes=100, max_total_bytes=1000))  # type: ignore[attr-defined]
    ex.close()  # type: ignore[attr-defined]


def test_header_values_cannot_inject_lines(serve: Callable[..., Serve]) -> None:
    server = serve(lambda h: reply(h, 200, b"{}"))
    with pytest.raises(MCPError) as caught:
        open_exchange(server.url(), "POST", headers={"X-Test": "a\r\nInjected: 1"},  # type: ignore[arg-type]
                      body=b"{}", timeout=1, allow_private_network=True)
    assert caught.value.code == MCP_PROTOCOL_ERROR
    assert server.requests == []


def test_session_id_characters() -> None:
    assert valid_session_id("1868a90c-f7d1-4e3a")
    assert valid_session_id("eyJhbGciOiJIUzI1NiJ9.e30.sig")
    for bad in ("", "has space", "tab\t", "new\nline", "中文", "x" * 513, "\x7f"):
        assert not valid_session_id(bad)
