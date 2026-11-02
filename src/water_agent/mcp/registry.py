"""MCP 目录（P7-S14，规格 ``docs/design/17-p7-external-capabilities.md`` §7，决策记录 ``docs/design/p7-step-14-decision.md``）。

管理员**带外**从运维写死的目录搜索 / 浏览 MCP server 条目，并为合适的条目生成**已停用、全拒**的待审提案文本，
由运维离线贴进部署覆盖层。本模块：

- **只读外发一个地址**——目录基址（``WATER_AGENT_MCP_REGISTRY_URL``，运维写死）。出站只经 ``external/http_get.py::governed_get``
  （SSRF 分类、钉死 IP、3xx 不跟随、字节与时间上限、``ProviderGateway`` 限流 / 熔断 / 对账），**不直接调用 ``open_exchange``**；
- **永不连接目录条目里的任何地址**（P7-I13）：条目 URL 是第三方目录写的，搜索与提案阶段对条目 URL 零字节、对条目主机零 DNS。
  因此本模块**不得导入** ``MCPStdioClient`` / ``MCPHttpClient`` / ``open_bridge``（``tests/test_mcp_registry_invariants.py`` 用 AST 判定）；
- **不应用提案**：提案只是文本，不落盘、不 spawn、不写覆盖层 / Web 管理层。运维贴进覆盖层之后才有 S6 的探测与 S8 的连接期 SSRF 校验；
- 条目是**不可信的第三方文本**：字段白名单、字符正向白名单、核心边界归一化、注入检测；任何一项不过，该条标 ``unsafe``
  （描述不展示、不可生成提案）。**整页严格校验**：任何一条形状不对，整页被拒，不部分采纳。

目录协议 v1 是**我们自己定义的最小协议**，**不是官方 MCP 注册表的格式**（Step 0 取得了官方注册表的真实样例，
字段形态完全不同，见决策记录 §0.1）。本模块**不声称兼容任何真实目录**（``unverified_against_real_registry``）。

**局限（必须如实）**：确定性规则（正则 / 字符白名单）必有语料之外的写法；条目与真实 server 的一致性、
条目描述是否如实、运维启用并放行后该 server 的实际行为，任何确定性规则都检不出。不得声称"目录搜索是安全的"。
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any
from urllib.parse import urlencode, urlsplit

from water_agent.external.envelope import neutralize_text
from water_agent.external.http_get import ExternalGetError, governed_get
from water_agent.mcp.config import MCPServerConfig
from water_agent.mcp.errors import (
    MCP_REGISTRY_AUTH,
    MCP_REGISTRY_BAD_RESPONSE,
    MCP_REGISTRY_ENTRY_NOT_FOUND,
    MCP_REGISTRY_ENTRY_UNSAFE,
    MCP_REGISTRY_NOT_CONFIGURED,
    MCP_REGISTRY_NOT_PROPOSABLE,
    MCP_REGISTRY_RATE_LIMITED,
    MCP_REGISTRY_UNAVAILABLE,
    MCPError,
)
from water_agent.mcp.http_transport import parse_server_url
from water_agent.mcp.importer import normalize_server_id, preview_import
from water_agent.runtime.governance.contracts import (
    CallCategory,
    Capability,
    Principal,
    ProviderCall,
    ProviderKind,
    RequestContext,
)
from water_agent.runtime.governance.rules import detect_injection

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

ENV_URL = "WATER_AGENT_MCP_REGISTRY_URL"
ENV_ALLOW_PRIVATE = "WATER_AGENT_MCP_REGISTRY_ALLOW_PRIVATE"

#: 协议上限（SDD §7.2）。
PAGE_LIMIT = 20
MAX_PAGES = 5
MAX_ENTRY_BYTES = 16 * 1024
MAX_TOTAL_BYTES = 256 * 1024
MAX_QUERY_CHARS = 64
MAX_CURSOR_CHARS = 128
MAX_DESCRIPTION_CHARS = 600
MAX_NAME_CHARS = 64
MAX_URL_CHARS = 300
MAX_REMOTES = 8
MAX_PACKAGES = 8
MAX_IDENTIFIER_CHARS = 120
#: 单次交换（含一次重试）的总预算，比网关画像的 15 s 少 1 s 余量。
NETWORK_BUDGET_SECONDS = 14.0
#: 一次搜索最多返回多少条给面板（5 页 × 20 条，再多没有意义）。
MAX_RESULTS = PAGE_LIMIT * MAX_PAGES
MEDIA_TYPE = "application/json"
#: 响应体读取上限：总量上限之外留一点 JSON 外壳的余量。
MAX_RESPONSE_BYTES = MAX_TOTAL_BYTES + 4096

_NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_VERSION_RE = re.compile(r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}")
_CURSOR_RE = re.compile(r"[A-Za-z0-9_\-.=]{1,128}")
_REGISTRY_TYPE_RE = re.compile(r"[a-z][a-z0-9_-]{0,23}")
_REMOTE_TYPE_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_ENTRY_KEYS = frozenset({"name", "version", "description", "remotes", "packages"})
_REMOTE_KEYS = frozenset({"type", "url"})
_PACKAGE_KEYS = frozenset({"registry_type", "identifier"})
_RESPONSE_KEYS = frozenset({"entries", "next_cursor"})
#: 能生成提案的远程类型。其余类型（``sse`` / ``stdio`` / ……）只作展示字符串。
PROPOSABLE_REMOTE_TYPE = "streamable-http"

#: 条目状态。
STATUS_OK = "ok"
STATUS_UNSAFE = "unsafe"

#: 不可提案的原因（稳定短码，面板据此显示固定文案；不含条目内容）。
REASON_UNSAFE = "unsafe"
REASON_NO_REMOTE = "no_remote"
REASON_NO_STREAMABLE_HTTP = "no_streamable_http"
REASON_URL_REJECTED = "url_rejected"
REASON_IP_LITERAL = "ip_literal"
REASON_LOCAL_HOST = "local_host"
REASON_AMBIGUOUS = "ambiguous_name"


# ---------------------------------------------------------------------------
# 字符正向白名单（与 S13 ``skills/charset.py`` 同口径同区间；本步与 S13 并行，不 import 它）
# ---------------------------------------------------------------------------
# 黑名单追不上 Tag 字符 / 变体选择符 / 同形字（Step 0 实测：``mcp/schema.py::ensure_description_safe`` 对 U+E0000 区全部放行，
# ``detect_injection`` 的不可见清单只有 8 个）。所以对目录条目的每个展示 / 提案字段改用**正向白名单**。

#: (起, 止) 闭区间。只列有正当用途的区段；其余一律不在白名单。
ALLOWED_RANGES: tuple[tuple[int, int], ...] = (
    (0x0020, 0x007E),  # ASCII 可打印
    (0x00A1, 0x00BF),  # Latin-1 标点与符号（软连字符 U+00AD 由类别规则剔除）
    (0x00D7, 0x00D7),  # ×
    (0x00F7, 0x00F7),  # ÷
    (0x0391, 0x03A9),  # 希腊大写
    (0x03B1, 0x03C9),  # 希腊小写
    (0x2010, 0x2027),  # 常用标点：连字符、破折号、引号、省略号
    (0x2030, 0x203E),  # ‰ ′ ″ ※ …
    (0x2044, 0x2044),  # ⁄
    (0x205E, 0x205E),
    (0x2070, 0x209C),  # 上下标
    (0x20AC, 0x20AC),  # €
    (0x2103, 0x2103),  # ℃
    (0x2109, 0x2109),  # ℉
    (0x2113, 0x2113),  # ℓ
    (0x2116, 0x2116),  # №
    (0x2122, 0x2122),  # ™
    (0x2126, 0x2126),  # Ω
    (0x212B, 0x212B),  # Å
    (0x2160, 0x217F),  # 罗马数字
    (0x2190, 0x21FF),  # 箭头
    (0x2200, 0x22FF),  # 数学运算符
    (0x2460, 0x2473),  # ①–⑳
    (0x2500, 0x257F),  # 框线
    (0x25A0, 0x25FF),  # 几何图形
    (0x2605, 0x2606),  # ★ ☆
    (0x2713, 0x2714),  # ✓ ✔
    (0x2717, 0x2718),  # ✗ ✘
    (0x3000, 0x303D),  # CJK 标点（含全角空格）
    (0x3400, 0x4DBF),  # CJK 扩展 A
    (0x4E00, 0x9FFF),  # CJK 基本区
    (0xFF01, 0xFF5E),  # 全角 ASCII
    (0xFFE0, 0xFFE6),  # 全角货币等
)
#: 与拉丁字母同形、工程符号里不用的希腊字母（拒绝）。``ρ ν κ`` 是合法工程符号，放行。
GREEK_LOOKALIKES: frozenset[int] = frozenset(ord(char) for char in "ΑΒΕΖΗΙΚΜΝΟΡΤΥΧοιυχ")
#: 码点类别黑名单（叠在区间之上）。
EXCLUDED_CATEGORIES: frozenset[str] = frozenset({"Cc", "Cf", "Co", "Cs", "Cn", "Zl", "Zp", "Zs", "Mn", "Mc", "Me"})
#: 属于被排除类别但明确放行的码点：两个空格。**换行 / 制表 / ``\r`` 都不放行**：目录条目的字段都是单行文本。
_ALWAYS_ALLOWED: frozenset[int] = frozenset({0x20, 0x3000})


def _base_allowed(codepoint: int) -> bool:
    if codepoint in _ALWAYS_ALLOWED:
        return True
    if codepoint in GREEK_LOOKALIKES:
        return False
    if not any(low <= codepoint <= high for low, high in ALLOWED_RANGES):
        return False
    return unicodedata.category(chr(codepoint)) not in EXCLUDED_CATEGORIES


@lru_cache(maxsize=65_536)
def is_allowed_char(char: str) -> bool:
    """单个字符是否在白名单内（含 NFKC 封闭：它的 NFKC 结果里每个字符也必须在白名单内）。"""

    if len(char) != 1:
        return False
    if not _base_allowed(ord(char)):
        return False
    return all(_base_allowed(ord(item)) for item in unicodedata.normalize("NFKC", char))


def disallowed_codepoints(text: str, *, limit: int = 8) -> list[int]:
    """文本里白名单之外的码点（只回码点，不回字符本身），最多 ``limit`` 个。"""

    found: list[int] = []
    for char in text:
        if not is_allowed_char(char):
            found.append(ord(char))
            if len(found) >= limit:
                break
    return found


# ---------------------------------------------------------------------------
# 配置读取
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegistryConfig:
    """运维配置的目录基址。``base_url`` 只在请求期交给 ``governed_get`` 判定（单一真源），这里不校验形状。"""

    base_url: str
    allow_private: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    @property
    def host(self) -> str:
        """目录主机名（审计 / 熔断分桶 / 面板状态用；不含路径与查询串）。无法解析时返回空串。"""

        if not self.base_url:
            return ""
        try:
            return parse_server_url(self.base_url, allow_private_network=True).host
        except (ValueError, MCPError):
            return ""

    def __repr__(self) -> str:  # 不泄漏基址
        return f"RegistryConfig(configured={self.configured}, allow_private={self.allow_private})"


def resolve_registry_config(environ: Mapping[str, str] | None = None) -> RegistryConfig:
    """读运维环境变量。未设 / 空白 = 功能关闭。``ALLOW_PRIVATE`` 只认字面 ``1``（与联网搜索同口径）。"""

    env = os.environ if environ is None else environ
    base_url = (env.get(ENV_URL) or "").strip()
    allow_private = (env.get(ENV_ALLOW_PRIVATE) or "").strip() == "1"
    return RegistryConfig(base_url=base_url, allow_private=allow_private)


def registry_principal(admin_user_id: str) -> Principal:
    """管理员请求内的**服务级主体**：只持有 ``MCP_REGISTRY_SEARCH``（不含任何默认本地能力）。

    ``subject`` 里带管理员 id，使限流 / 熔断按管理员分桶（一个管理员刷屏不影响另一个）；模型与普通成员没有任何入口。
    """

    token = re.sub(r"[^A-Za-z0-9._\-]", "-", admin_user_id)[:100] or "admin"
    return Principal.local(f"mcp-registry-{token}", capabilities=frozenset({Capability.MCP_REGISTRY_SEARCH}))


def registry_context(admin_user_id: str, *, request_id: str) -> RequestContext:
    return RequestContext(
        principal=registry_principal(admin_user_id),
        request_id=request_id,
        thread_id="mcp-registry",
        deadline_seconds=NETWORK_BUDGET_SECONDS,
    )


def provider_call_for(config: RegistryConfig) -> ProviderCall:
    """网关调用意图：熔断按目录主机分桶（``model`` = ``mcp_registry:<host>``）。"""

    return ProviderCall(
        provider=ProviderKind.MCP_REGISTRY,
        category=CallCategory.MCP_REGISTRY,
        model=f"mcp_registry:{config.host or 'unknown'}",
        required_capability=Capability.MCP_REGISTRY_SEARCH,
    )


# ---------------------------------------------------------------------------
# 条目模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RemoteView:
    type: str
    url: str


@dataclass(frozen=True, slots=True)
class PackageView:
    registry_type: str
    identifier: str


@dataclass(frozen=True, slots=True)
class RegistryEntry:
    """一条已校验、已归一化的目录条目。``unsafe`` 条目只保留名字与版本（描述 / 地址不展示、不可提案）。"""

    name: str
    version: str
    status: str
    description: str = ""
    remotes: tuple[RemoteView, ...] = ()
    packages: tuple[PackageView, ...] = ()
    #: 被丢弃的非白名单字段名的个数（名字本身是第三方文本，不回显）。
    dropped_fields: int = 0
    unsafe_reason: str = ""
    #: 提案资格：``None`` = 可提案；否则是 ``REASON_*`` 稳定短码。
    not_proposable_reason: str | None = None

    @property
    def proposable(self) -> bool:
        return self.status == STATUS_OK and self.not_proposable_reason is None

    def to_dict(self) -> dict[str, Any]:
        """面板视图：全部是纯文本字段。``unsafe`` 条目没有 description / remotes / packages。"""

        return {
            "name": self.name,
            "version": self.version,
            "status": self.status,
            "description": self.description,
            "remotes": [{"type": item.type, "url": display_url(item.url)} for item in self.remotes],
            "packages": [{"registry_type": item.registry_type, "identifier": item.identifier} for item in self.packages],
            "dropped_fields": self.dropped_fields,
            "unsafe_reason": self.unsafe_reason,
            "proposable": self.proposable,
            "not_proposable_reason": self.not_proposable_reason,
        }


@dataclass(frozen=True, slots=True)
class SearchResult:
    entries: tuple[RegistryEntry, ...]
    #: 本次读取的页数（审计 / 面板用）。
    pages: int
    #: 因 ``MAX_PAGES`` 截断而未取完。
    truncated: bool = False
    #: 被重名规则整体标为 ``unsafe`` 的名字个数。
    duplicate_names: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries": [entry.to_dict() for entry in self.entries],
            "count": len(self.entries),
            "pages": self.pages,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class Proposal:
    """一份待审提案（覆盖层格式文本）。恒 ``disabled: true``、不含 ``allow``（全拒）。"""

    name: str
    version: str
    server_id: str
    text: str
    proposal_hash: str
    notes: tuple[str, ...] = ()
    #: 丢弃的敏感载荷个数（查询串参数 / 请求头 / 令牌 / env）。
    secret_dropped: int = 0
    registry_host: str = ""
    #: 域名含非 ASCII 字符时的 punycode 形式（提示运维核对）。
    idna_host: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "server_id": self.server_id,
            "proposal_text": self.text,
            "proposal_hash": self.proposal_hash,
            "notes": list(self.notes),
            "secret_dropped": self.secret_dropped,
            "idna_host": self.idna_host,
        }


class McpRegistryError(RuntimeError):
    """目录读取 / 提案失败。``code`` 是 ``MCP_REGISTRY_*`` 稳定码，``str(exc)`` 只含码对应的固定文案（不含上游内容）。"""

    def __init__(self, code: str) -> None:
        # 经 MCPError 取固定前缀与码校验；detail 是固定的一句话，不含任何请求 / 响应内容
        base = MCPError(code, _DETAILS.get(code, ""))
        super().__init__(base.message)
        self.code = code
        self.message = base.message


_DETAILS: dict[str, str] = {
    MCP_REGISTRY_NOT_CONFIGURED: "运维没有配置目录基址（WATER_AGENT_MCP_REGISTRY_URL）",
    MCP_REGISTRY_UNAVAILABLE: "目录暂时不可用（不可达、超时、服务端错误或目的地不被允许）",
    MCP_REGISTRY_AUTH: "目录拒绝了本次请求（本部署不向目录发送任何凭据）",
    MCP_REGISTRY_RATE_LIMITED: "目录触发限流，请稍后再试",
    MCP_REGISTRY_BAD_RESPONSE: "目录返回的内容不符合协议 v1（整页已被拒绝，未采纳任何条目）",
    MCP_REGISTRY_ENTRY_NOT_FOUND: "目录里没有该 name@version",
    MCP_REGISTRY_ENTRY_UNSAFE: "该条目未通过安全检查，不可生成提案",
    MCP_REGISTRY_NOT_PROPOSABLE: "该条目没有可生成提案的 streamable-http 域名地址",
}

#: 内核错误码 → 目录错误码。禁止目的地对调用方就是"目录不可用"（不暴露网段分类）；超限响应是"坏响应"。
_FROM_EXTERNAL: dict[str, str] = {
    "EXTERNAL_DESTINATION_FORBIDDEN": MCP_REGISTRY_UNAVAILABLE,
    "EXTERNAL_TIMEOUT": MCP_REGISTRY_UNAVAILABLE,
    "EXTERNAL_UNAVAILABLE": MCP_REGISTRY_UNAVAILABLE,
    "EXTERNAL_AUTH": MCP_REGISTRY_AUTH,
    "EXTERNAL_RATE_LIMITED": MCP_REGISTRY_RATE_LIMITED,
    "EXTERNAL_BAD_RESPONSE": MCP_REGISTRY_BAD_RESPONSE,
    "EXTERNAL_TOO_LARGE": MCP_REGISTRY_BAD_RESPONSE,
}


def code_from_external(external_code: str) -> str:
    """未知的内核码一律当作"目录不可用"（不泄漏内核的命名空间，也不放大故障）。"""

    return _FROM_EXTERNAL.get(external_code, MCP_REGISTRY_UNAVAILABLE)


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------


def validate_query(query: str) -> str:
    """搜索词：单行、≤ 64 字符、无控制字符、每个字符过白名单。返回规范化（去首尾空白）后的词；不合格抛 ``ValueError``。"""

    if not isinstance(query, str):
        raise TypeError("搜索词必须是字符串")
    stripped = query.strip()
    if len(stripped) > MAX_QUERY_CHARS:
        raise ValueError(f"搜索词不得超过 {MAX_QUERY_CHARS} 个字符")
    if any(unicodedata.category(char) in ("Cc", "Cf", "Zl", "Zp") for char in stripped):
        raise ValueError("搜索词不得含控制字符或换行")
    if disallowed_codepoints(stripped, limit=1):
        raise ValueError("搜索词含有不允许的字符")
    return stripped


def validate_cursor(cursor: str | None) -> str | None:
    if cursor is None or cursor == "":
        return None
    if not isinstance(cursor, str) or _CURSOR_RE.fullmatch(cursor) is None or len(cursor) > MAX_CURSOR_CHARS:
        raise ValueError("分页游标不合法")
    return cursor


# ---------------------------------------------------------------------------
# 条目解析（整页严格校验；任何形状错误整页被拒）
# ---------------------------------------------------------------------------


class _Malformed(Exception):
    """目录响应的形状不符合协议 v1。不带任何响应内容。"""


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def _clean_display(text: str, limit: int) -> str:
    return neutralize_text(text, limit=limit)


def _text_is_clean(text: str) -> tuple[bool, str]:
    """字符白名单 + 核心边界归一化不改写 + 注入检测。返回 (干净, 原因短码)。"""

    if disallowed_codepoints(text, limit=1):
        return False, "charset"
    if detect_injection(text):
        return False, "injection"
    # 归一化会改写形似边界 / 模板 token / 外发语法的文本；改写过 = 原文含这些形态 = 不信任
    if neutralize_text(text, limit=max(len(text), 1) + 16) != " ".join(text.split()):
        return False, "markup"
    return True, ""


def display_url(url: str) -> str:
    """面板展示用的 URL：只留 ``scheme://主机[:端口]路径``——去掉 userinfo 与查询串（常含令牌）。解析不了就给固定占位。"""

    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return "(无法解析的地址)"
    if not parts.scheme or not host:
        return "(无法解析的地址)"
    if ":" not in host:
        # 展示**实际会被连接的**形式：非 ASCII 域名转 punycode，管理员据此识别同形字域名
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return "(无法解析的地址)"
    shown_host = f"[{host}]" if ":" in host else host
    return f"{parts.scheme}://{shown_host}{port}{parts.path}"


