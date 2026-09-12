"""P5 Step 39 deterministic long-term memory contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from water_agent.memory.contracts import (
    MEMORY_ERROR_CODES,
    MemoryEvent,
    MemoryEventType,
    MemoryKind,
    MemoryOrigin,
    MemoryRecord,
    MemoryScopeType,
    MemoryStatus,
)


def _record(
    *, scope_type: MemoryScopeType, project_id=None, status=MemoryStatus.ACTIVE, content="saved"
) -> MemoryRecord:
    now = datetime.now(UTC)
    return MemoryRecord(
        memory_id=uuid4(),
        tenant_id=uuid4(),
        owner_user_id=uuid4(),
        scope_type=scope_type,
        project_id=project_id,
        kind=MemoryKind.USER_PREFERENCE,
        semantic_key="  units   preference ",
        content=content,
        status=status,
        origin=MemoryOrigin.EXPLICIT_USER,
        revision=1,
        created_at=now,
        updated_at=now,
    )


def test_user_scope_rejects_project_id() -> None:
    with pytest.raises(ValidationError):
        _record(scope_type=MemoryScopeType.USER, project_id=uuid4())


def test_project_scope_requires_project_id() -> None:
    with pytest.raises(ValidationError):
        _record(scope_type=MemoryScopeType.PROJECT)


def test_memory_contract_normalizes_semantic_key_and_keeps_tombstone_empty() -> None:
    forgotten = _record(
        scope_type=MemoryScopeType.USER,
        status=MemoryStatus.FORGOTTEN,
        content="",
    )

    assert forgotten.semantic_key == "units preference"
    assert forgotten.content == ""
    assert forgotten.tombstone is True


def test_quarantined_memory_cannot_carry正文() -> None:
    with pytest.raises(ValidationError):
        _record(
            scope_type=MemoryScopeType.USER,
            status=MemoryStatus.QUARANTINED,
            content="instruction-shaped content",
        )


def test_event_contract_contains_hashes_but_no正文_field() -> None:
    event = MemoryEvent(
        event_id=uuid4(),
        event_type=MemoryEventType.CORRECTED,
        memory_id=uuid4(),
        actor_hash="actor-hash",
        before_content_hash="before-hash",
        after_content_hash="after-hash",
        revision=2,
        source_thread_id=uuid4(),
        source_message_id=uuid4(),
        reason="user correction",
    )

    assert "content" not in MemoryEvent.model_fields
    assert "instruction-shaped content" not in event.model_dump_json()


def test_memory_error_code_contract_is_stable() -> None:
    assert {
        "MEMORY_SCOPE_DENIED",
        "MEMORY_NOT_FOUND",
        "MEMORY_REVISION_CONFLICT",
        "MEMORY_KIND_INVALID",
        "MEMORY_SECRET_BLOCKED",
        "MEMORY_PII_BLOCKED",
        "MEMORY_INJECTION_BLOCKED",
        "MEMORY_CONFLICT_REQUIRES_CONFIRMATION",
        "MEMORY_EXPIRED",
        "MEMORY_FORGOTTEN",
        "MEMORY_STORE_UNAVAILABLE",
        "MEMORY_RECALL_BUDGET_EXCEEDED",
    } <= MEMORY_ERROR_CODES
