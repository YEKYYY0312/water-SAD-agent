"""同一会话同一时间只允许一个运行中的 run（review 2026-09-30）。

此前 ``POST /api/sessions/{thread_id}/messages`` 没有任何按 thread 的互斥：两个 run 同时
写同一个 checkpoint，后写者覆盖先写者——实测两个 run 都 succeeded，最新 checkpoint 里只剩
B 的问题和回答，A 从历史和后续上下文里静默消失。单个标签页流式时会禁用发送，但两个标签页、
两个项目成员或直接调 API 都能触发。

互斥放在 RunManager 进程内：Web 是单实例（见 RunManager 与 Step 38 的约定），且
``create_message`` 从检查到 ``manager.start`` 之间没有 ``await``，单 worker 下这段是原子的。
刻意不按数据库里的 queued/running 行判断：进程崩溃后残留的行会把会话永久锁死。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select

from water_agent.observability.json_trace import JsonTraceRecorder
from water_agent.storage.database import Database
from water_agent.storage.models import Base, Run
from water_agent.storage.repository import WebSessionRepository
from water_agent.webapp.app import create_app
from water_agent.webapp.auth.csrf import CSRF_COOKIE_NAME
from water_agent.webapp.auth.db_store import PostgresUserStore
from water_agent.webapp.auth.models import UserRole
from water_agent.webapp.run_manager import RunCancelled

_PASSWORD = "ExclusiveRun2026!"
_MAX_WAIT_SECONDS = 5.0


class _WaitingSession:
    """运行到被取消为止的假会话（模拟一个还没答完的问题）。

    最多等 ``_MAX_WAIT_SECONDS`` 后自行收口：断言失败时没人来取消，无界等待会让
    RunManager 的线程池在解释器退出时 join 不掉、整个 pytest 挂死。
    """

    def __init__(self, recorder: JsonTraceRecorder) -> None:
        self.recorder = recorder
        self.started = Event()

    def start_trace(self, run_id: str, query: str) -> None:
        self.recorder.start(run_id=run_id, trace_id=f"trace-{run_id}", query=query)

    async def aask(self, query, *, run_id, memory_context, on_token, on_event, cancel_check) -> dict:
        del query, run_id, memory_context, on_token
        self.started.set()
        on_event("values", {"status": "running", "messages": []})
        deadline = time.monotonic() + _MAX_WAIT_SECONDS
        while not cancel_check():
            if time.monotonic() > deadline:
                self.recorder.finish("succeeded")
                return {"status": "succeeded", "answer": "", "citations": [], "errors": [], "warnings": []}
            await asyncio.sleep(0.005)
        raise RunCancelled()


class _Factory:
    def __init__(self, trace_dir: Path) -> None:
        self.trace_dir = trace_dir
        self.sessions: list[_WaitingSession] = []

    def create(self, *, run_id, thread_id, user_id, principal, project_id, stack):
        del run_id, thread_id, user_id, principal, project_id, stack
        session = _WaitingSession(JsonTraceRecorder(self.trace_dir))
        self.sessions.append(session)
        return session


@pytest.fixture
def database(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'exclusive.sqlite'}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    db = Database(engine=engine)
    yield db
    db.dispose()


def _client(database: Database, tmp_path: Path) -> tuple[TestClient, _Factory, UUID]:
    username = f"exclusive-{uuid4().hex[:8]}"
    user = PostgresUserStore(database).create_user(username, _PASSWORD, UserRole.MEMBER)
    factory = _Factory(tmp_path / "traces")
    app = create_app(
        static_dir=tmp_path,
        database=database,
        session_factory=factory,
        memory_enabled=False,
        trace_dir=tmp_path / "traces",
    )
    client = TestClient(app, raise_server_exceptions=True)
    assert client.post("/api/auth/login", json={"username": username, "password": _PASSWORD}).status_code == 200
    return client, factory, user.user_id


def _wait_started(factory: _Factory, count: int) -> None:
    """RunManager 在工作线程里建会话：POST 返回 202 时会话可能还没创建。"""
    deadline = time.monotonic() + 5
    while len(factory.sessions) < count and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(factory.sessions) >= count
    assert factory.sessions[count - 1].started.wait(5)


def _send(client: TestClient, thread_id: str, text: str):
    return client.post(
        f"/api/sessions/{thread_id}/messages",
        json={"message": text},
        headers={"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME) or ""},
    )


def _run_count(database: Database, thread_id: str) -> int:
    with database.transaction() as session:
        return int(session.execute(select(func.count()).select_from(Run).where(Run.thread_id == thread_id)).scalar_one())


def test_second_message_on_a_busy_thread_is_refused_without_a_run_row(database: Database, tmp_path: Path) -> None:
    client, factory, user_id = _client(database, tmp_path)
    thread_id = WebSessionRepository(database).create(user_id, title="t").thread_id

    first = _send(client, thread_id, "问题 A：设计秒流量")
    assert first.status_code == 202, first.text
    _wait_started(factory, 1)

    second = _send(client, thread_id, "问题 B：最小管径")
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "WEB_RUN_IN_PROGRESS"
    assert _run_count(database, thread_id) == 1, "被拒的请求不得留下 queued 的 run 行"

    run_id = first.json()["data"]["run_id"]
    cancel = client.post(
        f"/api/runs/{run_id}/cancel", headers={"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME) or ""}
    )
    assert cancel.status_code in (200, 202), cancel.text
    client.get(f"/api/runs/{run_id}/events")  # 读到 finalize 为止：取消已收口

    third = _send(client, thread_id, "问题 B：最小管径")
    assert third.status_code == 202, "前一个 run 收口后同一会话可以继续发"
    _wait_started(factory, 2)
    third_run = third.json()["data"]["run_id"]
    client.post(f"/api/runs/{third_run}/cancel", headers={"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME) or ""})
    client.get(f"/api/runs/{third_run}/events")


def test_different_threads_run_concurrently(database: Database, tmp_path: Path) -> None:
    client, factory, user_id = _client(database, tmp_path)
    sessions = WebSessionRepository(database)
    first_thread = sessions.create(user_id, title="a").thread_id
    second_thread = sessions.create(user_id, title="b").thread_id
    first = _send(client, first_thread, "问题 A")
    second = _send(client, second_thread, "问题 B")
    assert first.status_code == 202 and second.status_code == 202
    for response in (first, second):
        run_id = response.json()["data"]["run_id"]
        client.post(f"/api/runs/{run_id}/cancel", headers={"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME) or ""})
        client.get(f"/api/runs/{run_id}/events")
    assert len(factory.sessions) == 2


def test_start_itself_refuses_a_busy_thread_and_closes_the_extra_row(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """兜底路径：跳过路由里的预检，RunManager.start 自己也拒绝，并把刚建的行收口为 cancelled。"""
    from water_agent.webapp.run_manager import RunManager

    client, factory, user_id = _client(database, tmp_path)
    thread_id = WebSessionRepository(database).create(user_id, title="t").thread_id
    first = _send(client, thread_id, "问题 A")
    assert first.status_code == 202
    _wait_started(factory, 1)

    monkeypatch.setattr(RunManager, "thread_busy", lambda self, _thread_id: False)
    second = _send(client, thread_id, "问题 B")
    assert second.status_code == 409, second.text
    with database.transaction() as session:
        statuses = sorted(
            session.execute(select(Run.status).where(Run.thread_id == thread_id)).scalars().all()
        )
    assert statuses == ["cancelled", "running"]

    run_id = first.json()["data"]["run_id"]
    client.post(f"/api/runs/{run_id}/cancel", headers={"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME) or ""})
    client.get(f"/api/runs/{run_id}/events")