def _parse_remote(raw: Any) -> tuple[RemoteView, bool]:
    if not isinstance(raw, dict):
        raise _Malformed
    keys = set(raw)
    if not {"type", "url"} <= keys:
        raise _Malformed
    kind = raw["type"]
    url = raw["url"]
    if not isinstance(kind, str) or not isinstance(url, str):
        raise _Malformed
    if _REMOTE_TYPE_RE.fullmatch(kind) is None or len(url) > MAX_URL_CHARS or not url:
        raise _Malformed
    return RemoteView(type=kind, url=url), bool(keys - _REMOTE_KEYS)


def _parse_package(raw: Any) -> tuple[PackageView, bool]:
    if not isinstance(raw, dict):
        raise _Malformed
    keys = set(raw)
    if not {"registry_type", "identifier"} <= keys:
        raise _Malformed
    registry_type = raw["registry_type"]
    identifier = raw["identifier"]
    if not isinstance(registry_type, str) or not isinstance(identifier, str):
        raise _Malformed
    if _REGISTRY_TYPE_RE.fullmatch(registry_type) is None or not identifier or len(identifier) > MAX_IDENTIFIER_CHARS:
        raise _Malformed
    return PackageView(registry_type=registry_type, identifier=identifier), bool(keys - _PACKAGE_KEYS)


