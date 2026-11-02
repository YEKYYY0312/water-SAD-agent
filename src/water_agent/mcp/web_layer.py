"""MCP 配置的第三层：Web 管理层（P7-S6，``docs/design/16-p7-plugin-mcp.md`` §3.8）。

Web 扩展面板的改动写在 ``WATER_AGENT_MCP_WEB_LAYER`` 指向的 JSON 文件里，合并顺序是
仓库默认层 → 部署覆盖层 → Web 管理层（``config.load_layered_gateway_config``）。所有设了该变量的
进程（Web、CLI、评测 runner）读到同一份有效配置；不进数据库，不需要迁移。

- **只能写 ``server_id`` / ``disabled`` / ``allow``**（``config.WEB_LAYER_SERVER_KEYS``），外加每条的
  ``updated_at`` / ``updated_by`` 元数据；能 spawn 什么命令、带什么参数与环境变量名、在哪个目录、
  是否需要人工确认，只由运维的两层决定。
- **不能新增 server**：下层没有的 ``server_id`` 是孤立条目，加载时忽略、:func:`orphan_entries` 列出。
- **坏文件响亮失败**：损坏 / 写了不允许的键 → ``MCP_CONFIG_INVALID``，与覆盖层同语义；面板可以整层
  恢复（:meth:`WebLayerStore.reset`）。
- **写入**：持文件锁（``fcntl.flock``，只覆盖同一台主机）→ 比对 ``revision`` → 整份合并并完整校验
  （与加载同一套：模型校验 + 路径存在性）→ 同目录临时文件 + ``os.replace``。校验不过不落盘。

放行不是授权：被 ``allow`` 放行的工具照样过 schema 子集、描述安检、确认门、``MCP_INVOKE`` 与执行期
护栏。层文件里没有任何密钥值（凭据槽位的值在凭据卷，见 ``mcp/credentials.py``）。
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from water_agent.mcp.config import (
    _SERVER_ID_RE,
    MCP_CONFIG_VERSION,
    WEB_LAYER_SERVER_KEYS,
    MCPGatewayConfig,
    OperatorLayers,
    _check_paths_exist,
    _read_json_object,
    check_allow_items,
    merge_web_layer,
    missing_paths,
)
from water_agent.mcp.errors import MCP_CONFIG_INVALID, MCPError
from water_agent.skills.models import SKILL_NAME_RE as _SKILL_NAME_RE

#: 层文件顶层只允许这四个键。``confirmation_handler_ready`` 只能写在仓库默认层。
#: P7-S9：``skills`` 是管理员对 skill 库的启停（每条只有 ``name`` / ``disabled`` + 元数据），与 ``servers``
#: 同一份文件、同一套 revision / 文件锁 / 整份校验。
WEB_LAYER_TOP_LEVEL_KEYS = frozenset({"version", "revision", "servers", "skills"})
#: 每条 skill 能写的键（不能写正文、来源、目录或任何别的东西）。
WEB_LAYER_SKILL_KEYS = frozenset({"name", "disabled"})
#: 每条除可改字段外允许的元数据键（不参与合并）。
WEB_LAYER_METADATA_KEYS = frozenset({"updated_at", "updated_by"})
_OVERRIDE_FIELDS = ("disabled", "allow")

#: 字段来源（列表接口的 ``sources``）。
LAYER_REPO = "repo"
LAYER_OVERLAY = "overlay"
LAYER_WEB = "web"
LAYER_DEFAULT = "default"
#: 列表接口报告来源的字段。
SOURCE_FIELDS = ("command", "disabled", "allow", "human_confirmation", "lifecycle", "credential_env")


class WebLayerStoreUnavailable(RuntimeError):
    """层文件所在目录不可写（未挂卷、只读根文件系统）或未设 ``WATER_AGENT_MCP_WEB_LAYER``。"""


class WebLayerRevisionConflict(RuntimeError):
    """``expected_revision`` 与文件里的不符（别人先改了）。"""

    def __init__(self, current_revision: int) -> None:
        super().__init__(f"Web 管理层已被修改（当前 revision {current_revision}）")
        self.current_revision = current_revision


class WebLayerRejected(ValueError):
    """改动合并后的有效配置加载不了：不落盘。``reason`` 不含路径与请求值，可原样回给面板。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class WebLayerServer(BaseModel):
    """层文件里的一条：某台 server 的启停 / 白名单改动。没写的字段沿用下层。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    server_id: str
    disabled: StrictBool = False
    allow: tuple[str, ...] | None = None
    updated_at: str | None = None
    updated_by: str | None = None

    @field_validator("server_id")
    @classmethod
    def _validate_server_id(cls, value: str) -> str:
        if not _SERVER_ID_RE.match(value):
            raise ValueError(f"server_id 必须匹配 {_SERVER_ID_RE.pattern!r}")
        return value

    @field_validator("allow")
    @classmethod
    def _validate_allow(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        return check_allow_items(value)

    def overrides(self) -> dict[str, Any]:
        """参与合并的部分：``server_id`` + 文件里**写了的**可改字段（没写 = 沿用下层）。"""
        return {
            "server_id": self.server_id,
            **{field: getattr(self, field) for field in _OVERRIDE_FIELDS if field in self.model_fields_set},
        }

    def to_json(self) -> dict[str, Any]:
        data = self.overrides()
        if isinstance(data.get("allow"), tuple):
            data["allow"] = list(data["allow"])
        if self.updated_at is not None:
            data["updated_at"] = self.updated_at
        if self.updated_by is not None:
            data["updated_by"] = self.updated_by
        return data


class WebLayerSkill(BaseModel):
    """层文件里的一条 skill 启停（P7-S9）。只能停用 / 启用 skill 库里已有的 skill，不能新增或改写正文。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=2, max_length=48)
    disabled: StrictBool = False
    updated_at: str | None = None
    updated_by: str | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not _SKILL_NAME_RE.match(value):
            raise ValueError("skill name 必须是 kebab-case")
        return value

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name, "disabled": self.disabled}
        if self.updated_at is not None:
            data["updated_at"] = self.updated_at
        if self.updated_by is not None:
            data["updated_by"] = self.updated_by
        return data


