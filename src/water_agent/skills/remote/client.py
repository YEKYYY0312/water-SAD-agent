"""Skill 注册表客户端（P7-S13，规格 §6.2 / §6.3 第 2–3 步）。

协议是**我们自己定义的最小静态协议**，`unverified_against_real_registry`（OpenClaw 的 ClawHub 不是静态协议，其服务端
不在 OpenClaw 仓库里，本机读不到；**不对接 ClawHub，不声称兼容任何真实注册表**）：

- ``GET {base}/index.json``（≤ 256 KiB，``application/json``）→
  ``{"version": 1, "skills": [{"name", "versions": [{"version", "sha256", "size"}]}]}``
- ``GET {base}/skills/{name}/{version}/SKILL.md``（≤ 64 KiB，``text/markdown`` 或 ``text/plain``）

**出口只经 ``external.http_get.governed_get``**（SSRF 分类、钉死 IP、3xx 不跟随、字节与时间上限、``ProviderGateway`` 限流 /
熔断 / 对账）；本模块不含任何原始网络原语，也不直接调用 ``open_exchange``（P7-I08 的登记表里没有它）。基址由运维写死
（``WATER_AGENT_SKILL_REGISTRY_URL``），名字与版本经严格校验后才拼进路径——请求路径里没有任何可由注册表内容决定的部分。

**失败关闭**：任一步失败抛 :class:`SkillRemoteError`（稳定码，说明里不含上游正文 / 完整 URL / 路径），**磁盘零写入**
（本模块不写盘）。不重试到别的地址，不回落。

**sha256 / size 一致只防传输错误，不是信任根**——index 与文件来自同一个注册表，被攻陷的注册表可以两者一致地撒谎。
信任根是批准人读过全文（规格 §6.4）。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

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
from water_agent.mcp.http_transport import Resolver
from water_agent.runtime.governance.contracts import (
    CallCategory,
    Capability,
    Principal,
    ProviderCall,
    ProviderKind,
    RequestContext,
)
from water_agent.runtime.governance.gateway import ProviderGateway
from water_agent.skills.errors import (
    SKILL_NOT_IN_REGISTRY,
    SKILL_REGISTRY_BAD_RESPONSE,
    SKILL_REGISTRY_MISMATCH,
    SKILL_REGISTRY_UNAVAILABLE,
)
from water_agent.skills.models import MAX_FILE_BYTES
from water_agent.skills.naming import is_valid_name, is_valid_version

MAX_INDEX_BYTES = 256 * 1024
INDEX_MEDIA_TYPE = "application/json"
SKILL_MEDIA_TYPES = ("text/markdown", "text/plain")
#: 一份 index 最多多少个 skill / 每个 skill 最多多少个版本（防止巨大 index 吃内存与时间）。
MAX_INDEX_SKILLS = 2000
MAX_INDEX_VERSIONS = 200
NETWORK_BUDGET_SECONDS = 19.0
_SHA256_CHARS = frozenset("0123456789abcdef")

#: ``governed_get`` 的稳定码 → 本模块的稳定码。**没有 EXTERNAL_* 泄漏到 CLI 输出**。
_EXTERNAL_TO_SKILL: dict[str, str] = {
    EXTERNAL_DESTINATION_FORBIDDEN: SKILL_REGISTRY_UNAVAILABLE,
    EXTERNAL_TIMEOUT: SKILL_REGISTRY_UNAVAILABLE,
    EXTERNAL_UNAVAILABLE: SKILL_REGISTRY_UNAVAILABLE,
    EXTERNAL_AUTH: SKILL_REGISTRY_UNAVAILABLE,
    EXTERNAL_RATE_LIMITED: SKILL_REGISTRY_UNAVAILABLE,
    EXTERNAL_BAD_RESPONSE: SKILL_REGISTRY_BAD_RESPONSE,
    EXTERNAL_TOO_LARGE: SKILL_REGISTRY_BAD_RESPONSE,
}

_MESSAGES: dict[str, str] = {
    SKILL_REGISTRY_UNAVAILABLE: "skill 注册表不可用（未配置、目的地不被允许、超时、被拒绝或限流）",
    SKILL_REGISTRY_BAD_RESPONSE: "skill 注册表返回了无法接受的响应",
    SKILL_REGISTRY_MISMATCH: "skill 注册表的声明与返回的内容不一致",
    SKILL_NOT_IN_REGISTRY: "注册表里没有该 name@version",
}


class SkillRemoteError(Exception):
    """取回失败。``str(exc)`` 与 ``message`` 都只含稳定码对应的固定文案（**不含**上游正文 / 网址 / 路径）。"""

    def __init__(self, code: str, detail: str = "") -> None:
        base = _MESSAGES.get(code, code)
        self.code = code
        self.message = f"{base}（{detail}）" if detail else base
        super().__init__(f"{code}：{self.message}")


@dataclass(frozen=True, slots=True)
class IndexVersion:
    version: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class RegistryIndex:
    """通过严格校验的 index。``skills`` 是 name → (版本 → 声明)。"""

    skills: Mapping[str, Mapping[str, IndexVersion]]

    def find(self, name: str, version: str) -> IndexVersion | None:
        return self.skills.get(name, {}).get(version)

    def versions_of(self, name: str) -> Mapping[str, IndexVersion]:
        return self.skills.get(name, {})


@dataclass(frozen=True, slots=True)
class FetchedSkill:
    name: str
    version: str
    raw: bytes
    sha256: str
    size: int
    registry_host: str


def sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegistryConfig:
    """注册表基址（运维写死）。``__repr__`` 不含完整 URL。"""

    base_url: str
    allow_private: bool = False

    def __post_init__(self) -> None:
        if not self.base_url or self.base_url != self.base_url.strip():
            raise ValueError("注册表基址为空或含首尾空白")

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).hostname or "unknown"

    def __repr__(self) -> str:
        return f"RegistryConfig(host={self.host!r}, allow_private={self.allow_private})"


def cli_principal() -> Principal:
    """运维 CLI 的主体：**只持有** ``Capability.SKILL_FETCH``（不含默认本地能力集——CLI 不需要检索 / 计算）。"""

    return Principal.local("skills-cli", capabilities=frozenset({Capability.SKILL_FETCH}))


def provider_call_for(config: RegistryConfig) -> ProviderCall:
    """网关调用意图：熔断按注册表主机分桶（``model`` = ``skill_registry:<host>``）。"""

    return ProviderCall(
        provider=ProviderKind.SKILL_REGISTRY,
        category=CallCategory.SKILL_REGISTRY,
        model=f"skill_registry:{config.host}",
        required_capability=Capability.SKILL_FETCH,
    )


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class RegistryClient:
    """对一个注册表基址的取回客户端。每个请求都是一次 ``governed_get``。"""

    def __init__(
        self,
        config: RegistryConfig,
        *,
        gateway: ProviderGateway | None = None,
        resolver: Resolver | None = None,
        ssl_context: object | None = None,
        principal: Principal | None = None,
        request_id: str = "skills-cli",
    ) -> None:
        self._config = config
        self._gateway = gateway
        self._resolver = resolver
        self._ssl_context = ssl_context
        self._principal = principal if principal is not None else cli_principal()
        self._request_id = request_id
        self.request_count = 0

    @property
    def host(self) -> str:
        return self._config.host

    def _context(self) -> RequestContext:
        return RequestContext(
            principal=self._principal,
            request_id=self._request_id,
            thread_id="skills-cli",
            deadline_seconds=NETWORK_BUDGET_SECONDS,
        )

    def _url(self, suffix: str) -> str:
        return self._config.base_url.rstrip("/") + suffix

    def _get(self, suffix: str, *, max_bytes: int, media_types: tuple[str, ...]) -> tuple[bytes, str]:
        """一次受治理的 GET；``media_types`` 里任一匹配即可（``governed_get`` 一次只比一个，故逐个试在内核外做不到——
        这里取响应后自己再核对媒体类型）。"""

        self.request_count += 1
        try:
            response = governed_get(
                self._url(suffix),
                headers={"Accept": ", ".join(media_types)},
                max_bytes=max_bytes,
                timeout=NETWORK_BUDGET_SECONDS,
                allow_private_network=self._config.allow_private,
                ctx=self._context(),
                call=provider_call_for(self._config),
                require_media_type=None,
                resolver=self._resolver,
                ssl_context=self._ssl_context,
                gateway=self._gateway,
            )
        except ExternalGetError as exc:
            raise SkillRemoteError(_EXTERNAL_TO_SKILL.get(exc.code, SKILL_REGISTRY_UNAVAILABLE)) from None
        if response.media_type not in media_types:
            raise SkillRemoteError(SKILL_REGISTRY_BAD_RESPONSE, "媒体类型不符")
        return response.body, response.media_type

    # -- index ------------------------------------------------------------------------------------------------

    def fetch_index(self) -> RegistryIndex:
        body, _media = self._get("/index.json", max_bytes=MAX_INDEX_BYTES, media_types=(INDEX_MEDIA_TYPE,))
        return parse_index(body)

    # -- SKILL.md ---------------------------------------------------------------------------------------------

    def fetch_skill_file(self, name: str, version: str, *, declared: IndexVersion) -> FetchedSkill:
        """取 ``SKILL.md``。``name`` / ``version`` 必须已通过校验（拼进路径之前再核一次）。"""

        if not is_valid_name(name) or not is_valid_version(version):
            raise SkillRemoteError(SKILL_REGISTRY_BAD_RESPONSE, "请求参数不合法")
        suffix = f"/skills/{quote(name, safe='')}/{quote(version, safe='')}/SKILL.md"
        body, _media = self._get(suffix, max_bytes=min(MAX_FILE_BYTES, declared.size + 1), media_types=SKILL_MEDIA_TYPES)
        if len(body) != declared.size or sha256_hex(body) != declared.sha256:
            raise SkillRemoteError(SKILL_REGISTRY_MISMATCH, "字节数或 sha256 与 index 声明不一致")
        return FetchedSkill(
            name=name, version=version, raw=body, sha256=declared.sha256, size=declared.size,
            registry_host=self._config.host,
        )


# ---------------------------------------------------------------------------
# index 解析：严格，整份校验——任何一个条目不合法，整份 index 被拒
# ---------------------------------------------------------------------------


def parse_index(body: bytes) -> RegistryIndex:
    try:
        document = json.loads(body.decode("utf-8", errors="strict"))
    except (ValueError, UnicodeDecodeError):
        raise SkillRemoteError(SKILL_REGISTRY_BAD_RESPONSE, "index 不是合法 JSON") from None
    if not isinstance(document, dict) or document.get("version") != 1 or set(document) != {"version", "skills"}:
        raise SkillRemoteError(SKILL_REGISTRY_BAD_RESPONSE, "index 的形状不是协议 v1")
    skills = document["skills"]
    if not isinstance(skills, list) or len(skills) > MAX_INDEX_SKILLS:
        raise SkillRemoteError(SKILL_REGISTRY_BAD_RESPONSE, "index.skills 不是合理大小的列表")
    parsed: dict[str, dict[str, IndexVersion]] = {}
    for entry in skills:
        if not isinstance(entry, dict) or set(entry) != {"name", "versions"}:
            raise SkillRemoteError(SKILL_REGISTRY_BAD_RESPONSE, "index 条目形状不对")
        name, versions = entry["name"], entry["versions"]
        if not is_valid_name(name) or name in parsed:
            raise SkillRemoteError(SKILL_REGISTRY_BAD_RESPONSE, "index 里有不合法或重复的 name")
        if not isinstance(versions, list) or not versions or len(versions) > MAX_INDEX_VERSIONS:
            raise SkillRemoteError(SKILL_REGISTRY_BAD_RESPONSE, "index 的 versions 不是合理大小的非空列表")
        by_version: dict[str, IndexVersion] = {}
        for item in versions:
            if not isinstance(item, dict) or set(item) != {"version", "sha256", "size"}:
                raise SkillRemoteError(SKILL_REGISTRY_BAD_RESPONSE, "index 的版本条目形状不对")
            version, digest, size = item["version"], item["sha256"], item["size"]
            if not is_valid_version(version) or version in by_version:
                raise SkillRemoteError(SKILL_REGISTRY_BAD_RESPONSE, "index 里有不合法或重复的 version")
            if not (isinstance(digest, str) and len(digest) == 64 and set(digest) <= _SHA256_CHARS):
                raise SkillRemoteError(SKILL_REGISTRY_BAD_RESPONSE, "index 里的 sha256 不是 64 位小写十六进制")
            if type(size) is not int or not 1 <= size <= MAX_FILE_BYTES:
                raise SkillRemoteError(SKILL_REGISTRY_BAD_RESPONSE, "index 里的 size 不在 1..65536")
            by_version[version] = IndexVersion(version=version, sha256=digest, size=size)
        parsed[name] = by_version
    return RegistryIndex(skills=parsed)


__all__ = [
    "INDEX_MEDIA_TYPE",
    "MAX_INDEX_BYTES",
    "SKILL_MEDIA_TYPES",
    "FetchedSkill",
    "IndexVersion",
    "RegistryClient",
    "RegistryConfig",
    "RegistryIndex",
    "SkillRemoteError",
    "cli_principal",
    "parse_index",
    "provider_call_for",
    "sha256_hex",
]