def _proposal_eligibility(remotes: Sequence[RemoteView]) -> str | None:
    """条目能否生成提案：``None`` = 可以；否则返回 ``REASON_*``。

    只接受 ``type == streamable-http`` 且 URL 通过 ``parse_server_url``（https、无 userinfo / fragment、规范点分十进制 IPv4）
    **并且主机名不是 IP 字面量、不是 localhost / 单标签主机**的远程。``parse_server_url`` 放行 ``127.0.0.1`` / ``[::1]`` /
    ``10.0.0.5``（Step 0 实测），所以"非 IP 字面量"是本模块自己加的一层——目录条目永远不能把提案指向 IP 地址。
    真正挡内网的连接期 SSRF 分类在 S8、发生在运维应用提案之后，提案阶段不能指望它。
    """

    if not remotes:
        return REASON_NO_REMOTE
    streamable = [item for item in remotes if item.type == PROPOSABLE_REMOTE_TYPE]
    if not streamable:
        return REASON_NO_STREAMABLE_HTTP
    first: str | None = None
    for item in streamable:
        try:
            parsed = parse_server_url(item.url, allow_private_network=False)
        except (ValueError, MCPError):
            first = first or REASON_URL_REJECTED
            continue
        if _is_ip_literal(parsed.host):
            first = first or REASON_IP_LITERAL
            continue
        if parsed.host == "localhost" or parsed.host.endswith(".localhost") or "." not in parsed.host:
            first = first or REASON_LOCAL_HOST
            continue
        return None
    return first or REASON_URL_REJECTED


