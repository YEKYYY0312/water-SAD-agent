"""P5 Step 37: authentication contract tests.

All tests use in-memory stores only — no database or network required.
Each test gets a fresh app instance with isolated stores to prevent bleed-through.
"""
from __future__ import annotations

import secrets
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from water_agent.webapp.auth import audit as auth_audit
from water_agent.webapp.auth.bootstrap import run_bootstrap
from water_agent.webapp.auth.csrf import CSRF_COOKIE_NAME
from water_agent.webapp.auth.dependencies import SESSION_COOKIE_NAME
from water_agent.webapp.auth.models import AUTH_ERROR_CODES, UserRole
from water_agent.webapp.auth.password import hash_password, verify_password
from water_agent.webapp.auth.rate_limit import LoginRateLimiter
from water_agent.webapp.auth.session_store import SessionStore
from water_agent.webapp.auth.user_store import UserStore

# ---------------------------------------------------------------------------
# Test fixtures — fresh isolated instances per test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_audit():
    auth_audit._clear_events()
    yield
    auth_audit._clear_events()


def _fresh_app(
    user_store: UserStore | None = None,
    session_store: SessionStore | None = None,
    rate_limiter: LoginRateLimiter | None = None,
):
    """Return a TestClient backed by fresh in-memory stores."""
    import water_agent.webapp.auth.rate_limit as rl_mod
    import water_agent.webapp.auth.session_store as ss_mod
    import water_agent.webapp.auth.user_store as us_mod

    us = user_store or UserStore()
    ss = session_store or SessionStore()
    rl = rate_limiter or LoginRateLimiter()

    # Patch module-level singletons for this call
    _orig_us = us_mod._store
    _orig_ss = ss_mod._store
    _orig_rl = rl_mod._limiter
    us_mod._store = us
    ss_mod._store = ss
    rl_mod._limiter = rl

    from water_agent.webapp.app import create_app
    client = TestClient(create_app(), raise_server_exceptions=True)

    # Restore originals (the fixture teardown pattern is simpler here since
    # TestClient is used synchronously)
    us_mod._store = _orig_us
    ss_mod._store = _orig_ss
    rl_mod._limiter = _orig_rl

    # Return both client and the stores so tests can inspect them
    return client, us, ss, rl


def _make_app_with_stores():
    """Return (client, user_store, session_store, rate_limiter) all fresh."""
    import water_agent.webapp.auth.rate_limit as rl_mod
    import water_agent.webapp.auth.session_store as ss_mod
    import water_agent.webapp.auth.user_store as us_mod
    from water_agent.webapp.app import create_app

    us = UserStore()
    ss = SessionStore()
    rl = LoginRateLimiter(max_failures=3, window_seconds=60, lockout_seconds=120)

    us_mod._store = us
    ss_mod._store = ss
    rl_mod._limiter = rl

    client = TestClient(create_app(), raise_server_exceptions=False)
    return client, us, ss, rl


# ---------------------------------------------------------------------------
# 1. Public registration route returns 404
# ---------------------------------------------------------------------------

def test_public_registration_get_returns_404():
    client, *_ = _make_app_with_stores()
    r = client.get("/api/auth/register")
    assert r.status_code == 404
    assert r.json()["ok"] is False
    assert r.json()["error"]["code"] == "WEB_NOT_FOUND"


