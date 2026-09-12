"""P5 Step 37: authentication audit event sink.

All audit events are written to structured logs (stdout JSON lines) and to an
in-memory list for test inspection.  Step 38 will additionally persist them to
a PostgreSQL table.

Contract: audit entries NEVER contain passwords, password hashes, session
secrets, CSRF tokens, full usernames in failure paths, or API keys.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("water_agent.auth.audit")

# In-memory buffer: used by tests to assert events were recorded.
_events: list[dict[str, Any]] = []


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def audit_bootstrap_admin_skipped() -> None:
    _emit("auth.bootstrap_admin_skipped", {})


# ---------------------------------------------------------------------------
# Test helper — clear buffer between tests
# ---------------------------------------------------------------------------

def _clear_events() -> None:
    _events.clear()


def _get_events() -> list[dict[str, Any]]:
    return list(_events)
