"""T27-070..081: process restart, commit gap, original terminal envelope."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_project_calculation_export import setup_publication
from test_project_calculation_web import begin, project_client

from water_agent.webapp.app import create_app


def events(response):
    assert response.status_code == 200, response.text
    return [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]


def test_T27_070_081_web_restart_original_event_and_bytes(project_client, tmp_path):
    client, app, db, user, session, plan, headers = project_client
    run_id, _ = begin(project_client)
    before = events(client.get(f"/api/runs/{run_id}/events"))
    assert [e["type"] for e in before] == ["approval_required"]
    assert (
        client.post(
            f"/api/runs/{run_id}/project-approval",
            json={"approved": True, "reason": "ok"},
            headers=headers,
        ).status_code
        == 200
    )
    first = events(client.get(f"/api/runs/{run_id}/events"))
    content = client.get(f"/api/runs/{run_id}/export").content
    assert [e["type"] for e in first] == ["finalize", "stream_end"]
    again = create_app(
        database=db,
        static_dir=tmp_path,
        export_dir=tmp_path / "exports",
        session_dir=tmp_path / "sessions",
        memory_enabled=False,
    )
    with TestClient(again) as reopened:
        reopened.cookies.update(client.cookies)
        assert events(reopened.get(f"/api/runs/{run_id}/events")) == first
        assert reopened.get(f"/api/runs/{run_id}/export").content == content
        assert (
            reopened.get(
                f"/api/runs/{run_id}/events", headers={"Last-Event-ID": first[0]["event_id"]}
            ).text
            == ""
        )


def test_T27_071_cancel_reopen_no_tools(project_client, tmp_path):
    client, app, db, user, session, plan, headers = project_client
    run_id, _ = begin(project_client)
    assert client.post(f"/api/runs/{run_id}/cancel", headers=headers).status_code == 202
    first = events(client.get(f"/api/runs/{run_id}/events"))
    assert first[0]["payload"]["status"] == "cancelled"
    assert events(client.get(f"/api/runs/{run_id}/events")) == first
    assert client.get(f"/api/runs/{run_id}/export").status_code != 200


def test_T27_079_guard_retracts_without_answer(project_client, monkeypatch):
    client, app, db, user, session, plan, headers = project_client
    run_id, _ = begin(project_client)
    monkeypatch.setattr(
        app.state.run_manager.project_runtime.publisher, "check_output", lambda _: False
    )
    response = client.post(
        f"/api/runs/{run_id}/project-approval",
        json={"approved": True, "reason": "ok"},
        headers=headers,
    )
    assert response.status_code == 503
    stream = events(client.get(f"/api/runs/{run_id}/events"))
    assert [e["type"] for e in stream] == ["draft_retract", "finalize", "stream_end"]
    assert not any(e.get("payload", {}).get("answer") for e in stream)


CHILD = """
import sys,json,hashlib
from dataclasses import replace
from pathlib import Path
from test_project_calculation_execution import fixture
from test_project_calculation_revision import Authority
from water_agent.documents.storage import ExportSnapshotStore
from water_agent.multi_agent.project_calculation_service import CalculationService
from water_agent.multi_agent.project_publication import DocumentBackend,ProjectPublisher
from water_agent.tools import project_calculation as tools
from water_agent.docgen import project_calculation_document as docs
_,ctx,_=fixture();ctx=replace(ctx,**json.loads(Path(sys.argv[2]).read_text()))
backend=DocumentBackend(Path(sys.argv[1])/'documents')
service=CalculationService(Path(sys.argv[1])/'state',Authority(ctx),document_backend=backend)
pub=ProjectPublisher(service,ExportSnapshotStore(Path(sys.argv[1])/'snapshots'),backend,set_succeeded=lambda _:None)
tools.run_calc=lambda *_:sys.exit(99)
docs.render_project_calculation_docx=lambda *_:sys.exit(98)
record=pub.read(ctx);blob=pub.document(ctx)
print(json.dumps({'record':record,'sha':hashlib.sha256(blob).hexdigest()}))
"""


def test_real_subprocess_committed_read_zero_compute_or_render(tmp_path):
    pub, service, plan, ctx, statuses = setup_publication(tmp_path)
    record = pub.publish(ctx)
    content = pub.document(ctx)
    identity = tmp_path / "identity.json"
    identity.write_text(
        json.dumps({k: getattr(ctx, k) for k in ("owner_user_id", "thread_id", "run_id")})
    )
    identity.chmod(0o600)
    process = subprocess.run(
        ["_venv/bin/python", "-c", CHILD, str(tmp_path), str(identity)],
        env={**os.environ, "PYTHONPATH": "src:tests"},
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0, process.stderr
    restored = json.loads(process.stdout)
    assert restored["record"] == record and restored["sha"] == hashlib.sha256(content).hexdigest()


def test_commit_database_gap_recovers_without_new_events(project_client, monkeypatch):
    client, app, db, user, session, plan, headers = project_client
    runtime = app.state.run_manager.project_runtime
    run_id, _ = begin(project_client)
    original = runtime.publisher.set_succeeded
    monkeypatch.setattr(
        runtime.publisher,
        "set_succeeded",
        lambda _: (_ for _ in ()).throw(OSError("synthetic DB failure")),
    )
    response = client.post(
        f"/api/runs/{run_id}/project-approval",
        json={"approved": True, "reason": "ok"},
        headers=headers,
    )
    assert response.status_code == 503
    ctx = runtime.context(run_id, user.user_id)
    with runtime.service._locked(ctx):
        before = runtime.service._load(ctx)["publication_events"][run_id]
    monkeypatch.setattr(runtime.publisher, "set_succeeded", original)
    assert (
        client.get(f"/api/runs/{run_id}/project-calculation").json()["data"]["status"]
        == "succeeded"
    )
    after = events(client.get(f"/api/runs/{run_id}/events"))
    assert after[0] == before


def test_cancel_commit_race_does_not_destroy_package(project_client, monkeypatch):
    client, app, db, user, session, plan, headers = project_client
    runtime = app.state.run_manager.project_runtime
    run_id, _ = begin(project_client)
    ctx = runtime.context(run_id, user.user_id)
    original = runtime._committed
    first = True

    def raced(context):
        nonlocal first
        if first:
            first = False
            runtime.service.approve(ctx, True, "synthetic concurrent decision")
            runtime.publisher.publish(ctx)
            return False
        return original(context)

    monkeypatch.setattr(runtime, "_committed", raced)
    assert (
        client.post(f"/api/runs/{run_id}/cancel", headers=headers).json()["data"]["status"]
        == "succeeded"
    )
    assert client.get(f"/api/runs/{run_id}/export").status_code == 200


def test_cancel_rejected_run_is_idempotent(project_client):
    client, app, db, user, session, plan, headers = project_client
    run_id, _ = begin(project_client)
    assert (
        client.post(
            f"/api/runs/{run_id}/project-approval",
            json={"approved": False, "reason": "reject"},
            headers=headers,
        ).json()["data"]["status"]
        == "failed"
    )
    before = events(client.get(f"/api/runs/{run_id}/events"))
    response = client.post(f"/api/runs/{run_id}/cancel", headers=headers)
    assert response.status_code == 202 and response.json()["data"]["status"] == "failed"
    assert (
        client.get(f"/api/runs/{run_id}/project-calculation").json()["data"]["status"] == "failed"
    )
    assert events(client.get(f"/api/runs/{run_id}/events")) == before


def test_historical_download_metadata_and_original_bytes(project_client):
    client, app, db, user, session, plan, headers = project_client
    run_id, _ = begin(project_client)
    assert (
        client.post(
            f"/api/runs/{run_id}/project-approval",
            json={"approved": True, "reason": "ok"},
            headers=headers,
        ).status_code
        == 200
    )
    first = client.get(f"/api/runs/{run_id}/export")
    assert first.headers["X-Project-Current"] == "true"
    runtime = app.state.run_manager.project_runtime
    preview = runtime.preview(user.user_id, session.thread_id, plan, 1, 1)
    next_run = runtime.execute(
        user.user_id, session.thread_id, preview["confirmation_id"], preview["plan_hash"]
    )
    from test_project_calculation_web import wait_pending

    wait_pending(client, next_run["run_id"])
    again = client.get(f"/api/runs/{run_id}/export")
    assert again.content == first.content and again.headers["X-Project-Current"] == "false"
    assert (
        client.get(f"/api/runs/{run_id}/project-calculation").json()["data"]["historical"] is True
    )


def test_started_crash_then_direct_events_recovery(project_client, tmp_path):
    from dataclasses import replace
    from water_agent.storage.repository import RunRepository

    client, app, db, user, session, plan, headers = project_client
    runtime = app.state.run_manager.project_runtime
    row = RunRepository(db).create(session.thread_id, user.user_id)
    ctx = replace(
        runtime.selector(user.user_id, session.thread_id, row.run_id), plan_id=plan["plan_id"]
    )
    runtime.service.confirm(plan, ctx, base_revision=0, generation=0)
    RunRepository(db).set_status(row.run_id, user.user_id, "running", expected_statuses=("queued",))
    child = """
