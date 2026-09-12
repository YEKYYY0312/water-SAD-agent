"""P5 Step 37: authentication contract tests.

All tests use in-memory stores only — no database or network required.
Each test gets a fresh app instance with isolated stores to prevent bleed-through.
"""
from __future__ import annotations

import secrets
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from water_agent.webapp.auth import audit as auth_audit
from water_agent.webapp.auth.bootstrap import run_bootstrap
from water_agent.webapp.auth.dependencies import SESSION_COOKIE_NAME
from water_agent.webapp.auth.models import UserRole
from water_agent.webapp.auth.password import hash_password
from water_agent.webapp.auth.rate_limit import LoginRateLimiter
from water_agent.webapp.auth.session_store import SessionStore
from water_agent.webapp.auth.user_store import UserStore
from water_agent.webapp.auth.csrf import CSRF_COOKIE_NAME


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
    import water_agent.webapp.auth.user_store as us_mod
    import water_agent.webapp.auth.session_store as ss_mod
    import water_agent.webapp.auth.rate_limit as rl_mod

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
    import water_agent.webapp.auth.user_store as us_mod
    import water_agent.webapp.auth.session_store as ss_mod
    import water_agent.webapp.auth.rate_limit as rl_mod
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
    import water_agent.webapp.auth.user_store as us_mod
    original = us_mod._store
    us_mod._store = UserStore()

    secret = tmp_path / "admin_password"
    secret.write_text("SecureBootstrapPass!2026", encoding="utf-8")

    result = run_bootstrap(secret_path=secret)
    assert result == 0
    assert us_mod._store.has_admin()

    events = auth_audit._get_events()
    assert any(e["event"] == "auth.bootstrap_admin_created" for e in events)

    us_mod._store = original


# ---------------------------------------------------------------------------
# 3. Bootstrap is idempotent — second call skips
# ---------------------------------------------------------------------------

def test_bootstrap_is_idempotent(tmp_path: Path):
    import water_agent.webapp.auth.user_store as us_mod
    original = us_mod._store
    us_mod._store = UserStore()

    secret = tmp_path / "admin_password"
    secret.write_text("SecureBootstrapPass!2026", encoding="utf-8")

    run_bootstrap(secret_path=secret)
    admin1_id = list(us_mod._store._by_id.values())[0].user_id

    result = run_bootstrap(secret_path=secret)
    assert result == 0
    # Still only one user
    assert len(us_mod._store._by_id) == 1
    assert list(us_mod._store._by_id.values())[0].user_id == admin1_id

    skipped = [e for e in auth_audit._get_events() if e["event"] == "auth.bootstrap_admin_skipped"]
    assert len(skipped) == 1

    us_mod._store = original


# ---------------------------------------------------------------------------
# 4. Fixed session id rejected (session must be server-generated)
# ---------------------------------------------------------------------------

def test_fixed_session_id_is_rejected():
    client, us, ss, rl = _make_app_with_stores()
    us.create_user("alice", "GoodPassword2026!", UserRole.MEMBER)
    # Attempt to authenticate with a hand-crafted session cookie
    client.cookies.set(SESSION_COOKIE_NAME, "attacker-chosen-session-id")
    r = client.get("/api/auth/me", cookies={SESSION_COOKIE_NAME: "attacker-chosen-session-id"})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 5. Cookie tampering fails
# ---------------------------------------------------------------------------

def test_tampered_session_cookie_is_rejected():
    client, us, ss, rl = _make_app_with_stores()
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
    client, us, ss, rl = _make_app_with_stores()
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
    import water_agent.webapp.auth.user_store as us_mod
    import water_agent.webapp.auth.session_store as ss_mod
    import water_agent.webapp.auth.rate_limit as rl_mod
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
    client, us, ss, rl = _make_app_with_stores()
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
    client, us, ss, rl = _make_app_with_stores()
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
    client, us, ss, rl = _make_app_with_stores()
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
    client, us, ss, rl = _make_app_with_stores()
    us.create_user("frank", "FrankPass2026!", UserRole.MEMBER)

    client.post("/api/auth/login", json={"username": "frank", "password": "FrankPass2026!"})
    me = client.get("/api/auth/me")
    assert me.status_code == 200

    user = us.get_by_username("frank")
    assert user.password_hash not in me.text
    assert "FrankPass2026!" not in me.text


def test_argon2id_hash_is_not_plaintext():
    from water_agent.webapp.auth.password import hash_password, verify_password
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
    client, us, ss, rl = _make_app_with_stores()
    us.create_user("grace", "GracePass2026!", UserRole.MEMBER)
    client.post("/api/auth/login", json={"username": "grace", "password": "GracePass2026!"})

    # POST logout without CSRF header
    r = client.post("/api/auth/logout")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "AUTH_CSRF_INVALID"


def test_logout_with_correct_csrf_succeeds():
    client, us, ss, rl = _make_app_with_stores()
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
    client, us, ss, rl = _make_app_with_stores()
    us.create_user("ivan", "IvanPass2026!", UserRole.MEMBER, must_change_password=True)

    client.post("/api/auth/login", json={"username": "ivan", "password": "IvanPass2026!"})
    r = client.get("/api/auth/me")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "AUTH_MUST_CHANGE_PASSWORD"


def test_must_change_password_allows_change_pass():
    client, us, ss, rl = _make_app_with_stores()
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
# 14. Login audit events do not contain passwords
# ---------------------------------------------------------------------------

def test_audit_events_never_contain_passwords():
    client, us, ss, rl = _make_app_with_stores()
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
    import water_agent.webapp.auth.user_store as us_mod
    import water_agent.webapp.auth.session_store as ss_mod
    import water_agent.webapp.auth.rate_limit as rl_mod
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
    from uuid import UUID
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
    client, us, ss, rl = _make_app_with_stores()
    us.create_user("lucy", "LucyPass2026!", UserRole.MEMBER)

    r = client.post("/api/auth/login", json={"username": "lucy", "password": "LucyPass2026!"})
    assert r.status_code == 200
    body = r.text
    user = us.get_by_username("lucy")
    # Hash must not appear
    assert user.password_hash not in body
    # subject ("user:<uuid>") must not appear
    assert f"user:{user.user_id}" not in body