class WebLayerDocument(BaseModel):
    """层文件。``revision`` 每次写入加一，文件不存在时为 0。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(default=MCP_CONFIG_VERSION, min_length=1)
    revision: StrictInt = Field(default=0, ge=0)
    servers: tuple[WebLayerServer, ...] = ()
    skills: tuple[WebLayerSkill, ...] = ()

    @model_validator(mode="after")
    def _unique_server_ids(self) -> WebLayerDocument:
        ids = [entry.server_id for entry in self.servers]
        duplicates = sorted({name for name in ids if ids.count(name) > 1})
        if duplicates:
            raise ValueError(f"server_id 重复：{duplicates}")
        names = [entry.name for entry in self.skills]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"skill name 重复：{duplicates}")
        return self

    def disabled_skills(self) -> tuple[str, ...]:
        """管理员停用的 skill 名（P7-S9）。"""
        return tuple(entry.name for entry in self.skills if entry.disabled)

    def skill_entry(self, name: str) -> WebLayerSkill | None:
        return next((item for item in self.skills if item.name == name), None)

    def entry(self, server_id: str) -> WebLayerServer | None:
        return next((item for item in self.servers if item.server_id == server_id), None)

    def overrides(self) -> list[dict[str, Any]]:
        return [entry.overrides() for entry in self.servers]

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "version": self.version,
            "revision": self.revision,
            "servers": [entry.to_json() for entry in self.servers],
        }
        # 没有 skill 启停时不写这个键：与 S6 写出的层文件逐字相同
        if self.skills:
            data["skills"] = [entry.to_json() for entry in self.skills]
        return data


def _check_keys(payload: Any) -> None:
    """先按键名给出可读的拒绝原因（只列键名，不回显值），再交给模型校验。"""
    if not isinstance(payload, dict):
        raise TypeError("顶层必须是对象")
    unknown = set(payload) - WEB_LAYER_TOP_LEVEL_KEYS
    if unknown:
        raise ValueError(
            f"顶层只能写 {sorted(WEB_LAYER_TOP_LEVEL_KEYS)}，不能写 {sorted(unknown)}"
            "（confirmation_handler_ready 只能写在仓库默认层）"
        )
    servers = payload.get("servers", [])
    if not isinstance(servers, list):
        raise TypeError("servers 必须是列表")
    skills = payload.get("skills", [])
    if not isinstance(skills, list):
        raise TypeError("skills 必须是列表")
    for item in skills:
        if not isinstance(item, dict):
            raise TypeError("skills 的每一项都必须是对象")
        unknown = set(item) - WEB_LAYER_SKILL_KEYS - WEB_LAYER_METADATA_KEYS
        if unknown:
            raise ValueError(
                f"每个 skill 只能写 {sorted(WEB_LAYER_SKILL_KEYS)}，不能写 {sorted(unknown)}："
                "skill 正文与来源只能由仓库评审或运维目录提供"
            )
    allowed = WEB_LAYER_SERVER_KEYS | WEB_LAYER_METADATA_KEYS
    for item in servers:
        if not isinstance(item, dict):
            raise TypeError("servers 的每一项都必须是对象")
        unknown = set(item) - allowed
        if unknown:
            raise ValueError(
                f"每台 server 只能写 {sorted(WEB_LAYER_SERVER_KEYS)}，不能写 {sorted(unknown)}："
                "命令、参数、环境变量、工作目录、生命周期、确认位与凭据槽位只能由运维在部署覆盖层修改"
            )


def load_web_layer(path: Path) -> WebLayerDocument:
    """读层文件。不存在 → 空层（revision 0）；存在但读不了 / 坏 / 写了不允许的键 → ``MCP_CONFIG_INVALID``。"""
    if not path.exists():
        return WebLayerDocument()
    if not path.is_file():
        raise MCPError(MCP_CONFIG_INVALID, f"Web 管理层 {path} 不是文件")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MCPError(MCP_CONFIG_INVALID, f"无法读取 Web 管理层 {path}：{exc}") from exc
    payload = _read_json_object(path, text)
    try:
        _check_keys(payload)
        return WebLayerDocument.model_validate(payload)
    except (TypeError, ValueError) as exc:
        raise MCPError(MCP_CONFIG_INVALID, f"Web 管理层 {path} 校验失败：{exc}") from exc


def disabled_skill_names() -> tuple[str, ...]:
    """Web 管理层里停用的 skill（P7-S9；会话与 runner 装配时读）。没设 ``WATER_AGENT_MCP_WEB_LAYER`` 或文件不存在
    时为空；文件损坏时与 MCP 同语义——``MCP_CONFIG_INVALID`` 响亮失败（不能把"停用"静默当成"启用"）。"""

    from water_agent.mcp.config import web_layer_path

    path = web_layer_path()
    if path is None:
        return ()
    return load_web_layer(path).disabled_skills()


def merged_config(
    layers: OperatorLayers, document: WebLayerDocument, *, web_path: Path | str
) -> MCPGatewayConfig:
    """运维两层 + Web 管理层，只做模型校验（不查文件系统）。运维两层必须已能通过模型校验。"""
    merged, _orphans = merge_web_layer(layers.payload() or {}, document.overrides())
    try:
        return MCPGatewayConfig.model_validate(merged)
    except ValueError as exc:
        raise MCPError(MCP_CONFIG_INVALID, f"Web 管理层 {web_path} 合并后校验失败：{exc}") from exc


def effective_config(
    layers: OperatorLayers, document: WebLayerDocument, *, web_path: Path | str
) -> MCPGatewayConfig | None:
    """有效配置：运维两层先过模型校验（报错指向运维的文件），再叠 Web 管理层，最后在合并结果上查
    路径存在性。运维两层都没有 → ``None``（功能关闭；Web 管理层不能凭空造出 server）。"""
    if layers.validate() is None:
        return None
    config = merged_config(layers, document, web_path=web_path)
    _check_paths_exist(config, f"{layers.source}（叠加 Web 管理层 {web_path}）")
    return config


def orphan_entries(layers: OperatorLayers, document: WebLayerDocument) -> tuple[WebLayerServer, ...]:
    """层文件里指向下层不存在的 server 的条目（加载时被忽略）。"""
    payload = layers.payload() if layers.configured else None
    base = payload if isinstance(payload, dict) else {}
    _merged, orphan_ids = merge_web_layer(base, document.overrides())
    return tuple(entry for entry in document.servers if entry.server_id in set(orphan_ids))


def check_candidate(
    layers: OperatorLayers, document: WebLayerDocument, *, web_path: Path | str
) -> MCPGatewayConfig:
    """写入前整份校验，与加载同一套（模型校验 + 路径存在性）。不通过抛 :class:`WebLayerRejected`，
    原因不含路径与请求值。"""
    try:
        operator = layers.validate()
    except MCPError as exc:
        raise WebLayerRejected(
            "部署配置（仓库默认层 / 部署覆盖层）本身无法加载，需运维先修复"
        ) from exc
    if operator is None:
        raise WebLayerRejected("部署配置里没有任何 MCP server，Web 管理层无可改动")
    try:
        config = merged_config(layers, document, web_path=web_path)
    except MCPError as exc:
        raise WebLayerRejected("改动合并后的配置未通过校验") from exc
    missing = missing_paths(config)
    if missing:
        server_id, field = missing[0]
        raise WebLayerRejected(
            f"server {server_id!r} 启用后无法加载：部署配置里的 {field} 不存在，需运维先修复部署覆盖层"
        )
    return config


def field_sources(
    layers: OperatorLayers, document: WebLayerDocument | None, server_id: str
) -> dict[str, str]:
    """每个字段来自哪一层：``web`` / ``overlay`` / ``repo`` / ``default``（字段缺省值）。"""

    def servers_of(payload: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("servers"), list):
            return {}
        return {
            item["server_id"]: item
            for item in payload["servers"]
            if isinstance(item, dict) and isinstance(item.get("server_id"), str)
        }

    base = servers_of(layers.base).get(server_id, {})
    overlay = servers_of(layers.overlay).get(server_id, {})
    entry = document.entry(server_id) if document is not None else None
    web = entry.overrides() if entry is not None else {}
    sources: dict[str, str] = {}
    for field in SOURCE_FIELDS:
        if field in web:
            sources[field] = LAYER_WEB
        elif field in overlay:
            sources[field] = LAYER_OVERLAY
        elif field in base:
            sources[field] = LAYER_REPO
        else:
            sources[field] = LAYER_DEFAULT
    return sources


_UNSET: Any = object()


def with_override(
    document: WebLayerDocument,
    server_id: str,
    *,
    user_id: str,
    disabled: Any = _UNSET,
    allow: Any = _UNSET,
    now: datetime | None = None,
) -> WebLayerDocument:
    """给一台 server 写启停 / 白名单。没传的字段保持层文件里原来的样子（原来没写就继续沿用下层）。

    ``allow=None`` 是"全拒"（与覆盖层同规则），不是"去掉改动"——去掉改动用 :func:`without_override`。
    """
    existing = document.entry(server_id)
    fields = {key: value for key, value in (existing.overrides() if existing else {}).items()}
    fields.pop("server_id", None)
    if disabled is not _UNSET:
        fields["disabled"] = disabled
    if allow is not _UNSET:
        fields["allow"] = None if allow is None else tuple(allow)
    stamp = (now or datetime.now(UTC)).isoformat()
    entry = WebLayerServer(server_id=server_id, updated_at=stamp, updated_by=user_id, **fields)
    servers = list(document.servers)
    position = next((i for i, item in enumerate(servers) if item.server_id == server_id), None)
    if position is None:
        servers.append(entry)
    else:
        servers[position] = entry
    return WebLayerDocument(
        version=MCP_CONFIG_VERSION, revision=document.revision, servers=tuple(servers), skills=document.skills
    )


def without_override(document: WebLayerDocument, server_id: str) -> WebLayerDocument:
    return WebLayerDocument(
        version=MCP_CONFIG_VERSION,
        revision=document.revision,
        servers=tuple(item for item in document.servers if item.server_id != server_id),
        skills=document.skills,
    )


def with_skill_toggle(
    document: WebLayerDocument,
    name: str,
    *,
    user_id: str,
    disabled: bool,
    now: datetime | None = None,
) -> WebLayerDocument:
    """停用 / 启用一个 skill（P7-S9）。``servers`` 原样保留。启用即删掉这条（回到缺省"启用"），
    层文件里只留停用记录。"""

    stamp = (now or datetime.now(UTC)).isoformat()
    others = tuple(item for item in document.skills if item.name != name)
    skills = (
        (*others, WebLayerSkill(name=name, disabled=True, updated_at=stamp, updated_by=user_id))
        if disabled
        else others
    )
    return WebLayerDocument(
        version=MCP_CONFIG_VERSION,
        revision=document.revision,
        servers=document.servers,
        skills=tuple(sorted(skills, key=lambda item: item.name)),
    )


@dataclass
class WebLayerStore:
    """层文件的读写。写入串行化靠同目录的 ``<文件名>.lock``（``fcntl.flock``，只覆盖同一台主机）。"""

    path: Path

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.lock")

    def read(self) -> WebLayerDocument:
        return load_web_layer(self.path)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as exc:
            raise WebLayerStoreUnavailable(str(self.path.parent)) from exc
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def update(
        self,
        change: Callable[[WebLayerDocument], WebLayerDocument],
        *,
        expected_revision: int,
        validate: Callable[[WebLayerDocument], object],
    ) -> WebLayerDocument:
        """锁内：读当前层 → 比对 revision → 应用改动 → ``validate``（抛错即不落盘）→ 原子替换。

        当前层文件损坏时 ``MCPError(MCP_CONFIG_INVALID)`` 原样上抛（不能在坏文件上增量修改，先整层恢复）。
        """
        with self._locked():
            current = load_web_layer(self.path)
            if current.revision != expected_revision:
                raise WebLayerRevisionConflict(current.revision)
            changed = change(current)
            candidate = WebLayerDocument(
                version=MCP_CONFIG_VERSION,
                revision=current.revision + 1,
                servers=changed.servers,
                skills=changed.skills,
            )
            validate(candidate)
            self._write(candidate)
            return candidate

    def reset(self) -> bool:
        """删除整个层文件（恢复为部署配置）。文件损坏时同样可用。返回此前是否存在。"""
        with self._locked():
            try:
                existed = self.path.exists()
                self.path.unlink(missing_ok=True)
            except OSError as exc:
                raise WebLayerStoreUnavailable(str(self.path.parent)) from exc
            return existed

    def _write(self, document: WebLayerDocument) -> None:
        text = json.dumps(document.to_json(), ensure_ascii=False, indent=2) + "\n"
        temporary: str | None = None
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            # 层文件里没有密钥：0644 与仓库其余配置文件一致
            os.chmod(temporary, 0o644)
            os.replace(temporary, self.path)
            temporary = None
        except OSError as exc:
            raise WebLayerStoreUnavailable(str(self.path.parent)) from exc
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)


__all__ = [
    "LAYER_DEFAULT",
    "LAYER_OVERLAY",
    "LAYER_REPO",
    "LAYER_WEB",
    "SOURCE_FIELDS",
    "WEB_LAYER_METADATA_KEYS",
    "WEB_LAYER_SKILL_KEYS",
    "WEB_LAYER_TOP_LEVEL_KEYS",
    "WebLayerDocument",
    "WebLayerRejected",
    "WebLayerRevisionConflict",
    "WebLayerServer",
    "WebLayerSkill",
    "WebLayerStore",
    "WebLayerStoreUnavailable",
    "check_candidate",
    "disabled_skill_names",
    "effective_config",
    "field_sources",
    "load_web_layer",
    "merged_config",
    "orphan_entries",
    "with_override",
    "with_skill_toggle",
    "without_override",
]