import os,sys
from pathlib import Path
from sqlalchemy import create_engine
from water_agent.storage.database import Database
from water_agent.webapp.run_manager import RunManager
from water_agent.tools import project_calculation as tools
manager=RunManager(Database(engine=create_engine(sys.argv[1])),export_dir=Path(sys.argv[2]),session_dir=Path(sys.argv[3]),memory_enabled=False)
runtime=manager.project_runtime
ctx=runtime.context(sys.argv[4])
runtime.service.fault=lambda stage:os._exit(17) if stage=='after_started' else None
tools.run_calc=lambda *_:os._exit(99)
runtime.service.execute(ctx)
"""
    process = subprocess.run(
        [
            "_venv/bin/python",
            "-c",
            child,
            str(db.engine.url),
            str(tmp_path / "exports"),
            str(tmp_path / "sessions"),
            str(row.run_id),
        ],
        env={**os.environ, "PYTHONPATH": "src"},
        capture_output=True,
        text=True,
    )
    assert process.returncode == 17, process.stderr
    first = events(client.get(f"/api/runs/{row.run_id}/events"))
    assert first[-2]["payload"]["status"] == "blocked"
    assert first[-2]["payload"]["error"] == "PROJECT_EXECUTION_INDETERMINATE"
    assert events(client.get(f"/api/runs/{row.run_id}/events")) == first


def test_old_failed_run_cancel_after_revision_keeps_original_terminal(project_client):
    client, app, db, user, session, plan, headers = project_client
    run_id, _ = begin(project_client)
    client.post(
        f"/api/runs/{run_id}/project-approval",
        json={"approved": False, "reason": "reject"},
        headers=headers,
    )
    before = events(client.get(f"/api/runs/{run_id}/events"))
    runtime = app.state.run_manager.project_runtime
    preview = runtime.preview(user.user_id, session.thread_id, plan, 1, 1)
    next_run = runtime.execute(
        user.user_id, session.thread_id, preview["confirmation_id"], preview["plan_hash"]
    )
    from test_project_calculation_web import wait_pending

    wait_pending(client, next_run["run_id"])
    response = client.post(f"/api/runs/{run_id}/cancel", headers=headers)
    assert response.status_code == 202 and response.json()["data"]["status"] == "failed"
    assert events(client.get(f"/api/runs/{run_id}/events")) == before
