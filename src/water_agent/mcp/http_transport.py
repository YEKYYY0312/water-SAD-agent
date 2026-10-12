"""MCP 远程出口：URL 校验、目的地址解析与分类、钉死 IP 的 HTTP 连接（P7-S8，``docs/design/16-p7-plugin-mcp.md`` §3.10）。

这是 ``mcp/`` 里**唯一**发起网络连接的模块，登记在出站清单 ``PROVIDER_CALL_SITES``
（``mcp_streamable_http``）里，静态扫描能命中它。纯标准库（``http.client`` + ``ssl`` + ``socket`` +
``urllib.parse``），不用 ``httpx`` / ``requests``（P4 决策 D1）。

出口由**运维写死的 URL** 决定：模型只能选工具、填参数，改不了连接目标；远端返回的任何地址（重定向、
``WWW-Authenticate``、授权服务器元数据）都不是指令，只能走同一套校验。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

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


__all__ = ["MAX_URL_LENGTH", "ParsedUrl", "parse_server_url"]
