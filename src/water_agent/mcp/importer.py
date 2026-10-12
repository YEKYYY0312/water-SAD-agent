"""导入 ``mcpServers`` 配置的无状态预览（P7-S6，``docs/design/16-p7-plugin-mcp.md`` §3.8）。

接受 Claude Code / Cursor 的 JSON（``{"mcpServers": {...}}``）与 Codex 的 TOML（``[mcp_servers.<name>]``，
标准库 ``tomllib``），产出**部署覆盖层格式**的提案文本，由运维离线审阅后贴进覆盖层。本模块
**不落盘、不 spawn、不改任何配置**：面板不能写 ``command`` / ``args``，导入也就只能生成待审提案。

提案纪律：

- 每台 ``disabled: true``，**不写 ``allow``**（即全拒）——运维贴进去之后什么都不会自动运行；
- ``env`` 的**值一律丢弃**、键变成 ``credential_env`` 槽位（导入文件里的 ``env`` 常含真实密钥，留在提案
  里就等于把密钥写进运维会到处粘贴的文本）；不能当槽位的键（``NODE_OPTIONS`` 这类）只列出名字、不进提案；
- ``url`` / ``type: http`` 条目（Claude Code / Cursor / Codex 的远程 server）生成 ``transport: streamable_http`` 的禁用提案（P7-S8）：
  请求头 / 令牌**一律丢弃**（只在备注里列头的名字，形似 ``Authorization`` 的提示改用 ``bearer_credential`` 槽位或 ``oauth``），
  URL 的查询串丢弃（常含 ``api_key=`` 之类）；旧的 HTTP+SSE（``type: sse``）与其他传输仍标为不支持；
- 名字归一成合法 ``server_id``，与现有 id 或同批条目冲突时改名并标出（同名 id 贴进覆盖层会改写那台
  server 的配置）；
- 逐条用 ``MCPServerConfig`` 做模型层校验（不查文件系统），不合格的逐条给原因。
"""
from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from pydantic import ValidationError

from water_agent.mcp.config import _SERVER_ID_RE, MCP_CONFIG_VERSION, MCPServerConfig
from water_agent.mcp.credentials import validate_slot_name

#: 输入上限（UTF-8 字节）。
MAX_IMPORT_BYTES = 64 * 1024
#: 一次最多导入多少台。
MAX_IMPORT_SERVERS = 64

STATUS_PROPOSED = "proposed"
STATUS_UNSUPPORTED = "unsupported"
STATUS_INVALID = "invalid"

FORMAT_JSON = "mcpServers_json"
FORMAT_TOML = "codex_toml"

_ID_JUNK_RE = re.compile(r"[^a-z0-9_]+")
#: 能照搬进提案的字段（其余字段忽略并在 notes 里列出键名）。
_COPIED_KEYS = frozenset({"command", "args", "cwd"})
#: 远程 server 条目里可能出现的请求头 / 令牌字段（值一律丢弃，只在备注里列名字）。
_HEADER_KEYS = ("headers", "http_headers", "httpHeaders", "env_http_headers")
_TOKEN_KEYS = ("bearer_token_env_var", "bearerTokenEnvVar", "bearer_token")
_KNOWN_KEYS = _COPIED_KEYS | frozenset(
    {"env", "type", "transport", "url", "name", "enabled", "disabled", *_HEADER_KEYS, *_TOKEN_KEYS}
)
#: 声明为 Streamable HTTP 的 ``type`` 取值（各客户端写法不一）。
_HTTP_TYPES = frozenset({"http", "streamable-http", "streamable_http", "streamablehttp", "streamableHttp"})


