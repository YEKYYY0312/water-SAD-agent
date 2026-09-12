"""P5 Step 37: authentication routes.

Implements:
  POST /api/auth/login
  POST /api/auth/logout
  GET  /api/auth/me
  POST /api/auth/change-pass
  GET  /api/auth/register  → 404 (public registration disabled)
  POST /api/auth/register  → 404

Admin user-management:
  GET    /api/admin/users
  POST   /api/admin/users
  PATCH  /api/admin/users/{user_id}/deactivate
  PATCH  /api/admin/users/{user_id}/reactivate
  POST   /api/admin/users/{user_id}/reset-password

Cookie behaviour:
  wa_session — HttpOnly, SameSite=Lax, Secure when WEB_SECURE_COOKIE=true
  wa_csrf    — non-HttpOnly (JS reads it for X-CSRF-Token header), SameSite=Lax
"""
from __future__ import annotations

import os
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from water_agent.webapp.auth import audit
from water_agent.webapp.auth.csrf import CSRF_COOKIE_NAME, validate_csrf
from water_agent.webapp.auth.dependencies import (
    SESSION_COOKIE_NAME,
    require_admin,
    require_authenticated,
    require_password_changed,
)
from water_agent.webapp.auth.models import UserRole
from water_agent.webapp.auth.password import (
    MIN_PASSWORD_LENGTH,
    PasswordTooShortError,
    hash_password,
    verify_password,
)
from water_agent.webapp.auth.rate_limit import get_rate_limiter
from water_agent.webapp.auth.session_store import get_session_store
from water_agent.webapp.auth.user_store import (
    UsernameTakenError,
    UserNotFoundError,
    get_user_store,
)

router = APIRouter()

_API_V1 = "v1"


def _ok(data: object) -> dict:
    return {"version": _API_V1, "ok": True, "data": data}


def _err(code: str, message: str) -> dict:
    return {"version": _API_V1, "ok": False, "error": {"code": code, "message": message}}


def _secure_cookie() -> bool:
    return os.getenv("WEB_SECURE_COOKIE", "false").lower() == "true"


def _set_session_cookies(response: JSONResponse, session_id: str, csrf_token: str) -> None:
    secure = _secure_cookie()
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_id,
        httponly=True,
        samesite="lax",
        secure=secure,
        max_age=8 * 3600,
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        csrf_token,
        httponly=False,   # JS must be able to read this to set the header
        samesite="lax",
        secure=secure,
        max_age=8 * 3600,
        path="/",
    )


def _clear_session_cookies(response: JSONResponse) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")


# ---------------------------------------------------------------------------
# Public registration endpoint — always 404
# ---------------------------------------------------------------------------

@router.get("/api/auth/register")
@router.post("/api/auth/register")
async def register_disabled() -> JSONResponse:
    """Public registration is disabled.  Only admins may create accounts."""
    return JSONResponse(
        status_code=404,
        content=_err("WEB_NOT_FOUND", "Resource not found"),
    )


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

class _LoginBody(BaseModel):
    username: str
    password: str


