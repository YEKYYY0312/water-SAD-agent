"""SQLAlchemy repository for the P5 Step 39 memory store.

The repository is the persistence and resource-authorization boundary.  It
does not register Agent tools or infer a caller from memory content.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, NoReturn, cast
from uuid import UUID, uuid4

from sqlalchemy import exists, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from water_agent.memory.contracts import (
    ALLOWED_STATUS_TRANSITIONS,
    MemoryConflictRequiresConfirmationError,
    MemoryEvent,
    MemoryEventType,
    MemoryExpiredError,
    MemoryForgottenError,
    MemoryKind,
    MemoryNotFoundError,
    MemoryOrigin,
    MemoryRecord,
    MemoryRevisionConflictError,
    MemoryScopeDeniedError,
    MemoryScopeType,
    MemoryStatus,
    MemoryStoreUnavailableError,
    MemoryUsage,
    actor_hash,
    content_hash,
)
from water_agent.memory.providers import MemoryEmbeddingStore, MemoryProviderUnavailableError
from water_agent.storage.database import Database
from water_agent.storage.models import (
    MemoryEventRow,
    MemoryRecordRow,
    MemoryUsageRow,
    ProjectMembership,
    User,
)


def _as_uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _now() -> datetime:
    return datetime.now(UTC)


def _safe_reason(reason: str, *bodies: str | None) -> str:
    """Keep event reasons bounded and prevent copying a memory body.

    Reasons are audit metadata, not a second content store.  A caller that
    accidentally supplies a full body receives a hash marker instead.
    """

    normalized = " ".join(reason.split())
    if any(body and body in normalized for body in bodies):
        return f"content_hash:{content_hash(normalized)}"
    return normalized[:256]


class MemoryRepository:
    """Persist memory records and append-only metadata in one database."""

    def __init__(
        self,
        database: Database,
        *,
        embedding_store: MemoryEmbeddingStore | None = None,
    ) -> None:
        self.database = database
        self.embedding_store = embedding_store

    @contextmanager
    def _transaction(self, *, duplicate_is_conflict: bool = False) -> Iterator[Session]:
        try:
            with self.database.transaction() as session:
                yield session
        except MemoryConflictRequiresConfirmationError:
            raise
        except MemoryScopeDeniedError:
            raise
        except MemoryRevisionConflictError:
            raise
        except (MemoryExpiredError, MemoryForgottenError):
            raise
        except MemoryProviderUnavailableError as exc:
            raise MemoryStoreUnavailableError() from exc
        except IntegrityError as exc:
            if duplicate_is_conflict:
                raise MemoryConflictRequiresConfirmationError() from exc
            raise MemoryStoreUnavailableError() from exc
        except (OSError, SQLAlchemyError) as exc:
            raise MemoryStoreUnavailableError() from exc

    @staticmethod
    def _read_access_clause(user_id: str):
        project_access = exists(
            select(ProjectMembership.project_id).where(
                ProjectMembership.project_id == MemoryRecordRow.project_id,
                ProjectMembership.user_id == user_id,
            )
        )
        return or_(
            (
                (MemoryRecordRow.scope_type == MemoryScopeType.USER.value)
                & (MemoryRecordRow.owner_user_id == user_id)
                & MemoryRecordRow.project_id.is_(None)
            ),
            ((MemoryRecordRow.scope_type == MemoryScopeType.PROJECT.value) & project_access),
        )

    @staticmethod
    def _write_access_clause(user_id: str):
        project_write_access = exists(
            select(ProjectMembership.project_id).where(
                ProjectMembership.project_id == MemoryRecordRow.project_id,
                ProjectMembership.user_id == user_id,
                ProjectMembership.role.in_(("owner", "member")),
            )
        )
        return or_(
            (
                (MemoryRecordRow.scope_type == MemoryScopeType.USER.value)
                & (MemoryRecordRow.owner_user_id == user_id)
                & MemoryRecordRow.project_id.is_(None)
            ),
            ((MemoryRecordRow.scope_type == MemoryScopeType.PROJECT.value) & project_write_access),
        )

    @staticmethod
    def _require_create_access(session: Session, record: MemoryRecord, actor_user_id: str) -> None:
        if record.scope_type is MemoryScopeType.USER:
            if record.owner_user_id != _as_uuid(actor_user_id):
                raise MemoryScopeDeniedError()
            return
        membership = session.execute(
            select(ProjectMembership.project_id).where(
                ProjectMembership.project_id == str(record.project_id),
                ProjectMembership.user_id == actor_user_id,
                ProjectMembership.role.in_(("owner", "member")),
            )
        ).first()
        if membership is None:
            raise MemoryScopeDeniedError()

    @staticmethod
    def _row(record: MemoryRecord) -> MemoryRecordRow:
        return MemoryRecordRow(
            memory_id=str(record.memory_id),
            tenant_id=str(record.tenant_id),
            owner_user_id=str(record.owner_user_id),
            scope_type=record.scope_type.value,
            project_id=str(record.project_id) if record.project_id is not None else None,
            kind=record.kind.value,
            semantic_key=record.semantic_key,
            content=record.content or None,
            status=record.status.value,
            origin=record.origin.value,
            source_thread_id=(
                str(record.source_thread_id) if record.source_thread_id is not None else None
            ),
            source_message_id=(
                str(record.source_message_id) if record.source_message_id is not None else None
            ),
            revision=record.revision,
            created_at=record.created_at,
            updated_at=record.updated_at,
            expires_at=record.expires_at,
            tombstone=record.tombstone,
        )

    @staticmethod
    def _record(row: MemoryRecordRow) -> MemoryRecord:
        return MemoryRecord(
            memory_id=_as_uuid(row.memory_id),
            tenant_id=_as_uuid(row.tenant_id),
            owner_user_id=_as_uuid(row.owner_user_id),
            scope_type=MemoryScopeType(row.scope_type),
            project_id=_as_uuid(row.project_id) if row.project_id is not None else None,
            kind=MemoryKind(row.kind),
            semantic_key=row.semantic_key,
            content=row.content or "",
            status=MemoryStatus(row.status),
            origin=MemoryOrigin(row.origin),
            source_thread_id=(
                _as_uuid(row.source_thread_id) if row.source_thread_id is not None else None
            ),
            source_message_id=(
                _as_uuid(row.source_message_id) if row.source_message_id is not None else None
            ),
            revision=row.revision,
            created_at=row.created_at,
            updated_at=row.updated_at,
            expires_at=row.expires_at,
            tombstone=bool(row.tombstone),
        )

    @staticmethod
    def _event(
        *,
        memory: MemoryRecord,
        event_type: MemoryEventType,
        operator: UUID | str,
        before: str | None,
        after: str | None,
        reason: str,
        body_candidates: tuple[str | None, ...] = (),
    ) -> MemoryEventRow:
        return MemoryEventRow(
            event_id=str(uuid4()),
            event_type=event_type.value,
            memory_id=str(memory.memory_id),
            actor_hash=actor_hash(operator),
            before_content_hash=before,
            after_content_hash=after,
            revision=memory.revision,
            created_at=_now(),
            source_thread_id=(
                str(memory.source_thread_id) if memory.source_thread_id is not None else None
            ),
            source_message_id=(
                str(memory.source_message_id) if memory.source_message_id is not None else None
            ),
            reason=_safe_reason(reason, memory.content, *body_candidates),
        )

    @staticmethod
    def _event_model(row: MemoryEventRow) -> MemoryEvent:
        return MemoryEvent(
            event_id=_as_uuid(row.event_id),
            event_type=MemoryEventType(row.event_type),
            memory_id=_as_uuid(row.memory_id),
            actor_hash=row.actor_hash,
            before_content_hash=row.before_content_hash,
            after_content_hash=row.after_content_hash,
            revision=row.revision,
            created_at=row.created_at,
            source_thread_id=(
                _as_uuid(row.source_thread_id) if row.source_thread_id is not None else None
            ),
            source_message_id=(
                _as_uuid(row.source_message_id) if row.source_message_id is not None else None
            ),
            reason=row.reason,
        )

    @staticmethod
    def _usage_model(row: MemoryUsageRow) -> MemoryUsage:
        return MemoryUsage(
            usage_id=_as_uuid(row.usage_id),
            tenant_id=_as_uuid(row.tenant_id),
            memory_id=_as_uuid(row.memory_id),
            user_id=_as_uuid(row.user_id),
            source_thread_id=(
                _as_uuid(row.source_thread_id) if row.source_thread_id is not None else None
            ),
            run_id=_as_uuid(row.run_id) if row.run_id is not None else None,
            score=row.score,
            created_at=row.created_at,
        )

    @staticmethod
    def _missing_or_denied(
        session: Session, memory_id: str, tenant_id: str, *, operation: str = "read"
    ) -> NoReturn:
        exists_in_tenant = session.execute(
            select(MemoryRecordRow.memory_id).where(
                MemoryRecordRow.memory_id == memory_id,
                MemoryRecordRow.tenant_id == tenant_id,
            )
        ).first()
        if exists_in_tenant is None:
            raise MemoryNotFoundError()
        raise MemoryScopeDeniedError(f"memory {operation} is not authorized")

    def create(
        self,
        record: MemoryRecord,
        *,
        actor_user_id: UUID | str,
        reason: str = "",
    ) -> MemoryRecord:
        actor = str(_as_uuid(actor_user_id))
        with self._transaction(duplicate_is_conflict=True) as session:
            self._require_create_access(session, record, actor)
            session.add(self._row(record))
            # The event has a foreign key to the record, but the ORM models
            # intentionally do not expose a relationship.  Flush the record
            # first so both writes remain in the same transaction.
            session.flush()
            session.add(
                self._event(
                    memory=record,
                    event_type=MemoryEventType.CREATED,
                    operator=actor,
                    before=None,
                    after=content_hash(record.content),
                    reason=reason,
                )
            )
        return record

    def authorize_create_scope(
        self,
        *,
        actor_user_id: UUID | str,
        scope_type: MemoryScopeType,
        project_id: UUID | str | None,
    ) -> None:
        """Authorize the requested write scope before inspecting candidate content."""

        actor = str(_as_uuid(actor_user_id))
        with self._transaction() as session:
            if scope_type is MemoryScopeType.USER:
                if project_id is not None:
                    raise MemoryScopeDeniedError("user-scoped memory cannot target a project")
                found = session.execute(select(User.user_id).where(User.user_id == actor)).first()
                if found is None:
                    raise MemoryScopeDeniedError()
                return
            if scope_type is not MemoryScopeType.PROJECT or project_id is None:
                raise MemoryScopeDeniedError("project-scoped memory requires a project")
            membership = session.execute(
                select(ProjectMembership.project_id).where(
                    ProjectMembership.project_id == str(_as_uuid(project_id)),
                    ProjectMembership.user_id == actor,
                    ProjectMembership.role.in_(("owner", "member")),
                )
            ).first()
            if membership is None:
                raise MemoryScopeDeniedError()

    def find_active_by_key(
        self,
        *,
        tenant_id: UUID | str,
        actor_user_id: UUID | str,
        scope_type: MemoryScopeType,
        project_id: UUID | str | None,
        kind: MemoryKind,
        semantic_key: str,
    ) -> MemoryRecord | None:
        """Return the authorized active key occupant, if one exists."""

        tenant_key = str(_as_uuid(tenant_id))
        actor = str(_as_uuid(actor_user_id))
        conditions = [
            MemoryRecordRow.tenant_id == tenant_key,
            MemoryRecordRow.scope_type == scope_type.value,
            MemoryRecordRow.kind == kind.value,
            MemoryRecordRow.semantic_key == semantic_key,
            MemoryRecordRow.status == MemoryStatus.ACTIVE.value,
            self._write_access_clause(actor),
        ]
        if scope_type is MemoryScopeType.USER:
            conditions.extend(
                [
                    MemoryRecordRow.owner_user_id == actor,
                    MemoryRecordRow.project_id.is_(None),
                ]
            )
        else:
            if project_id is None:
                raise MemoryScopeDeniedError("project-scoped memory requires a project")
            conditions.append(MemoryRecordRow.project_id == str(_as_uuid(project_id)))
        with self._transaction() as session:
            row = session.execute(select(MemoryRecordRow).where(*conditions)).scalar_one_or_none()
            return None if row is None else self._record(row)

    def create_quarantined(
        self,
        record: MemoryRecord,
        *,
        actor_user_id: UUID | str,
        attempted_content_hash: str,
        rule_ids: tuple[str, ...],
    ) -> MemoryRecord:
        """Persist only metadata for a confirmed unsafe candidate."""

        if record.status is not MemoryStatus.QUARANTINED or record.content:
            raise ValueError("quarantine persistence requires an empty quarantined record")
        actor = str(_as_uuid(actor_user_id))
        reason = f"rules={','.join(sorted(set(rule_ids)))}"
        with self._transaction() as session:
            self._require_create_access(session, record, actor)
            session.add(self._row(record))
            session.flush()
            session.add(
                self._event(
                    memory=record,
                    event_type=MemoryEventType.QUARANTINED,
                    operator=actor,
                    before=None,
                    after=attempted_content_hash,
                    reason=reason,
                )
            )
        return record

    def reaffirm(
        self,
        memory_id: UUID | str,
        *,
        tenant_id: UUID | str,
        actor_user_id: UUID | str,
        expected_revision: int,
        source_thread_id: UUID | str | None = None,
        source_message_id: UUID | str | None = None,
        reason: str = "",
    ) -> MemoryRecord:
        """Append a reaffirm event without duplicating or revising the active record."""

        memory_key = str(_as_uuid(memory_id))
        tenant_key = str(_as_uuid(tenant_id))
        actor = str(_as_uuid(actor_user_id))
        with self._transaction() as session:
            row = self._load_for_write(session, memory_key, tenant_key, actor)
            self._ensure_mutable(row)
            if row.revision != expected_revision:
                raise MemoryRevisionConflictError()
            memory = self._record(row)
            event_memory = memory.model_copy(
                update={
                    "source_thread_id": (
                        _as_uuid(source_thread_id) if source_thread_id is not None else None
                    ),
                    "source_message_id": (
                        _as_uuid(source_message_id) if source_message_id is not None else None
                    ),
                }
            )
            digest = content_hash(memory.content)
            session.add(
                self._event(
                    memory=event_memory,
                    event_type=MemoryEventType.REAFFIRMED,
                    operator=actor,
                    before=digest,
                    after=digest,
                    reason=reason,
                )
            )
            return memory

    def get_for_write(
        self,
        memory_id: UUID | str,
        *,
        tenant_id: UUID | str,
        actor_user_id: UUID | str,
    ) -> MemoryRecord:
        memory_key = str(_as_uuid(memory_id))
        tenant_key = str(_as_uuid(tenant_id))
        actor = str(_as_uuid(actor_user_id))
        with self._transaction() as session:
            return self._record(self._load_for_write(session, memory_key, tenant_key, actor))

    def get_for_user(
        self,
        memory_id: UUID | str,
        *,
        tenant_id: UUID | str,
        actor_user_id: UUID | str,
    ) -> MemoryRecord:
        memory_key = str(_as_uuid(memory_id))
        tenant_key = str(_as_uuid(tenant_id))
        actor = str(_as_uuid(actor_user_id))
        with self._transaction() as session:
            row = session.execute(
                select(MemoryRecordRow).where(
                    MemoryRecordRow.memory_id == memory_key,
                    MemoryRecordRow.tenant_id == tenant_key,
                    self._read_access_clause(actor),
                )
            ).scalar_one_or_none()
            if row is None:
                self._missing_or_denied(session, memory_key, tenant_key)
            return self._record(row)

    def list_active(
        self,
        *,
        tenant_id: UUID | str,
        actor_user_id: UUID | str,
        now: datetime | None = None,
    ) -> list[MemoryRecord]:
        tenant_key = str(_as_uuid(tenant_id))
        actor = str(_as_uuid(actor_user_id))
        point = now or _now()
        with self._transaction() as session:
            rows = session.execute(
                select(MemoryRecordRow)
                .where(
                    MemoryRecordRow.tenant_id == tenant_key,
                    MemoryRecordRow.status == MemoryStatus.ACTIVE.value,
                    or_(
                        MemoryRecordRow.expires_at.is_(None),
                        MemoryRecordRow.expires_at > point,
                    ),
                    exists(
                        select(User.user_id).where(
                            User.user_id == actor,
                            User.is_active.is_(True),
                        )
                    ),
                    self._read_access_clause(actor),
                )
                .order_by(MemoryRecordRow.created_at, MemoryRecordRow.memory_id)
            ).scalars()
            return [self._record(row) for row in rows]

    def list_active_by_ids(
        self,
        memory_ids: Sequence[UUID | str],
        *,
        tenant_id: UUID | str,
        actor_user_id: UUID | str,
        now: datetime | None = None,
    ) -> list[MemoryRecord]:
        """Resolve provider-returned IDs through the native auth filter."""

        ids = [str(_as_uuid(value)) for value in memory_ids]
        if not ids:
            return []
        tenant_key = str(_as_uuid(tenant_id))
        actor = str(_as_uuid(actor_user_id))
        point = now or _now()
        with self._transaction() as session:
            rows = session.execute(
                select(MemoryRecordRow)
                .where(
                    MemoryRecordRow.memory_id.in_(ids),
                    MemoryRecordRow.tenant_id == tenant_key,
                    MemoryRecordRow.status == MemoryStatus.ACTIVE.value,
                    or_(
                        MemoryRecordRow.expires_at.is_(None),
                        MemoryRecordRow.expires_at > point,
                    ),
                    exists(
                        select(User.user_id).where(
                            User.user_id == actor,
                            User.is_active.is_(True),
                        )
                    ),
                    self._read_access_clause(actor),
                )
                .order_by(MemoryRecordRow.memory_id)
            ).scalars()
            return [self._record(row) for row in rows]

    def correct(
        self,
        memory_id: UUID | str,
        *,
        tenant_id: UUID | str,
        actor_user_id: UUID | str,
        expected_revision: int,
        content: str,
        reason: str = "",
    ) -> MemoryRecord:
        memory_key = str(_as_uuid(memory_id))
        tenant_key = str(_as_uuid(tenant_id))
        actor = str(_as_uuid(actor_user_id))
        with self._transaction() as session:
            row = self._load_for_write(session, memory_key, tenant_key, actor)
            self._ensure_mutable(row)
            if row.revision != expected_revision:
                raise MemoryRevisionConflictError()
            before = row.content
            new_revision = row.revision + 1
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(MemoryRecordRow)
                    .where(
                        MemoryRecordRow.memory_id == memory_key,
                        MemoryRecordRow.tenant_id == tenant_key,
                        MemoryRecordRow.revision == expected_revision,
                        MemoryRecordRow.status == MemoryStatus.ACTIVE.value,
                        self._write_access_clause(actor),
                    )
                    .values(
                        content=content,
                        revision=new_revision,
                        updated_at=_now(),
                    )
                ),
            )
            if result.rowcount != 1:
                raise MemoryRevisionConflictError()
            row.content = content
            row.revision = new_revision
            row.updated_at = _now()
            if self.embedding_store is not None:
                # A correction invalidates the old vector. Re-indexing is an
                # explicit opt-in operation and must never be part of P0.
                self.embedding_store.delete_forget(session, memory_key)
            memory = self._record(row)
            session.add(
                self._event(
                    memory=memory,
                    event_type=MemoryEventType.CORRECTED,
                    operator=actor,
                    before=content_hash(before),
                    after=content_hash(content),
                    reason=reason,
                    body_candidates=(before,),
                )
            )
            return memory

    def transition(
        self,
        memory_id: UUID | str,
        *,
        tenant_id: UUID | str,
        actor_user_id: UUID | str,
        expected_revision: int,
        target_status: MemoryStatus,
        reason: str = "",
    ) -> MemoryRecord:
        memory_key = str(_as_uuid(memory_id))
        tenant_key = str(_as_uuid(tenant_id))
        actor = str(_as_uuid(actor_user_id))
        with self._transaction() as session:
            row = self._load_for_write(session, memory_key, tenant_key, actor)
            if target_status is MemoryStatus.FORGOTTEN:
                if row.status == MemoryStatus.FORGOTTEN.value:
                    raise MemoryForgottenError()
            else:
                self._ensure_mutable(row)
            if row.revision != expected_revision:
                raise MemoryRevisionConflictError()
            if target_status not in {
                MemoryStatus.SUPERSEDED,
                MemoryStatus.EXPIRED,
                MemoryStatus.FORGOTTEN,
                MemoryStatus.QUARANTINED,
            }:
                raise ValueError("invalid memory status transition")
            current_status = MemoryStatus(row.status)
            if target_status not in ALLOWED_STATUS_TRANSITIONS[current_status]:
                raise ValueError("invalid memory status transition")
            previous_content = row.content
            if target_status is MemoryStatus.FORGOTTEN:
                new_content = None
                tombstone = True
                after_hash = None
            elif target_status is MemoryStatus.QUARANTINED:
                new_content = None
                tombstone = False
                after_hash = None
            else:
                new_content = row.content
                tombstone = False
                after_hash = content_hash(row.content)
            before_hash = content_hash(row.content)
            new_revision = row.revision + 1
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(MemoryRecordRow)
                    .where(
                        MemoryRecordRow.memory_id == memory_key,
                        MemoryRecordRow.tenant_id == tenant_key,
                        MemoryRecordRow.revision == expected_revision,
                        (
                            MemoryRecordRow.status != MemoryStatus.FORGOTTEN.value
                            if target_status is MemoryStatus.FORGOTTEN
                            else MemoryRecordRow.status == MemoryStatus.ACTIVE.value
                        ),
                        self._write_access_clause(actor),
                    )
                    .values(
                        content=new_content,
                        status=target_status.value,
                        revision=new_revision,
                        updated_at=_now(),
                        tombstone=tombstone,
                    )
                ),
            )
            if result.rowcount != 1:
                raise MemoryRevisionConflictError()
            row.content = new_content
            row.status = target_status.value
            row.revision = new_revision
            row.updated_at = _now()
            row.tombstone = tombstone
            if target_status is MemoryStatus.FORGOTTEN and self.embedding_store is not None:
                # The optional provider cleanup runs before the parent
                # transaction commits. A failure rolls the tombstone back
                # instead of reporting a privacy deletion with a vector left.
                self.embedding_store.delete_forget(session, memory_key)
            memory = self._record(row)
            session.add(
                self._event(
                    memory=memory,
                    event_type=MemoryEventType(target_status.value),
                    operator=actor,
                    before=before_hash,
                    after=after_hash,
                    reason=reason,
                    body_candidates=(previous_content,),
                )
            )
            return memory

    @staticmethod
    def _load_for_write(
        session: Session, memory_id: str, tenant_id: str, actor_user_id: str
    ) -> MemoryRecordRow:
        row = session.execute(
            select(MemoryRecordRow)
            .where(
                MemoryRecordRow.memory_id == memory_id,
                MemoryRecordRow.tenant_id == tenant_id,
                MemoryRepository._read_access_clause(actor_user_id),
            )
            .with_for_update()
        ).scalar_one_or_none()
        if row is None:
            MemoryRepository._missing_or_denied(session, memory_id, tenant_id, operation="write")
        if not MemoryRepository._row_has_write_access(session, row, actor_user_id):
            raise MemoryScopeDeniedError("memory write is not authorized")
        return row

    @staticmethod
    def _row_has_write_access(session: Session, row: MemoryRecordRow, actor_user_id: str) -> bool:
        if row.scope_type == MemoryScopeType.USER.value:
            return row.owner_user_id == actor_user_id
        return (
            session.execute(
                select(ProjectMembership.project_id).where(
                    ProjectMembership.project_id == row.project_id,
                    ProjectMembership.user_id == actor_user_id,
                    ProjectMembership.role.in_(("owner", "member")),
                )
            ).first()
            is not None
        )

    @staticmethod
    def _ensure_mutable(row: MemoryRecordRow) -> None:
        if row.status == MemoryStatus.FORGOTTEN.value:
            raise MemoryForgottenError()
        if row.status == MemoryStatus.EXPIRED.value:
            raise MemoryExpiredError()
        if row.status != MemoryStatus.ACTIVE.value:
            raise MemoryStoreUnavailableError("record is not in a mutable state")

    def expire_due(
        self,
        *,
        now: datetime | None = None,
        tenant_id: UUID | str,
        actor_user_id: UUID | str | None = None,
    ) -> list[MemoryRecord]:
        """Expire due memories inside one tenant, never across owners.

        A caller without an actor is a deployment-level scheduler and stays
        scoped to the tenant.  A caller acting for a user may only expire the
        memories that user is allowed to write, so the sweep can never return
        another owner's body.
        """

        point = now or _now()
        tenant_key = str(_as_uuid(tenant_id))
        operator = str(_as_uuid(actor_user_id)) if actor_user_id is not None else "system"
        with self._transaction() as session:
            conditions = [
                MemoryRecordRow.tenant_id == tenant_key,
                MemoryRecordRow.status == MemoryStatus.ACTIVE.value,
                MemoryRecordRow.expires_at.is_not(None),
                MemoryRecordRow.expires_at <= point,
            ]
            if actor_user_id is not None:
                conditions.append(self._write_access_clause(operator))
            rows = list(session.execute(select(MemoryRecordRow).where(*conditions)).scalars())
            expired: list[MemoryRecord] = []
            for row in rows:
                expected_revision = row.revision
                new_revision = expected_revision + 1
                result = cast(
                    CursorResult[Any],
                    session.execute(
                        update(MemoryRecordRow)
                        .where(
                            MemoryRecordRow.memory_id == row.memory_id,
                            MemoryRecordRow.revision == expected_revision,
                            MemoryRecordRow.status == MemoryStatus.ACTIVE.value,
                        )
                        .values(
                            status=MemoryStatus.EXPIRED.value,
                            revision=new_revision,
                            updated_at=_now(),
                        )
                    ),
                )
                if result.rowcount != 1:
                    continue
                row.status = MemoryStatus.EXPIRED.value
                row.revision = new_revision
                row.updated_at = _now()
                memory = self._record(row)
                session.add(
                    self._event(
                        memory=memory,
                        event_type=MemoryEventType.EXPIRED,
                        operator=operator,
                        before=content_hash(row.content),
                        after=content_hash(row.content),
                        reason="expiration deadline reached",
                    )
                )
                expired.append(memory)
            return expired

    def record_usage(
        self,
        memory_id: UUID | str,
        *,
        tenant_id: UUID | str,
        actor_user_id: UUID | str,
        source_thread_id: UUID | str | None = None,
        run_id: UUID | str | None = None,
        score: float | None = None,
    ) -> MemoryUsage:
        memory_key = str(_as_uuid(memory_id))
        tenant_key = str(_as_uuid(tenant_id))
        actor = str(_as_uuid(actor_user_id))
        with self._transaction() as session:
            row = session.execute(
                select(MemoryRecordRow).where(
                    MemoryRecordRow.memory_id == memory_key,
                    MemoryRecordRow.tenant_id == tenant_key,
                    MemoryRecordRow.status == MemoryStatus.ACTIVE.value,
                    or_(
                        MemoryRecordRow.expires_at.is_(None),
                        MemoryRecordRow.expires_at > _now(),
                    ),
                    # Recording usage appends an event row, so it is a write:
                    # a project viewer must not be able to touch the audit log.
                    self._write_access_clause(actor),
                )
            ).scalar_one_or_none()
            if row is None:
                self._missing_or_denied(session, memory_key, tenant_key, operation="use")
            usage = MemoryUsageRow(
                usage_id=str(uuid4()),
                tenant_id=tenant_key,
                memory_id=memory_key,
                user_id=actor,
                source_thread_id=str(_as_uuid(source_thread_id)) if source_thread_id else None,
                run_id=str(_as_uuid(run_id)) if run_id else None,
                score=score,
                created_at=_now(),
            )
            session.add(usage)
            memory = self._record(row)
            session.add(
                self._event(
                    memory=memory,
                    event_type=MemoryEventType.RECALLED,
                    operator=actor,
                    before=content_hash(row.content),
                    after=content_hash(row.content),
                    reason="memory used",
                )
            )
            return self._usage_model(usage)

    def list_events(
        self,
        memory_id: UUID | str,
        *,
        tenant_id: UUID | str,
        actor_user_id: UUID | str,
    ) -> list[MemoryEvent]:
        memory_key = str(_as_uuid(memory_id))
        with self._transaction() as session:
            self._load_for_read(
                session, memory_key, str(_as_uuid(tenant_id)), str(_as_uuid(actor_user_id))
            )
            rows = session.execute(
                select(MemoryEventRow)
                .where(MemoryEventRow.memory_id == memory_key)
                .order_by(MemoryEventRow.created_at, MemoryEventRow.event_id)
            ).scalars()
            return [self._event_model(row) for row in rows]

    @staticmethod
    def _load_for_read(
        session: Session, memory_id: str, tenant_id: str, actor_user_id: str
    ) -> MemoryRecordRow:
        row = session.execute(
            select(MemoryRecordRow).where(
                MemoryRecordRow.memory_id == memory_id,
                MemoryRecordRow.tenant_id == tenant_id,
                MemoryRepository._read_access_clause(actor_user_id),
            )
        ).scalar_one_or_none()
        if row is None:
            MemoryRepository._missing_or_denied(session, memory_id, tenant_id)
        return row

    def list_usage(
        self,
        memory_id: UUID | str,
        *,
        tenant_id: UUID | str,
        actor_user_id: UUID | str,
    ) -> list[MemoryUsage]:
        memory_key = str(_as_uuid(memory_id))
        with self._transaction() as session:
            self._load_for_read(
                session, memory_key, str(_as_uuid(tenant_id)), str(_as_uuid(actor_user_id))
            )
            rows = session.execute(
                select(MemoryUsageRow)
                .where(MemoryUsageRow.memory_id == memory_key)
                .order_by(MemoryUsageRow.created_at, MemoryUsageRow.usage_id)
            ).scalars()
            return [self._usage_model(row) for row in rows]


__all__ = ["MemoryRepository"]
