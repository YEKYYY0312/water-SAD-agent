"""MCP 长驻连接池（P7-S5，``docs/design/16-p7-plugin-mcp.md`` §3.4）。

只有 Web 用它：``RunManager`` 的会话工厂每个 run 调一次 :meth:`McpConnectionPool.lease`，run 结束
:meth:`McpLease.release`。CLI 与评测 runner 不用池，始终现开现关（``McpGatewayPlugin.owning``）。

- ``lifecycle="per_run"``（缺省）的 server：与此前一样，每个租约现开、``release`` 时关闭；
- ``lifecycle="shared"`` 的 server：池里长驻、被所有 run 复用。**声明 shared 即声明这台 server
  不持有按用户区分的状态**——共享的是同一个进程，进程里的状态对所有用户可见（有测试如实演示）。

复用的是进程，不是授权：每次调用仍经 ``ToolRegistry.invoke``（主体 ``MCP_INVOKE``、参数护栏、
执行期确认门）。同一连接上的请求由 ``MCPStdioClient`` 串行。

**重载**按单台 server 的指纹判断：指纹 = 该 server 的已校验配置 + 网关确认位 + 解析后子进程 env
的摘要（只用于比较，不落日志、不进 trace）。指纹变了、进程死了、配置里删掉或停用了，旧连接
退役：没人借用时立即关闭，否则等最后一个借用它的租约释放后关闭。调用中途崩溃不重试（远端
操作可能不幂等），结构化错误照常回给模型，下一次租约重开。
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from typing import Any

from water_agent.mcp.bridge import MCPServerSession, open_bridge
from water_agent.mcp.config import MCPGatewayConfig, MCPServerConfig, child_env
from water_agent.mcp.wiring import (
    MCPGatewayAssembly,
    SkippedServer,
    compute_manifest_hash,
    pre_connection_skips,
    session_failed,
    session_skips,
)

CONNECTION_PER_RUN = "per_run"
CONNECTION_SHARED_NEW = "shared_new"
CONNECTION_SHARED_REUSED = "shared_reused"


def server_fingerprint(server: MCPServerConfig, gateway: MCPGatewayConfig) -> str:
    """单台 server 的指纹。env 的值只进摘要，摘要本身也不对外暴露。"""

    env = child_env(server)
    env_digest = hashlib.sha256(
        json.dumps(sorted(env.items()), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    payload = {
        "server": server.model_dump(mode="json"),
        "confirmation_handler_ready": gateway.confirmation_handler_ready,
        "env": env_digest,
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(slots=True, eq=False)
class _SharedEntry:
    server_id: str
    fingerprint: str
    session: MCPServerSession
    refs: int = 0
    retired: bool = False

    @property
    def alive(self) -> bool:
        client = self.session.client
        return client is not None and client.alive


@dataclass(slots=True)
class McpLease:
    """一个 run 对 MCP 连接的借用。``release`` 幂等；per_run 的连接在这里关闭。"""

    assembly: MCPGatewayAssembly
    _pool: McpConnectionPool | None = None
    _owned: list[MCPServerSession] = field(default_factory=list)
    _borrowed: list[_SharedEntry] = field(default_factory=list)
    _released: bool = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        for session in self._owned:
            session.close()
        if self._pool is not None:
            self._pool._release(self)


class McpConnectionPool:
    """进程内的 shared server 连接。线程安全；开进程在池锁之外进行。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, _SharedEntry] = {}
        self._retired: list[_SharedEntry] = []
        self._opening: dict[str, threading.Event] = {}
        self._leases: set[int] = set()
        self._closed = False

    # ------------------------------------------------------------------ 借用

    def lease(self, config: MCPGatewayConfig | None) -> McpLease:
        """为一个 run 准备 MCP 装配。配置是调用方用 ``load_mcp_gateway`` 加载好的（坏配置在
        那里就已响亮失败）；``None`` 表示功能关闭，同时让池里全部 shared 连接退役。"""

        with self._lock:
            if self._closed:
                raise RuntimeError("MCP 连接池已关闭，不能再借用")
        shared_ids = {
            server.server_id
            for server in (config.servers if config is not None else ())
            if server.lifecycle == "shared" and not server.disabled
        }
        if config is not None:
            shared_ids -= {skip.server_id for skip in pre_connection_skips(config)}
        self._retire_missing(shared_ids)
        lease = McpLease(
            assembly=MCPGatewayAssembly(sessions=(), skipped_servers=(), manifest_hash=""),
            _pool=self,
        )
        with self._lock:
            self._leases.add(id(lease))
        if config is None:
            return lease

        pre_skips = pre_connection_skips(config)
        blocked = {skip.server_id for skip in pre_skips}
        sessions: list[MCPServerSession] = []
        skipped: list[SkippedServer] = list(pre_skips)
        connections: dict[str, str] = {}
        try:
            for server in config.servers:
                if server.server_id in blocked:
                    continue
                if server.lifecycle == "shared":
                    entry, kind, failed = self._acquire_shared(server, config)
                    if entry is None:
                        assert failed is not None
                        skipped.extend(session_skips(failed))
                        continue
                    lease._borrowed.append(entry)
                    sessions.append(entry.session)
                    connections[server.server_id] = kind
                    continue
                session = open_bridge(server, config)
                if session_failed(session):
                    skipped.extend(session_skips(session))
                    continue
                lease._owned.append(session)
                sessions.append(session)
                connections[server.server_id] = CONNECTION_PER_RUN
        except BaseException:
            lease.release()
            raise
        lease.assembly = MCPGatewayAssembly(
            sessions=tuple(sessions),
            skipped_servers=tuple(skipped),
            manifest_hash=compute_manifest_hash(config),
            raw_config=config,
            connections=connections,
        )
        return lease

    def _acquire_shared(
        self, server: MCPServerConfig, gateway: MCPGatewayConfig
    ) -> tuple[_SharedEntry | None, str, MCPServerSession | None]:
        """借一台 shared server：指纹相同且活着就复用，否则开新的（同一台只有一个线程去开）。"""

        fingerprint = server_fingerprint(server, gateway)
        while True:
            to_close: list[_SharedEntry] = []
            with self._lock:
                if self._closed:
                    raise RuntimeError("MCP 连接池已关闭，不能再借用")
                entry = self._entries.get(server.server_id)
                if entry is not None and entry.fingerprint == fingerprint and entry.alive:
                    entry.refs += 1
                    return entry, CONNECTION_SHARED_REUSED, None
                if entry is not None:
                    del self._entries[server.server_id]
                    to_close.extend(self._retire_locked(entry))
                waiting = self._opening.get(server.server_id)
                if waiting is None:
                    opening = threading.Event()
                    self._opening[server.server_id] = opening
            self._close_entries(to_close)
            if waiting is not None:
                waiting.wait(timeout=server.startup_timeout_seconds + server.call_timeout_seconds)
                continue
            try:
                session = open_bridge(server, gateway)
            except BaseException:
                with self._lock:
                    self._opening.pop(server.server_id, None)
                opening.set()
                raise
            with self._lock:
                self._opening.pop(server.server_id, None)
                if session_failed(session):
                    opening.set()
                    return None, "", session
                if self._closed:
                    opening.set()
                    session.close()
                    raise RuntimeError("MCP 连接池已关闭，不能再借用")
                entry = _SharedEntry(server.server_id, fingerprint, session, refs=1)
                self._entries[server.server_id] = entry
            opening.set()
            return entry, CONNECTION_SHARED_NEW, None

    # ------------------------------------------------------------------ 退役与释放

    def _retire_locked(self, entry: _SharedEntry) -> list[_SharedEntry]:
        """标记退役；没人借用就返回给调用方在锁外关闭。"""
        entry.retired = True
        if entry.refs <= 0:
            return [entry]
        self._retired.append(entry)
        return []

    def _retire_missing(self, keep: set[str]) -> None:
        to_close: list[_SharedEntry] = []
        with self._lock:
            for server_id in [sid for sid in self._entries if sid not in keep]:
                to_close.extend(self._retire_locked(self._entries.pop(server_id)))
        self._close_entries(to_close)

    def _release(self, lease: McpLease) -> None:
        to_close: list[_SharedEntry] = []
        with self._lock:
            self._leases.discard(id(lease))
            for entry in lease._borrowed:
                entry.refs -= 1
                if entry.retired and entry.refs <= 0 and entry in self._retired:
                    self._retired.remove(entry)
                    to_close.append(entry)
        self._close_entries(to_close)

    @staticmethod
    def _close_entries(entries: list[_SharedEntry]) -> None:
        for entry in entries:
            entry.session.close()

    # ------------------------------------------------------------------ 关闭

    def close(self) -> dict[str, Any]:
        """关闭全部 shared 连接（Web 应用关闭时，在排空 run 之后调用）。之后 ``lease`` 抛错。

        仍有未释放的租约时照样立即终止进程，并在返回值里报告数量——不静默。
        """
        with self._lock:
            self._closed = True
            entries = [*self._entries.values(), *self._retired]
            self._entries.clear()
            self._retired.clear()
            outstanding = len(self._leases)
        self._close_entries(entries)
        return {"closed_servers": len(entries), "outstanding_leases": outstanding}

    def snapshot(self) -> dict[str, Any]:
        """池的当前状态（测试与运维观察用）：只有 server id、引用计数与是否存活，不含指纹。"""
        with self._lock:
            return {
                "active": {
                    sid: {"refs": entry.refs, "alive": entry.alive}
                    for sid, entry in self._entries.items()
                },
                "retired": len(self._retired),
                "leases": len(self._leases),
                "closed": self._closed,
            }


__all__ = [
    "CONNECTION_PER_RUN",
    "CONNECTION_SHARED_NEW",
    "CONNECTION_SHARED_REUSED",
    "McpConnectionPool",
    "McpLease",
    "server_fingerprint",
]