@router.post("/api/auth/login")
async def login(body: _LoginBody) -> JSONResponse:
    limiter = get_rate_limiter()
    user_store = get_user_store()
    session_store = get_session_store()

    # Rate-limit check: log the event but never reveal the username
    if limiter.is_limited(body.username):
        audit.audit_login_rate_limited(failure_count=limiter.failure_count(body.username))
        return JSONResponse(
            status_code=429,
            content=_err("AUTH_RATE_LIMITED", "Too many failed login attempts; please wait"),
        )

    # Resolve user; always perform a password hash check to avoid timing oracles
    try:
        user = user_store.get_by_username(body.username)
        password_ok = verify_password(body.password, user.password_hash)
    except UserNotFoundError:
        password_ok = False
        user = None

    if not password_ok or user is None:
        limiter.record_failure(body.username)
        audit.audit_login_failure(reason="invalid_credentials")
        return JSONResponse(
            status_code=401,
            content=_err("AUTH_INVALID_CREDENTIALS", "Invalid credentials"),
        )

    if not user.is_active:
        # Do NOT call record_failure here — inactive ≠ wrong password
        audit.audit_login_failure(reason="account_inactive")
        return JSONResponse(
            status_code=403,
            content=_err("AUTH_USER_INACTIVE", "Account is disabled"),
        )

    limiter.reset(body.username)
    session = session_store.create(user.user_id)
    audit.audit_login_success(user_id=str(user.user_id), role=user.role.value)

    response = JSONResponse(
        status_code=200,
        content=_ok({
            "user": user.safe_dict(),
            "must_change_password": user.must_change_password,
        }),
    )
    _set_session_cookies(response, session.session_id, session.csrf_token)
    return response


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------

@router.post("/api/auth/logout", dependencies=[Depends(validate_csrf)])
async def logout(
    wa_session: str | None = Cookie(default=None),
    auth: tuple = Depends(require_authenticated),
) -> JSONResponse:
    user, _ = auth
    session_store = get_session_store()
    if wa_session:
        session_store.delete(wa_session)
    audit.audit_logout(user_id=str(user.user_id))
    response = JSONResponse(status_code=200, content=_ok({"logged_out": True}))
    _clear_session_cookies(response)
    return response


# ---------------------------------------------------------------------------
# Current user
# ---------------------------------------------------------------------------

@router.get("/api/auth/me")
async def me(auth: tuple = Depends(require_password_changed)) -> JSONResponse:
    user, principal = auth
    return JSONResponse(
        status_code=200,
        content=_ok({"user": user.safe_dict(), "principal_hash": principal.principal_hash}),
    )


# ---------------------------------------------------------------------------
# Change password
# ---------------------------------------------------------------------------

class _ChangePassBody(BaseModel):
    current_password: str
    new_password: str


@router.post("/api/auth/change-pass", dependencies=[Depends(validate_csrf)])
async def change_password(
    body: _ChangePassBody,
    auth: tuple = Depends(require_authenticated),  # allow even if must_change_password
) -> JSONResponse:
    user, _ = auth
    user_store = get_user_store()

    if not verify_password(body.current_password, user.password_hash):
        return JSONResponse(
            status_code=401,
            content=_err("AUTH_INVALID_CREDENTIALS", "Current password is incorrect"),
        )
    try:
        new_hash = hash_password(body.new_password)
    except PasswordTooShortError:
        return JSONResponse(
            status_code=422,
            content=_err("AUTH_WEAK_PASSWORD", f"Password must be at least {MIN_PASSWORD_LENGTH} characters"),
        )

    user_store.update_password_hash(user.user_id, new_hash)
    audit.audit_password_changed(user_id=str(user.user_id), actor_user_id=str(user.user_id))
    return JSONResponse(status_code=200, content=_ok({"changed": True}))


# ---------------------------------------------------------------------------
# Admin: list users
# ---------------------------------------------------------------------------

@router.get("/api/admin/users")
async def list_users(auth: tuple = Depends(require_admin)) -> JSONResponse:
    _, _ = auth
    user_store = get_user_store()
    return JSONResponse(
        status_code=200,
        content=_ok({"users": [u.safe_dict() for u in user_store.list_users()]}),
    )


# ---------------------------------------------------------------------------
# Admin: create user
# ---------------------------------------------------------------------------

class _CreateUserBody(BaseModel):
    username: str
    temporary_password: str
    role: str = "member"


