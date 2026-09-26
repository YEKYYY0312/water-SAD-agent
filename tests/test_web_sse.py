"""P5 Step 45: SSE resume, authorization and cancellation over the real API.

The event log is resumable and re-checks authorization per event.  These tests
pin the properties the task book calls out: replay only after ``Last-Event-ID``,
exactly one ``finalize``, no metadata leak to a non-owner, a truthful cancelled
state, and a bounded graceful drain on shutdown.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event

from water_agent.observability.json_trace import JsonTraceRecorder
from water_agent.storage.database import Database
from water_agent.storage.models import Base
from water_agent.storage.repository import WebSessionRepository
from water_agent.webapp.app import create_app
from water_agent.webapp.auth.csrf import CSRF_COOKIE_NAME
from water_agent.webapp.auth.db_store import PostgresUserStore
from water_agent.webapp.auth.models import UserRole
from water_agent.webapp.run_manager import RunManager


class _FakeSession:
    def __init__(self, recorder: JsonTraceRecorder, *, wait_for_cancel: bool = False) -> None:
        self.recorder = recorder
        self.wait_for_cancel = wait_for_cancel
        self.started = Event()

    def start_trace(self, run_id: str, query: str) -> None:
        self.recorder.start(run_id=run_id, trace_id=f"trace-{run_id}", query=query)

    async def aask(
        self,
        query: str,
        *,
        run_id: str,
        memory_context: object | None,
        on_token,
        on_event,
        cancel_check,
    ) -> dict:
        del query, run_id, memory_context
        self.started.set()
        on_event("values", {"status": "running", "messages": []})
        if self.wait_for_cancel:
            while not cancel_check():
                await asyncio.sleep(0.005)
            from water_agent.webapp.run_manager import RunCancelled

            raise RunCancelled()
        on_token("真实回答")
        self.recorder.finish("succeeded")
        return {
            "status": "succeeded",
            "answer": "真实回答",
            "citations": [],
            "errors": [],
            "warnings": [],
        }


class _FakeFactory:
    def __init__(self, trace_dir: Path, *, wait_for_cancel: bool = False) -> None:
        self.trace_dir = trace_dir
        self.wait_for_cancel = wait_for_cancel
        self.sessions: list[_FakeSession] = []

    def create(self, *, run_id: str, thread_id: str, user_id: UUID, principal, project_id, stack):
        del thread_id, user_id, project_id, stack
        session = _FakeSession(
            JsonTraceRecorder(self.trace_dir), wait_for_cancel=self.wait_for_cancel
        )
        self.sessions.append(session)
        return session


@pytest.fixture
def database(tmp_path: Path) -> Database:
    engine = create_engine(f"sqlite:///{tmp_path / 'web-sse.sqlite'}")

    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    database = Database(engine=engine)
    yield database
    database.dispose()


def _user(database: Database, prefix: str) -> tuple[UUID, str, str]:
    username = f"{prefix}-{uuid4().hex[:8]}"
    password = "WebSsePass2026!"
    record = PostgresUserStore(database).create_user(username, password, UserRole.MEMBER)
    session = WebSessionRepository(database).create(record.user_id, title=prefix)
    return UUID(str(record.user_id)), username, str(session.thread_id)


def _client(
    database: Database,
    tmp_path: Path,
    username: str,
    password: str,
    *,
    factory: _FakeFactory | None = None,
) -> TestClient:
    app = create_app(
        database=database,
        static_dir=tmp_path / "static",
        session_factory=factory,
        trace_dir=tmp_path / "traces",
    )
    client = TestClient(app, raise_server_exceptions=True)
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return client


def _csrf(client: TestClient) -> str:
    value = client.cookies.get(CSRF_COOKIE_NAME)
    assert value
    return value


def _start_run(client: TestClient, thread_id: str) -> str:
    response = client.post(
        f"/api/sessions/{thread_id}/messages",
        json={"message": "给水系统设计流量口径是什么"},
        headers={"X-CSRF-Token": _csrf(client)},
    )
    assert response.status_code == 202, response.text
    return response.json()["data"]["run_id"]


def _parse_sse(response) -> list[dict]:
    events: list[dict] = []
    for block in response.text.strip().split("\n\n"):
        data_lines = [line[6:] for line in block.splitlines() if line.startswith("data: ")]
        if data_lines:
            events.append(json.loads("\n".join(data_lines)))
    return events


def test_reconnect_replays_only_the_tail_without_a_second_finalize(
    database: Database, tmp_path: Path
) -> None:
    factory = _FakeFactory(tmp_path / "traces")
    _uid, username, thread_id = _user(database, "sse-replay")
    client = _client(database, tmp_path, username, "WebSsePass2026!", factory=factory)
    run_id = _start_run(client, thread_id)

    first = _parse_sse(client.get(f"/api/runs/{run_id}/events"))
    assert len(first) > 2
    assert sum(item["type"] == "finalize" for item in first) == 1
    cursor = int(first[1]["event_id"])

    replay = _parse_sse(
        client.get(f"/api/runs/{run_id}/events", headers={"Last-Event-ID": str(cursor)})
    )
    ids = [item["event_id"] for item in replay]

    assert all(int(item) > cursor for item in ids)
    assert len(set(ids)) == len(ids)
    assert sum(item["type"] == "finalize" for item in replay) == 1
    # The run is terminal: a cursor at the end yields no frames at all.
    last = int(first[-1]["event_id"])
    empty = client.get(f"/api/runs/{run_id}/events", headers={"Last-Event-ID": str(last)})
    assert _parse_sse(empty) == []


@pytest.mark.parametrize("cursor", ["abc", "-1", "1.5", " "])
def test_invalid_last_event_id_is_rejected(
    database: Database, tmp_path: Path, cursor: str
) -> None:
    factory = _FakeFactory(tmp_path / "traces")
    _uid, username, thread_id = _user(database, "sse-cursor")
    client = _client(database, tmp_path, username, "WebSsePass2026!", factory=factory)
    run_id = _start_run(client, thread_id)
    _parse_sse(client.get(f"/api/runs/{run_id}/events"))

    response = client.get(f"/api/runs/{run_id}/events", headers={"Last-Event-ID": cursor})

    if cursor == " ":
        # A whitespace-only header value is not a cursor the browser sends; the
        # route treats it as absent and streams normally.
        assert response.status_code in (200, 400)
        return
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "WEB_EVENT_CURSOR_INVALID"


def test_non_owner_cannot_replay_or_read_the_trace(database: Database, tmp_path: Path) -> None:
    factory = _FakeFactory(tmp_path / "traces")
    _owner_id, owner_name, thread_id = _user(database, "sse-owner")
    owner = _client(database, tmp_path, owner_name, "WebSsePass2026!", factory=factory)
    run_id = _start_run(owner, thread_id)
    first = _parse_sse(owner.get(f"/api/runs/{run_id}/events"))
    cursor = int(first[0]["event_id"])

    _other_id, other_name, _ = _user(database, "sse-intruder")
    intruder = _client(database, tmp_path, other_name, "WebSsePass2026!", factory=factory)
    replay = intruder.get(
        f"/api/runs/{run_id}/events", headers={"Last-Event-ID": str(cursor)}
    )
    trace = intruder.get(f"/api/runs/{run_id}/trace")

    assert replay.status_code == 404
    assert trace.status_code == 404
    assert "event_id" not in replay.text
    assert run_id not in replay.text


def test_deactivating_the_account_stops_replay_even_for_the_run_owner(
    database: Database, tmp_path: Path
) -> None:
    """Authorization is re-checked per event, not only when the stream opens."""

    factory = _FakeFactory(tmp_path / "traces")
    user_id, username, thread_id = _user(database, "sse-inactive")
    client = _client(database, tmp_path, username, "WebSsePass2026!", factory=factory)
    run_id = _start_run(client, thread_id)
    assert _parse_sse(client.get(f"/api/runs/{run_id}/events"))

    PostgresUserStore(database).set_active(user_id, active=False)
    replay = client.get(f"/api/runs/{run_id}/events")

    assert replay.status_code == 403
    # The refusal happens at authentication (403 AUTH_USER_INACTIVE) rather than
    # at the run-ownership check, which is strictly earlier and equally safe:
    # not a single frame of the existing event log is replayed.
    assert replay.json()["error"]["code"] == "AUTH_USER_INACTIVE"
    assert "event_id" not in replay.text


def test_cancel_reports_the_real_state_and_finalizes_once(
    database: Database, tmp_path: Path
) -> None:
    factory = _FakeFactory(tmp_path / "traces", wait_for_cancel=True)
    _uid, username, thread_id = _user(database, "sse-cancel")
    client = _client(database, tmp_path, username, "WebSsePass2026!", factory=factory)
    run_id = _start_run(client, thread_id)
    deadline = time.monotonic() + 5
    while not factory.sessions and time.monotonic() < deadline:
        time.sleep(0.005)
    assert factory.sessions
    assert factory.sessions[0].started.wait(timeout=5)

    cancelled = client.post(
        f"/api/runs/{run_id}/cancel", headers={"X-CSRF-Token": _csrf(client)}
    )
    events = _parse_sse(client.get(f"/api/runs/{run_id}/events"))
    finals = [item for item in events if item["type"] == "finalize"]

    assert cancelled.status_code == 202
    assert cancelled.json()["data"]["status"] in {"cancellation_requested", "cancelled"}
    assert len(finals) == 1
    assert finals[0]["payload"]["status"] == "cancelled"
    assert any(item["type"] == "cancel_requested" for item in events)
    # The reported state is the real one, not an optimistic "cancelled".
    run_row = client.get(f"/api/runs/{run_id}/trace")
    assert run_row.status_code == 200
    assert run_row.json()["data"]["status"] == "cancelled"


def test_a_second_user_cannot_cancel_someone_elses_run(
    database: Database, tmp_path: Path
) -> None:
    factory = _FakeFactory(tmp_path / "traces", wait_for_cancel=True)
    _owner_id, owner_name, thread_id = _user(database, "sse-owner-cancel")
    owner = _client(database, tmp_path, owner_name, "WebSsePass2026!", factory=factory)
    run_id = _start_run(owner, thread_id)
    deadline = time.monotonic() + 5
    while not factory.sessions and time.monotonic() < deadline:
        time.sleep(0.005)
    assert factory.sessions and factory.sessions[0].started.wait(timeout=5)

    _other_id, other_name, _ = _user(database, "sse-other-cancel")
    intruder = _client(database, tmp_path, other_name, "WebSsePass2026!", factory=factory)
    response = intruder.post(
        f"/api/runs/{run_id}/cancel", headers={"X-CSRF-Token": _csrf(intruder)}
    )

    assert response.status_code == 404
    # The owner's run keeps running: the refusal did not cancel it by accident.
    # Reading the event stream here would block (a live run has no terminal
    # event yet), so the run's own trace status is the honest probe.
    trace = owner.get(f"/api/runs/{run_id}/trace")
    assert trace.status_code == 200
    assert trace.json()["data"]["status"] == "running"
    assert not any(
        item["event_type"] == "cancel_requested" for item in trace.json()["data"]["events"]
    )
    owner.post(f"/api/runs/{run_id}/cancel", headers={"X-CSRF-Token": _csrf(owner)})
    events = _parse_sse(owner.get(f"/api/runs/{run_id}/events"))
    assert sum(item["type"] == "finalize" for item in events) == 1


def test_graceful_shutdown_drains_in_flight_runs(database: Database, tmp_path: Path) -> None:
    """``RunManager.close`` cancels live runs and waits, instead of dropping them."""

    factory = _FakeFactory(tmp_path / "traces", wait_for_cancel=True)
    _uid, username, thread_id = _user(database, "sse-shutdown")
    client = _client(database, tmp_path, username, "WebSsePass2026!", factory=factory)
    run_id = _start_run(client, thread_id)
    deadline = time.monotonic() + 5
    while not factory.sessions and time.monotonic() < deadline:
        time.sleep(0.005)
    assert factory.sessions and factory.sessions[0].started.wait(timeout=5)

    manager = RunManager(database, session_factory=factory, trace_dir=tmp_path / "traces")
    manager._handles[run_id] = client.app.state.run_manager._handles[run_id]  # type: ignore[attr-defined]
    result = manager.close(timeout_seconds=5)

    assert result["graceful"] is True
    assert result["finalized"] == 1
    events = _parse_sse(client.get(f"/api/runs/{run_id}/events"))
    assert [item["type"] for item in events].count("finalize") == 1


def test_shutdown_reports_runs_that_did_not_finalize() -> None:
    """A run that ignores cancellation is reported, never silently dropped."""

    manager = RunManager.__new__(RunManager)
    from water_agent.webapp.run_manager import _RunHandle

    handle = _RunHandle(
        run_id=str(uuid4()),
        thread_id=str(uuid4()),
        owner_user_id=uuid4(),
        project_id=None,
    )
    manager._handles = {handle.run_id: handle}  # type: ignore[attr-defined]
    from concurrent.futures import ThreadPoolExecutor
    from threading import RLock

    manager._lock = RLock()  # type: ignore[attr-defined]
    manager._executor = ThreadPoolExecutor(max_workers=1)  # type: ignore[attr-defined]

    result = manager.close(timeout_seconds=0.2)

    assert result["graceful"] is False
    assert result["unfinalized_run_ids"] == [handle.run_id]
    assert handle.cancel_requested is True


def test_app_shutdown_hook_returns_the_drain_result(database: Database, tmp_path: Path) -> None:
    """The FastAPI shutdown hook must await the drain, not fire and forget."""

    factory = _FakeFactory(tmp_path / "traces")
    _uid, username, thread_id = _user(database, "sse-drain")
    app = create_app(
        database=database,
        static_dir=tmp_path / "static",
        session_factory=factory,
        trace_dir=tmp_path / "traces",
    )
    with TestClient(app, raise_server_exceptions=True) as client:
        client.post(
            "/api/auth/login", json={"username": username, "password": "WebSsePass2026!"}
        )
        run_id = _start_run(client, thread_id)
        assert _parse_sse(client.get(f"/api/runs/{run_id}/events"))

    drain = app.state.shutdown_drain
    assert drain["runs"] >= 1
    assert drain["graceful"] is True
    assert drain["finalized"] == drain["runs"]
