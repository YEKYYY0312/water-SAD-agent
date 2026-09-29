"""P5 Step 39 memory repository and service integration tests."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import SQLAlchemyError

from water_agent.memory.contracts import (
    MemoryConflictRequiresConfirmationError,
    MemoryEventType,
    MemoryForgottenError,
    MemoryKind,
    MemoryOrigin,
    MemoryRevisionConflictError,
    MemoryScopeDeniedError,
    MemoryScopeType,
    MemoryStatus,
    MemoryStoreUnavailableError,
)
from water_agent.memory.repository import MemoryRepository
from water_agent.memory.service import MemoryService
from water_agent.storage.database import MIGRATION_HEAD, Database
from water_agent.storage.models import Base
from water_agent.storage.repository import ProjectRepository


@pytest.fixture
def database(tmp_path: Path) -> Database:
    engine = create_engine(f"sqlite:///{tmp_path / 'memory.sqlite'}")

    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return Database(engine=engine)


def _insert_users(database: Database, *user_ids: UUID) -> None:
    now = datetime.now(UTC)
    with database.transaction() as session:
        for user_id in user_ids:
            session.execute(
                text(
                    "INSERT INTO users "
                    "(user_id, username, password_hash, role, must_change_password, "
                    "is_active, created_at, updated_at) "
                    "VALUES (:id, :name, 'hash', 'member', false, true, :now, :now)"
                ),
                {"id": str(user_id), "name": f"user-{user_id}", "now": now},
            )


def _service(database: Database) -> MemoryService:
    return MemoryService(MemoryRepository(database))


def _postgres_database() -> Database:
    import os

    url = os.getenv("P5_TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "P5_TEST_DATABASE_URL is not configured; PostgreSQL integration is environment-dependent"
        )
    database = Database(url=url)
    database.require_ready()
    return database


def _create_user_memory(
    service: MemoryService,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    semantic_key: str = "units",
    content: str = "SI",
    expires_at: datetime | None = None,
):
    return service.create_memory(
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        scope_type=MemoryScopeType.USER,
        kind=MemoryKind.USER_PREFERENCE,
        semantic_key=semantic_key,
        content=content,
        origin=MemoryOrigin.EXPLICIT_USER,
        expires_at=expires_at,
    )


def test_alembic_head_and_memory_tables_are_declared() -> None:
    migration = Path("alembic/versions/0002_step39_memory.py").read_text(encoding="utf-8")
    assert MIGRATION_HEAD == "0006_step50_dxf_uploads"
    for table in ("memory_records", "memory_events", "memory_usage"):
        assert f'"{table}"' in migration
    assert 'down_revision = "0001_step38"' in migration
    assert "def downgrade()" in migration


def test_step39_memory_package_has_no_automatic_chat_write_path() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in Path("src/water_agent/memory").glob("*.py")
    )
    assert "water_agent.graphs" not in source
    assert "checkpointer" not in source
    assert "ToolRegistry" not in source


def test_database_constraints_repeat_scope_invariants(database: Database) -> None:
    owner = uuid4()
    _insert_users(database, owner)
    project_row = ProjectRepository(database).create(owner, "scope-test")

    with pytest.raises(SQLAlchemyError), database.transaction() as session:
        session.execute(
            text(
                "INSERT INTO memory_records "
                "(memory_id, tenant_id, owner_user_id, scope_type, project_id, kind, "
                "semantic_key, content, status, origin, revision, created_at, updated_at, tombstone) "
                "VALUES (:memory_id, :tenant_id, :owner, 'user', :project, 'user_preference', "
                "'invalid-db-scope', 'body', 'active', 'explicit_user', 1, :now, :now, false)"
            ),
            {
                "memory_id": str(uuid4()),
                "tenant_id": str(uuid4()),
                "owner": str(owner),
                "project": str(project_row.project_id),
                "now": datetime.now(UTC),
            },
        )

    with pytest.raises(SQLAlchemyError), database.transaction() as session:
        session.execute(
            text(
                "INSERT INTO memory_records "
                "(memory_id, tenant_id, owner_user_id, scope_type, kind, semantic_key, "
                "content, status, origin, revision, created_at, updated_at, tombstone) "
                "VALUES (:memory_id, :tenant_id, :owner, 'user', 'user_preference', "
                "'invalid-db-body', 'must not remain', 'forgotten', 'explicit_user', 1, :now, :now, true)"
            ),
            {
                "memory_id": str(uuid4()),
                "tenant_id": str(uuid4()),
                "owner": str(owner),
                "now": datetime.now(UTC),
            },
        )


def test_user_and_project_memory_are_authorized_by_server_side_scope(database: Database) -> None:
    owner, member, viewer, outsider = uuid4(), uuid4(), uuid4(), uuid4()
    _insert_users(database, owner, member, viewer, outsider)
    projects = ProjectRepository(database)
    project = projects.create(owner, "authorized-project")
    projects.add_member(project.project_id, owner, member, role="member")
    projects.add_member(project.project_id, owner, viewer, role="viewer")
    service = _service(database)
    tenant_id = uuid4()

    personal = _create_user_memory(service, tenant_id=tenant_id, actor_user_id=owner)
    assert (
        service.get_memory(personal.memory_id, tenant_id=tenant_id, actor_user_id=owner).content
        == "SI"
    )
    with pytest.raises(MemoryScopeDeniedError):
        service.get_memory(personal.memory_id, tenant_id=tenant_id, actor_user_id=outsider)

    project_memory = service.create_memory(
        tenant_id=tenant_id,
        actor_user_id=member,
        scope_type=MemoryScopeType.PROJECT,
        project_id=UUID(project.project_id),
        kind=MemoryKind.PROJECT_FACT,
        semantic_key="building type",
        content="office",
        origin=MemoryOrigin.EXPLICIT_USER,
    )
    assert (
        service.get_memory(
            project_memory.memory_id, tenant_id=tenant_id, actor_user_id=owner
        ).content
        == "office"
    )
    with pytest.raises(MemoryScopeDeniedError):
        service.create_memory(
            tenant_id=tenant_id,
            actor_user_id=viewer,
            scope_type=MemoryScopeType.PROJECT,
            project_id=UUID(project.project_id),
            kind=MemoryKind.PROJECT_FACT,
            semantic_key="viewer write",
            content="must fail",
            origin=MemoryOrigin.EXPLICIT_USER,
        )
    with pytest.raises(MemoryScopeDeniedError):
        service.get_memory(project_memory.memory_id, tenant_id=tenant_id, actor_user_id=outsider)


def test_active_duplicate_key_is_a_conflict_and_does_not_overwrite(database: Database) -> None:
    owner = uuid4()
    _insert_users(database, owner)
    service = _service(database)
    tenant_id = uuid4()
    original = _create_user_memory(service, tenant_id=tenant_id, actor_user_id=owner, content="SI")

    with pytest.raises(MemoryConflictRequiresConfirmationError) as exc_info:
        _create_user_memory(service, tenant_id=tenant_id, actor_user_id=owner, content="Imperial")

    assert exc_info.value.code == "MEMORY_CONFLICT_REQUIRES_CONFIRMATION"
    current = service.get_memory(original.memory_id, tenant_id=tenant_id, actor_user_id=owner)
    assert current.content == "SI"
    assert current.revision == 1


def test_correction_uses_optimistic_revision_and_writes_hash_event(database: Database) -> None:
    owner = uuid4()
    _insert_users(database, owner)
    service = _service(database)
    tenant_id = uuid4()
    original = _create_user_memory(service, tenant_id=tenant_id, actor_user_id=owner)

    corrected = service.correct_memory(
        original.memory_id,
        tenant_id=tenant_id,
        actor_user_id=owner,
        expected_revision=1,
        content="metric",
        reason="user correction",
    )
    assert corrected.revision == 2
    assert corrected.content == "metric"

    with pytest.raises(MemoryRevisionConflictError) as exc_info:
        service.correct_memory(
            original.memory_id,
            tenant_id=tenant_id,
            actor_user_id=owner,
            expected_revision=1,
            content="stale update",
            reason="stale client",
        )
    assert exc_info.value.code == "MEMORY_REVISION_CONFLICT"
    assert (
        service.get_memory(original.memory_id, tenant_id=tenant_id, actor_user_id=owner).content
        == "metric"
    )

    events = service.list_events(original.memory_id, tenant_id=tenant_id, actor_user_id=owner)
    assert [event.event_type for event in events] == [
        MemoryEventType.CREATED,
        MemoryEventType.CORRECTED,
    ]
    assert events[1].before_content_hash is not None
    assert events[1].after_content_hash is not None
    assert events[1].before_content_hash != events[1].after_content_hash
    assert str(owner) not in events[1].actor_hash


@pytest.mark.parametrize(
    ("target_status", "event_type"),
    [
        (MemoryStatus.SUPERSEDED, MemoryEventType.SUPERSEDED),
        (MemoryStatus.EXPIRED, MemoryEventType.EXPIRED),
        (MemoryStatus.QUARANTINED, MemoryEventType.QUARANTINED),
    ],
)
def test_inactive_status_transitions_are_audited(
    database: Database, target_status: MemoryStatus, event_type: MemoryEventType
) -> None:
    owner = uuid4()
    _insert_users(database, owner)
    service = _service(database)
    tenant_id = uuid4()
    record = _create_user_memory(
        service,
        tenant_id=tenant_id,
        actor_user_id=owner,
        semantic_key=target_status.value,
    )

    changed = service.transition_memory(
        record.memory_id,
        tenant_id=tenant_id,
        actor_user_id=owner,
        expected_revision=1,
        target_status=target_status,
        reason=f"mark {target_status.value}",
    )
    assert changed.status is target_status
    assert changed.revision == 2
    assert service.list_active(tenant_id=tenant_id, actor_user_id=owner) == []
    assert (
        service.list_events(record.memory_id, tenant_id=tenant_id, actor_user_id=owner)[
            -1
        ].event_type
        == event_type
    )


def test_expire_due_and_forget_leave_only_a_tombstone(database: Database) -> None:
    owner = uuid4()
    _insert_users(database, owner)
    service = _service(database)
    tenant_id = uuid4()
    record = _create_user_memory(
        service,
        tenant_id=tenant_id,
        actor_user_id=owner,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    expired = service.expire_due(now=datetime.now(UTC), tenant_id=tenant_id)
    assert [item.memory_id for item in expired] == [record.memory_id]
    forgotten = service.forget_memory(
        record.memory_id,
        tenant_id=tenant_id,
        actor_user_id=owner,
        expected_revision=2,
        reason="user requested deletion",
    )
    assert forgotten.status is MemoryStatus.FORGOTTEN
    assert forgotten.tombstone is True
    assert forgotten.content == ""
    assert service.list_active(tenant_id=tenant_id, actor_user_id=owner) == []
    assert (
        service.get_memory(record.memory_id, tenant_id=tenant_id, actor_user_id=owner).content == ""
    )

    with database.engine.connect() as connection:
        row = connection.execute(
            text("SELECT content, tombstone FROM memory_records WHERE memory_id=:id"),
            {"id": str(record.memory_id)},
        ).one()
        assert row.content is None
        assert row.tombstone in (True, 1)
        table_columns = {
            column["name"] for column in inspect(connection).get_columns("memory_events")
        }
        assert "content" not in table_columns
        event_rows = connection.execute(
            text(
                "SELECT before_content_hash, after_content_hash, reason FROM memory_events WHERE memory_id=:id"
            ),
            {"id": str(record.memory_id)},
        ).all()
        assert all("SI" not in str(row) for row in event_rows)

    with pytest.raises(MemoryForgottenError):
        service.forget_memory(
            record.memory_id,
            tenant_id=tenant_id,
            actor_user_id=owner,
            expected_revision=3,
            reason="second forget",
        )


def test_record_usage_has_no_memory正文_and_emits_recalled_event(database: Database) -> None:
    owner = uuid4()
    _insert_users(database, owner)
    service = _service(database)
    tenant_id = uuid4()
    record = _create_user_memory(service, tenant_id=tenant_id, actor_user_id=owner)

    usage = service.record_usage(
        record.memory_id,
        tenant_id=tenant_id,
        actor_user_id=owner,
        source_thread_id=uuid4(),
        run_id=uuid4(),
        score=0.75,
    )
    assert usage.memory_id == record.memory_id
    with database.engine.connect() as connection:
        table_columns = {
            column["name"] for column in inspect(connection).get_columns("memory_usage")
        }
        assert "content" not in table_columns
        assert (
            connection.execute(
                text("SELECT count(*) FROM memory_usage WHERE usage_id=:id"),
                {"id": str(usage.usage_id)},
            ).scalar_one()
            == 1
        )
    assert (
        service.list_events(record.memory_id, tenant_id=tenant_id, actor_user_id=owner)[
            -1
        ].event_type
        == MemoryEventType.RECALLED
    )


def test_database_errors_map_to_memory_store_unavailable() -> None:
    class BrokenDatabase:
        @contextmanager
        def transaction(self):
            raise SQLAlchemyError("database secret details must not escape")
            yield

    service = MemoryService(MemoryRepository(BrokenDatabase()))  # type: ignore[arg-type]
    with pytest.raises(MemoryStoreUnavailableError) as exc_info:
        service.create_memory(
            tenant_id=uuid4(),
            actor_user_id=uuid4(),
            scope_type=MemoryScopeType.USER,
            kind=MemoryKind.USER_PREFERENCE,
            semantic_key="db failure",
            content="value",
            origin=MemoryOrigin.EXPLICIT_USER,
        )
    assert exc_info.value.code == "MEMORY_STORE_UNAVAILABLE"
    assert "secret" not in str(exc_info.value).lower()


def test_postgres_memory_roundtrip_uses_step39_schema() -> None:
    database = _postgres_database()
    try:
        owner = uuid4()
        _insert_users(database, owner)
        service = _service(database)
        tenant_id = uuid4()
        record = _create_user_memory(
            service,
            tenant_id=tenant_id,
            actor_user_id=owner,
            semantic_key=f"postgres-{uuid4()}",
        )
        corrected = service.correct_memory(
            record.memory_id,
            tenant_id=tenant_id,
            actor_user_id=owner,
            expected_revision=1,
            content="metric",
            reason="postgres correction",
        )
        forgotten = service.forget_memory(
            record.memory_id,
            tenant_id=tenant_id,
            actor_user_id=owner,
            expected_revision=corrected.revision,
            reason="postgres forget",
        )
        assert forgotten.status is MemoryStatus.FORGOTTEN
        assert forgotten.content == ""
    finally:
        database.dispose()


def test_expire_due_never_returns_another_owners_body(database: Database) -> None:
    """A user-scoped expiry sweep must stay inside the caller's own memories.

    Before the fix, expire_due() filtered only on status and expires_at, so one
    user's sweep selected every due row in the tenant and returned the bodies.
    """

    alice, bob = uuid4(), uuid4()
    _insert_users(database, alice, bob)
    service = _service(database)
    tenant_id = uuid4()
    past = datetime.now(UTC) - timedelta(seconds=1)

    alice_record = _create_user_memory(
        service,
        tenant_id=tenant_id,
        actor_user_id=alice,
        semantic_key="alice-units",
        content="alice body",
        expires_at=past,
    )
    bob_record = _create_user_memory(
        service,
        tenant_id=tenant_id,
        actor_user_id=bob,
        semantic_key="bob-units",
        content="bob private body",
        expires_at=past,
    )

    expired = service.expire_due(
        now=datetime.now(UTC),
        tenant_id=tenant_id,
        actor_user_id=alice,
    )

    returned_ids = {item.memory_id for item in expired}
    assert returned_ids == {alice_record.memory_id}
    assert bob_record.memory_id not in returned_ids
    assert all("bob private body" not in (item.content or "") for item in expired)

    # Bob's record must still be ACTIVE: another user's sweep cannot expire it.
    still_active = service.get_memory(
        bob_record.memory_id, tenant_id=tenant_id, actor_user_id=bob
    )
    assert still_active.status is MemoryStatus.ACTIVE


def test_expire_due_stays_inside_the_requested_tenant(database: Database) -> None:
    owner = uuid4()
    _insert_users(database, owner)
    service = _service(database)
    tenant_a, tenant_b = uuid4(), uuid4()
    past = datetime.now(UTC) - timedelta(seconds=1)

    record_a = _create_user_memory(
        service,
        tenant_id=tenant_a,
        actor_user_id=owner,
        semantic_key="a-units",
        expires_at=past,
    )
    record_b = _create_user_memory(
        service,
        tenant_id=tenant_b,
        actor_user_id=owner,
        semantic_key="b-units",
        expires_at=past,
    )

    expired = service.expire_due(now=datetime.now(UTC), tenant_id=tenant_a)

    assert [item.memory_id for item in expired] == [record_a.memory_id]
    untouched = service.get_memory(
        record_b.memory_id, tenant_id=tenant_b, actor_user_id=owner
    )
    assert untouched.status is MemoryStatus.ACTIVE