@router.post("/api/admin/users", dependencies=[Depends(validate_csrf)])
async def create_user(
    body: _CreateUserBody,
    auth: tuple = Depends(require_admin),
) -> JSONResponse:
    admin_user, _ = auth
    user_store = get_user_store()

    try:
        role = UserRole(body.role)
    except ValueError:
        return JSONResponse(
            status_code=422,
            content=_err("AUTH_USER_NOT_FOUND", f"Invalid role: {body.role!r}"),
        )
    try:
        new_user = user_store.create_user(
            username=body.username,
            plaintext_password=body.temporary_password,
            role=role,
            must_change_password=True,
        )
    except UsernameTakenError:
        return JSONResponse(
            status_code=409,
            content=_err("AUTH_USERNAME_TAKEN", "Username is already in use"),
        )
    except PasswordTooShortError:
        return JSONResponse(
            status_code=422,
            content=_err("AUTH_WEAK_PASSWORD", f"Password must be at least {MIN_PASSWORD_LENGTH} characters"),
        )

    audit.audit_user_created(
        new_user_id=str(new_user.user_id),
        role=new_user.role.value,
        actor_user_id=str(admin_user.user_id),
    )
    return JSONResponse(status_code=201, content=_ok({"user": new_user.safe_dict()}))


# ---------------------------------------------------------------------------
# Admin: deactivate / reactivate
# ---------------------------------------------------------------------------

@router.patch("/api/admin/users/{user_id}/deactivate", dependencies=[Depends(validate_csrf)])
async def deactivate_user(
    user_id: UUID,
    auth: tuple = Depends(require_admin),
) -> JSONResponse:
    admin_user, _ = auth
    user_store = get_user_store()
    try:
        user_store.get_by_id(user_id)
    except UserNotFoundError:
        return JSONResponse(
            status_code=404,
            content=_err("AUTH_USER_NOT_FOUND", "User not found"),
        )
    user_store.set_active(user_id, active=False)
    audit.audit_user_deactivated(target_user_id=str(user_id), actor_user_id=str(admin_user.user_id))
    # Evict all sessions for the deactivated user
    get_session_store().delete_all_for_user(user_id)
    return JSONResponse(status_code=200, content=_ok({"deactivated": True}))


@router.patch("/api/admin/users/{user_id}/reactivate", dependencies=[Depends(validate_csrf)])
async def reactivate_user(
    user_id: UUID,
    auth: tuple = Depends(require_admin),
) -> JSONResponse:
    admin_user, _ = auth
    user_store = get_user_store()
    try:
        user_store.get_by_id(user_id)
    except UserNotFoundError:
        return JSONResponse(
            status_code=404,
            content=_err("AUTH_USER_NOT_FOUND", "User not found"),
        )
    user_store.set_active(user_id, active=True)
    audit.audit_user_reactivated(target_user_id=str(user_id), actor_user_id=str(admin_user.user_id))
    return JSONResponse(status_code=200, content=_ok({"reactivated": True}))


# ---------------------------------------------------------------------------
# Admin: reset password
# ---------------------------------------------------------------------------

class _ResetPasswordBody(BaseModel):
    temporary_password: str


@router.post("/api/admin/users/{user_id}/reset-password", dependencies=[Depends(validate_csrf)])
async def reset_password(
    user_id: UUID,
    body: _ResetPasswordBody,
    auth: tuple = Depends(require_admin),
) -> JSONResponse:
    admin_user, _ = auth
    user_store = get_user_store()
    try:
        user_store.get_by_id(user_id)
    except UserNotFoundError:
        return JSONResponse(
            status_code=404,
            content=_err("AUTH_USER_NOT_FOUND", "User not found"),
        )
    try:
        new_hash = hash_password(body.temporary_password)
    except PasswordTooShortError:
        return JSONResponse(
            status_code=422,
            content=_err("AUTH_WEAK_PASSWORD", f"Password must be at least {MIN_PASSWORD_LENGTH} characters"),
        )
    user_store.reset_password_hash(user_id, new_hash)

    audit.audit_password_changed(user_id=str(user_id), actor_user_id=str(admin_user.user_id))
    return JSONResponse(status_code=200, content=_ok({"reset": True}))
