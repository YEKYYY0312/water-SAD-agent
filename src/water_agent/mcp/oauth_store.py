"""MCP OAuth 令牌与客户端注册的保管（P7-S8，``docs/design/16-p7-plugin-mcp.md`` §3.10）。

与 S6 的凭据槽位同一纪律：存**凭据卷** ``<credentials_dir>/mcp/<server_id>/``（目录 0700、文件 0600、原子替换），
卷刻意不在 ``/var/lib/water-agent`` 之下，**不进备份**；**只写不读**——没有任何接口把令牌原文交给调用方，只有
OAuth 模块自己读它来填 ``Authorization`` 头。路径只由已校验的 ``server_id`` 拼出。

两个文件：

- ``oauth.json``：访问令牌、refresh token、过期时间、scope、签发它的 issuer 与它绑定的规范 resource URI，
  以及签发时的端点快照（刷新 / 撤销要用）。``authorization_id`` 在**每次授权**时新生成、刷新不变——连接池的指纹用
  它的摘要，所以"刷新不换指纹，重新授权 / 清除换指纹"；
- ``oauth_client.json``：动态注册得到的 ``client_id``，**按 issuer 存**：issuer 变了（或回调地址变了）就作废、
  重新注册，不复用别家授权服务器给的 client_id。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from water_agent.config.credentials import credentials_dir
from water_agent.mcp.credentials import _SERVER_SEGMENT_RE, MCP_CREDENTIALS_SUBDIR

TOKEN_FILE = "oauth.json"
CLIENT_FILE = "oauth_client.json"
FORMAT_VERSION = 1

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def store_lock(server_id: str) -> threading.RLock:
    """同一台 server 的读-改-写互斥（进程内）。刷新与重新授权不会交错写文件。"""
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(server_id, threading.RLock())


@dataclass(frozen=True)
class StoredToken:
    """一份服务级授权。**不要把它放进日志 / trace / 响应**——``__repr__`` 已隐去令牌字段。"""

    authorization_id: str
    issuer: str
    resource: str
    client_id: str
    client_auth: str
    token_endpoint: str
    revocation_endpoint: str | None
    access_token: str = field(repr=False)
    refresh_token: str | None = field(repr=False)
    issued_at: float
    expires_at: float | None
    scope: str | None

    def authorization_digest(self) -> str:
        """授权身份的摘要（连接池指纹用）：不含任何令牌值，刷新前后相同。"""
        raw = json.dumps(
            [self.authorization_id, self.issuer, self.resource, self.client_id, self.scope or ""],
            ensure_ascii=False,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def lifetime(self) -> float | None:
        return None if self.expires_at is None else max(self.expires_at - self.issued_at, 0.0)

    def seconds_left(self, now: float) -> float | None:
        return None if self.expires_at is None else self.expires_at - now

    def with_tokens(
        self, *, access_token: str, refresh_token: str | None, issued_at: float, expires_at: float | None,
        scope: str | None,
    ) -> StoredToken:
        """刷新之后的新令牌（同一份授权：``authorization_id`` 不变；refresh token 被轮换就换成新的）。"""
        data = asdict(self)
        data.update(
            access_token=access_token,
            refresh_token=refresh_token if refresh_token else self.refresh_token,
            issued_at=issued_at,
            expires_at=expires_at,
            scope=scope if scope else self.scope,
        )
        return StoredToken(**data)


@dataclass(frozen=True)
class StoredClient:
    issuer: str
    client_id: str
    redirect_uri: str


class OAuthStore:
    """一台 server 的令牌 / 注册信息文件。"""

    def __init__(self, server_id: str) -> None:
        self.server_id = server_id

    # ------------------------------------------------------------------ 文件

    def _dir(self) -> Path:
        return credentials_dir() / MCP_CREDENTIALS_SUBDIR / _checked(self.server_id)

    def _path(self, name: str) -> Path:
        return self._dir() / name

    def _write(self, name: str, payload: dict[str, Any]) -> None:
        directory = self._dir()
        for parent in (directory.parent, directory):
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = directory / name
        temporary = target.with_name(f".{name}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(descriptor, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        finally:
            os.close(descriptor)
        os.replace(temporary, target)

    def _read(self, name: str) -> dict[str, Any] | None:
        try:
            raw = self._path(name).read_text(encoding="utf-8")
            parsed = json.loads(raw)
        except (OSError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) and parsed.get("version") == FORMAT_VERSION else None

    def _remove(self, name: str) -> None:
        with contextlib.suppress(OSError):
            self._path(name).unlink(missing_ok=True)

    # ------------------------------------------------------------------ 令牌

    def load(self) -> StoredToken | None:
        data = self._read(TOKEN_FILE)
        if data is None:
            return None
        try:
            return StoredToken(
                authorization_id=str(data["authorization_id"]),
                issuer=str(data["issuer"]),
                resource=str(data["resource"]),
                client_id=str(data["client_id"]),
                client_auth=str(data.get("client_auth", "none")),
                token_endpoint=str(data["token_endpoint"]),
                revocation_endpoint=data.get("revocation_endpoint"),
                access_token=str(data["access_token"]),
                refresh_token=data.get("refresh_token"),
                issued_at=float(data["issued_at"]),
                expires_at=None if data.get("expires_at") is None else float(data["expires_at"]),
                scope=data.get("scope"),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def save(self, token: StoredToken) -> None:
        with store_lock(self.server_id):
            self._write(TOKEN_FILE, {"version": FORMAT_VERSION, **asdict(token)})

    def clear(self) -> None:
        with store_lock(self.server_id):
            self._remove(TOKEN_FILE)

    def authorization_digest(self) -> str:
        """连接池指纹用：没有授权为 ``"none"``；有则是授权身份摘要（不含令牌值）。"""
        token = self.load()
        return "none" if token is None else token.authorization_digest()

    # ------------------------------------------------------------------ 动态注册

    def load_client(self, issuer: str, redirect_uri: str) -> StoredClient | None:
        """按 issuer 取注册信息；issuer 或回调地址对不上就当没有（作废，不复用）。"""
        data = self._read(CLIENT_FILE)
        if data is None or data.get("issuer") != issuer or data.get("redirect_uri") != redirect_uri:
            return None
        client_id = data.get("client_id")
        return StoredClient(issuer, str(client_id), redirect_uri) if isinstance(client_id, str) and client_id else None

    def save_client(self, client: StoredClient) -> None:
        with store_lock(self.server_id):
            self._write(CLIENT_FILE, {"version": FORMAT_VERSION, **asdict(client)})

    def clear_client(self) -> None:
        with store_lock(self.server_id):
            self._remove(CLIENT_FILE)


def _checked(server_id: str) -> str:
    if not _SERVER_SEGMENT_RE.match(server_id):
        raise ValueError("server_id 形状不合法")
    return server_id


__all__ = [
    "CLIENT_FILE",
    "TOKEN_FILE",
    "OAuthStore",
    "StoredClient",
    "StoredToken",
    "store_lock",
]
