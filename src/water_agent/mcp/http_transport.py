"""MCP 远程出口：URL 校验、目的地址解析与分类、钉死 IP 的 HTTP 连接（P7-S8，``docs/design/16-p7-plugin-mcp.md`` §3.10）。

这是 ``mcp/`` 里**唯一**发起网络连接的模块，登记在出站清单 ``PROVIDER_CALL_SITES``
（``mcp_streamable_http``）里，静态扫描能命中它。纯标准库（``http.client`` + ``ssl`` + ``socket`` +
``urllib.parse``），不用 ``httpx`` / ``requests``（P4 决策 D1）。

出口由**运维写死的 URL** 决定：模型只能选工具、填参数，改不了连接目标；远端返回的任何地址（重定向、
``WWW-Authenticate``、授权服务器元数据）都不是指令，只能走同一套校验。
"""
from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from water_agent.mcp.errors import (
    MCP_DESTINATION_FORBIDDEN,
    MCP_PROTOCOL_ERROR,
    MCP_RESPONSE_TOO_LARGE,
    MCPError,
)

#: URL 总长上限（防配置里塞超长文本）。
MAX_URL_LENGTH = 2048


@dataclass(frozen=True, slots=True)
class ParsedUrl:
    """已校验、已归一化的服务 URL。"""

    scheme: str
    host: str
    port: int
    path: str
    query: str

    @property
    def default_port(self) -> bool:
        return self.port == (443 if self.scheme == "https" else 80)

    @property
    def netloc(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return host if self.default_port else f"{host}:{self.port}"

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.netloc}"

    @property
    def request_target(self) -> str:
        """HTTP 请求行里的 origin-form（路径 + 查询）。"""
        path = self.path or "/"
        return f"{path}?{self.query}" if self.query else path

    def geturl(self) -> str:
        return urlunsplit((self.scheme, self.netloc, self.path, self.query, ""))

    def canonical_resource_uri(self) -> str:
        """RFC 8707 / MCP 授权规范的规范 URI：小写 scheme 与 host，无 fragment，无尾斜杠；查询串保留。"""
        path = self.path.rstrip("/")
        return urlunsplit((self.scheme, self.netloc, path, self.query, ""))


def _canonical_ipv4(host: str) -> bool:
    parts = host.split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if not part.isdecimal() or not part.isascii() or (len(part) > 1 and part.startswith("0")):
            return False
        if int(part) > 255:
            return False
    return True


_NUMERIC_LABEL_RE = re.compile(r"^(?:[0-9]+|0[xX][0-9a-fA-F]*)$")


def _looks_numeric(host: str) -> bool:
    """WHATWG 口径：最后一个标签是十进制或 ``0x`` 十六进制数字，就按 IPv4 地址解释（``inet_aton`` 同理）。

    ``2130706433``、``0177.0.0.1``、``127.1``、``0x7f.0.0.1`` 都满足；``example.com``、``a.b2`` 不满足。
    """
    return bool(_NUMERIC_LABEL_RE.match(host.rstrip(".").rsplit(".", 1)[-1]))


