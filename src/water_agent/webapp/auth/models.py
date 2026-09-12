"""P5 Step 37: in-memory user and session schema contracts.

Step 38 replaces these in-memory stores with PostgreSQL tables via SQLAlchemy/Alembic.
The data shapes here define the stable contract so routes stay unchanged at migration time.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

class UserRole(str, Enum):
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


# ---------------------------------------------------------------------------
# User record (mirrors future users table)
# ---------------------------------------------------------------------------

class UserRecord(BaseModel):
    """Mutable user record; frozen=False so Step 38 can update fields."""

    model_config = ConfigDict(extra="forbid")

    user_id: UUID = Field(default_factory=uuid4)
    username: str
    role: UserRole
    #: Argon2id hash string; NEVER expose in any response or log.
    password_hash: str = Field(repr=False)
    #: True = temporary password set by admin; user must change on first login.
    must_change_password: bool = False
    is_active: bool = True
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    def safe_dict(self) -> dict:
        """Safe view for API responses — no hash, no internal fields."""
        return {
            "user_id": str(self.user_id),
            "username": self.username,
            "role": self.role.value,
            "must_change_password": self.must_change_password,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat(),
        }


# ---------------------------------------------------------------------------
# Session record (mirrors future auth_sessions table)
# ---------------------------------------------------------------------------

SESSION_TTL_SECONDS = 8 * 3600  # 8 hours


class SessionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(default_factory=lambda: secrets.token_urlsafe(32))
    user_id: UUID
    csrf_token: str = Field(default_factory=lambda: secrets.token_urlsafe(32))
    created_at: datetime = Field(default_factory=_utc_now)
    expires_at: datetime = Field(
        default_factory=lambda: _utc_now() + timedelta(seconds=SESSION_TTL_SECONDS)
    )

    def is_expired(self) -> bool:
        return _utc_now() > self.expires_at


# ---------------------------------------------------------------------------
# Stable error codes (auth namespace)
# ---------------------------------------------------------------------------

AUTH_ERROR_CODES: frozenset[str] = frozenset(
    {
        "AUTH_INVALID_CREDENTIALS",      # login failed (user unknown OR wrong password — no distinction)
        "AUTH_SESSION_INVALID",          # session missing, expired, or tampered
        "AUTH_CSRF_INVALID",             # CSRF token mismatch
        "AUTH_RATE_LIMITED",             # too many failed login attempts
        "AUTH_MUST_CHANGE_PASSWORD",     # temporary password; only change-pass allowed
        "AUTH_USER_INACTIVE",            # account disabled
        "AUTH_FORBIDDEN",                # wrong role / IDOR guard
        "AUTH_USER_NOT_FOUND",           # admin-facing; not exposed on login path
        "AUTH_USERNAME_TAKEN",           # admin creates duplicate username
        "AUTH_WEAK_PASSWORD",            # password policy violation
    }
)