class ImportRejected(ValueError):
    """整份输入不能处理（过大、不是 JSON / TOML、没有 server 段）。``reason`` 可原样回给面板。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ImportEntry:
    """一条导入结果。``name`` 是导入文件里的名字（运维自己贴进来的文本）。"""

    name: str
    status: str
    server_id: str | None = None
    credential_env: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "server_id": self.server_id,
            "credential_env": list(self.credential_env),
            "notes": list(self.notes),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class ImportPreview:
    """预览结果：提案文本（覆盖层格式，可直接复制 / 下载）+ 逐条结果。"""

    source_format: str
    proposal: dict[str, Any]
    entries: tuple[ImportEntry, ...] = field(default_factory=tuple)

    @property
    def proposal_text(self) -> str:
        return json.dumps(self.proposal, ensure_ascii=False, indent=2) + "\n"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_format": self.source_format,
            "proposal_text": self.proposal_text,
            "proposed_count": sum(1 for entry in self.entries if entry.status == STATUS_PROPOSED),
            "entries": [entry.to_dict() for entry in self.entries],
        }


def _parse(text: str) -> tuple[str, dict[str, Any]]:
    """自动识别 JSON / TOML，返回 (格式, {名字: 条目})。"""
    stripped = text.lstrip("\ufeff").strip()
    if not stripped:
        raise ImportRejected("内容为空")
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ImportRejected(f"不是合法 JSON（行 {exc.lineno} 列 {exc.colno}）") from exc
        servers = payload.get("mcpServers") if isinstance(payload, dict) else None
        if not isinstance(servers, dict):
            raise ImportRejected('JSON 里没有 "mcpServers" 对象（Claude Code / Cursor 的格式）')
        return FORMAT_JSON, servers
    try:
        payload = tomllib.loads(stripped)
    except tomllib.TOMLDecodeError as exc:
        raise ImportRejected("既不是 JSON 也不是合法 TOML") from exc
    servers = payload.get("mcp_servers")
    if not isinstance(servers, dict):
        raise ImportRejected("TOML 里没有 [mcp_servers.<name>] 段（Codex 的格式）")
    return FORMAT_TOML, servers


def normalize_server_id(name: str) -> str | None:
    """把导入文件里的名字归一成合法 ``server_id``；归一不出来返回 ``None``。"""
    candidate = _ID_JUNK_RE.sub("_", name.strip().lower()).strip("_")
    candidate = re.sub(r"_+", "_", candidate)
    candidate = candidate.lstrip("0123456789_")[:32].rstrip("_")
    return candidate if _SERVER_ID_RE.match(candidate) else None


def _unique_id(base: str, taken: set[str]) -> str:
    for suffix in range(2, 1000):
        tail = f"_{suffix}"
        candidate = f"{base[: 32 - len(tail)]}{tail}"
        if candidate not in taken:
            return candidate
    raise ImportRejected("server_id 冲突过多")


def _reasons(error: ValidationError) -> tuple[str, ...]:
    """模型校验失败的原因：字段名 + pydantic 的说明（不含 ``input_value``；env 的值从不进模型）。"""
    reasons = []
    for item in error.errors():
        location = ".".join(str(part) for part in item.get("loc", ())) or "配置"
        reasons.append(f"{location}：{item.get('msg', '不合法')}")
    return tuple(reasons)


def _remote_proposal(raw: dict[str, Any], proposal: dict[str, Any], notes: list[str], *, explicit_type: bool) -> None:
    """Streamable HTTP 条目：只带 transport 与（去掉查询串的）url；令牌、请求头的值一律丢弃。"""
    proposal["transport"] = "streamable_http"
    url = raw.get("url")
    if isinstance(url, str):
        try:
            parts = urlsplit(url)
        except ValueError:
            proposal["url"] = url  # 交给模型校验报原因
        else:
            if parts.query:
                names = sorted({key for key, _value in parse_qsl(parts.query, keep_blank_values=True)})
                notes.append(
                    f"URL 的查询串已丢弃（参数名：{', '.join(names) or '无'}；常含 api_key / token）："
                    "认证请改用 bearer_credential 槽位或 oauth"
                )
            proposal["url"] = urlunsplit(parts._replace(query=""))
    else:
        proposal["url"] = url
    if not explicit_type:
        notes.append("条目没有声明传输类型，按 Streamable HTTP 处理；若 server 实际是旧版 HTTP+SSE，连接会失败")
    header_names: list[str] = []
    for key in _HEADER_KEYS:
        headers = raw.get(key)
        if isinstance(headers, dict):
            header_names.extend(str(name) for name in headers)
    token_fields = [key for key in _TOKEN_KEYS if key in raw]
    if header_names or token_fields:
        shown = sorted({*header_names, *token_fields})
        notes.append(f"丢弃了请求头 / 令牌的值（名字：{', '.join(shown)}）")
    if any(name.lower() == "authorization" or "token" in name.lower() or "key" in name.lower() for name in header_names) \
            or token_fields:
        notes.append(
            "这个 server 需要认证：提案里没有任何令牌。请改用 bearer_credential 槽位"
            "（运维声明 credential_env，管理员在面板填值）或 oauth（服务级授权）"
        )
    if raw.get("env") is not None:
        notes.append("远程 server 没有子进程环境：env 已丢弃")


def _entry(
    name: str, raw: Any, *, taken: set[str]
) -> tuple[ImportEntry, dict[str, Any] | None]:
    if not isinstance(raw, dict):
        return ImportEntry(name, STATUS_INVALID, reasons=("条目不是对象",)), None

    transport = raw.get("type", raw.get("transport"))
    remote = "url" in raw and transport in (None, *_HTTP_TYPES)
    if transport in _HTTP_TYPES and "url" not in raw:
        return ImportEntry(name, STATUS_INVALID, reasons=("type 是 http，但条目没有 url",)), None
    if not remote and (transport not in (None, "stdio") or "url" in raw):
        which = "旧的 HTTP+SSE（type: sse）" if transport == "sse" else f"传输方式 {str(transport)[:30]!r}"
        return (
            ImportEntry(
                name,
                STATUS_UNSUPPORTED,
                reasons=(f"{which}不支持：只导入 stdio 与 Streamable HTTP（本网关不实现已废弃的 HTTP+SSE）",),
            ),
            None,
        )

    notes: list[str] = []
    server_id = normalize_server_id(name)
    if server_id is None:
        return (
            ImportEntry(
                name,
                STATUS_INVALID,
                reasons=(f"名字归一不出合法 server_id（须匹配 {_SERVER_ID_RE.pattern}）",),
            ),
            None,
        )
    if server_id != name:
        notes.append(f"server_id 由导入文件里的名字归一为 {server_id!r}")
    if server_id in taken:
        renamed = _unique_id(server_id, taken)
        notes.append(
            f"server_id {server_id!r} 与现有 server 或同批条目冲突，改名为 {renamed!r}"
            "（同名 id 贴进覆盖层会改写那台 server 的配置）"
        )
        server_id = renamed

    proposal: dict[str, Any] = {"server_id": server_id}
    if remote:
        _remote_proposal(raw, proposal, notes, explicit_type=transport is not None)
    else:
        for key in ("command", "args", "cwd"):
            if key in raw:
                proposal[key] = raw[key]
        if "args" in proposal:
            notes.append("args 原样保留：请检查里面有没有写进去的令牌")

    slots: list[str] = []
    env = None if remote else raw.get("env")
    if env is not None:
        if not isinstance(env, dict):
            notes.append("env 不是对象，已丢弃")
        else:
            rejected: list[str] = []
            for key in env:
                try:
                    validate_slot_name(str(key))
                except ValueError:
                    rejected.append(str(key))
                    continue
                slots.append(str(key))
            if env:
                notes.append(f"丢弃了 {len(env)} 个环境变量的值；能当凭据槽位的键改成了 credential_env")
            if rejected:
                notes.append(
                    f"这些变量不能作为凭据槽位，已从提案中去掉：{', '.join(sorted(rejected))}"
                    "（如确需设置，由运维改写成内联 env 或 env_file）"
                )
    if slots:
        proposal["credential_env"] = slots

    ignored = sorted(str(key) for key in raw if key not in _KNOWN_KEYS)
    if ignored:
        notes.append(f"忽略了不支持的字段：{', '.join(ignored)}")
    proposal["disabled"] = True

    try:
        MCPServerConfig.model_validate(proposal)
    except ValidationError as exc:
        return ImportEntry(name, STATUS_INVALID, server_id=server_id, reasons=_reasons(exc)), None
    except (TypeError, ValueError) as exc:  # 归一化阶段的类型错误
        return ImportEntry(name, STATUS_INVALID, server_id=server_id, reasons=(str(exc),)), None

    taken.add(server_id)
    entry = ImportEntry(
        name,
        STATUS_PROPOSED,
        server_id=server_id,
        credential_env=tuple(slots),
        notes=tuple(notes),
    )
    return entry, proposal


def preview_import(text: str, *, existing_ids: Iterable[str] = ()) -> ImportPreview:
    """把一份 ``mcpServers`` 配置转换成覆盖层格式的待审提案。无副作用。"""
    if len(text.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise ImportRejected(f"内容超过 {MAX_IMPORT_BYTES // 1024} KiB 上限")
    source_format, servers = _parse(text)
    if not servers:
        raise ImportRejected("没有任何 server 条目")
    if len(servers) > MAX_IMPORT_SERVERS:
        raise ImportRejected(f"一次最多导入 {MAX_IMPORT_SERVERS} 台 server")

    taken = set(existing_ids)
    entries: list[ImportEntry] = []
    proposals: list[dict[str, Any]] = []
    for name, raw in servers.items():
        entry, proposal = _entry(str(name), raw, taken=taken)
        entries.append(entry)
        if proposal is not None:
            proposals.append(proposal)
    return ImportPreview(
        source_format=source_format,
        proposal={"version": MCP_CONFIG_VERSION, "servers": proposals},
        entries=tuple(entries),
    )


__all__ = [
    "FORMAT_JSON",
    "FORMAT_TOML",
    "MAX_IMPORT_BYTES",
    "MAX_IMPORT_SERVERS",
    "STATUS_INVALID",
    "STATUS_PROPOSED",
    "STATUS_UNSUPPORTED",
    "ImportEntry",
    "ImportPreview",
    "ImportRejected",
    "normalize_server_id",
    "preview_import",
]
