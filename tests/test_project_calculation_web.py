"""D authenticated API; SQLite/private synthetic users, no model configuration."""

import time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from test_project_calculation_execution import fixture

from water_agent.storage.database import Database
from water_agent.storage.models import Base
from water_agent.storage.repository import ProjectRepository, WebSessionRepository
from water_agent.webapp.app import create_app
from water_agent.webapp.auth.db_store import PostgresUserStore
from water_agent.webapp.auth.models import UserRole


@pytest.fixture
def project_client(tmp_path, monkeypatch):
    monkeypatch.setenv("WATER_AGENT_PROJECT_CALCULATION", "on")
    engine = create_engine(
        f"sqlite:///{tmp_path}/web.sqlite", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    db = Database(engine=engine)
    store = PostgresUserStore(db)
    user = store.create_user("project-user", "ProjectPass2026!", UserRole.MEMBER)
    project = ProjectRepository(db).create(user.user_id, "synthetic")
    session = WebSessionRepository(db).create(
        user.user_id, project_id=project.project_id, title="synthetic"
    )
    app = create_app(
        database=db,
        static_dir=tmp_path,
        export_dir=tmp_path / "exports",
        session_dir=tmp_path / "sessions",
        memory_enabled=False,
    )
    with TestClient(app) as client:
        assert (
            client.post(
                "/api/auth/login", json={"username": user.username, "password": "ProjectPass2026!"}
            ).status_code
            == 200
        )
        headers = {"X-CSRF-Token": client.cookies.get("wa_csrf")}
        plan, _, _ = fixture()
        plan["project_id"] = project.project_id
        yield client, app, db, user, session, plan, headers
    db.dispose()


def wait_pending(client, run_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get(f"/api/runs/{run_id}/project-calculation")
        if response.status_code == 200 and response.json()["data"]["status"] != "running":
            return response.json()["data"]
        time.sleep(0.02)
    pytest.fail("project run did not settle")


def begin(project_client):
    client, app, db, user, session, plan, headers = project_client
    prefix = f"/api/sessions/{session.thread_id}/project-calculations"
    preview = client.post(
        prefix + "/preview",
        json={"plan": plan, "base_revision": 0, "generation": 0},
        headers=headers,
    )
    assert preview.status_code == 200, preview.text
    data = preview.json()["data"]
    response = client.post(
        prefix + "/execute",
        json={"confirmation_id": data["confirmation_id"], "expected_plan_hash": data["plan_hash"]},
        headers=headers,
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["data"]["run_id"]
    pending = wait_pending(client, run_id)
    assert pending["status"] == "waiting_approval", pending
    return run_id, data


def test_D1_no_llm_and_strict_approval(project_client):
    client, app, db, user, session, plan, headers = project_client
    run_id, confirmation = begin(project_client)
    response = client.post(
        f"/api/runs/{run_id}/project-approval",
        json={"approved": "false", "reason": "invalid"},
        headers=headers,
    )
    assert response.status_code == 200 and response.json()["data"]["status"] == "waiting_approval"
    assert client.get(f"/api/runs/{run_id}/export").status_code != 200
    response = client.post(
        f"/api/runs/{run_id}/project-approval",
        json={"approved": True, "reason": "confirmed"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "succeeded"
    doc = client.get(f"/api/runs/{run_id}/export")
    assert doc.status_code == 200 and doc.content[:2] == b"PK", (
        doc.text[:300] if doc.status_code != 200 else ""
    )


def test_D1_csrf_scope_and_confirmation(project_client):
    client, app, db, user, session, plan, headers = project_client
    prefix = f"/api/sessions/{session.thread_id}/project-calculations"
    assert (
        client.post(
            prefix + "/preview", json={"plan": plan, "base_revision": 0, "generation": 0}
        ).status_code
        == 403
    )
    assert (
        client.post(
            prefix + "/execute",
            json={"confirmation_id": str(uuid4()), "expected_plan_hash": "a" * 64},
            headers=headers,
        ).status_code
        == 409
    )
    plan["project_id"] = str(uuid4())
    assert (
        client.post(
            prefix + "/preview",
            json={"plan": plan, "base_revision": 0, "generation": 0},
            headers=headers,
        ).status_code
        == 404
    )


def test_D1_confirmation_replay_rejected(project_client):
    client, app, db, user, session, plan, headers = project_client
    run_id, data = begin(project_client)
    response = client.post(
        f"/api/sessions/{session.thread_id}/project-calculations/execute",
        json={"confirmation_id": data["confirmation_id"], "expected_plan_hash": data["plan_hash"]},
        headers=headers,
    )
    assert response.status_code == 409


def test_D1_anonymous(project_client):
    client, app, db, user, session, plan, headers = project_client
    client.cookies.clear()
    assert (
        client.post(
            f"/api/sessions/{session.thread_id}/project-calculations/preview",
            json={"plan": plan, "base_revision": 0, "generation": 0},
            headers=headers,
        ).status_code
        == 401
    )


def test_default_off_is_inert(project_client, tmp_path, monkeypatch):
    client, app, db, user, session, plan, headers = project_client
    monkeypatch.setenv("WATER_AGENT_PROJECT_CALCULATION", "off")
    off = create_app(
        database=db, static_dir=tmp_path, export_dir=tmp_path / "off-exports", memory_enabled=False
    )
    with TestClient(off) as other:
        other.cookies.update(client.cookies)
        assert off.state.run_manager.project_runtime is None
        response = other.post(
            f"/api/sessions/{session.thread_id}/project-calculations/preview",
            json={"plan": plan, "base_revision": 0, "generation": 0},
            headers=headers,
        )
        assert response.status_code == 404
        assert not (tmp_path / "off-exports" / ".project-calculations").exists()


def test_cross_user_and_viewer_cannot_act(project_client):
    client, app, db, user, session, plan, headers = project_client
    from water_agent.storage.models import User

    run_id, _ = begin(project_client)
    with db.transaction() as sql:
        sql.get(User, str(user.user_id)).role = "viewer"
    assert (
        client.post(
            f"/api/runs/{run_id}/project-approval",
            json={"approved": True, "reason": "no"},
            headers=headers,
        ).status_code
        == 403
    )
    assert client.get(f"/api/runs/{run_id}/project-calculation").status_code == 403
    with db.transaction() as sql:
        sql.get(User, str(user.user_id)).role = "member"
    outsider = PostgresUserStore(db).create_user(
        "project-other", "ProjectPass2026!", UserRole.MEMBER
    )
    ProjectRepository(db).add_member(session.project_id, user.user_id, outsider.user_id)
    assert (
        client.post(
            "/api/auth/login", json={"username": outsider.username, "password": "ProjectPass2026!"}
        ).status_code
        == 200
    )
    other_headers = {"X-CSRF-Token": client.cookies.get("wa_csrf")}
    assert client.get(f"/api/runs/{run_id}/project-calculation").status_code == 404
    assert client.get(f"/api/runs/{run_id}/events").status_code == 404
    assert (
        client.post(
            f"/api/runs/{run_id}/project-approval",
            json={"approved": True, "reason": "no"},
            headers=other_headers,
        ).status_code
        == 404
    )


def test_pure_qa_does_not_enter_project_path(project_client, monkeypatch):
    client, app, db, user, session, plan, headers = project_client
    runtime = app.state.run_manager.project_runtime
    monkeypatch.setattr(
        runtime, "execute", lambda *_: pytest.fail("ordinary QA entered project calculation")
    )
    monkeypatch.setattr(
        "water_agent.webapp.run_routes.chat_model_problem",
        lambda: ("WEB_LLM_NOT_CONFIGURED", "Synthetic test: no model configured"),
    )
    response = client.post(
        f"/api/sessions/{session.thread_id}/messages", json={"message": "你好"}, headers=headers
    )
    # The ordinary path may require an LLM; it must not silently invoke the project calculator.
    assert response.status_code == 424


def test_old_worker_must_not_release_new_thread_reservation(project_client, monkeypatch):
    client, app, db, user, session, plan, headers = project_client
    run_id, _ = begin(project_client)
    runtime = app.state.run_manager.project_runtime
    manager = app.state.run_manager
    ctx = runtime.context(run_id, user.user_id)
    handle = manager._handles[run_id]
    new_id = str(uuid4())

    def late(_):
        manager._active_by_thread[ctx.thread_id] = new_id
        handle.finalized = True
        handle.status = "cancelled"
        return {"status": "blocked", "error": "PROJECT_STALE_SOURCE"}

    monkeypatch.setattr(runtime.service, "execute", late)
    monkeypatch.setattr(runtime, "_terminal", lambda *_: None)
    runtime._execute(ctx, handle)
    assert manager._active_by_thread[ctx.thread_id] == new_id
    assert handle.status == "cancelled"


def test_cancel_revoked_writer_returns_403(project_client):
    client, app, db, user, session, plan, headers = project_client
    from water_agent.storage.models import User

    run_id, _ = begin(project_client)
    with db.transaction() as sql:
        sql.get(User, str(user.user_id)).role = "viewer"
    assert client.post(f"/api/runs/{run_id}/cancel", headers=headers).status_code == 403


def test_thread_reserved_before_confirm_changes_head(project_client, monkeypatch):
    from threading import Event, Thread
    from water_agent.webapp.run_manager import _RunHandle
    from uuid import UUID

    client, app, db, user, session, plan, headers = project_client
    runtime = app.state.run_manager.project_runtime
    manager = app.state.run_manager
    preview = runtime.preview(user.user_id, session.thread_id, plan, 0, 0)
    entered = Event()
    release = Event()
    claimed = Event()
    result = []
    original = runtime.service.confirm

    def paused(*a, **kw):
        entered.set()
        assert release.wait(10)
        return original(*a, **kw)

    monkeypatch.setattr(runtime.service, "confirm", paused)
    writer = Thread(
        target=lambda: result.append(
            client.post(
                f"/api/sessions/{session.thread_id}/project-calculations/execute",
                json={
                    "confirmation_id": preview["confirmation_id"],
                    "expected_plan_hash": preview["plan_hash"],
                },
                headers=headers,
            )
        )
    )
    fake_id = str(uuid4())
    fake = _RunHandle(
        run_id=fake_id,
        thread_id=session.thread_id,
        owner_user_id=user.user_id,
        project_id=UUID(session.project_id),
    )

    def qa():
        with manager._lock:
            if not manager._thread_busy_locked(session.thread_id):
                manager._handles[fake_id] = fake
                manager._active_by_thread[session.thread_id] = fake_id
                claimed.set()

    writer.start()
    assert entered.wait(10)
    other = Thread(target=qa)
    other.start()
    sneaked = claimed.wait(0.2)
    release.set()
    writer.join(15)
    other.join(15)
    fake.finalized = True
    assert not sneaked, "QA claimed thread after project confirmation had already begun"
    assert result and result[0].status_code == 202, result[0].text if result else "no result"


def test_confirmed_queue_is_a_valid_event_source(project_client, monkeypatch):
    client, app, db, user, session, plan, headers = project_client
    runtime = app.state.run_manager.project_runtime
    monkeypatch.setattr(app.state.run_manager._executor, "submit", lambda *_: None)
    preview = runtime.preview(user.user_id, session.thread_id, plan, 0, 0)
    run = runtime.execute(
        user.user_id, session.thread_id, preview["confirmation_id"], preview["plan_hash"]
    )
    runtime.ensure_events(run["run_id"])
    ctx = runtime.context(run["run_id"], user.user_id)
    runtime._execute(ctx, app.state.run_manager._handles[run["run_id"]])
    assert client.get(f"/api/runs/{run['run_id']}/events").status_code == 200


def test_approval_annotation_and_head_are_authorized(project_client):
    client, app, db, user, session, plan, headers = project_client
    run_id, _ = begin(project_client)
    pending = client.get(f"/api/runs/{run_id}/project-calculation").json()["data"]
    assert (
        client.post(
            f"/api/runs/{run_id}/project-approval",
            json={"approved": True, "reason": "six artifacts verified"},
            headers=headers,
        ).status_code
        == 200
    )
    note = client.get(f"/api/runs/{run_id}/project-approval")
    assert note.status_code == 200
    data = note.json()["data"]
    assert data["record"]["approved"] is True and len(data["record"]["artifact_ids"]) == 6
    assert data["release_manifest_hash"] == pending["release_manifest_hash"]
    download = client.get(f"/api/runs/{run_id}/export")
    assert 'rel="describedby"' in download.headers["Link"]
    head = client.get(f"/api/sessions/{session.thread_id}/project-calculations")
    assert head.status_code == 200 and head.json()["data"]["base_revision"] == 1
    assert head.json()["data"]["active_plan"]["nodes"][0]["inputs"]["flow_lps"]["value"] == 10.0
    assert head.json()["data"]["published_runs"] == [{"run_id": run_id, "revision": 1}]
    published = client.get(f"/api/runs/{run_id}/project-calculation").json()["data"]
    assert [node["node_id"] for node in published["candidate"]] == ["v", "hf", "H"]
    assert client.get("/api/system/capabilities").json()["data"]["project_calculation"] is True


def test_download_metadata_revocation_is_mapped(project_client, monkeypatch):
    client, app, db, user, session, plan, headers = project_client
    run_id, _ = begin(project_client)
    client.post(
        f"/api/runs/{run_id}/project-approval",
        json={"approved": True, "reason": "ok"},
        headers=headers,
    )
    monkeypatch.setattr(
        app.state.run_manager.project_runtime,
        "currentness",
        lambda _: (_ for _ in ()).throw(PermissionError("synthetic revocation")),
    )
    assert client.get(f"/api/runs/{run_id}/export").status_code == 403


def test_waiting_status_cannot_overwrite_concurrent_commit(project_client, monkeypatch):
    """Repositories may select before UPDATE; serialize D's handoff with the project commit."""
    from threading import Event, Thread

    client, app, db, user, session, plan, headers = project_client
    runtime = app.state.run_manager.project_runtime
    repo = app.state.run_manager.run_repository
    waiting = Event()
    release = Event()
    finished = Event()
    responses = []
    original = repo.set_status

    def selected_then_write(run_id, owner, status, *, expected_statuses=()):
        if status == "waiting_approval":
            # Equivalent to the repository having already selected its expected row.
            waiting.set()
            assert release.wait(10)
            return original(run_id, owner, status)
        return original(run_id, owner, status, expected_statuses=expected_statuses)

    monkeypatch.setattr(repo, "set_status", selected_then_write)
    preview = runtime.preview(user.user_id, session.thread_id, plan, 0, 0)
    run = runtime.execute(
        user.user_id, session.thread_id, preview["confirmation_id"], preview["plan_hash"]
    )
    assert waiting.wait(10)

    def approve():
        try:
            responses.append(
                client.post(
                    f"/api/runs/{run['run_id']}/project-approval",
                    json={"approved": True, "reason": "ok"},
                    headers=headers,
                )
            )
        finally:
            finished.set()

    worker = Thread(target=approve)
    worker.start()
    early = finished.wait(0.2)
    release.set()
    worker.join(15)
    assert not early, "publication overtook a selected pending-status write"
    assert responses and responses[0].status_code == 200
    assert repo.get_owned_for_user(run["run_id"], user.user_id).status == "succeeded"
