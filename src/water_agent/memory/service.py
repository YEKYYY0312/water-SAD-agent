"""Application service for explicit, scoped long-term memory operations."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from uuid import UUID, uuid4

from water_agent.memory.contracts import (
    MemoryEvent,
    MemoryKind,
    MemoryKindInvalidError,
    MemoryOrigin,
    MemoryRecord,
    MemoryScopeType,
    MemoryStatus,
    MemoryUsage,
)
from water_agent.memory.repository import MemoryRepository


def _as_uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _utc_now() -> datetime:
    return datetime.now(UTC)


class MemoryService:
    """Coordinate validated memory mutations without any Agent auto-write path."""

    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository

    def create_memory(
        self,
        *,
        tenant_id: UUID | str,
        actor_user_id: UUID | str,
        scope_type: MemoryScopeType,
        kind: MemoryKind,
        semantic_key: str,
        content: str,
        origin: MemoryOrigin,
        project_id: UUID | str | None = None,
        source_thread_id: UUID | str | None = None,
        source_message_id: UUID | str | None = None,
        expires_at: datetime | None = None,
        status: MemoryStatus = MemoryStatus.ACTIVE,
        reason: str = "",
    ) -> MemoryRecord:
        """Create one explicit memory; the service owns the memory UUID."""

        actor = _as_uuid(actor_user_id)
        try:
            memory_kind = MemoryKind(kind)
        except (TypeError, ValueError) as exc:
            raise MemoryKindInvalidError() from exc
        memory = MemoryRecord(
            memory_id=uuid4(),
            tenant_id=_as_uuid(tenant_id),
            owner_user_id=actor,
            scope_type=scope_type,
            project_id=_as_uuid(project_id) if project_id is not None else None,
            kind=memory_kind,
            semantic_key=semantic_key,
            content=content,
            status=status,
            origin=origin,
            source_thread_id=(_as_uuid(source_thread_id) if source_thread_id is not None else None),
            source_message_id=(
                _as_uuid(source_message_id) if source_message_id is not None else None
            ),
            revision=1,
            created_at=_utc_now(),
            updated_at=_utc_now(),
            expires_at=expires_at,
        )
        if status is MemoryStatus.ACTIVE and not content.strip():
            raise ValueError("active memory content must not be empty")
        return self.repository.create(memory, actor_user_id=actor, reason=reason)

    def authorize_create_scope(
        self,
        *,
        actor_user_id: UUID | str,
        scope_type: MemoryScopeType,
        project_id: UUID | str | None,
    ) -> None:
        self.repository.authorize_create_scope(
            actor_user_id=actor_user_id,
            scope_type=scope_type,
            project_id=project_id,
        )

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
        return self.repository.find_active_by_key(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            scope_type=scope_type,
            project_id=project_id,
            kind=kind,
            semantic_key=semantic_key,
        )

    def create_quarantined_memory(
        self,
        *,
        tenant_id: UUID | str,
        actor_user_id: UUID | str,
        scope_type: MemoryScopeType,
        kind: MemoryKind,
        semantic_key: str,
        attempted_content_hash: str,
        rule_ids: tuple[str, ...],
        origin: MemoryOrigin,
        project_id: UUID | str | None = None,
        source_thread_id: UUID | str | None = None,
        source_message_id: UUID | str | None = None,
    ) -> MemoryRecord:
        actor = _as_uuid(actor_user_id)
        now = _utc_now()
        memory = MemoryRecord(
            memory_id=uuid4(),
            tenant_id=_as_uuid(tenant_id),
            owner_user_id=actor,
            scope_type=scope_type,
            project_id=_as_uuid(project_id) if project_id is not None else None,
            kind=kind,
            semantic_key=semantic_key,
            content="",
            status=MemoryStatus.QUARANTINED,
            origin=origin,
            source_thread_id=(_as_uuid(source_thread_id) if source_thread_id is not None else None),
            source_message_id=(
                _as_uuid(source_message_id) if source_message_id is not None else None
            ),
            revision=1,
            created_at=now,
            updated_at=now,
        )
        return self.repository.create_quarantined(
            memory,
            actor_user_id=actor,
            attempted_content_hash=attempted_content_hash,
            rule_ids=rule_ids,
        )

    def get_memory_for_write(
        self,
        memory_id: UUID | str,
        *,
        tenant_id: UUID | str,
        actor_user_id: UUID | str,
    ) -> MemoryRecord:
        return self.repository.get_for_write(
            memory_id,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
        )

    def reaffirm_memory(
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
        return self.repository.reaffirm(
            memory_id,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            expected_revision=expected_revision,
            source_thread_id=source_thread_id,
            source_message_id=source_message_id,
            reason=reason,
        )

    def get_memory(
        self,
        memory_id: UUID | str,
        *,
        tenant_id: UUID | str,
        actor_user_id: UUID | str,
    ) -> MemoryRecord:
        return self.repository.get_for_user(
            memory_id,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
        )

    def list_active(
        self,
        *,
        tenant_id: UUID | str,
        actor_user_id: UUID | str,
        now: datetime | None = None,
    ) -> list[MemoryRecord]:
        return self.repository.list_active(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            now=now,
        )

    def correct_memory(
        self,
        memory_id: UUID | str,
        *,
        tenant_id: UUID | str,
        actor_user_id: UUID | str,
        expected_revision: int,
        content: str,
        reason: str = "",
    ) -> MemoryRecord:
        if not content.strip():
            raise ValueError("corrected memory content must not be empty")
        return self.repository.correct(
            memory_id,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            expected_revision=expected_revision,
            content=content,
            reason=reason,
        )

    def transition_memory(
        self,
        memory_id: UUID | str,
        *,
        tenant_id: UUID | str,
        actor_user_id: UUID | str,
        expected_revision: int,
        target_status: MemoryStatus,
        reason: str = "",
    ) -> MemoryRecord:
        return self.repository.transition(
            memory_id,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            expected_revision=expected_revision,
            target_status=target_status,
            reason=reason,
        )

    def supersede_memory(self, memory_id: UUID | str, **kwargs) -> MemoryRecord:
        return self.transition_memory(memory_id, target_status=MemoryStatus.SUPERSEDED, **kwargs)

    def expire_memory(self, memory_id: UUID | str, **kwargs) -> MemoryRecord:
        return self.transition_memory(memory_id, target_status=MemoryStatus.EXPIRED, **kwargs)

    def quarantine_memory(self, memory_id: UUID | str, **kwargs) -> MemoryRecord:
        return self.transition_memory(memory_id, target_status=MemoryStatus.QUARANTINED, **kwargs)

    def forget_memory(self, memory_id: UUID | str, **kwargs) -> MemoryRecord:
        return self.transition_memory(memory_id, target_status=MemoryStatus.FORGOTTEN, **kwargs)

    def expire_due(
        self,
        *,
        now: datetime | None = None,
        tenant_id: UUID | str | None = None,
        actor_user_id: UUID | str | None = None,
    ) -> list[MemoryRecord]:
        return self.repository.expire_due(
            now=now,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
        )

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
        if score is not None and not math.isfinite(score):
            raise ValueError("usage score must be finite")
        return self.repository.record_usage(
            memory_id,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            source_thread_id=source_thread_id,
            run_id=run_id,
            score=score,
        )

    def list_events(
        self,
        memory_id: UUID | str,
        *,
        tenant_id: UUID | str,
        actor_user_id: UUID | str,
    ) -> list[MemoryEvent]:
        return self.repository.list_events(
            memory_id,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
        )

    def list_usage(
        self,
        memory_id: UUID | str,
        *,
        tenant_id: UUID | str,
        actor_user_id: UUID | str,
    ) -> list[MemoryUsage]:
        return self.repository.list_usage(
            memory_id,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
        )


__all__ = ["MemoryService"]
