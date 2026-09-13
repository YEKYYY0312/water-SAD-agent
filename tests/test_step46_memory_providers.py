"""P5 Step 46 memory provider contracts and safe FTS fallback."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event

from water_agent.memory.contracts import (
    MemoryKind,
    MemoryOrigin,
    MemoryScopeType,
    MemoryStoreUnavailableError,
)
from water_agent.memory.providers import (
    MemoryProviderRequest,
    MemoryProviderUnavailableError,
    PgVectorMemoryProvider,
    PostgresFtsMemoryProvider,
)
from water_agent.memory.recall import MemoryRecallService
from water_agent.memory.repository import MemoryRepository
from water_agent.memory.service import MemoryService
from water_agent.storage.database import Database
from water_agent.storage.models import Base


@pytest.fixture
def database(tmp_path: Path) -> Database:
    engine = create_engine(f"sqlite:///{tmp_path / 'step46-memory.sqlite'}")

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
        f"step46-{uuid4().hex[:10]}", "Step46Pass2026!", UserRole.MEMBER
    )
    return user.user_id, uuid4()


def _memory(database: Database, user_id: UUID, tenant_id: UUID, content: str):
    return MemoryService(MemoryRepository(database)).create_memory(
        tenant_id=tenant_id,
        actor_user_id=user_id,
        scope_type=MemoryScopeType.USER,
        kind=MemoryKind.USER_PREFERENCE,
        semantic_key="units",
        content=content,
        origin=MemoryOrigin.EXPLICIT_USER,
    )


def test_default_recall_uses_native_postgres_fts_provider(database: Database) -> None:
    user_id, tenant_id = _user(database)
    memory = _memory(database, user_id, tenant_id, "本人偏好 SI 单位")

    result = MemoryRecallService(database).recall(
        tenant_id=tenant_id,
        actor_user_id=user_id,
        query="SI 单位",
    )

    assert result.provider_name == PostgresFtsMemoryProvider.name
    assert [hit.memory.memory_id for hit in result.hits] == [memory.memory_id]
    assert result.fallback_used is False


def test_provider_failure_falls_back_to_fts_without_changing_authorization(
    database: Database,
) -> None:
    user_id, tenant_id = _user(database)
    owned = _memory(database, user_id, tenant_id, "本人偏好 SI 单位")
    other_user, _ = _user(database)
    _memory(database, other_user, tenant_id, "其他用户偏好 SI 单位")

    class BrokenProvider:
        name = "broken_provider"

        def search(self, request: MemoryProviderRequest):
            del request
            raise MemoryProviderUnavailableError("provider unavailable")

    result = MemoryRecallService(database, provider=BrokenProvider()).recall(
        tenant_id=tenant_id,
        actor_user_id=user_id,
        query="SI 单位",
    )

    assert result.provider_name == PostgresFtsMemoryProvider.name
    assert result.fallback_used is True
    assert [hit.memory.memory_id for hit in result.hits] == [owned.memory_id]


def test_pgvector_provider_fails_closed_without_postgresql_and_is_fallback_eligible(
    database: Database,
) -> None:
    provider = PgVectorMemoryProvider(lambda _: [1.0, 0.0], dimensions=2)
    request = MemoryProviderRequest(
        database=database,
        tenant_id=uuid4(),
        actor_user_id=uuid4(),
        query="单位",
        project_id=None,
        now=datetime.now(UTC),
    )

    with pytest.raises(MemoryProviderUnavailableError, match="PostgreSQL"):
        provider.search(request)


def test_memory_repository_forget_calls_embedding_cleanup_before_commit(database: Database) -> None:
    user_id, tenant_id = _user(database)
    deleted: list[str] = []

    class Cleanup:
        def delete_forget(self, session, memory_id: str) -> None:
            del session
            deleted.append(memory_id)

    repository = MemoryRepository(database, embedding_store=Cleanup())
    service = MemoryService(repository)
    memory = service.create_memory(
        tenant_id=tenant_id,
        actor_user_id=user_id,
        scope_type=MemoryScopeType.USER,
        kind=MemoryKind.USER_PREFERENCE,
        semantic_key="units",
        content="本人偏好 SI 单位",
        origin=MemoryOrigin.EXPLICIT_USER,
    )

    forgotten = service.forget_memory(
        memory.memory_id,
        tenant_id=tenant_id,
        actor_user_id=user_id,
        expected_revision=1,
    )

    assert forgotten.tombstone is True
    assert deleted == [str(memory.memory_id)]


def test_memory_repository_correction_invalidates_stale_embedding(database: Database) -> None:
    user_id, tenant_id = _user(database)
    deleted: list[str] = []

    class Cleanup:
        def delete_forget(self, session, memory_id: str) -> None:
            del session
            deleted.append(memory_id)

    repository = MemoryRepository(database, embedding_store=Cleanup())
    service = MemoryService(repository)
    memory = service.create_memory(
        tenant_id=tenant_id,
        actor_user_id=user_id,
        scope_type=MemoryScopeType.USER,
        kind=MemoryKind.USER_PREFERENCE,
        semantic_key="units",
        content="旧值",
        origin=MemoryOrigin.EXPLICIT_USER,
    )

    service.correct_memory(
        memory.memory_id,
        tenant_id=tenant_id,
        actor_user_id=user_id,
        expected_revision=1,
        content="新值",
    )

    assert deleted == [str(memory.memory_id)]


def test_embedding_cleanup_failure_does_not_report_forget_success(database: Database) -> None:
    user_id, tenant_id = _user(database)

    class FailingCleanup:
        def delete_forget(self, session, memory_id: str) -> None:
            del session, memory_id
            raise MemoryProviderUnavailableError("vector store unavailable")

    repository = MemoryRepository(database, embedding_store=FailingCleanup())
    service = MemoryService(repository)
    memory = service.create_memory(
        tenant_id=tenant_id,
        actor_user_id=user_id,
        scope_type=MemoryScopeType.USER,
        kind=MemoryKind.USER_PREFERENCE,
        semantic_key="units",
        content="待删除",
        origin=MemoryOrigin.EXPLICIT_USER,
    )

    with pytest.raises(MemoryStoreUnavailableError) as exc_info:
        service.forget_memory(
            memory.memory_id,
            tenant_id=tenant_id,
            actor_user_id=user_id,
            expected_revision=1,
        )

    assert getattr(exc_info.value, "code", None) == "MEMORY_STORE_UNAVAILABLE"
    current = service.get_memory(memory.memory_id, tenant_id=tenant_id, actor_user_id=user_id)
    assert current.status.value == "active"


def test_pgvector_vector_validation_rejects_bad_dimensions_and_non_finite_values(
    database: Database,
) -> None:
    provider = PgVectorMemoryProvider(lambda _: [1.0, float("nan")], dimensions=2)
    with pytest.raises(ValueError, match="finite"):
        provider._validate_vector([1.0, float("nan")])
    with pytest.raises(ValueError, match="dimensions"):
        provider._validate_vector([1.0])