def _parse_entry(raw: Any) -> RegistryEntry:
    if not isinstance(raw, dict):
        raise _Malformed
    if len(json.dumps(raw, ensure_ascii=False).encode("utf-8")) > MAX_ENTRY_BYTES:
        raise _Malformed
    name = raw.get("name")
    version = raw.get("version")
    description = raw.get("description", "")
    if not isinstance(name, str) or not isinstance(version, str) or not isinstance(description, str):
        raise _Malformed
    if _NAME_RE.fullmatch(name) is None:
        # 名字形状不对（含同形字 / 大写 / 控制字符）是整页形状错误：名字是条目的主键，宁可整页拒也不展示一个看不懂的名字
        raise _Malformed
    if _VERSION_RE.fullmatch(version) is None:
        raise _Malformed
    dropped = len(set(raw) - _ENTRY_KEYS)

    raw_remotes = raw.get("remotes", [])
    raw_packages = raw.get("packages", [])
    if not isinstance(raw_remotes, list) or not isinstance(raw_packages, list):
        raise _Malformed
    if len(raw_remotes) > MAX_REMOTES or len(raw_packages) > MAX_PACKAGES:
        raise _Malformed
    remotes: list[RemoteView] = []
    packages: list[PackageView] = []
    for item in raw_remotes:
        view, had_extra = _parse_remote(item)
        remotes.append(view)
        dropped += int(had_extra)
    for item in raw_packages:
        view_p, had_extra = _parse_package(item)
        packages.append(view_p)
        dropped += int(had_extra)

    # ---- 不可信文本：字符白名单 / 归一化 / 注入检测。任何一项不过 → unsafe（描述与地址都不展示）----
    reason = ""
    clean, why = _text_is_clean(description)  # 对**全文**检测，再截断展示：注入藏在第 601 个字符之后也查得出
    if not clean:
        reason = why
    if not reason:
        for remote in remotes:
            for text in (remote.type, remote.url):
                clean, why = _text_is_clean(text)
                if not clean:
                    reason = why
                    break
            if reason:
                break
    if not reason:
        for package in packages:
            for text in (package.registry_type, package.identifier):
                clean, why = _text_is_clean(text)
                if not clean:
                    reason = why
                    break
            if reason:
                break
    if reason:
        return RegistryEntry(
            name=name,
            version=version,
            status=STATUS_UNSAFE,
            dropped_fields=dropped,
            unsafe_reason=reason,
            not_proposable_reason=REASON_UNSAFE,
        )
    return RegistryEntry(
        name=name,
        version=version,
        status=STATUS_OK,
        description=_clean_display(description, MAX_DESCRIPTION_CHARS),
        remotes=tuple(remotes),
        packages=tuple(packages),
        dropped_fields=dropped,
        not_proposable_reason=_proposal_eligibility(remotes),
    )


