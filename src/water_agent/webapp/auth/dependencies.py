"""P5 Step 37: authentication FastAPI dependencies.

Provides:
- require_authenticated: raises 401 if no valid session cookie
- require_admin: raises 403 if authenticated user is not admin
- require_password_changed: raises 403 if must_change_password is set
  (allowed only on /api/auth/change-pass)

Principal construction happens ONLY here — never from request JSON,
URL parameters, or model output.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import Cookie, HTTPException, Request

from water_agent.runtime.governance.contracts import Capability, Principal
from water_agent.webapp.auth.models import UserRecord, UserRole
from water_agent.webapp.auth.session_store import SessionNotFoundError, get_session_store
from water_agent.webapp.auth.user_store import UserNotFoundError, get_user_store

# Capabilities granted to authenticated web users.
# Step 38 will resolve these per-role from a capability table.
_MEMBER_CAPABILITIES: frozenset[Capability] = frozenset(
    {
        Capability.RETRIEVE_CLAUSES,
        Capability.CITE_CLAUSES,
        Capability.CALCULATE,
        Capability.MATERIAL_STATS,
        Capability.READ_DRAWING,
        Capability.EXPORT_TO_ALLOWED_DIR,
        Capability.LLM_INFERENCE,
        Capability.EMBEDDING,
        Capability.RERANK,
    }
)
_ADMIN_CAPABILITIES: frozenset[Capability] = _MEMBER_CAPABILITIES

TENANT_ID = "tenant:default"
SESSION_COOKIE_NAME = "wa_session"


def _build_principal(user: UserRecord) -> Principal:
    """Construct Principal from a validated UserRecord.

    This is the ONLY place Principal.authenticated() is called for web
    requests — never from JSON, URL params, or model output.
    """
    return Principal.authenticated(
        subject=f"user:{user.user_id}",
        auth_domain=TENANT_ID,
        capabilities=_ADMIN_CAPABILITIES if user.role is UserRole.ADMIN else _MEMBER_CAPABILITIES,
    )


def _resolve_session(wa_session: str | None) -> tuple[UserRecord, str]:
    """Return (user_record, session_id) or raise HTTPException."""
    if not wa_session:
        raise HTTPException(
            status_code=401,
            detail={"code": "AUTH_SESSION_INVALID", "message": "Not authenticated"},
        )
    session_store = get_session_store()
    user_store = get_user_store()
    try:
        session = session_store.get(wa_session)
    except SessionNotFoundError:
        raise HTTPException(
            status_code=401,
            detail={"code": "AUTH_SESSION_INVALID", "message": "Session not found or expired"},
        )
    if session.is_expired():
        session_store.delete(wa_session)
        raise HTTPException(
            status_code=401,
            detail={"code": "AUTH_SESSION_INVALID", "message": "Session expired"},
        )
    try:
        user = user_store.get_by_id(session.user_id)
    except UserNotFoundError:
        raise HTTPException(
            status_code=401,
            detail={"code": "AUTH_SESSION_INVALID", "message": "User not found"},
        )
    if not user.is_active:
        raise HTTPException(
            status_code=403,
            detail={"code": "AUTH_USER_INACTIVE", "message": "Account is disabled"},
        )
    return user, wa_session


def require_authenticated(
    wa_session: str | None = Cookie(default=None),
) -> tuple[UserRecord, Principal]:
    """FastAPI dependency: resolve session cookie to (UserRecord, Principal).

    Raises 401 on invalid/missing/expired session.
    Raises 403 on inactive account.
    """
    user, _ = _resolve_session(wa_session)
    return user, _build_principal(user)


def require_admin(
    wa_session: str | None = Cookie(default=None),
) -> tuple[UserRecord, Principal]:
    """FastAPI dependency: require an active admin session."""
    user, session_id = _resolve_session(wa_session)
    if user.role is not UserRole.ADMIN:
        raise HTTPException(
            status_code=403,
            detail={"code": "AUTH_FORBIDDEN", "message": "Admin access required"},
        )
    return user, _build_principal(user)


def require_password_changed(
    wa_session: str | None = Cookie(default=None),
) -> tuple[UserRecord, Principal]:
    """FastAPI dependency: require authentication AND that the password has been changed.

    Use this on every endpoint EXCEPT /api/auth/change-pass.
    """
    user, session_id = _resolve_session(wa_session)
    if user.must_change_password:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "AUTH_MUST_CHANGE_PASSWORD",
                "message": "Temporary password must be changed before continuing",
            },
        )
    return user, _build_principal(user)
