"""P5 Step 46 Memory MCP adapter isolation and governance tests."""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event

from water_agent.mcp.config import MCPGatewayConfig, MCPServerConfig
from water_agent.mcp.memory_adapter import (
    READ_ONLY_MEMORY_TOOL_NAMES,
    MemoryMCPAdapter,
    filter_memory_mcp_allowlist,
)
from water_agent.memory.contracts import MemoryKind, MemoryOrigin, MemoryScopeType
from water_agent.memory.providers import (
    MemoryMCPProvider,
    MemoryProviderUnavailableError,
)
from water_agent.memory.recall import MemoryRecallService
from water_agent.memory.repository import MemoryRepository
from water_agent.memory.service import MemoryService
from water_agent.runtime.governance.contracts import Capability
from water_agent.runtime.governance.inventory import (
    capability_for_tool,
    clear_mcp_tool_bindings,
)
from water_agent.storage.database import Database
from water_agent.storage.models import Base


@pytest.fixture
def database(tmp_path: Path) -> Database:
    engine = create_engine(f"sqlite:///{tmp_path / 'step46-mcp.sqlite'}")

    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    database = Database(engine=engine)
    yield database
    database.dispose()


def _user(database: Database) -> tuple[UUID, UUID]:
    from water_agent.webapp.auth.db_store import PostgresUserStore
    from water_agent.webapp.auth.models import UserRole

    user = PostgresUserStore(database).create_user(
        f"step46-mcp-{uuid4().hex[:10]}", "Step46Pass2026!", UserRole.MEMBER
    )
    return user.user_id, uuid4()


def _memory(database: Database, user_id: UUID, tenant_id: UUID, content: str, key: str):
    return MemoryService(MemoryRepository(database)).create_memory(
        tenant_id=tenant_id,
        actor_user_id=user_id,
        scope_type=MemoryScopeType.USER,
        kind=MemoryKind.USER_PREFERENCE,
        semantic_key=key,
        content=content,
        origin=MemoryOrigin.EXPLICIT_USER,
    )


def test_memory_mcp_allowlist_only_keeps_read_search_tools() -> None:
    assert filter_memory_mcp_allowlist(
        ("memory_search", "memory_forget", "memory_write", "search_memory")
    ) == ("memory_search", "search_memory")
    assert READ_ONLY_MEMORY_TOOL_NAMES.isdisjoint({"memory_forget", "memory_write"})


def test_memory_mcp_open_uses_the_existing_p4_bridge_and_capability_registration() -> None:
    fixture = Path(__file__).parent / "fixtures" / "fake_memory_mcp_server.py"
    config = MCPServerConfig(
        server_id="memory_fixture",
        command=sys.executable,
        args=(str(fixture),),
        allow=("memory_search",),
        startup_timeout_seconds=2.0,
        call_timeout_seconds=2.0,
    )
    gateway = MCPGatewayConfig(servers=(), confirmation_handler_ready=False)
    adapter = MemoryMCPAdapter.open(config, gateway)
    try:
        assert adapter.search_ids(
            query="单位",
            tenant_id=uuid4(),
            actor_user_id=uuid4(),
            project_id=None,
            limit=8,
        )
        assert adapter.schema_token_cost < 4500
        assert capability_for_tool("mcp_memory_fixture_memory_search") is Capability.MCP_INVOKE
    finally:
        adapter.close()
        clear_mcp_tool_bindings()


def test_memory_mcp_dangerous_confirmation_is_not_exposed() -> None:
    fixture = Path(__file__).parent / "fixtures" / "fake_memory_mcp_server.py"
    config = MCPServerConfig(
        server_id="memory_fixture",
        command=sys.executable,
        args=(str(fixture),),
        allow=("memory_search",),
        human_confirmation=True,
        startup_timeout_seconds=2.0,
        call_timeout_seconds=2.0,
    )
    gateway = MCPGatewayConfig(servers=(), confirmation_handler_ready=False)

    with pytest.raises(MemoryProviderUnavailableError, match="not available"):
        MemoryMCPAdapter.open(config, gateway)
    clear_mcp_tool_bindings()


def test_memory_mcp_provider_uses_native_authorized_body_not_remote_body(
    database: Database,
) -> None:
    user_id, tenant_id = _user(database)
    memory = _memory(database, user_id, tenant_id, "native memory body", "native")

    adapter = MemoryMCPAdapter.from_invoker(
        lambda payload: {
            "ok": True,
            "structured_content": {
                "hits": [
                    {
                        "memory_id": str(memory.memory_id),
                        "score": 0.9,
                        "content": "untrusted remote body",
                    }
                ]
            },
        },
        search_tool_name="memory_search",
    )
    provider = MemoryMCPProvider(adapter, MemoryRepository(database))
    result = MemoryRecallService(database, provider=provider).recall(
        tenant_id=tenant_id,
        actor_user_id=user_id,
        query="单位",
    )

    assert len(result.hits) == 1
    assert result.hits[0].memory.content == "native memory body"
    assert "untrusted remote body" not in result.context_text


def test_memory_mcp_provider_failure_falls_back_to_native_fts(database: Database) -> None:
    user_id, tenant_id = _user(database)
    memory = _memory(database, user_id, tenant_id, "native FTS body 单位", "fts")

    class BrokenAdapter:
        def search_ids(self, **kwargs):
            del kwargs
            raise MemoryProviderUnavailableError("MCP unavailable")

    provider = MemoryMCPProvider(BrokenAdapter(), MemoryRepository(database))
    result = MemoryRecallService(database, provider=provider).recall(
        tenant_id=tenant_id,
        actor_user_id=user_id,
        query="单位",
    )

    assert result.fallback_used is True
    assert [hit.memory.memory_id for hit in result.hits] == [memory.memory_id]


def test_memory_mcp_remote_ids_are_filtered_by_native_scope(database: Database) -> None:
    user_id, tenant_id = _user(database)
    other_user, _ = _user(database)
    other_memory = _memory(database, other_user, tenant_id, "other user body 单位", "other")

    adapter = MemoryMCPAdapter.from_invoker(
        lambda payload: {
            "ok": True,
            "structured_content": {
                "hits": [{"memory_id": str(other_memory.memory_id), "score": 1.0}]
            },
        },
        search_tool_name="memory_search",
    )
    provider = MemoryMCPProvider(adapter, MemoryRepository(database))
    result = MemoryRecallService(database, provider=provider).recall(
        tenant_id=tenant_id,
        actor_user_id=user_id,
        query="单位",
    )

    assert result.hits == ()
