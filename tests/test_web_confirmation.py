"""P7-S7：Web 入口的人工确认（``waiting_confirmation`` 态、``confirmation_required`` 事件、决定接口）。

走真实的 ``WebAgenticSessionFactory`` → ``AgenticSession(confirmation=True)``（mock 生成器、fixture 语料），
只把模型换成跨 run 共用的脚本模型、把 PostgreSQL checkpointer 换成进程内共用的内存实现（SQLite 上没有
PG saver；``ScopedPostgresSaver`` 的归属校验照旧是真的）。MCP 用 ``fake_mcp_server.py --mode stateful``，
``lifecycle: shared``：进程跨 run 保留，``peek`` 能直接读到 ``bump`` 的副作用有没有发生。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine, event

from water_agent.harness.doubles import ScriptedChatModel
from water_agent.mcp.config import MCP_CONFIG_VERSION
from water_agent.mcp.wiring import load_mcp_gateway
from water_agent.retrieval.trivium import TriviumRetriever
from water_agent.runtime.agentic import AgenticSession
from water_agent.session import default_checkpoint_serde
from water_agent.storage.database import Database
from water_agent.storage.models import Base
from water_agent.storage.repository import ProjectRepository, RunRepository, WebSessionRepository
from water_agent.webapp import run_manager as rm
from water_agent.webapp.app import create_app
from water_agent.webapp.auth.csrf import CSRF_COOKIE_NAME
from water_agent.webapp.auth.db_store import PostgresUserStore
from water_agent.webapp.auth.models import UserRole

FAKE_SERVER = Path(__file__).resolve().parent / "fixtures" / "fake_mcp_server.py"
SERVER = "s7web"
BUMP = f"mcp_{SERVER}_bump"
PASSWORD = "WebConfirm2026!"


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    engine = create_engine(f"sqlite:///{tmp_path / 'web-confirmation.sqlite'}")

    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    database = Database(engine=engine)
    yield database
    database.dispose()


class _Harness:
    """一个"Web 进程"：app + RunManager + 会话工厂（连接池），与其他进程共用 checkpoint 与脚本模型。"""

    def __init__(self, database: Database, tmp_path: Path, saver: InMemorySaver) -> None:
        self.factory = rm.WebAgenticSessionFactory(
            database,
            WebSessionRepository(database),
            trace_dir=tmp_path / "traces",
            session_dir=tmp_path / "sessions",
            mock_generator=True,
        )
        self.app = create_app(
            static_dir=tmp_path,
            database=database,
            session_factory=self.factory,
            memory_enabled=False,
            trace_dir=tmp_path / "traces",
            upload_dir=tmp_path / "uploads",
            export_dir=tmp_path / "exports",
        )
        manager = self.app.state.run_manager

        @contextmanager
        def shared_checkpointer(_database: Any) -> Iterator[Any]:
            yield saver

        manager.checkpointer_factory = shared_checkpointer
        self.manager = manager

    def client(self, database: Database, username: str) -> TestClient:
        client = TestClient(self.app, raise_server_exceptions=True)
        response = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
        assert response.status_code == 200, response.text
        return client

    def peek(self) -> int:
        """直接读 shared server 进程里的计数器（不经注册表，只读）。"""
        lease = self.factory.mcp_pool.lease(load_mcp_gateway(), confirmation_ready=True)
        try:
            client = lease.assembly.sessions[0].client
            assert client is not None
            return int(client.call_tool("peek", {})["structured_content"]["counter"])
        finally:
            lease.release()

    def close(self) -> None:
        self.manager.close(timeout_seconds=2.0)


@pytest.fixture
def web(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database: Database) -> Iterator[dict[str, Any]]:
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "version": MCP_CONFIG_VERSION,
                "servers": [
                    {
                        "server_id": SERVER,
                        "command": sys.executable,
                        "args": [str(FAKE_SERVER), "--mode", "stateful"],
                        "allow": ["bump"],
                        "human_confirmation": True,
                        "lifecycle": "shared",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    for key in ("WATER_AGENT_MCP_OVERLAY", "WATER_AGENT_TOOL_EXPOSURE", "DRAWING_ROOT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WATER_AGENT_MCP_CONFIG", str(config))

    saver = InMemorySaver(serde=default_checkpoint_serde())
    script: dict[str, ScriptedChatModel] = {}

    @contextmanager
    def shared_checkpointer(_database: Any) -> Iterator[Any]:
        yield saver

    def build_with_script(args: argparse.Namespace, **kwargs: Any) -> AgenticSession:
        # 只换模型与语料；装配（MCP 租约、确认门、注册表、护栏、checkpoint）全走生产路径
        return AgenticSession(
            args,
            model=script["model"],
            retriever=TriviumRetriever(db_path=None, fallback_clauses=[]),
            **kwargs,
        )

    monkeypatch.setattr(rm, "postgres_checkpointer", shared_checkpointer)
    monkeypatch.setattr(rm, "build_agentic_session", build_with_script)
    harnesses: list[_Harness] = []

    def new_process() -> _Harness:
        harness = _Harness(database, tmp_path, saver)
        harnesses.append(harness)
        return harness

    yield {"new_process": new_process, "script": script, "saver": saver}
    for harness in harnesses:
        harness.close()


def _user(database: Database, prefix: str) -> tuple[UUID, str]:
    username = f"{prefix}-{uuid4().hex[:8]}"
    user = PostgresUserStore(database).create_user(username, PASSWORD, UserRole.MEMBER)
    return user.user_id, username


def _csrf(client: TestClient) -> dict[str, str]:
    value = client.cookies.get(CSRF_COOKIE_NAME)
    assert value
    return {"X-CSRF-Token": value}


def _events(client: TestClient, run_id: str, cursor: str | None = None) -> list[dict[str, Any]]:
    headers = {"Last-Event-ID": cursor} if cursor else {}
    response = client.get(f"/api/runs/{run_id}/events", headers=headers)
    assert response.status_code == 200, response.text
    events: list[dict[str, Any]] = []
    for block in response.text.strip().split("\n\n"):
        lines = [line[6:] for line in block.splitlines() if line.startswith("data: ")]
        if lines:
            events.append(json.loads("\n".join(lines)))
    return events


def _ask(client: TestClient, thread_id: str, text: str = "请把计数器加一") -> Any:
    return client.post(
        f"/api/sessions/{thread_id}/messages", json={"message": text}, headers=_csrf(client)
    )


def _decide(client: TestClient, run_id: str, confirmation_id: str, approved: Any, **extra: Any) -> Any:
    return client.post(
        f"/api/runs/{run_id}/confirmation",
        json={"confirmation_id": confirmation_id, "approved": approved, **extra},
        headers=_csrf(client),
    )


def _wait_for_suspension(client: TestClient, run_id: str) -> dict[str, Any]:
    events = _events(client, run_id)
    assert events[-1]["type"] == "stream_end", [e["type"] for e in events]
    assert events[-1]["payload"]["status"] == "waiting_confirmation"
    assert "finalize" not in [e["type"] for e in events]
    required = [e for e in events if e["type"] == "confirmation_required"]
    assert len(required) == 1
    return required[0]["payload"]


def test_confirmation_round_trip_on_the_same_run(web: dict[str, Any], database: Database) -> None:
    """P7-S7-A06：作答停在确认门 → run 为 ``waiting_confirmation``、SSE 发 ``confirmation_required`` 后以
    ``stream_end(waiting_confirmation)`` 结束；挂起期间发消息 409 ``WEB_CONFIRMATION_PENDING`` 且不建 run；
    历史投影带出挂起的请求；批准 202 后**同一个 run** 继续（按返回的 event_cursor 重新订阅），副作用恰好
    一次；重复提交 409 ``WEB_CONFIRMATION_STALE``。"""

    web["script"]["model"] = ScriptedChatModel(
        steps=[
            {"tool_calls": [{"name": BUMP, "args": {"note": "web-note"}}]},
            {"content": "已按你的确认把计数器加一。"},
        ]
    )
    process = web["new_process"]()
    user_id, username = _user(database, "owner")
    thread_id = WebSessionRepository(database).create(user_id, title="确认").thread_id
    client = process.client(database, username)

    created = _ask(client, thread_id)
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["run_id"]
    request = _wait_for_suspension(client, run_id)
    assert request["tool_name"] == BUMP
    assert request["server_id"] == SERVER
    assert '"note": "web-note"' in request["arguments_text"]
    assert request["arguments_redacted"] is False
    assert RunRepository(database).get_for_user(run_id, user_id).status == "waiting_confirmation"
    assert process.peek() == 0

    history = client.get(f"/api/sessions/{thread_id}/messages").json()["data"]
    assert history["pending_confirmation"]["run_id"] == run_id
    assert history["pending_confirmation"]["confirmation_id"] == request["confirmation_id"]
    session = client.get(f"/api/sessions/{thread_id}").json()["data"]
    assert session["latest_run"]["status"] == "waiting_confirmation"

    blocked = _ask(client, thread_id, "另一个问题")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "WEB_CONFIRMATION_PENDING"
    assert len(RunRepository(database).list_for_session(thread_id, user_id)) == 1

    decided = _decide(client, run_id, request["confirmation_id"], True)
    assert decided.status_code == 202, decided.text
    body = decided.json()["data"]
    assert body["run_id"] == run_id
    assert body["status"] == "queued"
    resumed = _events(client, run_id, body["event_cursor"])
    kinds = [e["type"] for e in resumed]
    assert kinds[:2] == ["confirmation_decided", "run_queued"]
    assert resumed[0]["payload"]["decision"] == "approved"
    finalize = next(e for e in resumed if e["type"] == "finalize")
    assert finalize["payload"]["status"] == "succeeded", finalize
    assert finalize["payload"]["answer"] == "已按你的确认把计数器加一。"
    results = [e for e in resumed if e["type"] == "tool_result" and e["payload"]["tool_name"] == BUMP]
    assert [e["payload"]["status"] for e in results] == ["succeeded"]
    assert all(e["run_id"] == run_id for e in resumed)
    assert process.peek() == 1
    assert RunRepository(database).get_for_user(run_id, user_id).status == "succeeded"
    assert len(RunRepository(database).list_for_session(thread_id, user_id)) == 1
    assert client.get(f"/api/sessions/{thread_id}/messages").json()["data"]["pending_confirmation"] is None

    again = _decide(client, run_id, request["confirmation_id"], True)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "WEB_CONFIRMATION_STALE"
    assert process.peek() == 1


def test_confirmation_endpoint_authorization(web: dict[str, Any], database: Database) -> None:
    """P7-S7-A06：决定接口的检查顺序——别人的 run（外人、项目所有者都不是 run 所有者）404；run 所有者
    被降为 viewer 后 403；``confirmation_id`` 过期 409 ``WEB_CONFIRMATION_STALE``；``approved`` 不是严格
    bool 422；全部被拒的请求都不改变 run 状态、不执行。正确的拒绝 202，零次执行。"""

    web["script"]["model"] = ScriptedChatModel(
        steps=[
            {"tool_calls": [{"name": BUMP, "args": {"note": "auth"}}]},
            {"content": "你拒绝了这次操作，计数器没有改动。"},
        ]
    )
    process = web["new_process"]()
    owner_id, owner_name = _user(database, "powner")
    member_id, member_name = _user(database, "member")
    _outsider_id, outsider_name = _user(database, "outsider")
    projects = ProjectRepository(database)
    project = projects.create(owner_id, "确认项目")
    projects.add_member(project.project_id, owner_id, member_id, role="member")
    thread_id = WebSessionRepository(database).create(
        owner_id, project_id=project.project_id, title="项目会话"
    ).thread_id

    member = process.client(database, member_name)
    created = _ask(member, thread_id)
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["run_id"]
    request = _wait_for_suspension(member, run_id)
    cid = request["confirmation_id"]

    outsider = process.client(database, outsider_name)
    owner = process.client(database, owner_name)
    for other in (outsider, owner):
        refused = _decide(other, run_id, cid, True)
        assert refused.status_code == 404, refused.text
        assert refused.json()["error"]["code"] == "WEB_NOT_FOUND"

    projects.add_member(project.project_id, owner_id, member_id, role="viewer")
    demoted = _decide(member, run_id, cid, True)
    assert demoted.status_code == 403
    assert demoted.json()["error"]["code"] == "WEB_FORBIDDEN"
    projects.add_member(project.project_id, owner_id, member_id, role="member")

    stale = _decide(member, run_id, "not-the-pending-one", True)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "WEB_CONFIRMATION_STALE"
    assert _decide(member, run_id, cid, "yes").status_code == 422
    assert RunRepository(database).get_for_user(run_id, member_id).status == "waiting_confirmation"
    assert process.peek() == 0

    rejected = _decide(member, run_id, cid, False, reason="先不要动")
    assert rejected.status_code == 202, rejected.text
    resumed = _events(member, run_id, rejected.json()["data"]["event_cursor"])
    assert resumed[0]["payload"]["decision"] == "rejected"
    finalize = next(e for e in resumed if e["type"] == "finalize")
    assert finalize["payload"]["status"] == "succeeded", finalize
    result = next(e for e in resumed if e["type"] == "tool_result" and e["payload"]["tool_name"] == BUMP)
    assert result["payload"]["status"] == "failed"
    assert result["payload"]["code"] == "TOOL_CONFIRMATION_REJECTED"
    assert process.peek() == 0


def test_pending_confirmation_survives_restart(web: dict[str, Any], database: Database) -> None:
    """P7-S7-A06：挂起状态在 checkpoint 里——新建 RunManager（模拟进程重启，事件日志与连接池都没了）后，
    历史仍带出挂起的请求、新消息仍 409，决定照常 202（``event_cursor`` 从 0 开始）并在同一个 run 上收尾，
    恰好执行一次。"""

    web["script"]["model"] = ScriptedChatModel(
        steps=[
            {"tool_calls": [{"name": BUMP, "args": {"note": "restart"}}]},
            {"content": "重启后按你的确认执行了。"},
        ]
    )
    before = web["new_process"]()
    user_id, username = _user(database, "restart")
    thread_id = WebSessionRepository(database).create(user_id, title="重启").thread_id
    client = before.client(database, username)
    run_id = _ask(client, thread_id).json()["data"]["run_id"]
    request = _wait_for_suspension(client, run_id)
    before.close()

    after = web["new_process"]()
    client = after.client(database, username)
    assert client.get(f"/api/runs/{run_id}/events").status_code == 404, "新进程里没有旧的事件日志"
    pending = client.get(f"/api/sessions/{thread_id}/messages").json()["data"]["pending_confirmation"]
    assert pending["run_id"] == run_id
    assert pending["confirmation_id"] == request["confirmation_id"]
    assert _ask(client, thread_id, "另一个问题").json()["error"]["code"] == "WEB_CONFIRMATION_PENDING"

    decided = _decide(client, run_id, request["confirmation_id"], True)
    assert decided.status_code == 202, decided.text
    assert decided.json()["data"]["event_cursor"] == "0"
    resumed = _events(client, run_id)
    finalize = next(e for e in resumed if e["type"] == "finalize")
    assert finalize["payload"]["status"] == "succeeded", finalize
    results = [e for e in resumed if e["type"] == "tool_result" and e["payload"]["tool_name"] == BUMP]
    assert [e["payload"]["status"] for e in results] == ["succeeded"]
    assert after.peek() == 1
    assert RunRepository(database).get_for_user(run_id, user_id).status == "succeeded"