def parse_page(body: bytes) -> tuple[list[RegistryEntry], str | None]:
    """解析并严格校验一页。形状有任何问题抛 ``McpRegistryError(MCP_REGISTRY_BAD_RESPONSE)``，不部分采纳。"""

    try:
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict) or set(payload) != _RESPONSE_KEYS:
            raise _Malformed
        raw_entries = payload["entries"]
        next_cursor = payload["next_cursor"]
        if not isinstance(raw_entries, list) or len(raw_entries) > PAGE_LIMIT:
            raise _Malformed
        if next_cursor is not None and (
            not isinstance(next_cursor, str) or _CURSOR_RE.fullmatch(next_cursor) is None
        ):
            raise _Malformed
        entries = [_parse_entry(item) for item in raw_entries]
    except (ValueError, _Malformed, UnicodeDecodeError, RecursionError):
        raise McpRegistryError(MCP_REGISTRY_BAD_RESPONSE) from None
    return entries, next_cursor


def _mark_duplicates(entries: list[RegistryEntry]) -> tuple[list[RegistryEntry], int]:
    """同一 ``name@version`` 出现多次，或名字归一为同一 ``server_id`` 的不同名字（``a-b`` / ``a_b``）：全部标为 ``unsafe``。

    重名意味着"提案指向哪一条"是含糊的，而提案是运维要贴进覆盖层的文本——宁可都不可提案，也不挑一条。
    """

    by_key: dict[str, list[int]] = {}
    for index, entry in enumerate(entries):
        by_key.setdefault(f"{entry.name}@{entry.version}", []).append(index)
    by_id: dict[str, set[str]] = {}
    for entry in entries:
        by_id.setdefault(normalize_server_id(entry.name) or entry.name, set()).add(entry.name)
    flagged: set[int] = set()
    for indexes in by_key.values():
        if len(indexes) > 1:
            flagged.update(indexes)
    for index, entry in enumerate(entries):
        if len(by_id.get(normalize_server_id(entry.name) or entry.name, ())) > 1:
            flagged.add(index)
    if not flagged:
        return entries, 0
    result = list(entries)
    for index in flagged:
        entry = entries[index]
        result[index] = RegistryEntry(
            name=entry.name,
            version=entry.version,
            status=STATUS_UNSAFE,
            dropped_fields=entry.dropped_fields,
            unsafe_reason="duplicate",
            not_proposable_reason=REASON_UNSAFE,
        )
    return result, len({entries[index].name for index in flagged})


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


