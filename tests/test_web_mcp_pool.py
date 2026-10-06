"""Web 路径接上 MCP 连接池（P7-S5-A07）。

会话工厂走真实的 ``build_agentic_session`` → ``AgenticSession``（mock 生成器、fixture 语料），只把
PostgreSQL checkpointer 换成内存实现；MCP 用 ``fake_mcp_server.py --mode stateful``。
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event

from water_agent.mcp.config import MCP_CONFIG_VERSION
from water_agent.runtime.governance.binding import local_request_context
from water_agent.storage.database import Database
from water_agent.storage.models import Base
from water_agent.webapp import run_manager as rm

FAKE_SERVER = Path(__file__).resolve().parent / "fixtures" / "fake_mcp_server.py"


@pytest.fixture
def shared_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps(
            {
                "version": MCP_CONFIG_VERSION,
                "servers": [
                    {
                        "server_id": "webpool",
                        "command": sys.executable,
                        "args": [str(FAKE_SERVER), "--mode", "stateful"],
                        "allow": ["whoami", "bump", "slow_echo"],
                        "lifecycle": "shared",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    for key in ("WATER_AGENT_MCP_OVERLAY", "WATER_AGENT_TOOL_EXPOSURE", "DRAWING_ROOT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WATER_AGENT_MCP_CONFIG", str(path))
    return path


@pytest.fixture
def factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[rm.WebAgenticSessionFactory]:
    from langgraph.checkpoint.memory import InMemorySaver

    from water_agent.session import default_checkpoint_serde

    @contextmanager
    def memory_checkpointer(_database: Any) -> Iterator[Any]:
        yield InMemorySaver(serde=default_checkpoint_serde())

    monkeypatch.setattr(rm, "postgres_checkpointer", memory_checkpointer)
    monkeypatch.setattr(rm, "ScopedPostgresSaver", lambda saver, _sessions, _user: saver)
    instance = rm.WebAgenticSessionFactory(
        None,  # type: ignore[arg-type]  # checkpointer 已换成内存实现，不碰数据库
        None,  # type: ignore[arg-type]
        trace_dir=tmp_path / "traces",
        session_dir=tmp_path / "sessions",
        mock_generator=True,
    )
    yield instance
    instance.close()


def _create(factory: rm.WebAgenticSessionFactory, stack: ExitStack) -> Any:
    thread_id = uuid4().hex
    return factory.create(
        run_id=uuid4().hex,
        thread_id=thread_id,
        user_id=uuid4(),
        principal=local_request_context(thread_id=thread_id).principal,
        project_id=None,
        stack=stack,
    )


def _pid(session: Any) -> int:
    client = session.mcp_assembly.sessions[0].client
    return int(client.call_tool("whoami", {})["structured_content"]["pid"])


def _gone(pid: int) -> bool:
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_web_runs_reuse_shared_server(shared_config: Path, factory: rm.WebAgenticSessionFactory) -> None:
    """P7-S5-A07：连续两个 Web run 复用同一台 shared server 的进程；run 结束只释放租约，进程留在池里；
    工具照常注册进会话（复用的是进程，不是授权）。"""

    with ExitStack() as stack:
        first = _create(factory, stack)
        pid = _pid(first)
        assert first.mcp_assembly.connections == {"webpool": "shared_new"}
        assert "mcp_webpool_whoami" in first.mcp_tool_names
        assert "mcp_invoke" in {cap.value for cap in first.request_context.principal.capabilities}
    assert factory.mcp_pool.snapshot()["active"]["webpool"] == {"refs": 0, "alive": True}

    with ExitStack() as stack:
        second = _create(factory, stack)
        assert _pid(second) == pid
        assert second.mcp_assembly.connections == {"webpool": "shared_reused"}

    assert factory.close() == {"closed_servers": 1, "outstanding_leases": 0}
    assert _gone(pid)


def test_lease_is_released_when_session_construction_fails(
    shared_config: Path, factory: rm.WebAgenticSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P7-S5-A07：会话构造在借到连接之后抛错，租约照样被释放（不泄漏引用计数）。"""

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("会话构造失败")

    monkeypatch.setattr(rm, "build_agentic_session", explode)
    with pytest.raises(RuntimeError, match="会话构造失败"), ExitStack() as stack:
        _create(factory, stack)
    snapshot = factory.mcp_pool.snapshot()
    assert snapshot["leases"] == 0
    assert snapshot["active"]["webpool"]["refs"] == 0


def test_bad_config_fails_before_any_lease(
    shared_config: Path, factory: rm.WebAgenticSessionFactory
) -> None:
    """P7-S5-A07：坏配置在借用之前就 MCP_CONFIG_INVALID（不启动任何子进程、不留租约）。"""

    from water_agent.mcp.errors import MCP_CONFIG_INVALID, MCPError

    shared_config.write_text("{ 这不是 JSON", encoding="utf-8")
    with pytest.raises(MCPError) as exc, ExitStack() as stack:
        _create(factory, stack)
    assert exc.value.code == MCP_CONFIG_INVALID
    assert factory.mcp_pool.snapshot() == {"active": {}, "retired": 0, "leases": 0, "closed": False}


def test_run_manager_close_drains_runs_then_closes_the_pool(
    shared_config: Path, factory: rm.WebAgenticSessionFactory, tmp_path: Path
) -> None:
    """P7-S5-A07：``RunManager.close()`` 先排空 run，再关 MCP 连接池；结果一并报告。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'pool.sqlite'}")

    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    database = Database(engine=engine)
    try:
        manager = rm.RunManager(database, session_factory=factory, memory_enabled=False)
        with ExitStack() as stack:
            pid = _pid(_create(factory, stack))
        drained = manager.close(timeout_seconds=1.0)
        assert drained["graceful"] is True
        assert drained["mcp_pool"] == {"closed_servers": 1, "outstanding_leases": 0}
        assert _gone(pid)
    finally:
        database.dispose()
