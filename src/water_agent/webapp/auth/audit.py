"""P5 Step 37: authentication audit event sink.

All audit events are written to structured logs (stdout JSON lines) and to an
in-memory list for test inspection.  Step 38 does not add an audit table;
durable audit retention remains a separate deployment concern.

Contract: audit entries NEVER contain passwords, password hashes, session
secrets, CSRF tokens, full usernames in failure paths, or API keys.
"""
from __future__ import annotations

import json
import logging
import sys
from collections import deque
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("water_agent.auth.audit")


def _ensure_stdout_handler() -> None:
    """让审计事件在 uvicorn 下真的落到 stdout（review 09-28 #6）。

    uvicorn 只给自己的 ``uvicorn.*`` logger 配 handler；``water_agent.*`` 没有
    handler 时走 Python 的 lastResort，它只输出 WARNING 及以上——于是所有
    ``logger.info`` 的审计事件在容器日志里**一条都没有**，而模块文档声称
    "written to structured logs (stdout JSON lines)"。

    自带一个只输出消息本身（已是一行 JSON）的 stdout handler，并关掉向根 logger
    的传播，避免部署方另配根 handler 时每条事件打两遍。只装一次（幂等）。
    """
    if any(getattr(handler, "_wa_audit", False) for handler in logger.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler._wa_audit = True  # type: ignore[attr-defined]
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


_ensure_stdout_handler()

#: 进程内缓冲：测试用它断言事件已记录。**有界**——此前是无界 list，生产进程里
#: 每次登录尝试都往里追加、从不清理，是一个随请求量线性增长的内存泄漏。
#: 持久审计仍是部署层的事（见模块文档）；这里只保留最近若干条供诊断。
_EVENT_BUFFER_LIMIT = 1000
_events: deque[dict[str, Any]] = deque(maxlen=_EVENT_BUFFER_LIMIT)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _emit(event_type: str, data: dict[str, Any]) -> None:
    entry: dict[str, Any] = {
        "event": event_type,
        "ts": _utc_now(),
        **data,
    }
    _events.append(entry)
    # Emit as a single-line JSON log record so log aggregators can parse it.
    logger.info(json.dumps(entry, ensure_ascii=False))


def audit_login_success(*, user_id: str, role: str) -> None:
    _emit("auth.login_success", {"user_id": user_id, "role": role})


def audit_login_failure(*, reason: str) -> None:
    """Record a failed login attempt.  reason must NOT contain the password or
    the full username — only a category such as 'invalid_credentials'."""
    _emit("auth.login_failure", {"reason": reason})


def audit_login_rate_limited(*, failure_count: int) -> None:
    """Record that a login attempt was blocked by the rate limiter.
    failure_count is the current window count; username is intentionally omitted."""
    _emit("auth.login_rate_limited", {"failure_count": failure_count})


def audit_logout(*, user_id: str) -> None:
    _emit("auth.logout", {"user_id": user_id})


def audit_password_changed(*, user_id: str, actor_user_id: str) -> None:
    _emit("auth.password_changed", {"user_id": user_id, "actor_user_id": actor_user_id})


def audit_user_created(*, new_user_id: str, role: str, actor_user_id: str) -> None:
    _emit("auth.user_created", {"new_user_id": new_user_id, "role": role, "actor_user_id": actor_user_id})


def audit_user_deactivated(*, target_user_id: str, actor_user_id: str) -> None:
    _emit("auth.user_deactivated", {"target_user_id": target_user_id, "actor_user_id": actor_user_id})


def audit_user_reactivated(*, target_user_id: str, actor_user_id: str) -> None:
    _emit("auth.user_reactivated", {"target_user_id": target_user_id, "actor_user_id": actor_user_id})


def audit_bootstrap_admin_created(*, user_id: str) -> None:
    _emit("auth.bootstrap_admin_created", {"user_id": user_id})


def audit_bootstrap_admin_skipped(*, reason: str = "active_admin_exists") -> None:
    """reason is a category ('active_admin_exists' / 'admin_exists_inactive'), never a username."""
    _emit("auth.bootstrap_admin_skipped", {"reason": reason})


# ---------------------------------------------------------------------------
# P7-S6: Web 扩展面板（MCP 管理）。只记谁、哪台 server、改了哪些字段名、allow 增减的
# 工具名；凭据只记槽位名与动作，**永不记值**。不新建审计表，与上面同一个出口。
# ---------------------------------------------------------------------------


def audit_mcp_server_updated(
    *,
    actor_user_id: str,
    server_id: str,
    fields: list[str],
    allow_added: list[str],
    allow_removed: list[str],
    revision: int,
) -> None:
    _emit(
        "mcp.server_updated",
        {
            "actor_user_id": actor_user_id,
            "server_id": server_id,
            "fields": sorted(fields),
            "allow_added": sorted(allow_added),
            "allow_removed": sorted(allow_removed),
            "revision": revision,
        },
    )


def audit_mcp_override_removed(
    *, actor_user_id: str, server_id: str, orphan: bool, revision: int
) -> None:
    _emit(
        "mcp.override_removed",
        {"actor_user_id": actor_user_id, "server_id": server_id, "orphan": orphan, "revision": revision},
    )


def audit_mcp_web_layer_reset(*, actor_user_id: str, existed: bool) -> None:
    _emit("mcp.web_layer_reset", {"actor_user_id": actor_user_id, "existed": existed})


def audit_mcp_credential_changed(
    *, actor_user_id: str, server_id: str, slot: str, action: str
) -> None:
    """action is 'set' or 'cleared'; the value itself is never passed in."""
    _emit(
        "mcp.credential_changed",
        {"actor_user_id": actor_user_id, "server_id": server_id, "slot": slot, "action": action},
    )


def audit_mcp_server_probed(*, actor_user_id: str, server_id: str, status: str) -> None:
    """探测会按需 spawn 运维配置的命令：不是配置变更，但留一条记录。"""
    _emit("mcp.server_probed", {"actor_user_id": actor_user_id, "server_id": server_id, "status": status})


# ---------------------------------------------------------------------------
# Test helper — clear buffer between tests
# ---------------------------------------------------------------------------

def _clear_events() -> None:
    _events.clear()


def _get_events() -> list[dict[str, Any]]:
    return list(_events)
