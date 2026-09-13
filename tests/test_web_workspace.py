"""P5 Step 44: authorized workspace resources, feedback and event projections."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import create_engine, event, select

from water_agent.storage.database import Database
from water_agent.storage.models import Base, FeedbackCandidateRow
from water_agent.storage.repository import (
    ProjectRepository,
    ResourceNotFoundError,
    RunRepository,
    WebSessionRepository,
)
from water_agent.webapp.app import create_app
from water_agent.webapp.auth.csrf import CSRF_COOKIE_NAME
from water_agent.webapp.auth.db_store import PostgresUserStore
from water_agent.webapp.auth.models import UserRole
from water_agent.webapp.run_manager import RunManager, _RunHandle


@pytest.fixture
def database(tmp_path: Path) -> Database:
    engine = create_engine(f"sqlite:///{tmp_path / 'workspace.sqlite'}")

    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    database = Database(engine=engine)
    yield database
    database.dispose()


def _login(database: Database, tmp_path: Path, username: str) -> tuple[TestClient, UUID]:
    user = PostgresUserStore(database).create_user(
        username, "WorkspacePass2026!", UserRole.MEMBER
    )
    app = create_app(database=database, static_dir=tmp_path)
    client = TestClient(app, raise_server_exceptions=True)
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "WorkspacePass2026!"},
    )
    assert response.status_code == 200, response.text
    return client, user.user_id


def _csrf(client: TestClient) -> str:
    value = client.cookies.get(CSRF_COOKIE_NAME)
    assert value
    return value


def test_project_switch_lists_only_authorized_projects_and_sessions(
    database: Database, tmp_path: Path
) -> None:
    owner_client, _ = _login(database, tmp_path, f"owner-{uuid4()}")
    outsider_client, outsider_id = _login(database, tmp_path, f"outsider-{uuid4()}")

    created = owner_client.post(
        "/api/projects",
        json={"name": "泵房改造"},
        headers={"X-CSRF-Token": _csrf(owner_client)},
    )
    assert created.status_code == 201
    project_id = created.json()["data"]["project_id"]
    session_response = owner_client.post(
        f"/api/projects/{project_id}/sessions",
        json={"title": "首轮校核"},
        headers={"X-CSRF-Token": _csrf(owner_client)},
    )
    assert session_response.status_code == 201
    thread_id = session_response.json()["data"]["thread_id"]

    projects = owner_client.get("/api/projects")
    assert projects.status_code == 200
    assert [item["project_id"] for item in projects.json()["data"]["projects"]] == [project_id]
    sessions = owner_client.get(f"/api/projects/{project_id}/sessions")
    assert sessions.status_code == 200
    assert sessions.json()["data"]["sessions"][0]["thread_id"] == thread_id
    restored = owner_client.get(f"/api/sessions/{thread_id}")
    assert restored.status_code == 200
    assert restored.json()["data"]["project_id"] == project_id

    assert outsider_client.get("/api/projects").json()["data"]["projects"] == []
    assert outsider_client.get(f"/api/projects/{project_id}/sessions").status_code == 404
    assert outsider_client.get(f"/api/sessions/{thread_id}").status_code == 404
    with pytest.raises(ResourceNotFoundError):
        ProjectRepository(database).get_for_user(project_id, outsider_id)


def test_personal_session_can_be_created_and_restored(database: Database, tmp_path: Path) -> None:
    client, _ = _login(database, tmp_path, f"personal-{uuid4()}")
    created = client.post(
        "/api/sessions",
        json={"title": "个人会话"},
        headers={"X-CSRF-Token": _csrf(client)},
    )
    assert created.status_code == 201
    thread_id = created.json()["data"]["thread_id"]
    restored = client.get(f"/api/sessions/{thread_id}")
    assert restored.status_code == 200
    assert restored.json()["data"]["project_id"] is None


def test_feedback_is_persisted_as_isolated_candidate_not_eval_gold(
    database: Database, tmp_path: Path
) -> None:
    client, user_id = _login(database, tmp_path, f"feedback-{uuid4()}")
    session = WebSessionRepository(database).create(user_id, title="反馈会话")
    run = RunRepository(database).create(session.thread_id, user_id)

    response = client.post(
        f"/api/runs/{run.run_id}/feedback",
        json={"rating": "not_useful", "comment": "引用需要人工复核"},
        headers={"X-CSRF-Token": _csrf(client)},
    )
    assert response.status_code == 201
    feedback_id = response.json()["data"]["feedback_id"]

    with database.transaction() as db_session:
        row = db_session.execute(
            select(FeedbackCandidateRow).where(FeedbackCandidateRow.feedback_id == feedback_id)
        ).scalar_one()
        assert row.status == "candidate"
        assert row.user_id == str(user_id)
        assert row.run_id == str(run.run_id)
        assert row.rating == "not_useful"

    duplicate = client.post(
        f"/api/runs/{run.run_id}/feedback",
        json={"rating": "useful"},
        headers={"X-CSRF-Token": _csrf(client)},
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["data"]["feedback_id"] == feedback_id
    assert duplicate.json()["data"]["status"] == "candidate"


def test_unauthorized_run_feedback_is_not_written(database: Database, tmp_path: Path) -> None:
    owner_client, owner_id = _login(database, tmp_path, f"run-owner-{uuid4()}")
    outsider_client, _ = _login(database, tmp_path, f"run-outsider-{uuid4()}")
    session = WebSessionRepository(database).create(owner_id, title="私有运行")
    run = RunRepository(database).create(session.thread_id, owner_id)

    response = outsider_client.post(
        f"/api/runs/{run.run_id}/feedback",
        json={"rating": "useful"},
        headers={"X-CSRF-Token": _csrf(outsider_client)},
    )
    assert response.status_code == 404
    with database.transaction() as db_session:
        assert db_session.execute(select(FeedbackCandidateRow)).scalars().all() == []
    assert owner_client.get(f"/api/runs/{run.run_id}/trace").status_code == 404


def test_run_manager_emits_real_tool_start_and_result_cards(database: Database, tmp_path: Path) -> None:
    manager = RunManager(database, session_factory=None, max_workers=1)
    try:
        handle = _RunHandle(
            run_id=str(uuid4()),
            thread_id=str(uuid4()),
            owner_user_id=UUID("00000000-0000-0000-0000-000000000001"),
            project_id=None,
        )
        manager._on_agent_event(
            handle,
            "messages",
            (
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "calc_velocity", "args": {"diameter_mm": 150}, "id": "call-1"}
                    ],
                ),
                {},
            ),
        )
        manager._on_agent_event(
            handle,
            "values",
            {
                "status": "running",
                "messages": [
                    ToolMessage(
                        name="calc_velocity",
                        tool_call_id="call-1",
                        content=json.dumps(
                            {"velocity_mps": 0.42, "assumptions": []}, ensure_ascii=False
                        ),
                    )
                ],
            },
        )
        event_types = [event.event_type for event in handle.events]
        assert "tool_started" in event_types
        assert "tool_result" in event_types
        result_event = next(event for event in handle.events if event.event_type == "tool_result")
        assert result_event.payload["calculation"]["programmatic"] is True
        assert result_event.payload["calculation"]["values"] == {"velocity_mps": 0.42}
    finally:
        manager.close()


def test_run_manager_never_marks_unstructured_tool_output_as_success(
    database: Database,
) -> None:
    manager = RunManager(database, session_factory=None, max_workers=1)
    try:
        handle = _RunHandle(
            run_id=str(uuid4()),
            thread_id=str(uuid4()),
            owner_user_id=UUID("00000000-0000-0000-0000-000000000001"),
            project_id=None,
        )
        manager._on_agent_event(
            handle,
            "values",
            {
                "messages": [
                    ToolMessage(name="read_drawing", tool_call_id="call-bad", content="unexpected")
                ]
            },
        )
        result_event = next(event for event in handle.events if event.event_type == "tool_result")
        assert result_event.payload["status"] == "failed"
        assert result_event.payload["code"] == "TOOL_RESULT_INVALID"
    finally:
        manager.close()


def test_run_manager_does_not_show_internal_citation_validator_as_tool() -> None:
    class _Session:
        registry = type("_Registry", (), {"get": staticmethod(lambda _: None)})()

    handle = _RunHandle(
        run_id=str(uuid4()),
        thread_id=str(uuid4()),
        owner_user_id=UUID("00000000-0000-0000-0000-000000000001"),
        project_id=None,
        session=_Session(),
    )
    manager = object.__new__(RunManager)
    manager._on_agent_event(
        handle,
        "values",
        {
            "messages": [
                ToolMessage(
                    name="citation_validator",
                    tool_call_id="validator-1",
                    content=json.dumps({"cited": ["FIX 1.1.1"]}),
                )
            ]
        },
    )
    assert handle.events == [] or all(event.event_type != "tool_result" for event in handle.events)


def test_capability_status_distinguishes_p3_and_p4_blocked_states(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DRAWING_ROOT", raising=False)
    client, _ = _login(database, tmp_path, f"capabilities-{uuid4()}")
    response = client.get("/api/system/capabilities")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["drawing"]["status"] == "not_configured"
    assert data["drawing"]["supported_format"] == "dxf"
    assert data["drawing"]["dwg"]["status"] == "blocked"
    assert data["drawing"]["real_samples"] is False
    assert data["mcp"]["status"] in {"blocked", "not_configured", "unavailable"}

    monkeypatch.setenv("DRAWING_ROOT", str(tmp_path / "private-drawing-root"))
    unavailable = client.get("/api/system/capabilities").json()["data"]["drawing"]
    assert unavailable["status"] == "unavailable"
    assert str(tmp_path) not in unavailable["message"]


def test_trace_projection_omits_query_and_body_but_keeps_safe_observations() -> None:
    projected = RunManager._trace_projection(
        {
            "run_id": "run-1",
            "trace_id": "trace-1",
            "query": "用户私密问题",
            "status": "succeeded",
            "started_at": "2026-09-13T08:00:00Z",
            "finished_at": "2026-09-13T08:00:01Z",
            "events": [
                {
                    "event_id": "event-1",
                    "event_type": "retrieve_complete",
                    "node_name": "retrieve",
                    "query": "用户私密问题",
                    "retrieved_context": [{"source_id": "FIX 1.1.1", "text": "正文"}],
                    "citations": [{"source_id": "FIX 1.1.1", "text": "正文"}],
                    "data": {"content": "正文", "count": 1},
                    "status": "running",
                }
            ],
        }
    )
    serialized = json.dumps(projected, ensure_ascii=False)
    assert "用户私密问题" not in serialized
    assert "正文" not in serialized
    assert projected["events"][0]["retrieved_count"] == 1
    assert projected["events"][0]["citation_source_ids"] == ["FIX 1.1.1"]
