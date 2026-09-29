"""P5: server-side authorization for every Web resource.

Every test drives the real FastAPI app over a SQLite database: no network and no
real model.  Resource ids are selectors only — the repositories decide access,
so a guessed id must never widen the caller's scope, and a request body must
never be able to claim an identity.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select

from water_agent.observability.json_trace import JsonTraceRecorder
from water_agent.storage.database import Database
from water_agent.storage.models import Base, FeedbackCandidateRow, Project, WebSession
from water_agent.storage.repository import (
    ProjectRepository,
    RunRepository,
    WebSessionRepository,
)
from water_agent.webapp.app import create_app
from water_agent.webapp.auth.csrf import CSRF_COOKIE_NAME
from water_agent.webapp.auth.db_store import PostgresSessionStore, PostgresUserStore
from water_agent.webapp.auth.models import UserRole

_PASSWORD = "AuthorizationPass2026!"
_PROJECT_TITLE = "绝密泵房改造"
_SESSION_TITLE = "绝密校核会话"
_PERSONAL_TITLE = "绝密个人会话"

# GET routes that only require an authenticated session.
READ_ROUTES = (
    ("GET", "/api/projects"),
    ("GET", "/api/projects/{project_id}"),
    ("GET", "/api/projects/{project_id}/sessions"),
    ("GET", "/api/sessions"),
    ("GET", "/api/sessions/{thread_id}"),
    ("GET", "/api/runs/{run_id}/events"),
    ("GET", "/api/runs/{run_id}/trace"),
)

# State-mutating routes: all carry the CSRF dependency.
WRITE_ROUTES = (
    ("POST", "/api/projects", {"name": "新项目"}),
    ("POST", "/api/projects/{project_id}/sessions", {"title": "新会话"}),
    ("POST", "/api/sessions", {"title": "新会话"}),
    ("POST", "/api/sessions/{thread_id}/messages", {"message": "问题"}),
    ("POST", "/api/runs/{run_id}/cancel", None),
    ("POST", "/api/runs/{run_id}/feedback", {"rating": "useful"}),
)


class _FakeSession:
    """AgentSession double: no model and no network, only a trace recorder."""

    def __init__(self, recorder: JsonTraceRecorder) -> None:
        self.recorder = recorder

    def start_trace(self, run_id: str, query: str) -> None:
        self.recorder.start(run_id=run_id, trace_id=f"trace-{run_id}", query=query)

    async def aask(
        self,
        query: str,
        *,
        run_id: str,
        project_id: object,
        memory_context: object,
        on_token,
        on_event,
        cancel_check,
    ) -> dict:
        del query, run_id, project_id, memory_context, cancel_check
        on_event("values", {"status": "running", "messages": []})
        on_token("授权用例回答")
        self.recorder.finish("succeeded")
        return {
            "status": "succeeded",
            "answer": "授权用例回答",
            "citations": [],
            "errors": [],
            "warnings": [],
        }


class _FakeFactory:
    """Factory double so authorization tests can own a real, managed run id."""

    def __init__(self, trace_dir: Path) -> None:
        self.trace_dir = trace_dir

    def create(self, *, run_id, thread_id, user_id, principal, project_id, stack):
        del run_id, thread_id, user_id, principal, project_id, stack
        return _FakeSession(JsonTraceRecorder(self.trace_dir))


@pytest.fixture
def database(tmp_path: Path) -> Database:
    engine = create_engine(f"sqlite:///{tmp_path / 'authorization.sqlite'}")

    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    database = Database(engine=engine)
    yield database
    database.dispose()


def _new_client(
    database: Database,
    tmp_path: Path,
    *,
    session_factory: object | None = None,
) -> TestClient:
    app = create_app(
        static_dir=tmp_path,
        database=database,
        session_factory=session_factory,
        memory_enabled=False,
        trace_dir=tmp_path / "traces",
        session_dir=tmp_path / "sessions",
    )
    return TestClient(app, raise_server_exceptions=True)


def _login(
    database: Database,
    tmp_path: Path,
    username: str,
    *,
    role: UserRole = UserRole.MEMBER,
    must_change_password: bool = False,
    session_factory: object | None = None,
) -> tuple[TestClient, UUID]:
    user = PostgresUserStore(database).create_user(
        username, _PASSWORD, role, must_change_password=must_change_password
    )
    client = _new_client(database, tmp_path, session_factory=session_factory)
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": _PASSWORD},
    )
    assert response.status_code == 200, response.text
    return client, user.user_id


def _csrf(client: TestClient) -> str:
    value = client.cookies.get(CSRF_COOKIE_NAME)
    assert value
    return value


def _seed_owned_resources(database: Database, owner_id: UUID) -> dict[str, str]:
    """Create project/session/run rows owned by ``owner_id`` through repositories."""

    project = ProjectRepository(database).create(owner_id, _PROJECT_TITLE)
    sessions = WebSessionRepository(database)
    project_session = sessions.create(
        owner_id, project_id=project.project_id, title=_SESSION_TITLE
    )
    personal_session = sessions.create(owner_id, title=_PERSONAL_TITLE)
    run = RunRepository(database).create(project_session.thread_id, owner_id)
    return {
        "project_id": project.project_id,
        "thread_id": project_session.thread_id,
        "personal_thread_id": personal_session.thread_id,
        "run_id": run.run_id,
    }


def _url(template: str, ids: dict[str, str]) -> str:
    return template.format(**ids)


def _parse_sse(response) -> list[dict]:
    events: list[dict] = []
    for block in response.text.strip().split("\n\n"):
        data_lines = [line[6:] for line in block.splitlines() if line.startswith("data: ")]
        if data_lines:
            events.append(json.loads("\n".join(data_lines)))
    return events


# ---------------------------------------------------------------------------
# 1. Anonymous access is refused for every protected route
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,template", READ_ROUTES)
def test_anonymous_read_returns_401_without_leaking_titles(
    database: Database, tmp_path: Path, method: str, template: str
) -> None:
    _, owner_id = _login(database, tmp_path, f"owner-{uuid4()}")
    ids = _seed_owned_resources(database, owner_id)
    anonymous = _new_client(database, tmp_path)

    response = anonymous.request(method, _url(template, ids))

    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "AUTH_SESSION_INVALID"
    for title in (_PROJECT_TITLE, _SESSION_TITLE, _PERSONAL_TITLE):
        assert title not in response.text


@pytest.mark.parametrize("method,template,body", WRITE_ROUTES)
def test_anonymous_write_is_refused_with_401(
    database: Database, tmp_path: Path, method: str, template: str, body: dict | None
) -> None:
    _, owner_id = _login(database, tmp_path, f"owner-{uuid4()}")
    ids = _seed_owned_resources(database, owner_id)
    anonymous = _new_client(database, tmp_path)
    url = _url(template, ids)

    # A bare unauthenticated call is answered by the CSRF dependency first (the
    # mutation never reaches the route); a self-chosen CSRF pair, which any raw
    # client can fabricate, still has to face the session check.
    flagged = anonymous.request(method, url, json=body)
    assert flagged.status_code == 403, flagged.text
    assert flagged.json()["error"]["code"] == "AUTH_CSRF_INVALID"

    forged = f"forged-csrf-{uuid4().hex}"
    forged_client = _new_client(database, tmp_path)
    forged_client.cookies.set(CSRF_COOKIE_NAME, forged)
    unauthorised = forged_client.request(
        method,
        url,
        json=body,
        headers={"X-CSRF-Token": forged},
    )
    assert unauthorised.status_code == 401, unauthorised.text
    assert unauthorised.json()["error"]["code"] == "AUTH_SESSION_INVALID"
    for title in (_PROJECT_TITLE, _SESSION_TITLE, _PERSONAL_TITLE):
        assert title not in unauthorised.text


# ---------------------------------------------------------------------------
# 2. CSRF is enforced on every state-mutating route
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,template,body", WRITE_ROUTES)
def test_mutating_route_without_csrf_header_returns_403(
    database: Database, tmp_path: Path, method: str, template: str, body: dict | None
) -> None:
    client, owner_id = _login(database, tmp_path, f"csrf-{uuid4()}")
    ids = _seed_owned_resources(database, owner_id)
    url = _url(template, ids)

    response = client.request(method, url, json=body)

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "AUTH_CSRF_INVALID"

    # The same call with the header passes the CSRF gate; the route's own
    # outcome decides the result and it is never a CSRF refusal.
    passed = client.request(method, url, json=body, headers={"X-CSRF-Token": _csrf(client)})
    assert passed.json().get("error", {}).get("code") != "AUTH_CSRF_INVALID"


# ---------------------------------------------------------------------------
# 3. Cross-user resource ids answer 404 and never leak server-side content
# ---------------------------------------------------------------------------

def test_cross_user_ids_answer_404_and_reveal_nothing(
    database: Database, tmp_path: Path
) -> None:
    factory = _FakeFactory(tmp_path / "traces")
    owner_client, owner_id = _login(
        database, tmp_path, f"run-owner-{uuid4()}", session_factory=factory
    )
    outsider_client, _ = _login(database, tmp_path, f"outsider-{uuid4()}")
    ids = _seed_owned_resources(database, owner_id)

    created = owner_client.post(
        f"/api/sessions/{ids['thread_id']}/messages",
        json={"message": "授权用例"},
        headers={"X-CSRF-Token": _csrf(owner_client)},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["run_id"]

    attempts = (
        ("GET", f"/api/projects/{ids['project_id']}", None),
        ("GET", f"/api/projects/{ids['project_id']}/sessions", None),
        ("POST", f"/api/projects/{ids['project_id']}/sessions", {"title": "越权会话"}),
        ("GET", f"/api/sessions/{ids['thread_id']}", None),
        ("POST", f"/api/sessions/{ids['thread_id']}/messages", {"message": "越权提问"}),
        ("GET", f"/api/runs/{run_id}/events", None),
        ("GET", f"/api/runs/{run_id}/trace", None),
        ("POST", f"/api/runs/{run_id}/cancel", None),
        ("POST", f"/api/runs/{run_id}/feedback", {"rating": "useful"}),
    )
    for method, url, body in attempts:
        response = outsider_client.request(
            method, url, json=body, headers={"X-CSRF-Token": _csrf(outsider_client)}
        )
        assert response.status_code == 404, (method, url, response.text)
        assert response.json()["error"]["code"] == "WEB_NOT_FOUND"
        for title in (_PROJECT_TITLE, _SESSION_TITLE, _PERSONAL_TITLE):
            assert title not in response.text

    with database.transaction() as db_session:
        assert db_session.execute(select(FeedbackCandidateRow)).scalars().all() == []
        assert db_session.execute(select(WebSession)).scalars().all() != []

    # The same ids are reachable by their real owner, so the 404 above is an
    # authorization decision and not a broken route.
    assert owner_client.get(f"/api/projects/{ids['project_id']}").status_code == 200
    assert owner_client.get(f"/api/sessions/{ids['thread_id']}").status_code == 200
    events = owner_client.get(f"/api/runs/{run_id}/events")
    assert events.status_code == 200
    # 完整事件流：终态 finalize 后跟显式 stream_end（review 09-28 #4 修复后才真正发出）。
    assert [item["type"] for item in _parse_sse(events)][-2:] == ["finalize", "stream_end"]
    assert owner_client.get(f"/api/runs/{run_id}/trace").status_code == 200
    assert (
        owner_client.post(
            f"/api/runs/{run_id}/feedback",
            json={"rating": "useful"},
            headers={"X-CSRF-Token": _csrf(owner_client)},
        ).status_code
        == 201
    )


def test_cancelling_another_users_run_is_refused(
    database: Database, tmp_path: Path
) -> None:
    factory = _FakeFactory(tmp_path / "traces")
    owner_client, owner_id = _login(
        database, tmp_path, f"cancel-owner-{uuid4()}", session_factory=factory
    )
    outsider_client, _ = _login(database, tmp_path, f"cancel-outsider-{uuid4()}")
    thread_id = WebSessionRepository(database).create(owner_id, title=_PERSONAL_TITLE).thread_id
    created = owner_client.post(
        f"/api/sessions/{thread_id}/messages",
        json={"message": "授权用例"},
        headers={"X-CSRF-Token": _csrf(owner_client)},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["run_id"]

    refused = outsider_client.post(
        f"/api/runs/{run_id}/cancel",
        headers={"X-CSRF-Token": _csrf(outsider_client)},
    )
    assert refused.status_code == 404, refused.text
    assert refused.json()["error"]["code"] == "WEB_NOT_FOUND"
    assert _PERSONAL_TITLE not in refused.text

    accepted = owner_client.post(
        f"/api/runs/{run_id}/cancel",
        headers={"X-CSRF-Token": _csrf(owner_client)},
    )
    assert accepted.status_code == 202, accepted.text


# ---------------------------------------------------------------------------
# 4. Project membership gates project-scoped reads and writes
# ---------------------------------------------------------------------------

def test_project_membership_grants_read_but_viewer_role_cannot_write(
    database: Database, tmp_path: Path
) -> None:
    _, owner_id = _login(database, tmp_path, f"owner-{uuid4()}")
    member_client, member_id = _login(database, tmp_path, f"member-{uuid4()}")
    viewer_client, viewer_id = _login(database, tmp_path, f"viewer-{uuid4()}")
    outsider_client, _ = _login(database, tmp_path, f"outsider-{uuid4()}")

    projects = ProjectRepository(database)
    project = projects.create(owner_id, _PROJECT_TITLE)
    session = WebSessionRepository(database).create(
        owner_id, project_id=project.project_id, title=_SESSION_TITLE
    )
    projects.add_member(project.project_id, owner_id, member_id, role="member")
    projects.add_member(project.project_id, owner_id, viewer_id, role="viewer")

    member_project = member_client.get(f"/api/projects/{project.project_id}")
    assert member_project.status_code == 200
    assert member_project.json()["data"]["role"] == "member"
    member_sessions = member_client.get(f"/api/projects/{project.project_id}/sessions")
    assert member_sessions.status_code == 200
    assert [item["thread_id"] for item in member_sessions.json()["data"]["sessions"]] == [
        session.thread_id
    ]
    assert member_client.get(f"/api/sessions/{session.thread_id}").status_code == 200
    member_created = member_client.post(
        f"/api/projects/{project.project_id}/sessions",
        json={"title": "成员新会话"},
        headers={"X-CSRF-Token": _csrf(member_client)},
    )
    assert member_created.status_code == 201, member_created.text

    # A non-member is answered 404 (existence is not disclosed), never 403.
    for url in (
        f"/api/projects/{project.project_id}",
        f"/api/projects/{project.project_id}/sessions",
        f"/api/sessions/{session.thread_id}",
    ):
        refused = outsider_client.get(url)
        assert refused.status_code == 404, refused.text
        assert refused.json()["error"]["code"] == "WEB_NOT_FOUND"
        assert _PROJECT_TITLE not in refused.text
    assert (
        outsider_client.post(
            f"/api/projects/{project.project_id}/sessions",
            json={"title": "越权会话"},
            headers={"X-CSRF-Token": _csrf(outsider_client)},
        ).status_code
        == 404
    )

    # _can_write_project("viewer", UserRole.MEMBER) is False: the viewer still
    # reads the project but cannot add a project session.
    viewer_project = viewer_client.get(f"/api/projects/{project.project_id}")
    assert viewer_project.status_code == 200
    assert viewer_project.json()["data"]["role"] == "viewer"
    viewer_write = viewer_client.post(
        f"/api/projects/{project.project_id}/sessions",
        json={"title": "viewer 会话"},
        headers={"X-CSRF-Token": _csrf(viewer_client)},
    )
    assert viewer_write.status_code == 403, viewer_write.text
    assert viewer_write.json()["error"]["code"] == "WEB_FORBIDDEN"

    # The project-scoped viewer role only gates project writes; a personal
    # session belongs to no project, so it is still allowed.
    personal = viewer_client.post(
        "/api/sessions",
        json={"title": "viewer 个人会话"},
        headers={"X-CSRF-Token": _csrf(viewer_client)},
    )
    assert personal.status_code == 201, personal.text
    assert personal.json()["data"]["project_id"] is None


def test_global_viewer_role_cannot_create_projects_or_sessions(
    database: Database, tmp_path: Path
) -> None:
    viewer_client, _ = _login(
        database, tmp_path, f"global-viewer-{uuid4()}", role=UserRole.VIEWER
    )

    assert viewer_client.get("/api/projects").status_code == 200
    project = viewer_client.post(
        "/api/projects",
        json={"name": "viewer 项目"},
        headers={"X-CSRF-Token": _csrf(viewer_client)},
    )
    assert project.status_code == 403, project.text
    assert project.json()["error"]["code"] == "WEB_FORBIDDEN"
    session = viewer_client.post(
        "/api/sessions",
        json={"title": "viewer 会话"},
        headers={"X-CSRF-Token": _csrf(viewer_client)},
    )
    assert session.status_code == 403, session.text
    assert session.json()["error"]["code"] == "WEB_FORBIDDEN"


# ---------------------------------------------------------------------------
# 5. A deactivated account is refused mid-session
# ---------------------------------------------------------------------------

def test_deactivated_account_is_refused_and_session_is_not_upgraded(
    database: Database, tmp_path: Path
) -> None:
    client, user_id = _login(database, tmp_path, f"deactivated-{uuid4()}")
    session_id = client.cookies.get("wa_session")
    assert session_id
    assert PostgresSessionStore(database).get(session_id).user_id == user_id
    assert client.get("/api/projects").status_code == 200

    PostgresUserStore(database).set_active(user_id, active=False)

    read = client.get("/api/projects")
    assert read.status_code == 403, read.text
    assert read.json()["error"]["code"] == "AUTH_USER_INACTIVE"
    me = client.get("/api/auth/me")
    assert me.status_code == 403
    assert me.json()["error"]["code"] == "AUTH_USER_INACTIVE"
    write = client.post(
        "/api/sessions",
        json={"title": "停用后仍然写入"},
        headers={"X-CSRF-Token": _csrf(client)},
    )
    assert write.status_code == 403
    assert write.json()["error"]["code"] == "AUTH_USER_INACTIVE"

    # The session row still exists (nothing was silently upgraded to a valid
    # identity) and becomes usable again only after the account is enabled.
    assert PostgresSessionStore(database).get(session_id).user_id == user_id
    PostgresUserStore(database).set_active(user_id, active=True)
    assert client.get("/api/projects").status_code == 200


# ---------------------------------------------------------------------------
# 6. must_change_password gates every route except change-pass
# ---------------------------------------------------------------------------

def test_must_change_password_gate_blocks_routes_but_allows_change_pass(
    database: Database, tmp_path: Path
) -> None:
    client, user_id = _login(
        database,
        tmp_path,
        f"temp-password-{uuid4()}",
        must_change_password=True,
    )
    assert client.get("/api/auth/me").status_code == 403

    ids = _seed_owned_resources(database, user_id)
    for method, template, body in WRITE_ROUTES:
        path = _url(template, ids)
        response = client.request(
            method, path, json=body, headers={"X-CSRF-Token": _csrf(client)}
        )
        assert response.status_code == 403, (method, path, response.text)
        assert response.json()["error"]["code"] == "AUTH_MUST_CHANGE_PASSWORD"

    assert client.get("/api/projects").status_code == 403
    assert client.get("/api/projects").json()["error"]["code"] == "AUTH_MUST_CHANGE_PASSWORD"

    changed = client.post(
        "/api/auth/change-pass",
        json={"current_password": _PASSWORD, "new_password": "RotatedAuthPass2026!"},
        headers={"X-CSRF-Token": _csrf(client)},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["data"]["changed"] is True

    assert client.get("/api/projects").status_code == 200
    assert client.get("/api/auth/me").json()["data"]["user"]["must_change_password"] is False


# ---------------------------------------------------------------------------
# 7. Admin routes require the admin role
# ---------------------------------------------------------------------------

def test_admin_routes_require_the_admin_role(
    database: Database, tmp_path: Path
) -> None:
    member_client, member_id = _login(database, tmp_path, f"member-{uuid4()}")
    admin_client, admin_id = _login(
        database, tmp_path, f"admin-{uuid4()}", role=UserRole.ADMIN
    )

    refused = member_client.get("/api/admin/users")
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "AUTH_FORBIDDEN"
    assert str(admin_id) not in refused.text

    allowed = admin_client.get("/api/admin/users")
    assert allowed.status_code == 200, allowed.text
    listed = {item["user_id"] for item in allowed.json()["data"]["users"]}
    assert {str(member_id), str(admin_id)} <= listed


# ---------------------------------------------------------------------------
# 8. The Principal is built server-side; bodies cannot claim an identity
# ---------------------------------------------------------------------------

_IDENTITY_CLAIMS = ("user_id", "principal", "role", "tenant_id", "capabilities")


def test_identity_fields_in_request_bodies_are_rejected(
    database: Database, tmp_path: Path
) -> None:
    client, user_id = _login(database, tmp_path, f"identity-{uuid4()}")
    ids = _seed_owned_resources(database, user_id)

    endpoints = (
        ("POST", "/api/projects", {"name": "身份用例"}),
        ("POST", "/api/sessions", {"title": "身份用例"}),
        ("POST", "/api/projects/{project_id}/sessions", {"title": "身份用例"}),
        ("POST", "/api/sessions/{thread_id}/messages", {"message": "身份用例"}),
        ("POST", "/api/runs/{run_id}/feedback", {"rating": "useful"}),
    )
    for method, template, body in endpoints:
        url = _url(template, ids)
        for field in _IDENTITY_CLAIMS:
            injected = str(uuid4())
            response = client.request(
                method,
                url,
                json={**body, field: injected},
                headers={"X-CSRF-Token": _csrf(client)},
            )
            assert response.status_code == 422, (url, field, response.text)
            assert response.json()["error"]["code"] == "WEB_INVALID_REQUEST"
            assert injected not in response.text

    # The same routes answer normally without the claim, and the created rows
    # belong to the caller according to the database — the responses never carry
    # an owner/identity field a client could have supplied.
    project = client.post(
        "/api/projects",
        json={"name": "身份用例"},
        headers={"X-CSRF-Token": _csrf(client)},
    )
    assert project.status_code == 201, project.text
    assert project.json()["data"]["role"] == "owner"
    with database.transaction() as db_session:
        owner_row = db_session.execute(
            select(Project).where(Project.project_id == project.json()["data"]["project_id"])
        ).scalar_one()
        assert owner_row.owner_user_id == str(user_id)

    created = client.post(
        "/api/sessions",
        json={"title": "身份用例"},
        headers={"X-CSRF-Token": _csrf(client)},
    )
    assert created.status_code == 201, created.text
    payload = created.json()["data"]
    assert set(payload) == {
        "thread_id",
        "project_id",
        "title",
        "created_at",
        "updated_at",
        "runs",
        "latest_run",
    }
    with database.transaction() as db_session:
        row = db_session.execute(
            select(WebSession).where(WebSession.thread_id == payload["thread_id"])
        ).scalar_one()
        assert row.owner_user_id == str(user_id)
