"""P5: Web session persistence and cross-client / cross-process boundaries.

Sessions are rows, not browser state: a new app built over the same Database must
restore them, two clients in one process must not share one, and expiry/logout
must be decided and recorded by the server.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event

from water_agent.storage.database import Database
from water_agent.storage.models import AuthSession, Base
from water_agent.storage.repository import (
    ProjectRepository,
    ResourceNotFoundError,
    RunRepository,
    WebSessionRepository,
)
from water_agent.webapp.app import create_app
from water_agent.webapp.auth.csrf import CSRF_COOKIE_NAME
from water_agent.webapp.auth.db_store import PostgresSessionStore, PostgresUserStore
from water_agent.webapp.auth.dependencies import SESSION_COOKIE_NAME
from water_agent.webapp.auth.models import UserRole
from water_agent.webapp.auth.session_store import SessionNotFoundError

_PASSWORD = "SessionPass2026!"


@pytest.fixture
def database(tmp_path: Path) -> Database:
    engine = create_engine(f"sqlite:///{tmp_path / 'sessions.sqlite'}")

    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    database = Database(engine=engine)
    yield database
    database.dispose()


def _new_client(database: Database, tmp_path: Path) -> TestClient:
    app = create_app(
        static_dir=tmp_path,
        database=database,
        memory_enabled=False,
        trace_dir=tmp_path / "traces",
        session_dir=tmp_path / "sessions",
    )
    return TestClient(app, raise_server_exceptions=True)


def _login(database: Database, tmp_path: Path, username: str) -> tuple[TestClient, UUID]:
    user = PostgresUserStore(database).create_user(username, _PASSWORD, UserRole.MEMBER)
    client = _new_client(database, tmp_path)
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": _PASSWORD},
    )
    assert response.status_code == 200, response.text
    return client, user.user_id


def _sign_in(database: Database, tmp_path: Path, username: str) -> TestClient:
    """Log an existing user in on a freshly built app (the restart case)."""

    client = _new_client(database, tmp_path)
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": _PASSWORD},
    )
    assert response.status_code == 200, response.text
    return client


def _csrf(client: TestClient) -> str:
    value = client.cookies.get(CSRF_COOKIE_NAME)
    assert value
    return value


def _session_rows(database: Database, session_id: str) -> AuthSession | None:
    with database.transaction() as db_session:
        return db_session.get(AuthSession, session_id)


# ---------------------------------------------------------------------------
# 1. A personal session is restored by its owner after the app is rebuilt
# ---------------------------------------------------------------------------

def test_personal_session_survives_a_new_app_over_the_same_database(
    database: Database, tmp_path: Path
) -> None:
    username = f"restart-{uuid4()}"
    client, user_id = _login(database, tmp_path, username)
    created = client.post(
        "/api/sessions",
        json={"title": "重启后仍在的会话"},
        headers={"X-CSRF-Token": _csrf(client)},
    )
    assert created.status_code == 201, created.text
    thread_id = created.json()["data"]["thread_id"]
    assert created.json()["data"]["project_id"] is None

    # A second create_app() + TestClient over the same Database stands in for a
    # process restart: the browser cookie is gone, the session must not be.
    restarted = _sign_in(database, tmp_path, username)

    restored = restarted.get(f"/api/sessions/{thread_id}")
    assert restored.status_code == 200, restored.text
    assert restored.json()["data"]["thread_id"] == thread_id
    assert restored.json()["data"]["project_id"] == created.json()["data"]["project_id"]
    assert restored.json()["data"]["title"] == "重启后仍在的会话"
    listed = [
        item["thread_id"]
        for item in restarted.get("/api/sessions").json()["data"]["sessions"]
    ]
    assert listed == [thread_id]

    with database.transaction() as db_session:
        restarted_session_id = restarted.cookies.get(SESSION_COOKIE_NAME)
        assert restarted_session_id
        assert db_session.get(AuthSession, restarted_session_id) is not None
    # The restored session still belongs to its original owner.
    assert WebSessionRepository(database).get_for_user(thread_id, user_id).thread_id == thread_id


def test_project_session_survives_a_new_app_over_the_same_database(
    database: Database, tmp_path: Path
) -> None:
    username = f"restart-project-{uuid4()}"
    client, user_id = _login(database, tmp_path, username)
    project = ProjectRepository(database).create(user_id, "重启项目")
    created = client.post(
        f"/api/projects/{project.project_id}/sessions",
        json={"title": "项目会话"},
        headers={"X-CSRF-Token": _csrf(client)},
    )
    assert created.status_code == 201, created.text
    thread_id = created.json()["data"]["thread_id"]

    restarted = _sign_in(database, tmp_path, username)

    restored = restarted.get(f"/api/sessions/{thread_id}")
    assert restored.status_code == 200, restored.text
    assert restored.json()["data"]["thread_id"] == thread_id
    assert restored.json()["data"]["project_id"] == project.project_id


# ---------------------------------------------------------------------------
# 2. Listing is scoped to the caller and to the project
# ---------------------------------------------------------------------------

def test_session_listing_is_scoped_to_caller_and_project(
    database: Database, tmp_path: Path
) -> None:
    alice_client, alice_id = _login(database, tmp_path, f"alice-{uuid4()}")
    bob_client, bob_id = _login(database, tmp_path, f"bob-{uuid4()}")

    sessions = WebSessionRepository(database)
    projects = ProjectRepository(database)
    project = projects.create(alice_id, "A 的项目")
    other_project = projects.create(alice_id, "A 的另一个项目")
    alice_project_session = sessions.create(
        alice_id, project_id=project.project_id, title="A 项目会话"
    )
    sessions.create(alice_id, project_id=other_project.project_id, title="A 另一项目会话")
    alice_personal_session = sessions.create(alice_id, title="A 个人会话")
    bob_personal_session = sessions.create(bob_id, title="B 个人会话")

    alice_personal = [
        item["thread_id"] for item in alice_client.get("/api/sessions").json()["data"]["sessions"]
    ]
    assert alice_personal == [alice_personal_session.thread_id]
    assert alice_project_session.thread_id not in alice_personal
    assert bob_personal_session.thread_id not in alice_personal

    alice_project = [
        item["thread_id"]
        for item in alice_client.get(
            f"/api/projects/{project.project_id}/sessions"
        ).json()["data"]["sessions"]
    ]
    assert alice_project == [alice_project_session.thread_id]

    assert bob_client.get("/api/sessions").json()["data"]["sessions"][0]["thread_id"] == (
        bob_personal_session.thread_id
    )
    assert len(bob_client.get("/api/sessions").json()["data"]["sessions"]) == 1
    assert bob_client.get(f"/api/projects/{project.project_id}/sessions").status_code == 404
    assert bob_client.get(f"/api/sessions/{alice_personal_session.thread_id}").status_code == 404


# ---------------------------------------------------------------------------
# 3. Run rows are bound to the session owner
# ---------------------------------------------------------------------------

def test_run_rows_are_bound_to_the_session_owner(
    database: Database, tmp_path: Path
) -> None:
    _, alice_id = _login(database, tmp_path, f"alice-{uuid4()}")
    _, bob_id = _login(database, tmp_path, f"bob-{uuid4()}")
    session = WebSessionRepository(database).create(alice_id, title="A 个人会话")
    run = RunRepository(database).create(session.thread_id, alice_id)
    runs = RunRepository(database)

    with pytest.raises(ResourceNotFoundError):
        runs.get_for_user(run.run_id, bob_id)
    assert runs.get_owned_for_user(run.run_id, alice_id).run_id == run.run_id
    with pytest.raises(ResourceNotFoundError):
        runs.get_owned_for_user(run.run_id, bob_id)

    assert runs.list_for_session(session.thread_id, bob_id) == []
    assert [item.run_id for item in runs.list_for_session(session.thread_id, alice_id)] == [
        run.run_id
    ]


def test_project_member_reads_runs_but_only_the_owner_may_own_them(
    database: Database, tmp_path: Path
) -> None:
    _, owner_id = _login(database, tmp_path, f"owner-{uuid4()}")
    _, member_id = _login(database, tmp_path, f"member-{uuid4()}")
    projects = ProjectRepository(database)
    project = projects.create(owner_id, "共享项目")
    projects.add_member(project.project_id, owner_id, member_id, role="member")
    session = WebSessionRepository(database).create(
        owner_id, project_id=project.project_id, title="共享会话"
    )
    run = RunRepository(database).create(session.thread_id, owner_id)
    runs = RunRepository(database)

    # Project membership makes the run readable, but ownership stays with the
    # recorded owner so cancellation cannot be inherited from membership.
    assert runs.get_for_user(run.run_id, member_id).run_id == run.run_id
    with pytest.raises(ResourceNotFoundError):
        runs.get_owned_for_user(run.run_id, member_id)
    assert [item.run_id for item in runs.list_for_session(session.thread_id, member_id)] == [
        run.run_id
    ]


# ---------------------------------------------------------------------------
# 4. Two clients in one process must not share a session
# ---------------------------------------------------------------------------

def test_two_clients_in_one_process_do_not_share_a_session(
    database: Database, tmp_path: Path
) -> None:
    alice_username = f"alice-{uuid4()}"
    bob_username = f"bob-{uuid4()}"
    alice_client, alice_id = _login(database, tmp_path, alice_username)
    bob_client, bob_id = _login(database, tmp_path, bob_username)

    alice_me = alice_client.get("/api/auth/me")
    bob_me = bob_client.get("/api/auth/me")
    assert alice_me.json()["data"]["user"]["username"] == alice_username
    assert bob_me.json()["data"]["user"]["username"] == bob_username
    assert alice_me.json()["data"]["user"]["user_id"] == str(alice_id)
    assert bob_me.json()["data"]["user"]["user_id"] == str(bob_id)

    alice_session_id = alice_client.cookies.get(SESSION_COOKIE_NAME)
    bob_session_id = bob_client.cookies.get(SESSION_COOKIE_NAME)
    assert alice_session_id and bob_session_id and alice_session_id != bob_session_id
    store = PostgresSessionStore(database)
    assert store.get(alice_session_id).user_id == alice_id
    assert store.get(bob_session_id).user_id == bob_id

    # Copying A's cookie into another client cannot change who the server thinks
    # the caller is: the identity comes from the session row, not the browser.
    copied = _new_client(database, tmp_path)
    copied.cookies.set(SESSION_COOKIE_NAME, alice_session_id)
    copied_me = copied.get("/api/auth/me")
    assert copied_me.status_code == 200, copied_me.text
    assert copied_me.json()["data"]["user"]["username"] == alice_username
    assert copied_me.json()["data"]["user"]["user_id"] == str(alice_id)

    unknown = _new_client(database, tmp_path)
    unknown.cookies.set(SESSION_COOKIE_NAME, f"unknown-{uuid4().hex}")
    assert unknown.get("/api/auth/me").status_code == 401
    assert unknown.get("/api/auth/me").json()["error"]["code"] == "AUTH_SESSION_INVALID"


# ---------------------------------------------------------------------------
# 5. An expired session is rejected and its row removed
# ---------------------------------------------------------------------------

def test_expired_session_is_rejected_and_deleted(
    database: Database, tmp_path: Path
) -> None:
    client, _ = _login(database, tmp_path, f"expired-{uuid4()}")
    session_id = client.cookies.get(SESSION_COOKIE_NAME)
    assert session_id
    assert _session_rows(database, session_id) is not None

    with database.transaction() as db_session:
        row = db_session.get(AuthSession, session_id)
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(seconds=60)

    read = client.get("/api/projects")
    assert read.status_code == 401, read.text
    assert read.json()["error"]["code"] == "AUTH_SESSION_INVALID"
    assert _session_rows(database, session_id) is None
    with pytest.raises(SessionNotFoundError):
        PostgresSessionStore(database).get(session_id)

    # The CSRF-protected mutating path refuses the same expired session.
    write = client.post(
        "/api/sessions",
        json={"title": "过期会话写入"},
        headers={"X-CSRF-Token": _csrf(client)},
    )
    assert write.status_code == 401, write.text
    assert write.json()["error"]["code"] == "AUTH_SESSION_INVALID"


def test_session_ttl_row_is_in_the_future_when_created(
    database: Database, tmp_path: Path
) -> None:
    client, _ = _login(database, tmp_path, f"ttl-{uuid4()}")
    session_id = client.cookies.get(SESSION_COOKIE_NAME)
    assert session_id
    row = _session_rows(database, session_id)
    assert row is not None
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    assert expires_at > datetime.now(UTC)


# ---------------------------------------------------------------------------
# 6. Logout deletes the session server-side
# ---------------------------------------------------------------------------

def test_logout_deletes_the_session_server_side(
    database: Database, tmp_path: Path
) -> None:
    client, _ = _login(database, tmp_path, f"logout-{uuid4()}")
    session_id = client.cookies.get(SESSION_COOKIE_NAME)
    assert session_id
    assert _session_rows(database, session_id) is not None

    logged_out = client.post(
        "/api/auth/logout",
        headers={"X-CSRF-Token": _csrf(client)},
    )
    assert logged_out.status_code == 200, logged_out.text
    assert logged_out.json()["data"]["logged_out"] is True

    assert _session_rows(database, session_id) is None
    with pytest.raises(SessionNotFoundError):
        PostgresSessionStore(database).get(session_id)
    assert client.get("/api/auth/me").status_code == 401

    replay = _new_client(database, tmp_path)
    replay.cookies.set(SESSION_COOKIE_NAME, session_id)
    replayed = replay.get("/api/auth/me")
    assert replayed.status_code == 401, replayed.text
    assert replayed.json()["error"]["code"] == "AUTH_SESSION_INVALID"


# ---------------------------------------------------------------------------
# 7. PostgreSQL-gated variant of the restore property
# ---------------------------------------------------------------------------

def test_personal_session_restores_through_postgresql(tmp_path: Path) -> None:
    url = os.getenv("P5_TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "P5_TEST_DATABASE_URL is not configured; "
            "PostgreSQL integration is environment-dependent"
        )
    database = Database(url=url)
    database.require_ready()
    try:
        username = f"pg-restart-{uuid4()}"
        client, user_id = _login(database, tmp_path, username)
        created = client.post(
            "/api/sessions",
            json={"title": "PG 重启会话"},
            headers={"X-CSRF-Token": _csrf(client)},
        )
        assert created.status_code == 201, created.text
        thread_id = created.json()["data"]["thread_id"]

        restarted = _sign_in(database, tmp_path, username)
        restored = restarted.get(f"/api/sessions/{thread_id}")
        assert restored.status_code == 200, restored.text
        assert restored.json()["data"]["thread_id"] == thread_id
        assert restored.json()["data"]["project_id"] is None
        assert WebSessionRepository(database).get_for_user(thread_id, user_id).thread_id == (
            thread_id
        )
    finally:
        database.dispose()


# ---------------------------------------------------------------------------
# P5-S49: conversation history for the workbench
# ---------------------------------------------------------------------------


def _history_client(database: Database, tmp_path: Path, username: str, saver) -> tuple[TestClient, UUID]:
    from contextlib import contextmanager

    client, user_id = _login(database, tmp_path, username)

    @contextmanager
    def factory(_database):
        yield saver

    client.app.state.run_manager.checkpointer_factory = factory
    return client, user_id


def _seed_thread(saver, thread_id: str) -> None:
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langgraph.graph import START, MessagesState, StateGraph

    graph = StateGraph(MessagesState)
    graph.add_node("noop", lambda state: {})
    graph.add_edge(START, "noop")
    app = graph.compile(checkpointer=saver)
    app.invoke(
        {
            "messages": [
                HumanMessage(content="[运行时上下文]\n会话引用：x"),
                HumanMessage(content="你好，我现在给你 dxf 图纸你可以直接读取吗"),
                AIMessage(content="目前网页端还不能读取 DXF。"),
                HumanMessage(content="那我给你 CAD 设计图的 PDF 呢"),
                AIMessage(content="", tool_calls=[{"id": "c1", "name": "retrieve_clauses", "args": {}}]),
                ToolMessage(name="retrieve_clauses", tool_call_id="c1", content="{\"hits\": []}"),
                AIMessage(content="第一版回答（被校验器回流）。"),
                AIMessage(content="", tool_calls=[{"id": "v1", "name": "citation_validator", "args": {}}]),
                ToolMessage(name="citation_validator", tool_call_id="v1", content="请声明引用"),
                AIMessage(content="有文字层的 PDF 可以读；CAD 矢量 PDF 读不到文字。"),
            ]
        },
        config={"configurable": {"thread_id": thread_id}},
    )


def test_history_returns_every_turn_with_only_the_final_answer(database: Database, tmp_path: Path) -> None:
    from langgraph.checkpoint.memory import InMemorySaver

    saver = InMemorySaver()
    client, user_id = _history_client(database, tmp_path, f"hist-{uuid4()}", saver)
    thread = WebSessionRepository(database).create(user_id, title="历史")
    _seed_thread(saver, thread.thread_id)

    response = client.get(f"/api/sessions/{thread.thread_id}/messages")
    assert response.status_code == 200, response.text
    messages = response.json()["data"]["messages"]
    assert [m["role"] for m in messages] == ["user", "agent", "user", "agent"]
    assert messages[1]["text"] == "目前网页端还不能读取 DXF。"
    assert messages[3]["text"] == "有文字层的 PDF 可以读；CAD 矢量 PDF 读不到文字。"
    body = response.text
    for leaked in ("运行时上下文", "retrieve_clauses", "citation_validator", "请声明引用", "第一版回答"):
        assert leaked not in body


def test_history_of_another_users_thread_is_404(database: Database, tmp_path: Path) -> None:
    from langgraph.checkpoint.memory import InMemorySaver

    saver = InMemorySaver()
    _, owner_id = _history_client(database, tmp_path, f"owner-{uuid4()}", saver)
    thread = WebSessionRepository(database).create(owner_id, title="私有")
    _seed_thread(saver, thread.thread_id)

    intruder, _ = _history_client(database, tmp_path, f"intruder-{uuid4()}", saver)
    assert intruder.get(f"/api/sessions/{thread.thread_id}/messages").status_code == 404
    assert intruder.get(f"/api/sessions/{uuid4()}/messages").status_code == 404


def test_history_of_a_new_thread_is_empty(database: Database, tmp_path: Path) -> None:
    from langgraph.checkpoint.memory import InMemorySaver

    client, user_id = _history_client(database, tmp_path, f"empty-{uuid4()}", InMemorySaver())
    thread = WebSessionRepository(database).create(user_id, title="空")
    response = client.get(f"/api/sessions/{thread.thread_id}/messages")
    assert response.status_code == 200
    assert response.json()["data"]["messages"] == []


def test_history_reapplies_the_output_guardrail() -> None:
    """checkpoint 存的是模型原文；刷新页面不得绕过 finalize 当时的输出护栏。"""

    from langchain_core.messages import AIMessage, HumanMessage

    from water_agent.webapp.run_manager import project_conversation

    class _BlockAll:
        def check_output(self, text, ctx):
            del text, ctx

            class _Outcome:
                allowed = False
                content = ""

            return _Outcome()

    projected = project_conversation(
        [HumanMessage(content="问题"), AIMessage(content="被拦截的原文")],
        thread_id="t",
        pipeline=_BlockAll(),
    )
    assert projected == [
        {"role": "user", "text": "问题"},
        {"role": "agent", "text": "", "withheld": True},
    ]
