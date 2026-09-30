"""P5 Step 37: authentication contract tests.

All tests use in-memory stores only — no database or network required.
Each test gets a fresh app instance with isolated stores to prevent bleed-through.
"""
from __future__ import annotations

import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from water_agent.webapp.auth import audit as auth_audit
from water_agent.webapp.auth.bootstrap import run_bootstrap
from water_agent.webapp.auth.csrf import CSRF_COOKIE_NAME
from water_agent.webapp.auth.dependencies import SESSION_COOKIE_NAME
from water_agent.webapp.auth.models import UserRole
from water_agent.webapp.auth.password import hash_password
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


# ---------------------------------------------------------------------------
# review 2026-09-30：认证加固
# ---------------------------------------------------------------------------


class _Clock:
    """可控时钟：限流器的窗口/锁定期测试不 sleep。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_login_rejects_an_overlong_username_before_touching_the_limiter():
    """用户名上限与库表列一致（128）。此前无上限：每个超长用户名的失败登录都新建一个
    限流桶且永不删除，匿名请求就能把进程内存撑爆（实测 20 次 1 MiB 用户名 ≈ 23 MiB）。"""
    client, _us, _ss, rl = _make_app_with_stores()
    r = client.post("/api/auth/login", json={"username": "x" * 129, "password": "whatever"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "WEB_INVALID_REQUEST"
    assert rl.bucket_count() == 0
    r = client.post("/api/auth/login", json={"username": "x", "password": "p" * 1025})
    assert r.status_code == 422


def test_rate_limiter_bucket_count_is_bounded():
    clock = _Clock()
    rl = LoginRateLimiter(max_failures=3, window_seconds=60, lockout_seconds=120, max_buckets=50, clock=clock)
    for index in range(500):
        rl.record_failure(f"user-{index}")
    assert rl.bucket_count() <= 50


def test_rate_limiter_forgets_idle_usernames():
    """失败记录过了窗口且不在锁定期就是空桶：访问即释放，不再常驻内存。"""
    clock = _Clock()
    rl = LoginRateLimiter(max_failures=3, window_seconds=60, lockout_seconds=120, clock=clock)
    rl.record_failure("alice")
    clock.now += 61
    assert rl.is_limited("alice") is False
    assert rl.bucket_count() == 0


def test_lockout_outlasts_the_failure_window():
    """LOCKOUT_SECONDS 此前定义了却从未使用：锁定只持续到最早一次失败滑出窗口。"""
    clock = _Clock()
    rl = LoginRateLimiter(max_failures=3, window_seconds=60, lockout_seconds=120, clock=clock)
    for _ in range(3):
        rl.record_failure("bob")
    assert rl.is_limited("bob")
    clock.now += 90  # 失败已滑出 60 s 窗口，但仍在 120 s 锁定期内
    assert rl.is_limited("bob")
    clock.now += 31
    assert rl.is_limited("bob") is False


def test_a_locked_account_survives_a_bucket_flood():
    """桶满时优先淘汰未锁定的桶：灌满用户名不能把被锁定账号的计数挤掉。"""
    clock = _Clock()
    rl = LoginRateLimiter(max_failures=3, window_seconds=60, lockout_seconds=120, max_buckets=20, clock=clock)
    for _ in range(3):
        rl.record_failure("victim")
    for index in range(200):
        rl.record_failure(f"junk-{index}")
    assert rl.is_limited("victim")


def test_login_hashing_does_not_block_the_event_loop(monkeypatch):
    """Argon2（~50 ms、64 MiB）此前直接跑在 async 路由里，单 worker 下全站停顿：
    40 个并发登录期间 /health/live 从 7 ms 变成 1.2 s（review 实测）。"""
    import asyncio
    import time

    import httpx

    import water_agent.webapp.auth.rate_limit as rl_mod
    import water_agent.webapp.auth.session_store as ss_mod
    import water_agent.webapp.auth.user_store as us_mod
    from water_agent.webapp.app import create_app
    from water_agent.webapp.auth import routes as auth_routes

    us = UserStore()
    us.create_user("slow", "SlowPassword2026!", UserRole.MEMBER)
    monkeypatch.setattr(us_mod, "_store", us)
    monkeypatch.setattr(ss_mod, "_store", SessionStore())
    monkeypatch.setattr(rl_mod, "_limiter", LoginRateLimiter())

    def slow_verify(_plaintext: str, _stored: str) -> bool:
        time.sleep(0.5)
        return False

    monkeypatch.setattr(auth_routes, "verify_password", slow_verify)
    app = create_app()

    async def scenario() -> tuple[float, int]:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            # 从发起登录之前计时：同步阻塞会冻结整个事件循环（连本协程的 sleep 也醒不来），
            # 只有这样才量得到 /health/live 被卡住的时长。
            started = time.perf_counter()
            login = asyncio.create_task(
                client.post("/api/auth/login", json={"username": "slow", "password": "wrong-password"})
            )
            await asyncio.sleep(0.05)
            health = await client.get("/health/live")
            elapsed = time.perf_counter() - started
            assert health.status_code == 200
            return elapsed, (await login).status_code

    elapsed, login_status = asyncio.run(scenario())
    assert login_status == 401
    assert elapsed < 0.3, f"登录的哈希计算阻塞了事件循环：/health/live 用了 {elapsed:.2f}s"


def test_change_password_revokes_every_other_session():
    """改密此前不吊销会话：泄露的会话在受害者改密后仍然有效（最长 8 小时）。"""
    import water_agent.webapp.auth.rate_limit as rl_mod
    import water_agent.webapp.auth.session_store as ss_mod
    import water_agent.webapp.auth.user_store as us_mod
    from water_agent.webapp.app import create_app

    us, ss = UserStore(), SessionStore()
    us_mod._store, ss_mod._store, rl_mod._limiter = us, ss, LoginRateLimiter()
    us.create_user("victim", "VictimPass2026!", UserRole.MEMBER)
    app = create_app()
    victim = TestClient(app, raise_server_exceptions=True)
    attacker = TestClient(app, raise_server_exceptions=True)
    for client in (victim, attacker):
        assert client.post("/api/auth/login", json={"username": "victim", "password": "VictimPass2026!"}).status_code == 200

    changed = victim.post(
        "/api/auth/change-pass",
        json={"current_password": "VictimPass2026!", "new_password": "RotatedVictim2026!"},
        headers={"X-CSRF-Token": victim.cookies.get(CSRF_COOKIE_NAME) or ""},
    )
    assert changed.status_code == 200, changed.text
    assert attacker.get("/api/auth/me").status_code == 401
    # 改密的这个客户端拿到新会话，继续可用
    assert victim.get("/api/auth/me").status_code == 200
    assert len(ss.list_for_user(us.get_by_username("victim").user_id)) == 1


def test_admin_reset_revokes_sessions_even_after_the_user_sets_a_new_password():
    """此前管理员重置后旧会话只是暂时 403；受害者用临时密码登录并改密后，攻击者的旧会话又变回 200。"""
    import water_agent.webapp.auth.rate_limit as rl_mod
    import water_agent.webapp.auth.session_store as ss_mod
    import water_agent.webapp.auth.user_store as us_mod
    from water_agent.webapp.app import create_app

    us, ss = UserStore(), SessionStore()
    us_mod._store, ss_mod._store, rl_mod._limiter = us, ss, LoginRateLimiter()
    us.create_user("root", "RootAdminPass2026!", UserRole.ADMIN)
    victim_id = us.create_user("carol", "CarolPass2026!", UserRole.MEMBER).user_id
    app = create_app()
    admin, attacker, carol = (TestClient(app, raise_server_exceptions=True) for _ in range(3))
    assert admin.post("/api/auth/login", json={"username": "root", "password": "RootAdminPass2026!"}).status_code == 200
    assert attacker.post("/api/auth/login", json={"username": "carol", "password": "CarolPass2026!"}).status_code == 200

    reset = admin.post(
        f"/api/admin/users/{victim_id}/reset-password",
        json={"temporary_password": "TemporaryCarol2026!"},
        headers={"X-CSRF-Token": admin.cookies.get(CSRF_COOKIE_NAME) or ""},
    )
    assert reset.status_code == 200
    assert attacker.get("/api/auth/me").status_code == 401

    assert carol.post("/api/auth/login", json={"username": "carol", "password": "TemporaryCarol2026!"}).status_code == 200
    assert carol.post(
        "/api/auth/change-pass",
        json={"current_password": "TemporaryCarol2026!", "new_password": "CarolRotated2026!"},
        headers={"X-CSRF-Token": carol.cookies.get(CSRF_COOKIE_NAME) or ""},
    ).status_code == 200
    assert attacker.get("/api/auth/me").status_code == 401


def test_admin_routes_refuse_a_temporary_password_admin():
    """require_admin 此前不查 must_change_password：拿着临时密码的管理员不改密就能建管理员、
    改模型 Key、重置 root 密码。"""
    client, us, _ss, _rl = _make_app_with_stores()
    us.create_user("ops", "OpsTemporary2026!", UserRole.ADMIN, must_change_password=True)
    assert client.post("/api/auth/login", json={"username": "ops", "password": "OpsTemporary2026!"}).status_code == 200
    csrf = client.cookies.get(CSRF_COOKIE_NAME) or ""
    listed = client.get("/api/admin/users")
    assert listed.status_code == 403
    assert listed.json()["error"]["code"] == "AUTH_MUST_CHANGE_PASSWORD"
    created = client.post(
        "/api/admin/users",
        json={"username": "shadow", "temporary_password": "ShadowAdmin2026!", "role": "admin"},
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 403
    credentials = client.put(
        "/api/settings/credentials/DEEPSEEK_API_KEY",
        json={"value": "sk-overwritten-by-a-temporary-admin"},
        headers={"X-CSRF-Token": csrf},
    )
    assert credentials.status_code == 403


def test_change_password_refuses_to_keep_the_current_password():
    """强制改密时把临时密码原样填回去，等于没改。前端已拦，这里是 API 层的同一条规则。"""
    client, us, _ss, _rl = _make_app_with_stores()
    us.create_user("dan", "DanTemporary2026!", UserRole.MEMBER, must_change_password=True)
    client.post("/api/auth/login", json={"username": "dan", "password": "DanTemporary2026!"})
    r = client.post(
        "/api/auth/change-pass",
        json={"current_password": "DanTemporary2026!", "new_password": "DanTemporary2026!"},
        headers={"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME) or ""},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "AUTH_PASSWORD_REUSED"
    assert client.get("/api/auth/me").status_code == 403  # 仍须改密


def test_change_password_wrong_current_password_is_rate_limited():
    """拿到会话的人不能无限次猜当前密码（此前 8 次都是 401、没有 429）。"""
    client, us, _ss, _rl = _make_app_with_stores()  # 限流器 max_failures=3
    us.create_user("erin", "ErinPassword2026!", UserRole.MEMBER)
    client.post("/api/auth/login", json={"username": "erin", "password": "ErinPassword2026!"})
    headers = {"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME) or ""}
    codes = [
        client.post(
            "/api/auth/change-pass",
            json={"current_password": f"guess-{i}-attempt", "new_password": "ErinRotated2026!"},
            headers=headers,
        ).status_code
        for i in range(4)
    ]
    assert codes == [401, 401, 401, 429]


def test_admin_create_user_validates_the_username():
    """纯空白用户名此前也能建出来（201）；超过 128 字符在 PG 上会 DataError 变 500。"""
    client, us, _ss, _rl = _make_app_with_stores()
    us.create_user("admin2", "AdminTwoPass2026!", UserRole.ADMIN)
    client.post("/api/auth/login", json={"username": "admin2", "password": "AdminTwoPass2026!"})
    headers = {"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME) or ""}
    for username in ("   ", "", "u" * 129):
        r = client.post(
            "/api/admin/users",
            json={"username": username, "temporary_password": "TempMember2026!", "role": "member"},
            headers=headers,
        )
        assert r.status_code == 422, (username, r.text)
    ok = client.post(
        "/api/admin/users",
        json={"username": "new member", "temporary_password": "TempMember2026!", "role": "member"},
        headers=headers,
    )
    assert ok.status_code == 201
