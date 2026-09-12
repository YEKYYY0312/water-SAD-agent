"""P5 Step 39 long-term memory domain contracts.

This module contains only deterministic value objects and stable error codes.
It does not know about Web requests, Agent assembly, or a database.
"""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from water_agent.domain.models import content_hash_of


def _utc_now() -> datetime:
    return datetime.now(UTC)


class MemoryScopeType(str, Enum):
    USER = "user"
    PROJECT = "project"


class MemoryStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    FORGOTTEN = "forgotten"
    QUARANTINED = "quarantined"


class MemoryOrigin(str, Enum):
    EXPLICIT_USER = "explicit_user"
    APPROVED_IMPORT = "approved_import"


class MemoryKind(str, Enum):
    USER_PREFERENCE = "user_preference"
    PROJECT_FACT = "project_fact"
    PROJECT_DECISION = "project_decision"
    WORKFLOW_NOTE = "workflow_note"


class MemoryEventType(str, Enum):
    CREATED = "created"
    REAFFIRMED = "reaffirmed"
    CORRECTED = "corrected"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    FORGOTTEN = "forgotten"
    QUARANTINED = "quarantined"
    RECALLED = "recalled"


MEMORY_ERROR_CODES: frozenset[str] = frozenset(
    {
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
    }
)


class MemoryError(RuntimeError):
    """A stable, structured long-term memory failure."""

    def __init__(self, code: str, message: str) -> None:
        if code not in MEMORY_ERROR_CODES:
            raise ValueError(f"unknown memory error code: {code}")
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {"error": True, "code": self.code, "message": self.message}


class MemoryScopeDeniedError(MemoryError):
    def __init__(self, message: str = "memory scope is not authorized") -> None:
        super().__init__("MEMORY_SCOPE_DENIED", message)


class MemoryNotFoundError(MemoryError):
    def __init__(self, message: str = "memory not found") -> None:
        super().__init__("MEMORY_NOT_FOUND", message)


class MemoryRevisionConflictError(MemoryError):
    def __init__(self, message: str = "memory revision is stale") -> None:
        super().__init__("MEMORY_REVISION_CONFLICT", message)


class MemoryKindInvalidError(MemoryError):
    def __init__(self, message: str = "memory kind is invalid") -> None:
        super().__init__("MEMORY_KIND_INVALID", message)


class MemorySecretBlockedError(MemoryError):
    def __init__(self, message: str = "credential-like content cannot be remembered") -> None:
        super().__init__("MEMORY_SECRET_BLOCKED", message)


class MemoryPiiBlockedError(MemoryError):
    def __init__(self, message: str = "PII cannot be remembered under the P0 policy") -> None:
        super().__init__("MEMORY_PII_BLOCKED", message)


class MemoryInjectionBlockedError(MemoryError):
    def __init__(self, message: str = "instruction-shaped content cannot be active memory") -> None:
        super().__init__("MEMORY_INJECTION_BLOCKED", message)


class MemoryConflictRequiresConfirmationError(MemoryError):
    def __init__(self, message: str = "active memory key already exists") -> None:
        super().__init__("MEMORY_CONFLICT_REQUIRES_CONFIRMATION", message)


class MemoryExpiredError(MemoryError):
    def __init__(self, message: str = "memory has expired") -> None:
        super().__init__("MEMORY_EXPIRED", message)


class MemoryForgottenError(MemoryError):
    def __init__(self, message: str = "memory has been forgotten") -> None:
        super().__init__("MEMORY_FORGOTTEN", message)


class MemoryStoreUnavailableError(MemoryError):
    def __init__(self, message: str = "memory store is unavailable") -> None:
        super().__init__("MEMORY_STORE_UNAVAILABLE", message)


def normalize_semantic_key(value: str) -> str:
    """Normalize a program-owned key without changing its meaning."""

    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if not normalized:
        raise ValueError("semantic_key must not be empty")
    if len(normalized) > 255:
        raise ValueError("semantic_key is too long")
    return normalized


def content_hash(value: str | None) -> str | None:
    """Return the shared short hash, or no hash when the tombstone has no body."""

    if not value:
        return None
    return content_hash_of(value)


def actor_hash(actor_user_id: UUID | str) -> str:
    """Hash an operator identifier before it crosses the audit boundary."""

    return content_hash_of(str(actor_user_id))