@dataclass
class RegistryCatalogClient:
    """目录协议 v1 客户端。**只读、只访问 ``config.base_url``、永不连接条目地址。**"""

    config: RegistryConfig
    ctx: RequestContext
    resolver: Callable[[str, int], Sequence[str]] | None = None
    ssl_context: object | None = None
    gateway: Any | None = None
    _call: ProviderCall = field(init=False)

    def __post_init__(self) -> None:
        self._call = provider_call_for(self.config)

    def _fetch(self, query: str, cursor: str | None) -> bytes:
        params: dict[str, str] = {"limit": str(PAGE_LIMIT)}
        if query:
            params["query"] = query
        if cursor:
            params["cursor"] = cursor
        url = self.config.base_url.rstrip("/") + "/servers?" + urlencode(params)
        timeout = NETWORK_BUDGET_SECONDS
        if self.gateway is not None:
            timeout = min(float(self.gateway.timeout_for(ProviderKind.MCP_REGISTRY)), NETWORK_BUDGET_SECONDS)
        try:
            response = governed_get(
                url,
                headers={"Accept": MEDIA_TYPE},
                max_bytes=MAX_RESPONSE_BYTES,
                timeout=timeout,
                allow_private_network=self.config.allow_private,
                ctx=self.ctx,
                call=self._call,
                require_media_type=MEDIA_TYPE,
                resolver=self.resolver,
                ssl_context=self.ssl_context,
                gateway=self.gateway,
            )
        except ExternalGetError as exc:
            raise McpRegistryError(code_from_external(exc.code)) from None
        return response.body

    def search(self, query: str = "", cursor: str | None = None) -> SearchResult:
        """搜索 / 浏览。至多 ``MAX_PAGES`` 页、总量 ≤ 256 KiB；任何一页形状不对整次失败。"""

        if not self.config.configured:
            raise McpRegistryError(MCP_REGISTRY_NOT_CONFIGURED)
        try:
            clean_query = validate_query(query)
            current = validate_cursor(cursor)
        except (TypeError, ValueError):
            raise McpRegistryError(MCP_REGISTRY_BAD_RESPONSE) from None
        collected: list[RegistryEntry] = []
        total_bytes = 0
        pages = 0
        truncated = False
        while True:
            body = self._fetch(clean_query, current)
            total_bytes += len(body)
            if total_bytes > MAX_TOTAL_BYTES:
                raise McpRegistryError(MCP_REGISTRY_BAD_RESPONSE)
            entries, next_cursor = parse_page(body)
            pages += 1
            collected.extend(entries)
            if next_cursor is None:
                break
            if pages >= MAX_PAGES:
                truncated = True
                break
            current = next_cursor
        marked, duplicates = _mark_duplicates(collected[:MAX_RESULTS])
        return SearchResult(entries=tuple(marked), pages=pages, truncated=truncated, duplicate_names=duplicates)

    def find(self, name: str, version: str) -> RegistryEntry:
        """**服务端重取**：用 ``name`` 作搜索词重新读目录，按 ``name@version`` 精确匹配。

        提案路径只信这里取到的条目，**不信客户端回传的任何内容**。同一 ``name@version`` 命中不止一条（重名）已被
        ``_mark_duplicates`` 标为 ``unsafe``，调用方据此拒绝。
        """

        if _NAME_RE.fullmatch(name or "") is None or _VERSION_RE.fullmatch(version or "") is None:
            raise McpRegistryError(MCP_REGISTRY_ENTRY_NOT_FOUND)
        result = self.search(name)
        matches = [entry for entry in result.entries if entry.name == name and entry.version == version]
        if not matches:
            raise McpRegistryError(MCP_REGISTRY_ENTRY_NOT_FOUND)
        return matches[0]


