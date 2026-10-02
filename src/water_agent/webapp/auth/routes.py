"""P5 Step 37: authentication routes.

Implements:
  POST /api/auth/login
  POST /api/auth/logout
  GET  /api/auth/status   (P5-S47: unconditional three-state probe for AuthGate)
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

import asyncio
import os
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

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
    burn_password_check,
    hash_password,
    verify_password,
)
from water_agent.webapp.auth.rate_limit import get_rate_limiter
from water_agent.webapp.auth.session_store import SessionNotFoundError, get_session_store
from water_agent.webapp.auth.user_store import (
    LastActiveAdminError,
    UsernameTakenError,
    UserNotFoundError,
    get_user_store,
)

router = APIRouter()

_API_V1 = "v1"

#: 与 users.username 列一致；不设上限时每个超长用户名的失败登录都会新建一个限流桶。
MAX_USERNAME_LENGTH = 128
#: 口令上限只防滥用（Argon2 的输入成本随长度增长），远大于任何合理口令。
MAX_PASSWORD_LENGTH = 1024

#: 同时进行的 Argon2 计算上限。每次约 50 ms、64 MiB：放进线程池是为了不卡事件循环
#: （此前直接跑在 async 路由里，单 worker 下一波登录就让全站停顿），限并发是为了不让
#: 一波并发登录把容器内存（生产 2 GiB）打爆。超出的请求排队等待，而不是失败。
_HASH_SLOTS = asyncio.Semaphore(4)


async def _hashing(func, /, *args):
    """在线程池里做一次口令哈希相关的计算（受 ``_HASH_SLOTS`` 限并发）。"""
    async with _HASH_SLOTS:
        return await run_in_threadpool(func, *args)


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
    username: str = Field(min_length=1, max_length=MAX_USERNAME_LENGTH)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


def _check_credentials(username: str, password: str):
    """查用户并校验口令（线程池里跑）；用户不存在时照样付一次哈希成本，防计时侧信道。"""
    user_store = get_user_store()
    try:
        user = user_store.get_by_username(username)
    except UserNotFoundError:
        burn_password_check(password)
        return None, False
    return user, verify_password(password, user.password_hash)


@router.post("/api/auth/login")
async def login(body: _LoginBody) -> JSONResponse:
    limiter = get_rate_limiter()
    session_store = get_session_store()

    # Rate-limit check: log the event but never reveal the username
    if limiter.is_limited(body.username):
        audit.audit_login_rate_limited(failure_count=limiter.failure_count(body.username))
        return JSONResponse(
            status_code=429,
            content=_err("AUTH_RATE_LIMITED", "Too many failed login attempts; please wait"),
        )

    # Resolve user; always perform a password hash check to avoid timing oracles.
    # The not-found branch used to skip the hash entirely (0.5 ms vs 51 ms measured),
    # which made the response time itself a username-existence oracle.
    user, password_ok = await _hashing(_check_credentials, body.username, body.password)

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
# Auth status (unconditional probe for the React AuthGate)
# ---------------------------------------------------------------------------

@router.get("/api/auth/status")
async def auth_status(wa_session: str | None = Cookie(default=None)) -> JSONResponse:
    """Unconditional three-state probe for the frontend auth gate.

    Always 200.  ``authenticated`` carries the state so the frontend can
    distinguish "not logged in" from "logged in but must change password"
    without tripping the 401/403 guards on /api/auth/me.  Never leaks user
    fields when unauthenticated; never consumes login rate-limit budget.
    """
    if not wa_session:
        return JSONResponse(status_code=200, content=_ok({"authenticated": False}))
    session_store = get_session_store()
    user_store = get_user_store()
    try:
        session = session_store.get(wa_session)
    except SessionNotFoundError:
        return JSONResponse(status_code=200, content=_ok({"authenticated": False}))
    if session.is_expired():
        session_store.delete(wa_session)
        return JSONResponse(status_code=200, content=_ok({"authenticated": False}))
    try:
        user = user_store.get_by_id(session.user_id)
    except UserNotFoundError:
        return JSONResponse(status_code=200, content=_ok({"authenticated": False}))
    if not user.is_active:
        return JSONResponse(status_code=200, content=_ok({"authenticated": False}))
    return JSONResponse(
        status_code=200,
        content=_ok(
            {
                "authenticated": True,
                "user": user.safe_dict(),
                "must_change_password": user.must_change_password,
            }
        ),
    )


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
    current_password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    new_password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


@router.post("/api/auth/change-pass", dependencies=[Depends(validate_csrf)])
async def change_password(
    body: _ChangePassBody,
    auth: tuple = Depends(require_authenticated),  # allow even if must_change_password
) -> JSONResponse:
    user, _ = auth
    user_store = get_user_store()
    session_store = get_session_store()
    limiter = get_rate_limiter()
    # 拿到会话的人不能无限次猜当前密码；键与登录用户名的桶分开（用户名本身可含冒号，
    # 但用户 id 是 UUID，前缀后不会与任何用户名撞桶）。
    guess_key = f"change-pass:{user.user_id}"
    if limiter.is_limited(guess_key):
        audit.audit_login_rate_limited(failure_count=limiter.failure_count(guess_key))
        return JSONResponse(
            status_code=429,
            content=_err("AUTH_RATE_LIMITED", "Too many failed attempts; please wait"),
        )

    if not await _hashing(verify_password, body.current_password, user.password_hash):
        limiter.record_failure(guess_key)
        return JSONResponse(
            status_code=401,
            content=_err("AUTH_INVALID_CREDENTIALS", "Current password is incorrect"),
        )
    limiter.reset(guess_key)
    if body.new_password == body.current_password:
        # 强制改密时把临时密码原样填回去等于没改（前端已拦，这里是 API 层的同一条规则）
        return JSONResponse(
            status_code=422,
            content=_err("AUTH_PASSWORD_REUSED", "New password must differ from the current password"),
        )
    try:
        new_hash = await _hashing(hash_password, body.new_password)
    except PasswordTooShortError:
        return JSONResponse(
            status_code=422,
            content=_err("AUTH_WEAK_PASSWORD", f"Password must be at least {MIN_PASSWORD_LENGTH} characters"),
        )

    user_store.update_password_hash(user.user_id, new_hash)
    # 改密即吊销该用户的全部会话（泄露的会话不能活过一次改密），再给当前客户端签发新会话
    session_store.delete_all_for_user(user.user_id)
    session = session_store.create(user.user_id)
    audit.audit_password_changed(user_id=str(user.user_id), actor_user_id=str(user.user_id))
    response = JSONResponse(status_code=200, content=_ok({"changed": True}))
    _set_session_cookies(response, session.session_id, session.csrf_token)
    return response


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
    username: str = Field(min_length=1, max_length=MAX_USERNAME_LENGTH, pattern=r"\S")
    temporary_password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
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
        new_user = await _hashing(
            lambda: user_store.create_user(
                username=body.username,
                plaintext_password=body.temporary_password,
                role=role,
                must_change_password=True,
            )
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
    if user_id == admin_user.user_id:
        # 停用自己即把自己锁在门外；唯一的管理员这么做之后连 bootstrap-admin 也救不回来
        # （它不会重新激活已存在的同名账号），只能直接改库。
        return JSONResponse(
            status_code=409,
            content=_err(
                "AUTH_CANNOT_DEACTIVATE_SELF",
                "Administrators cannot deactivate their own account",
            ),
        )
    user_store = get_user_store()
    try:
        # 判定与写入在存储层是同一个原子步骤：两个管理员同时互相停用时，后提交的
        # 那个会发现自己要停用的是最后一个活跃管理员。
        user_store.deactivate(user_id)
    except UserNotFoundError:
        return JSONResponse(
            status_code=404,
            content=_err("AUTH_USER_NOT_FOUND", "User not found"),
        )
    except LastActiveAdminError:
        return JSONResponse(
            status_code=409,
            content=_err(
                "AUTH_LAST_ACTIVE_ADMIN",
                "The last active administrator cannot be deactivated",
            ),
        )
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
    temporary_password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


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
        new_hash = await _hashing(hash_password, body.temporary_password)
    except PasswordTooShortError:
        return JSONResponse(
            status_code=422,
            content=_err("AUTH_WEAK_PASSWORD", f"Password must be at least {MIN_PASSWORD_LENGTH} characters"),
        )
    user_store.reset_password_hash(user_id, new_hash)
    # 重置即吊销该用户的全部会话：此前旧会话只是暂时 403，用户用临时密码登录并改密后，
    # 泄露的旧会话又变回有效。
    get_session_store().delete_all_for_user(user_id)

    audit.audit_password_changed(user_id=str(user_id), actor_user_id=str(admin_user.user_id))
    return JSONResponse(status_code=200, content=_ok({"reset": True}))