class MemoryRecord(BaseModel):
    """Current memory revision, including metadata-only tombstones."""

    model_config = ConfigDict(extra="forbid")

    memory_id: UUID
    tenant_id: UUID
    owner_user_id: UUID
    scope_type: MemoryScopeType
    project_id: UUID | None = None
    kind: MemoryKind
    semantic_key: str = Field(min_length=1, max_length=255)
    content: str = ""
    status: MemoryStatus = MemoryStatus.ACTIVE
    origin: MemoryOrigin
    source_thread_id: UUID | None = None
    source_message_id: UUID | None = None
    revision: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)
    expires_at: datetime | None = None
    tombstone: bool = False

    @field_validator("semantic_key")
    @classmethod
    def _normalize_key(cls, value: str) -> str:
        return normalize_semantic_key(value)

    @model_validator(mode="before")
    @classmethod
    def _default_tombstone(cls, values: Any) -> Any:
        if isinstance(values, dict):
            status = values.get("status")
            if status in (MemoryStatus.FORGOTTEN, MemoryStatus.FORGOTTEN.value):
                values = dict(values)
                values.setdefault("tombstone", True)
        return values

    @model_validator(mode="after")
    def _validate_scope_and_body(self) -> Self:
        if self.scope_type is MemoryScopeType.USER and self.project_id is not None:
            raise ValueError("user-scoped memory must not have project_id")
        if self.scope_type is MemoryScopeType.PROJECT and self.project_id is None:
            raise ValueError("project-scoped memory requires project_id")
        if self.kind is MemoryKind.USER_PREFERENCE and self.scope_type is not MemoryScopeType.USER:
            raise ValueError("user_preference memory must be user-scoped")
        if (
            self.kind in (MemoryKind.PROJECT_FACT, MemoryKind.PROJECT_DECISION)
            and self.scope_type is not MemoryScopeType.PROJECT
        ):
            raise ValueError("project facts and decisions must be project-scoped")
        if (
            self.status
            in (
                MemoryStatus.ACTIVE,
                MemoryStatus.SUPERSEDED,
                MemoryStatus.EXPIRED,
            )
            and not self.content.strip()
        ):
            raise ValueError("active memory revisions must carry正文")
        if self.status in (MemoryStatus.FORGOTTEN, MemoryStatus.QUARANTINED) and self.content:
            raise ValueError("forgotten and quarantined memories cannot carry正文")
        if self.status is MemoryStatus.FORGOTTEN and not self.tombstone:
            raise ValueError("forgotten memory must be a tombstone")
        if self.status is not MemoryStatus.FORGOTTEN and self.tombstone:
            raise ValueError("only forgotten memory may be a tombstone")
        return self


class MemoryEvent(BaseModel):
    """Append-only audit metadata; deliberately has no content field."""

    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    event_type: MemoryEventType
    memory_id: UUID
    actor_hash: str = Field(min_length=1, max_length=128)
    before_content_hash: str | None = Field(default=None, max_length=128)
    after_content_hash: str | None = Field(default=None, max_length=128)
    revision: int = Field(ge=1)
    created_at: datetime = Field(default_factory=_utc_now)
    source_thread_id: UUID | None = None
    source_message_id: UUID | None = None
    reason: str = Field(default="", max_length=256)


class MemoryUsage(BaseModel):
    """A recall usage row containing no copied memory正文."""

    model_config = ConfigDict(extra="forbid")

    usage_id: UUID
    tenant_id: UUID
    memory_id: UUID
    user_id: UUID
    source_thread_id: UUID | None = None
    run_id: UUID | None = None
    score: float | None = None
    created_at: datetime = Field(default_factory=_utc_now)


ALLOWED_STATUS_TRANSITIONS: dict[MemoryStatus, frozenset[MemoryStatus]] = {
    MemoryStatus.ACTIVE: frozenset(
        {
            MemoryStatus.SUPERSEDED,
            MemoryStatus.EXPIRED,
            MemoryStatus.FORGOTTEN,
            MemoryStatus.QUARANTINED,
        }
    ),
    MemoryStatus.SUPERSEDED: frozenset({MemoryStatus.FORGOTTEN}),
    MemoryStatus.EXPIRED: frozenset({MemoryStatus.FORGOTTEN}),
    MemoryStatus.QUARANTINED: frozenset({MemoryStatus.FORGOTTEN}),
    MemoryStatus.FORGOTTEN: frozenset(),
}


__all__ = [
    "ALLOWED_STATUS_TRANSITIONS",
    "MEMORY_ERROR_CODES",
    "MemoryConflictRequiresConfirmationError",
    "MemoryError",
    "MemoryEvent",
    "MemoryEventType",
    "MemoryExpiredError",
    "MemoryForgottenError",
    "MemoryInjectionBlockedError",
    "MemoryKind",
    "MemoryKindInvalidError",
    "MemoryNotFoundError",
    "MemoryOrigin",
    "MemoryPiiBlockedError",
    "MemoryRecord",
    "MemoryRevisionConflictError",
    "MemoryScopeDeniedError",
    "MemoryScopeType",
    "MemorySecretBlockedError",
    "MemoryStatus",
    "MemoryStoreUnavailableError",
    "MemoryUsage",
    "actor_hash",
    "content_hash",
    "normalize_semantic_key",
]