# ---------------------------------------------------------------------------
# 提案
# ---------------------------------------------------------------------------


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def proposal_hash_of(definition: Mapping[str, Any], *, registry_host: str, name: str, version: str) -> str:
    """``sha256(规范化完整定义 + 目录主机 + 名称 + 版本)``。规范化 = ``json.dumps(sort_keys, ensure_ascii, 紧凑分隔符)``。

    定义里的 URL、版本、目录主机、名称任何一个变化，哈希都变（``tests/test_mcp_registry_proposal.py`` 逐项断言）。
    """

    payload = "\x1f".join((_canonical(definition), registry_host, name, version))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_proposal(
    entry: RegistryEntry,
    *,
    registry_host: str,
    existing_ids: Sequence[str] = (),
) -> Proposal:
    """为一条**服务端重取到的**条目生成提案文本。不合格抛 :class:`McpRegistryError`。

    格式与"丢弃什么"的唯一真源是 ``mcp/importer.py``：本函数把条目转成它认识的 ``mcpServers`` 输入再调用
    ``preview_import``（因此提案天然 ``disabled: true`` 且不含 ``allow``，查询串 / 请求头 / 令牌 / env 由它丢弃）。
    条目里**被丢弃**的敏感载荷由本函数在转换前计数（条目只有 ``type`` / ``url`` 两个白名单字段会进入转换，
    其余字段在 ``_parse_entry`` 阶段就不保留；URL 的查询串由 importer 丢弃并写进 notes）。
    """

    if entry.status != STATUS_OK:
        raise McpRegistryError(MCP_REGISTRY_ENTRY_UNSAFE)
    if entry.not_proposable_reason is not None:
        raise McpRegistryError(MCP_REGISTRY_NOT_PROPOSABLE)
    remote = next(
        item
        for item in entry.remotes
        if item.type == PROPOSABLE_REMOTE_TYPE and _proposal_eligibility([item]) is None
    )
    server_input = json.dumps({"mcpServers": {entry.name: {"type": "http", "url": remote.url}}})
    try:
        preview = preview_import(server_input, existing_ids=existing_ids)
    except Exception:  # noqa: BLE001 — importer 的任何拒绝都只表现为"不可提案"，不回显内容
        raise McpRegistryError(MCP_REGISTRY_NOT_PROPOSABLE) from None
    proposed = [item for item in preview.entries if item.status == "proposed"]
    if not proposed or not preview.proposal.get("servers"):
        raise McpRegistryError(MCP_REGISTRY_NOT_PROPOSABLE)
    server = preview.proposal["servers"][0]
    # 防御性复核：无论 importer 怎么改，目录提案必须是"已停用 + 无 allow + 无任何敏感键"
    if server.get("disabled") is not True or "allow" in server:
        raise McpRegistryError(MCP_REGISTRY_NOT_PROPOSABLE)
    for forbidden in ("env", "env_file", "headers", "bearer_credential", "oauth", "command", "args", "cwd"):
        server.pop(forbidden, None)
    # importer 保留原始 Unicode 主机名；提案里一律写 ASCII（punycode）形式，让运维看到的就是最终会被连接的主机
    canonical = parse_server_url(remote.url, allow_private_network=False)
    server["url"] = canonical.origin + canonical.path
    try:
        MCPServerConfig.model_validate(server)
    except (ValueError, TypeError):
        raise McpRegistryError(MCP_REGISTRY_NOT_PROPOSABLE) from None

    secret_dropped = 0
    notes = list(proposed[0].notes)
    if any("查询串已丢弃" in note for note in notes):
        secret_dropped += 1
    # 条目里的非白名单字段（常含 headers / env / token）在解析阶段已丢弃；这里只汇总个数
    secret_dropped += entry.dropped_fields
    if entry.dropped_fields:
        notes.append(f"条目里有 {entry.dropped_fields} 个不在白名单内的字段，已丢弃（可能含请求头 / 环境变量 / 令牌）")

    authority = remote.url.split("://", 1)[-1].split("/", 1)[0]
    idna_host = canonical.host if any(ord(char) > 127 for char in authority) else None
    if idna_host is not None:
        notes.append(f"域名含非 ASCII 字符，提案里是转换后的形式 {idna_host!r}：请运维核对是否就是想要的站点（可能是同形字域名）")
    notes.append("提案只是文本：已停用且不含 allow（全拒）；运维核对后贴进部署覆盖层，启用与放行是之后的独立步骤")

    definition = {key: value for key, value in server.items() if key != "server_id"}
    digest = proposal_hash_of(definition, registry_host=registry_host, name=entry.name, version=entry.version)
    text = json.dumps(preview.proposal, ensure_ascii=False, indent=2) + "\n"
    return Proposal(
        name=entry.name,
        version=entry.version,
        server_id=str(server.get("server_id", "")),
        text=text,
        proposal_hash=digest,
        notes=tuple(notes),
        secret_dropped=secret_dropped,
        registry_host=registry_host,
        idna_host=idna_host,
    )


__all__ = [
    "ALLOWED_RANGES",
    "ENV_ALLOW_PRIVATE",
    "ENV_URL",
    "MAX_PAGES",
    "MAX_QUERY_CHARS",
    "PAGE_LIMIT",
    "PROPOSABLE_REMOTE_TYPE",
    "REASON_AMBIGUOUS",
    "REASON_IP_LITERAL",
    "REASON_LOCAL_HOST",
    "REASON_NO_REMOTE",
    "REASON_NO_STREAMABLE_HTTP",
    "REASON_UNSAFE",
    "REASON_URL_REJECTED",
    "STATUS_OK",
    "STATUS_UNSAFE",
    "McpRegistryError",
    "PackageView",
    "Proposal",
    "RegistryCatalogClient",
    "RegistryConfig",
    "RegistryEntry",
    "RemoteView",
    "SearchResult",
    "build_proposal",
    "code_from_external",
    "disallowed_codepoints",
    "display_url",
    "is_allowed_char",
    "parse_page",
    "proposal_hash_of",
    "provider_call_for",
    "registry_context",
    "registry_principal",
    "resolve_registry_config",
    "validate_cursor",
    "validate_query",
]