def parse_server_url(url: str, *, allow_private_network: bool = False) -> ParsedUrl:
    """校验并归一化一个 MCP / OAuth 服务 URL（加载期与每次建连前都用它）。

    - 只接受 ``https://``；``http://`` 仅在 ``allow_private_network=True`` 时接受（本机 / 局域网调试）；
    - 不得含 userinfo（``user:pass@``）、fragment、控制字符与空白；
    - 主机名转小写并做 IDNA 编码；**只由数字和点组成的主机名必须是规范的点分十进制 IPv4**——
      ``2130706433``、``0177.0.0.1``、``127.1`` 这类写法会被某些解析器当成回环地址，这里直接拒绝；
    - 端口缺省按 scheme 补全，范围 1–65535。

    不符合即 ``ValueError``（配置加载把它包成 ``MCP_CONFIG_INVALID``）。
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url 不得为空")
    if url != url.strip() or any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        raise ValueError("url 不得含空白或控制字符")
    if len(url) > MAX_URL_LENGTH:
        raise ValueError(f"url 过长（上限 {MAX_URL_LENGTH} 字符）")
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("https", "http"):
        raise ValueError(f"url 的 scheme 必须是 https（得到 {parts.scheme!r}）")
    if scheme == "http" and not allow_private_network:
        raise ValueError("明文 http:// 只在 allow_private_network=true 时接受（本机 / 局域网调试）")
    if parts.fragment or "#" in url:
        raise ValueError("url 不得含 fragment")
    netloc = parts.netloc
    if "@" in netloc:
        raise ValueError("url 不得含 userinfo（user:pass@）；凭据请用 bearer_credential 或 oauth")
    try:
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"url 的主机或端口不合法：{exc}") from exc
    if not host:
        raise ValueError("url 缺少主机名")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("url 的端口必须在 1–65535")
    if ":" not in host:  # 非 IPv6 字面量
        if _looks_numeric(host) and not _canonical_ipv4(host):
            raise ValueError(
                f"url 的主机名 {host!r} 不是规范的点分十进制 IPv4（十进制 / 八进制 / 十六进制 / 短写形式一律拒绝）"
            )
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError(f"url 的主机名无法做 IDNA 编码：{exc}") from exc
    if len(host) > 253:
        raise ValueError("url 的主机名过长")
    return ParsedUrl(
        scheme=scheme,
        host=host.lower(),
        port=port if port is not None else (443 if scheme == "https" else 80),
        path=parts.path,
        query=parts.query,
    )



# ---------------------------------------------------------------------------
# 目的地址分类（SSRF）
# ---------------------------------------------------------------------------

#: 云厂商元数据地址：即使 ``allow_private_network=true`` 也永远拒绝（其中 ``100.100.100.200`` 在 CGNAT 段、
#: ``fd00:ec2::254`` 在 ULA 段，会被"私有段放行"漏掉）。
METADATA_ADDRESSES = frozenset(
    ipaddress.ip_address(item)
    for item in (
        "169.254.169.254",
        "169.254.170.2",
        "100.100.100.200",
        "192.0.0.192",
        "fd00:ec2::254",
        "fd00:ec2::23",
    )
)
_CGNAT = ipaddress.ip_network("100.64.0.0/10")

#: 分类结果。``PUBLIC`` 以外都不是公网；``allow_private_network`` 只放行 ``LOOPBACK`` 与 ``PRIVATE``。
PUBLIC = "public"
LOOPBACK = "loopback"
PRIVATE = "private"
_ALWAYS_FORBIDDEN = ("metadata", "link_local", "unspecified", "multicast", "reserved")


def classify_address(address: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    """把一个 IP 归入 ``public`` / ``loopback`` / ``private`` / ``metadata`` / ``link_local`` /
    ``unspecified`` / ``multicast`` / ``reserved``。

    IPv4-mapped IPv6（``::ffff:127.0.0.1``）、6to4（``2002::/16``）、NAT64（``64:ff9b::/96``）与 Teredo
    按它们内嵌的 IPv4 地址分类——否则 ``::ffff:169.254.169.254`` 能绕过 IPv4 规则。
    """
    ip = ipaddress.ip_address(address) if not isinstance(address, (ipaddress.IPv4Address, ipaddress.IPv6Address)) else address
    if isinstance(ip, ipaddress.IPv6Address):
        embedded: list[ipaddress.IPv4Address] = []
        if ip.ipv4_mapped is not None:
            embedded.append(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            embedded.append(ip.sixtofour)
        if ip.teredo is not None:
            embedded.extend(ip.teredo)
        if ip in ipaddress.ip_network("64:ff9b::/96"):
            embedded.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        if embedded:
            # 取最严的一个：内嵌地址里只要有一个不是 public，整体就不是
            ranked = [classify_address(item) for item in embedded]
            for kind in (*_ALWAYS_FORBIDDEN, LOOPBACK, PRIVATE):
                if kind in ranked:
                    return kind
        if ip == ipaddress.IPv6Address("::"):
            return "unspecified"
    if ip in METADATA_ADDRESSES:
        return "metadata"
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_multicast:
        return "multicast"
    if ip.is_loopback:
        return LOOPBACK
    if ip.is_link_local:
        return "link_local"
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT:
        return PRIVATE
    if ip.is_private:
        # 文档 / 基准测试 / 广播 / 240.0.0.0/4 等 ``is_private`` 里的"保留"段不是真正的内网，一律拒绝
        if isinstance(ip, ipaddress.IPv4Address) and (
            ip in ipaddress.ip_network("0.0.0.0/8")
            or ip in ipaddress.ip_network("192.0.0.0/24")
            or ip in ipaddress.ip_network("192.0.2.0/24")
            or ip in ipaddress.ip_network("198.18.0.0/15")
            or ip in ipaddress.ip_network("198.51.100.0/24")
            or ip in ipaddress.ip_network("203.0.113.0/24")
            or ip in ipaddress.ip_network("240.0.0.0/4")
        ):
            return "reserved"
        if isinstance(ip, ipaddress.IPv6Address) and not ip.is_site_local and ip not in ipaddress.ip_network("fc00::/7"):
            return "reserved"
        return PRIVATE
    if ip.is_reserved:
        return "reserved"
    return PUBLIC if ip.is_global else "reserved"


Resolver = Callable[[str, int], Sequence[str]]


def default_resolver(host: str, port: int) -> list[str]:
    """系统解析器。每次调用都真的解析一次（``socket.getaddrinfo`` 在调用时才取，便于测试替换）。"""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    seen: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if "%" in address:  # IPv6 scope id
            address = address.split("%", 1)[0]
        if address not in seen:
            seen.append(address)
    return seen


def resolve_destination(
    url: ParsedUrl, *, allow_private_network: bool, resolver: Resolver | None = None
) -> tuple[str, ...]:
    """解析并**校验全部应答**，返回允许连接的地址（按应答顺序）。

    任一应答落在禁止段整体拒绝（``MCP_DESTINATION_FORBIDDEN``）：攻击者能控制 DNS 时，"挑出安全的那个"
    不是防线。IP 字面量不经解析器，同样分类。拒绝发生在发出第一个字节之前。
    """
    host = url.host
    try:
        addresses: Sequence[str] = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            addresses = (resolver or default_resolver)(host, url.port)
        except OSError as exc:
            raise MCPError(
                MCP_DESTINATION_FORBIDDEN, f"无法解析主机名 {host!r}（{type(exc).__name__}）；按失败处理，不连接"
            ) from exc
    if not addresses:
        raise MCPError(MCP_DESTINATION_FORBIDDEN, f"主机名 {host!r} 没有解析出任何地址")
    allowed_kinds = {PUBLIC, *( [LOOPBACK, PRIVATE] if allow_private_network else [])}
    for address in addresses:
        try:
            kind = classify_address(address)
        except ValueError as exc:
            raise MCPError(MCP_DESTINATION_FORBIDDEN, f"主机 {host!r} 解析出无法识别的地址") from exc
        if kind not in allowed_kinds:
            hint = "（allow_private_network=true 可放行回环与私有网段）" if kind in (LOOPBACK, PRIVATE) else ""
            raise MCPError(
                MCP_DESTINATION_FORBIDDEN,
                f"主机 {host!r} 解析到{_KIND_LABELS.get(kind, kind)}地址，拒绝连接{hint}",
            )
    return tuple(str(item) for item in addresses)


_KIND_LABELS = {
    LOOPBACK: "回环",
    PRIVATE: "私有网段",
    "metadata": "云元数据",
    "link_local": "链路本地",
    "unspecified": "未指定",
    "multicast": "组播",
    "reserved": "保留段",
}


# ---------------------------------------------------------------------------
# 钉死 IP 的连接
# ---------------------------------------------------------------------------


class TransportFailure(Exception):
    """网络层 / HTTP 状态层的失败（不是策略拒绝）。``mcp_code`` 给调用方映射 MCP 错误码，
    子类让 ``ProviderGateway`` 的错误分类（超时 / 连接错误 → 瞬时，401 / 403 → 认证）认得出来。"""

    def __init__(self, mcp_code: str, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.mcp_code = mcp_code
        self.message = message
        self.status_code = status_code


class TransportTimeout(TransportFailure, TimeoutError):
    pass


class TransportHttpStatus(TransportFailure):
    """服务端返回 4xx / 5xx。``status_code`` 让 ``ProviderGateway`` 的错误分类生效（401 / 403 → 认证、
    零重试且不计熔断；408 / 429 / 5xx → 瞬时；其余 → 永久）。``headers`` / ``body`` 留给协议层解释
    （``WWW-Authenticate``、JSON-RPC 错误体）；body 已截断到 :data:`ERROR_BODY_LIMIT`。"""

    def __init__(self, status_code: int, headers: list[tuple[str, str]], body: bytes) -> None:
        super().__init__("MCP_CONNECT_FAILED", f"HTTP {status_code}", status_code=status_code)
        self.headers = headers
        self.body = body

    def header_all(self, name: str) -> list[str]:
        lowered = name.lower()
        return [value for key, value in self.headers if key.lower() == lowered]


class TransportConnectionError(TransportFailure, ConnectionError):
    pass


class _PinnedMixin:
    """只连接校验过的地址，不再解析主机名；TLS 的 SNI 与证书校验仍按主机名。"""

    _addresses: tuple[str, ...]
    _connect_timeout: float

    def _connect_pinned(self, host: str, port: int) -> socket.socket:
        last_error: OSError | None = None
        for address in self._addresses:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            sock = socket.socket(family, socket.SOCK_STREAM)
            sock.settimeout(self._connect_timeout)
            try:
                sock.connect((address, port))
            except OSError as exc:
                sock.close()
                last_error = exc
                continue
            return sock
        raise TransportConnectionError(
            "MCP_CONNECT_FAILED",
            f"无法连接 {host}:{port}（{type(last_error).__name__ if last_error else '无可用地址'}）",
        ) from last_error


class _PinnedHTTPConnection(_PinnedMixin, http.client.HTTPConnection):
    def __init__(self, host: str, port: int, *, addresses: tuple[str, ...], timeout: float) -> None:
        http.client.HTTPConnection.__init__(self, host, port, timeout=timeout)
        self._addresses = addresses
        self._connect_timeout = timeout

    def connect(self) -> None:
        self.sock = self._connect_pinned(self.host, self.port)


class _PinnedHTTPSConnection(_PinnedMixin, http.client.HTTPSConnection):
    def __init__(
        self, host: str, port: int, *, addresses: tuple[str, ...], timeout: float, context: ssl.SSLContext
    ) -> None:
        http.client.HTTPSConnection.__init__(self, host, port, timeout=timeout, context=context)
        self._addresses = addresses
        self._connect_timeout = timeout
        self._tls_context = context

    def connect(self) -> None:
        raw = self._connect_pinned(self.host, self.port)
        try:
            self.sock = self._tls_context.wrap_socket(raw, server_hostname=self.host)
        except ssl.SSLCertVerificationError as exc:
            raw.close()
            raise TransportConnectionError(
                "MCP_CONNECT_FAILED", f"TLS 证书校验失败（{exc.verify_message}）：{self.host}"
            ) from exc
        except (ssl.SSLError, OSError) as exc:
            raw.close()
            raise TransportConnectionError(
                "MCP_CONNECT_FAILED", f"TLS 握手失败（{type(exc).__name__}）：{self.host}"
            ) from exc


# ---------------------------------------------------------------------------
# 一次 HTTP 交换
# ---------------------------------------------------------------------------

USER_AGENT = "water-agent-mcp/1"
_SESSION_ID_CHARS = re.compile(r"^[\x21-\x7e]+$")
#: 4xx / 5xx 响应体最多读这么多（只用来识别 JSON-RPC 错误体），其余丢弃。
ERROR_BODY_LIMIT = 4096


@dataclass(frozen=True, slots=True)
class Challenge:
    """``WWW-Authenticate`` 里的一个质询：scheme + 参数（键小写）。"""

    scheme: str
    params: dict[str, str]


_CHALLENGE_START = re.compile(r"(?:^|,)\s*([A-Za-z][A-Za-z0-9!#$%&'*+.^_`|~-]*)(?=\s+[A-Za-z0-9_-]+\s*=|\s*$|\s*,)")
_PARAM = re.compile(
    r"\s*,?\s*([A-Za-z][A-Za-z0-9_-]*)\s*=\s*(?:\"((?:[^\"\\\\]|\\\\.)*)\"|([^\s,\"]+))"
)


def parse_www_authenticate(values: Sequence[str]) -> list[Challenge]:
    """解析 ``WWW-Authenticate``（RFC 7235 / 6750）：``Bearer realm="x", error="invalid_token", scope="a b"``。

    支持一个头里的多个质询与多个同名头；引号字符串做反斜杠转义还原。解析不了的片段跳过，不抛异常——
    响应头是不可信输入，宁可少认也不因畸形头崩溃。
    """
    challenges: list[Challenge] = []
    for raw in values:
        text = raw.strip()
        position = 0
        current: Challenge | None = None
        while position < len(text):
            start = _CHALLENGE_START.match(text, position)
            param = _PARAM.match(text, position)
            if param is not None and current is not None:
                # 先尝试把它当成当前质询的参数
                key, quoted, token = param.group(1).lower(), param.group(2), param.group(3)
                value = re.sub(r"\\\\(.)", r"\1", quoted) if quoted is not None else token
                current.params[key] = value
                position = param.end()
                continue
            if start is not None:
                current = Challenge(start.group(1).lower(), {})
                challenges.append(current)
                position = start.end()
                continue
            position += 1
    return challenges


def valid_session_id(value: str) -> bool:
    """``MCP-Session-Id`` 只能含可见 ASCII（0x21–0x7E）。"""
    return bool(value) and len(value) <= 512 and bool(_SESSION_ID_CHARS.match(value))


def _check_header_value(name: str, value: str) -> str:
    if "\r" in value or "\n" in value or "\x00" in value:
        raise MCPError(MCP_PROTOCOL_ERROR, f"请求头 {name} 含非法字符")
    return value


@dataclass(frozen=True, slots=True)
class SseEvent:
    event: str
    data: str
    event_id: str | None = None


@dataclass
class Exchange:
    """一次已发出请求、已读到状态行和响应头的交换。调用方负责 :meth:`close`。"""

    status: int
    reason: str
    headers: list[tuple[str, str]]
    _response: http.client.HTTPResponse
    _connection: http.client.HTTPConnection
    deadline_at: float
    _closed: bool = field(default=False, repr=False)

    def header(self, name: str) -> str | None:
        lowered = name.lower()
        for key, value in self.headers:
            if key.lower() == lowered:
                return value
        return None

    def header_all(self, name: str) -> list[str]:
        lowered = name.lower()
        return [value for key, value in self.headers if key.lower() == lowered]

    @property
    def media_type(self) -> str:
        return (self.header("Content-Type") or "").split(";", 1)[0].strip().lower()

    def _remaining(self) -> float:
        remaining = self.deadline_at - time.monotonic()
        if remaining <= 0:
            raise TransportTimeout("MCP_TIMEOUT", "等待响应超过时间预算")
        return remaining

    def _arm_timeout(self) -> None:
        sock = self._connection.sock
        if sock is not None:
            sock.settimeout(self._remaining())

    def read_body(self, limit: int) -> bytes:
        """读完整个响应体；超过 ``limit`` 字节即 ``MCP_RESPONSE_TOO_LARGE``（不再继续读）。"""
        declared = self.header("Content-Length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            raise MCPError(MCP_RESPONSE_TOO_LARGE, f"响应声明 {declared} 字节，超过上限 {limit}")
        chunks: list[bytes] = []
        total = 0
        while True:
            self._arm_timeout()
            try:
                chunk = self._response.read(min(8192, limit + 1 - total))
            except TimeoutError as exc:
                raise TransportTimeout("MCP_TIMEOUT", "读取响应超时") from exc
            except (OSError, http.client.HTTPException) as exc:
                raise TransportConnectionError("MCP_CONNECT_FAILED", f"读取响应失败（{type(exc).__name__}）") from exc
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > limit:
                raise MCPError(MCP_RESPONSE_TOO_LARGE, f"响应超过上限 {limit} 字节")
            chunks.append(chunk)

    def events(
        self, *, max_event_bytes: int, max_total_bytes: int, check: Callable[[], None] | None = None
    ) -> Iterator[SseEvent]:
        """按 SSE 规则逐事件读（``text/event-stream``）。

        单事件 ``data`` 超过 ``max_event_bytes``、整条流超过 ``max_total_bytes`` 都是
        ``MCP_RESPONSE_TOO_LARGE`` 并断开；每个事件之间调用 ``check``（取消）；流在 deadline 前未结束按超时。
        流正常结束（EOF）时迭代器自然结束——调用方据此判断"没等到响应"。
        """
        total = 0
        event_name = "message"
        event_id: str | None = None
        data_lines: list[str] = []
        data_size = 0
        while True:
            if check is not None:
                check()
            self._arm_timeout()
            try:
                raw = self._response.readline(max_event_bytes + 2)
            except TimeoutError as exc:
                raise TransportTimeout("MCP_TIMEOUT", "读取事件流超时") from exc
            except (OSError, http.client.HTTPException) as exc:
                raise TransportConnectionError("MCP_CONNECT_FAILED", f"读取事件流失败（{type(exc).__name__}）") from exc
            if not raw:
                return
            total += len(raw)
            if total > max_total_bytes:
                raise MCPError(MCP_RESPONSE_TOO_LARGE, f"事件流超过总上限 {max_total_bytes} 字节")
            if len(raw) > max_event_bytes + 1 and not raw.endswith(b"\n"):
                raise MCPError(MCP_RESPONSE_TOO_LARGE, f"事件行超过上限 {max_event_bytes} 字节")
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line == "":
                if data_lines:
                    yield SseEvent(event_name, "\n".join(data_lines), event_id)
                event_name, event_id, data_lines, data_size = "message", None, [], 0
                continue
            if line.startswith(":"):
                continue
            field_name, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if field_name == "data":
                data_size += len(value) + 1
                if data_size > max_event_bytes:
                    raise MCPError(MCP_RESPONSE_TOO_LARGE, f"单个事件超过上限 {max_event_bytes} 字节")
                data_lines.append(value)
            elif field_name == "event":
                event_name = value or "message"
            elif field_name == "id":
                event_id = value

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._response.close()
        finally:
            self._connection.close()


def open_exchange(
    url: ParsedUrl,
    method: str,
    *,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
    allow_private_network: bool,
    resolver: Resolver | None = None,
    ssl_context: ssl.SSLContext | None = None,
) -> Exchange:
    """校验目的地 → 钉死 IP 连接 → 发请求 → 读状态行与响应头。

    - 每次调用都是**一次新连接**：解析、分类、连接各做一次，不复用连接，也不二次解析；
    - 3xx **一律不跟随**（``MCP_PROTOCOL_ERROR``，detail 只写状态码，不回显 ``Location``）；
    - 状态码与响应头原样交给调用方，401 / 403 / 404 等由协议层解释。
    """
    addresses = resolve_destination(url, allow_private_network=allow_private_network, resolver=resolver)
    started = time.monotonic()
    connection: http.client.HTTPConnection
    if url.scheme == "https":
        context = ssl_context if ssl_context is not None else ssl.create_default_context()
        connection = _PinnedHTTPSConnection(url.host, url.port, addresses=addresses, timeout=timeout, context=context)
    else:
        connection = _PinnedHTTPConnection(url.host, url.port, addresses=addresses, timeout=timeout)
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "identity",
        "Connection": "close",
        **{name: _check_header_value(name, value) for name, value in headers.items()},
    }
    try:
        connection.request(method, url.request_target, body=body, headers=request_headers)
        sock = connection.sock
        if sock is not None:
            sock.settimeout(max(timeout - (time.monotonic() - started), 0.001))
        response = connection.getresponse()
    except TransportFailure:
        connection.close()
        raise
    except TimeoutError as exc:
        connection.close()
        raise TransportTimeout("MCP_TIMEOUT", "等待响应头超时") from exc
    except (OSError, http.client.HTTPException) as exc:
        connection.close()
        raise TransportConnectionError(
            "MCP_CONNECT_FAILED", f"请求失败（{type(exc).__name__}）"
        ) from exc
    exchange = Exchange(
        status=response.status,
        reason=response.reason,
        headers=[(key, value) for key, value in response.getheaders()],
        _response=response,
        _connection=connection,
        deadline_at=started + timeout,
    )
    if 300 <= exchange.status < 400:
        exchange.close()
        raise MCPError(
            MCP_PROTOCOL_ERROR, f"server 返回 HTTP {exchange.status} 重定向；本网关不跟随重定向（出口只由运维配置决定）"
        )
    return exchange


__all__ = [
    "ERROR_BODY_LIMIT",
    "LOOPBACK",
    "MAX_URL_LENGTH",
    "METADATA_ADDRESSES",
    "PRIVATE",
    "PUBLIC",
    "USER_AGENT",
    "Challenge",
    "Exchange",
    "ParsedUrl",
    "Resolver",
    "SseEvent",
    "TransportConnectionError",
    "TransportFailure",
    "TransportHttpStatus",
    "TransportTimeout",
    "classify_address",
    "default_resolver",
    "open_exchange",
    "parse_server_url",
    "parse_www_authenticate",
    "resolve_destination",
    "valid_session_id",
]