def test_public_registration_post_returns_404():
    client, *_ = _make_app_with_stores()
    r = client.post("/api/auth/register", json={"username": "x", "password": "y"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 2. Bootstrap creates admin on first call
# ---------------------------------------------------------------------------

def test_bootstrap_creates_admin_from_secret(tmp_path: Path):
    import water_agent.webapp.auth.bootstrap as bs_mod
    import water_agent.webapp.auth.user_store as us_mod
    original = us_mod._store
    us = UserStore()
    us_mod._store = us

    secret = tmp_path / "admin_password"
    secret.write_text("SecureBootstrapPass!2026", encoding="utf-8")

    # Simulate a configured database (non-None return) without a real DB
    sentinel = object()
    bs_mod_orig = bs_mod.configure_database_stores
    bs_mod.configure_database_stores = lambda: sentinel  # type: ignore[assignment]
    try:
        result = run_bootstrap(secret_path=secret)
    finally:
        bs_mod.configure_database_stores = bs_mod_orig
        us_mod._store = original

    assert result == 0
    assert us.has_admin()

    events = auth_audit._get_events()
    assert any(e["event"] == "auth.bootstrap_admin_created" for e in events)


# ---------------------------------------------------------------------------
# 3. Bootstrap is idempotent — second call skips
# ---------------------------------------------------------------------------

def test_bootstrap_is_idempotent(tmp_path: Path):
    import water_agent.webapp.auth.bootstrap as bs_mod
    import water_agent.webapp.auth.user_store as us_mod
    original = us_mod._store
    us = UserStore()
    us_mod._store = us

    secret = tmp_path / "admin_password"
    secret.write_text("SecureBootstrapPass!2026", encoding="utf-8")

    sentinel = object()
    bs_mod_orig = bs_mod.configure_database_stores
    bs_mod.configure_database_stores = lambda: sentinel  # type: ignore[assignment]
    try:
        run_bootstrap(secret_path=secret)
        admin1_id = next(iter(us_mod._store._by_id.values())).user_id

        result = run_bootstrap(secret_path=secret)
    finally:
        bs_mod.configure_database_stores = bs_mod_orig
        us_mod._store = original

    assert result == 0
    # Still only one user
    assert len(us._by_id) == 1
    assert next(iter(us._by_id.values())).user_id == admin1_id

    skipped = [e for e in auth_audit._get_events() if e["event"] == "auth.bootstrap_admin_skipped"]
    assert len(skipped) == 1


def _bootstrap_against(store: UserStore, monkeypatch: pytest.MonkeyPatch) -> None:
    import water_agent.webapp.auth.bootstrap as bs_mod
    import water_agent.webapp.auth.user_store as us_mod

    monkeypatch.setattr(us_mod, "_store", store)
    monkeypatch.setattr(bs_mod, "configure_database_stores", lambda: object())


def test_bootstrap_with_a_deactivated_same_named_admin_succeeds_without_touching_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """回归：唯一的管理员停用自己后，bootstrap 每次都以 "username 'admin' already exists"
    退出 1，而 compose 里 app 依赖 bootstrap-admin 成功退出——app 永远起不来。

    现在同名管理员已存在即按幂等成功（退出 0），但 bootstrap 不是恢复后门：不重新激活
    该账号、不用 secret 文件里的密码重置它，也不需要 secret 文件。
    """
    store = UserStore()
    admin = store.create_user("admin", "OriginalAdminPass2026!", UserRole.ADMIN)
    store.set_active(admin.user_id, active=False)
    before = store.get_by_id(admin.user_id)
    secret = tmp_path / "admin_password"
    secret.write_text("ReplacementAdminPass2026!", encoding="utf-8")
    _bootstrap_against(store, monkeypatch)

    assert run_bootstrap(secret_path=secret) == 0
    assert run_bootstrap(secret_path=tmp_path / "missing-secret") == 0

    after = store.get_by_id(admin.user_id)
    assert after.is_active is False
    assert after.password_hash == before.password_hash
    assert after.must_change_password is before.must_change_password
    assert verify_password("OriginalAdminPass2026!", after.password_hash)
    assert not verify_password("ReplacementAdminPass2026!", after.password_hash)
    assert [user.user_id for user in store.list_users()] == [admin.user_id]
    assert store.has_admin() is False
    err = capsys.readouterr().err
    assert "already exists but is deactivated" in err
    assert "ReplacementAdminPass2026!" not in err
    skipped = [e for e in auth_audit._get_events() if e["event"] == "auth.bootstrap_admin_skipped"]
    assert [e["reason"] for e in skipped] == ["admin_exists_inactive", "admin_exists_inactive"]


def test_bootstrap_still_refuses_when_the_admin_name_belongs_to_a_non_admin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """同名账号不是管理员时 bootstrap 无法完成本职（建出管理员），仍退出 1 且不改动该账号。"""
    store = UserStore()
    member = store.create_user("admin", "MemberNamedAdmin2026!", UserRole.MEMBER)
    before = store.get_by_id(member.user_id)
    secret = tmp_path / "admin_password"
    secret.write_text("SecureBootstrapPass!2026", encoding="utf-8")
    _bootstrap_against(store, monkeypatch)

    assert run_bootstrap(secret_path=secret) == 1

    assert store.get_by_id(member.user_id) == before
    assert len(store.list_users()) == 1
    assert "is taken by a non-admin account" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 4. Fixed session id rejected (session must be server-generated)
# ---------------------------------------------------------------------------

def test_fixed_session_id_is_rejected():
    client, us, _ss, _rl = _make_app_with_stores()
    us.create_user("alice", "GoodPassword2026!", UserRole.MEMBER)
    # Attempt to authenticate with a hand-crafted session cookie
    client.cookies.set(SESSION_COOKIE_NAME, "attacker-chosen-session-id")
    r = client.get("/api/auth/me", cookies={SESSION_COOKIE_NAME: "attacker-chosen-session-id"})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 5. Cookie tampering fails
# ---------------------------------------------------------------------------

def test_tampered_session_cookie_is_rejected():
    client, us, _ss, _rl = _make_app_with_stores()
    us.create_user("bob", "GoodPassword2026!", UserRole.MEMBER)

    # Perform a real login
    r = client.post("/api/auth/login", json={"username": "bob", "password": "GoodPassword2026!"})
    assert r.status_code == 200

    # Tamper by using a different random string
    tampered = secrets.token_urlsafe(32)
    r2 = client.get("/api/auth/me", cookies={SESSION_COOKIE_NAME: tampered})
    assert r2.status_code == 401


# ---------------------------------------------------------------------------
# 6. Brute-force login triggers rate limit
# ---------------------------------------------------------------------------

def test_brute_force_triggers_rate_limit():
    client, us, _ss, _rl = _make_app_with_stores()
    us.create_user("charlie", "GoodPassword2026!", UserRole.MEMBER)

    # Exhaust failures (limiter configured at max_failures=3 above)
    for _ in range(3):
        client.post("/api/auth/login", json={"username": "charlie", "password": "wrong"})

    r = client.post("/api/auth/login", json={"username": "charlie", "password": "GoodPassword2026!"})
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "AUTH_RATE_LIMITED"

    events = auth_audit._get_events()
    assert any(e["event"] == "auth.login_rate_limited" for e in events)
    # Rate-limit events must not contain a password
    for e in events:
        assert "password" not in str(e)
        assert "GoodPassword2026!" not in str(e)


# ---------------------------------------------------------------------------
# 7. Different browser sessions are isolated
# ---------------------------------------------------------------------------

def test_different_sessions_are_isolated():
    import water_agent.webapp.auth.rate_limit as rl_mod
    import water_agent.webapp.auth.session_store as ss_mod
    import water_agent.webapp.auth.user_store as us_mod
    from water_agent.webapp.app import create_app

    us = UserStore()
    ss = SessionStore()
    rl = LoginRateLimiter()
    us_mod._store = us
    ss_mod._store = ss
    rl_mod._limiter = rl

    us.create_user("user1", "PasswordOne2026!", UserRole.MEMBER)
    us.create_user("user2", "PasswordTwo2026!", UserRole.MEMBER)

    app = create_app()
    client1 = TestClient(app, raise_server_exceptions=False)
    client2 = TestClient(app, raise_server_exceptions=False)

    r1 = client1.post("/api/auth/login", json={"username": "user1", "password": "PasswordOne2026!"})
    r2 = client2.post("/api/auth/login", json={"username": "user2", "password": "PasswordTwo2026!"})
    assert r1.status_code == 200
    assert r2.status_code == 200

    # Each client's /me should return their own user
    me1 = client1.get("/api/auth/me")
    me2 = client2.get("/api/auth/me")
    assert me1.status_code == 200
    assert me2.status_code == 200
    assert me1.json()["data"]["user"]["username"] == "user1"
    assert me2.json()["data"]["user"]["username"] == "user2"
    # Sessions must be different
    assert me1.json()["data"]["user"]["user_id"] != me2.json()["data"]["user"]["user_id"]


# ---------------------------------------------------------------------------
# 8. Unauthenticated request fails
# ---------------------------------------------------------------------------

def test_unauthenticated_request_returns_401():
    client, *_ = _make_app_with_stores()
    r = client.get("/api/auth/me")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "AUTH_SESSION_INVALID"


# ---------------------------------------------------------------------------
# 9. Principal correctly constructed from authenticated session
# ---------------------------------------------------------------------------

def test_principal_constructed_from_session():
    client, us, _ss, _rl = _make_app_with_stores()
    us.create_user("dave", "PrincipalTest2026!", UserRole.MEMBER)

    r = client.post("/api/auth/login", json={"username": "dave", "password": "PrincipalTest2026!"})
    assert r.status_code == 200

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    data = me.json()["data"]
    # subject must be "user:<uuid>" format — checked via principal_hash presence
    assert "principal_hash" in data
    # user_id must be present
    assert "user_id" in data["user"]
    # subject original value must NOT appear in the response
    user_id = data["user"]["user_id"]
    response_text = me.text
    assert f"user:{user_id}" not in response_text


# ---------------------------------------------------------------------------
# 10. IDOR access fails (user cannot access another user's resource)
# ---------------------------------------------------------------------------

def test_idor_admin_create_requires_admin_role():
    client, us, _ss, _rl = _make_app_with_stores()
    us.create_user("member1", "MemberPass2026!", UserRole.MEMBER)

    r = client.post("/api/auth/login", json={"username": "member1", "password": "MemberPass2026!"})
    assert r.status_code == 200
    csrf = client.cookies.get(CSRF_COOKIE_NAME)

    # Non-admin tries to create a user
    r2 = client.post(
        "/api/admin/users",
        json={"username": "newuser", "temporary_password": "TempPass2026!X"},
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert r2.status_code == 403
    assert r2.json()["error"]["code"] == "AUTH_FORBIDDEN"


# ---------------------------------------------------------------------------
# 11. Password hash not reversible and does not appear in responses
# ---------------------------------------------------------------------------

def test_password_hash_not_in_login_response():
    client, us, _ss, _rl = _make_app_with_stores()
    us.create_user("eve", "SecretPass2026!", UserRole.MEMBER)

    r = client.post("/api/auth/login", json={"username": "eve", "password": "SecretPass2026!"})
    assert r.status_code == 200

    response_text = r.text
    # The plaintext must not appear
    assert "SecretPass2026!" not in response_text
    # The stored hash must not appear
    user = us.get_by_username("eve")
    assert user.password_hash not in response_text


def test_password_hash_is_not_in_me_response():
    client, us, _ss, _rl = _make_app_with_stores()
    us.create_user("frank", "FrankPass2026!", UserRole.MEMBER)

    client.post("/api/auth/login", json={"username": "frank", "password": "FrankPass2026!"})
    me = client.get("/api/auth/me")
    assert me.status_code == 200

    user = us.get_by_username("frank")
    assert user.password_hash not in me.text
    assert "FrankPass2026!" not in me.text


def test_argon2id_hash_is_not_plaintext():
    from water_agent.webapp.auth.password import verify_password
    h = hash_password("TestPass12345678!")
    # Must start with the Argon2 PHC header
    assert h.startswith("$argon2id$")
    # Verify round-trip
    assert verify_password("TestPass12345678!", h)
    assert not verify_password("WrongPassword!!", h)


# ---------------------------------------------------------------------------
# 12. CSRF validation protects state-mutating endpoints
# ---------------------------------------------------------------------------

def test_logout_without_csrf_returns_403():
    client, us, _ss, _rl = _make_app_with_stores()
    us.create_user("grace", "GracePass2026!", UserRole.MEMBER)
    client.post("/api/auth/login", json={"username": "grace", "password": "GracePass2026!"})

    # POST logout without CSRF header
    r = client.post("/api/auth/logout")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "AUTH_CSRF_INVALID"


def test_logout_with_correct_csrf_succeeds():
    client, us, _ss, _rl = _make_app_with_stores()
    us.create_user("hank", "HankPass2026!", UserRole.MEMBER)
    client.post("/api/auth/login", json={"username": "hank", "password": "HankPass2026!"})

    csrf = client.cookies.get(CSRF_COOKIE_NAME)
    assert csrf is not None

    r = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    assert r.json()["data"]["logged_out"] is True


# ---------------------------------------------------------------------------
# 13. must_change_password blocks /me but allows change-pass
# ---------------------------------------------------------------------------

def test_must_change_password_blocks_me():
    client, us, _ss, _rl = _make_app_with_stores()
    us.create_user("ivan", "IvanPass2026!", UserRole.MEMBER, must_change_password=True)

    client.post("/api/auth/login", json={"username": "ivan", "password": "IvanPass2026!"})
    r = client.get("/api/auth/me")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "AUTH_MUST_CHANGE_PASSWORD"


def test_must_change_password_allows_change_pass():
    client, us, _ss, _rl = _make_app_with_stores()
    us.create_user("jack", "JackPass2026!", UserRole.MEMBER, must_change_password=True)

    client.post("/api/auth/login", json={"username": "jack", "password": "JackPass2026!"})
    csrf = client.cookies.get(CSRF_COOKIE_NAME)

    r = client.post(
        "/api/auth/change-pass",
        json={"current_password": "JackPass2026!", "new_password": "NewJackPass2026!"},
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert r.status_code == 200
    assert r.json()["data"]["changed"] is True

    # Now /me should work
    me = client.get("/api/auth/me")
    assert me.status_code == 200


# ---------------------------------------------------------------------------
# 13b. /api/auth/status: the unconditional three-state probe (P5-S47)
# ---------------------------------------------------------------------------

def test_status_anonymous_is_200_unauthenticated():
    client, _us, _ss, _rl = _make_app_with_stores()
    r = client.get("/api/auth/status")
    assert r.status_code == 200
    assert r.json()["data"] == {"authenticated": False}


def test_status_authenticated_returns_safe_dict_without_hash():
    client, us, _ss, _rl = _make_app_with_stores()
    us.create_user("laura", "LauraPass2026!", UserRole.MEMBER)
    client.post("/api/auth/login", json={"username": "laura", "password": "LauraPass2026!"})

    r = client.get("/api/auth/status")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["authenticated"] is True
    assert data["user"]["username"] == "laura"
    assert data["must_change_password"] is False
    # 不泄漏：密码哈希与明文都不出现在响应里
    user = us.get_by_username("laura")
    assert user.password_hash not in r.text
    assert "LauraPass2026!" not in r.text


def test_status_must_change_password_is_not_blocked():
    """must_change_password 用户必须能从 status 拿到三态，否则永远进不了改密面板。"""
    client, us, _ss, _rl = _make_app_with_stores()
    us.create_user("mike", "MikePass2026!", UserRole.MEMBER, must_change_password=True)
    client.post("/api/auth/login", json={"username": "mike", "password": "MikePass2026!"})

    r = client.get("/api/auth/status")
    assert r.status_code == 200  # 不是 403——这正是 /me 做不到的事
    data = r.json()["data"]
    assert data["authenticated"] is True
    assert data["must_change_password"] is True


def test_status_tampered_cookie_is_200_unauthenticated():
    client, _us, _ss, _rl = _make_app_with_stores()
    client.cookies.set("wa_session", "forged-session-id")
    r = client.get("/api/auth/status")
    assert r.status_code == 200
    assert r.json()["data"] == {"authenticated": False}


def test_status_does_not_consume_login_rate_limit():
    """status 是只读探针：连续调用不得消耗登录限流额度。"""
    client, us, _ss, rl = _make_app_with_stores()
    us.create_user("nina", "NinaPass2026!", UserRole.MEMBER)

    for _ in range(20):
        r = client.get("/api/auth/status")
        assert r.status_code == 200

    # 限流器里没有留下任何失败计数，登录照常
    assert rl.failure_count("nina") == 0
    r = client.post("/api/auth/login", json={"username": "nina", "password": "NinaPass2026!"})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# 14. Login audit events do not contain passwords
# ---------------------------------------------------------------------------

def test_audit_events_never_contain_passwords():
    client, us, _ss, _rl = _make_app_with_stores()
    us.create_user("karen", "KarenSecret2026!", UserRole.MEMBER)

    client.post("/api/auth/login", json={"username": "karen", "password": "KarenSecret2026!"})
    client.post("/api/auth/login", json={"username": "karen", "password": "WrongPassword2026!"})

    events = auth_audit._get_events()
    for e in events:
        event_str = str(e)
        assert "KarenSecret2026!" not in event_str
        assert "WrongPassword2026!" not in event_str
        # Full username must not appear in failure events
        failure_events = [ev for ev in events if "failure" in ev.get("event", "")]
        for fe in failure_events:
            assert "karen" not in str(fe)


# ---------------------------------------------------------------------------
# 15. Admin user management flow
# ---------------------------------------------------------------------------

def test_admin_can_create_and_deactivate_user():
    import water_agent.webapp.auth.rate_limit as rl_mod
    import water_agent.webapp.auth.session_store as ss_mod
    import water_agent.webapp.auth.user_store as us_mod
    from water_agent.webapp.app import create_app

    us = UserStore()
    ss = SessionStore()
    rl = LoginRateLimiter()
    us_mod._store = us
    ss_mod._store = ss
    rl_mod._limiter = rl

    us.create_user("admin", "AdminPass2026!", UserRole.ADMIN)
    client = TestClient(create_app(), raise_server_exceptions=False)

    client.post("/api/auth/login", json={"username": "admin", "password": "AdminPass2026!"})
    csrf = client.cookies.get(CSRF_COOKIE_NAME)

    # Create a new member
    r = client.post(
        "/api/admin/users",
        json={"username": "newmember", "temporary_password": "TempMember2026!", "role": "member"},
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert r.status_code == 201
    new_id = r.json()["data"]["user"]["user_id"]

    # Deactivate
    r2 = client.patch(
        f"/api/admin/users/{new_id}/deactivate",
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert r2.status_code == 200
    assert r2.json()["data"]["deactivated"] is True


def _admin_client(store: UserStore, monkeypatch: pytest.MonkeyPatch, username: str) -> TestClient:
    """A TestClient over ``store`` that is already logged in as ``username``."""
    import water_agent.webapp.auth.rate_limit as rl_mod
    import water_agent.webapp.auth.session_store as ss_mod
    import water_agent.webapp.auth.user_store as us_mod
    from water_agent.webapp.app import create_app

    monkeypatch.setattr(us_mod, "_store", store)
    monkeypatch.setattr(ss_mod, "_store", SessionStore())
    monkeypatch.setattr(rl_mod, "_limiter", LoginRateLimiter())
    client = TestClient(create_app(), raise_server_exceptions=False)
    login = client.post("/api/auth/login", json={"username": username, "password": "AdminPass2026!"})
    assert login.status_code == 200, login.text
    return client


def _deactivate(client: TestClient, user_id: object):
    return client.patch(
        f"/api/admin/users/{user_id}/deactivate",
        headers={"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME) or ""},
    )


def test_admin_cannot_deactivate_their_own_account(monkeypatch: pytest.MonkeyPatch):
    """回归：唯一的活跃管理员能停用自己。之后 bootstrap-admin 因同名账号已存在每次退出 1，
    compose 里依赖它成功退出的 app 再也起不来。现在一律拒绝停用自己（409）。"""
    store = UserStore()
    admin = store.create_user("admin", "AdminPass2026!", UserRole.ADMIN)
    client = _admin_client(store, monkeypatch, "admin")

    refused = _deactivate(client, admin.user_id)

    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "AUTH_CANNOT_DEACTIVATE_SELF"
    assert "AUTH_CANNOT_DEACTIVATE_SELF" in AUTH_ERROR_CODES
    assert store.get_by_id(admin.user_id).is_active is True
    assert client.get("/api/admin/users").status_code == 200, "会话没有被顺手吊销"

    # 有别的管理员时同样拒绝停用自己；停用别人照常可以。
    other = store.create_user("second-admin", "AdminPass2026!", UserRole.ADMIN)
    assert _deactivate(client, admin.user_id).status_code == 409
    allowed = _deactivate(client, other.user_id)
    assert allowed.status_code == 200, allowed.text
    assert store.get_by_id(other.user_id).is_active is False


class _ConcurrentDeactivationStore(UserStore):
    """确定性地模拟两个管理员同时互相停用：对方的请求先提交，本请求的写入后到。"""

    def __init__(self) -> None:
        super().__init__()
        self.lands_first: UUID | None = None

    def deactivate(self, user_id: UUID) -> None:
        if self.lands_first is not None:
            other, self.lands_first = self.lands_first, None
            super().deactivate(other)
        super().deactivate(user_id)


def test_concurrent_deactivation_cannot_remove_the_last_active_admin(
    monkeypatch: pytest.MonkeyPatch,
):
    """管理员 A、B 同时互相停用：两边都通过了"不是自己"的检查，只有原子的
    "最后一个活跃管理员"判定能保证至少留下一个（409 AUTH_LAST_ACTIVE_ADMIN）。"""
    store = _ConcurrentDeactivationStore()
    first = store.create_user("admin", "AdminPass2026!", UserRole.ADMIN)
    second = store.create_user("second-admin", "AdminPass2026!", UserRole.ADMIN)
    client = _admin_client(store, monkeypatch, "admin")
    store.lands_first = first.user_id  # B 停用 A 的请求抢先提交

    refused = _deactivate(client, second.user_id)

    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "AUTH_LAST_ACTIVE_ADMIN"
    assert "AUTH_LAST_ACTIVE_ADMIN" in AUTH_ERROR_CODES
    assert store.get_by_id(second.user_id).is_active is True
    assert store.has_admin() is True


# ---------------------------------------------------------------------------
# 16. Login response envelope never exposes hash or subject
# ---------------------------------------------------------------------------

def test_login_response_does_not_expose_subject_or_hash():
    client, us, _ss, _rl = _make_app_with_stores()
    us.create_user("lucy", "LucyPass2026!", UserRole.MEMBER)

    r = client.post("/api/auth/login", json={"username": "lucy", "password": "LucyPass2026!"})
    assert r.status_code == 200
    body = r.text
    user = us.get_by_username("lucy")
    # Hash must not appear
    assert user.password_hash not in body
    # subject ("user:<uuid>") must not appear
    assert f"user:{user.user_id}" not in body


# ---------------------------------------------------------------------------
# review 09-28 #6: login timing oracle and audit visibility
# ---------------------------------------------------------------------------

def test_unknown_username_pays_the_same_hash_cost_as_a_wrong_password(monkeypatch) -> None:
    """"用户不存在"曾直接返回（0.5 ms），"密码错"要跑 Argon2（51 ms），响应时间即用户名探测器。

    断言计数而非耗时：两条失败路径都必须恰好执行一次 Argon2 verify。计时断言在 CI
    上天然抖动，而计数正是"成本相等"的结构性保证。
    """

    import water_agent.webapp.auth.password as password_mod

    calls: list[str] = []
    real_ph = password_mod._PH

    class _CountingHasher:
        """PasswordHasher 的属性只读，换掉整个对象：hash 照常，verify 计数后委托。"""

        def hash(self, plaintext: str) -> str:
            return real_ph.hash(plaintext)

        def verify(self, stored_hash: str, plaintext: str) -> bool:
            calls.append(stored_hash[:12])
            return real_ph.verify(stored_hash, plaintext)

        def check_needs_rehash(self, stored_hash: str) -> bool:
            return real_ph.check_needs_rehash(stored_hash)

    monkeypatch.setattr(password_mod, "_PH", _CountingHasher())
    monkeypatch.setattr(password_mod, "_DUMMY_HASH", None)
    client, users, _ss, _rl = _fresh_app()
    users.create_user("timing-real", "TimingPass2026!", UserRole.MEMBER)

    calls.clear()
    wrong = client.post("/api/auth/login", json={"username": "timing-real", "password": "WrongPass2026!!"})
    wrong_calls = len(calls)
    calls.clear()
    unknown = client.post("/api/auth/login", json={"username": "timing-ghost", "password": "WrongPass2026!!"})
    unknown_calls = len(calls)

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"] == unknown.json()["error"], "两种失败对外必须完全一样"
    assert wrong_calls == unknown_calls == 1
    # 空验用的哈希与真实哈希同参数（否则 Argon2 耗时仍不同）
    assert all(prefix.startswith("$argon2id$v=") for prefix in calls)


def test_audit_events_reach_stdout_outside_pytest() -> None:
    """uvicorn 不给 water_agent.* 配 handler，INFO 审计事件此前一条都不输出。

    用子进程跑（不经 pytest 的日志捕获），模拟"只有 uvicorn 在配置日志"的生产环境。
    """

    import subprocess
    import sys

    code = (
        "import logging, uvicorn.config;"
        "logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG);"
        "from water_agent.webapp.auth import audit;"
        "audit.audit_login_failure(reason='invalid_credentials')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env={**__import__("os").environ, "PYTHONPATH": "src"},
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, (result.stdout, result.stderr)
    import json as _json

    entry = _json.loads(lines[0])
    assert entry["event"] == "auth.login_failure"
    assert entry["reason"] == "invalid_credentials"


def test_audit_buffer_is_bounded() -> None:
    from water_agent.webapp.auth import audit

    for _ in range(audit._EVENT_BUFFER_LIMIT + 50):
        audit.audit_logout(user_id="u")
    assert len(audit._get_events()) == audit._EVENT_BUFFER_LIMIT
