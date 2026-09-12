"""Deterministic contracts for P5 Step 40 explicit memory writes."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from water_agent.memory.contracts import (
    MemoryKind,
    MemoryOrigin,
    MemoryRecord,
    MemoryScopeType,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class CandidateOperation(str, Enum):
    CREATE = "create"
    CORRECT = "correct"


class CandidateDisposition(str, Enum):
    READY = "ready"
    REAFFIRM = "reaffirm"
    CONFLICT = "conflict"
    CORRECTION = "correction"
    QUARANTINED = "quarantined"


class ConflictResolution(str, Enum):
    CORRECT_EXISTING = "correct_existing"
    KEEP_BOTH = "keep_both"
    CANCEL = "cancel"


class MemoryValueBasis(str, Enum):
    """User-declared, confirmation-visible provenance for numeric content."""

    USER_STATEMENT = "user_statement"
    APPROVED_INPUT = "approved_input"
    APPROVED_DECISION = "approved_decision"


class MemoryWriteAction(str, Enum):
    CREATED = "created"
    REAFFIRMED = "reaffirmed"
    CORRECTED = "corrected"
    QUARANTINED = "quarantined"
    FORGOTTEN = "forgotten"
    CANCELLED = "cancelled"


class ConflictPreview(BaseModel):
    """Authorized comparison shown to the user; never written to trace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_id: UUID
    revision: int = Field(ge=1)
    current_content: str
    current_content_hash: str = Field(min_length=1)
    proposed_content: str
    proposed_content_hash: str = Field(min_length=1)


class MemoryCandidate(BaseModel):
    """Short-lived, in-process proposal that cannot write by itself."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: UUID
    operation: CandidateOperation
    disposition: CandidateDisposition
    scope_type: MemoryScopeType
    project_id: UUID | None = None
    kind: MemoryKind
    semantic_key: str = Field(min_length=1, max_length=255)
    content: str = ""
    content_hash: str = Field(min_length=1, max_length=128)
    origin: MemoryOrigin = MemoryOrigin.EXPLICIT_USER
    source_thread_id: UUID | None = None
    source_message_id: UUID | None = None
    memory_expires_at: datetime | None = None
    value_basis: MemoryValueBasis = MemoryValueBasis.USER_STATEMENT
    target_memory_id: UUID | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    conflict: ConflictPreview | None = None
    guard_rule_ids: tuple[str, ...] = ()
    requires_confirmation: Literal[True] = True
    created_at: datetime = Field(default_factory=_utc_now)

    @model_validator(mode="after")
    def _validate_disposition(self) -> Self:
        if self.disposition is CandidateDisposition.QUARANTINED:
            if self.content:
                raise ValueError("quarantined candidate cannot retain content")
            if not self.guard_rule_ids:
                raise ValueError("quarantined candidate requires guard rule ids")
        if self.disposition is CandidateDisposition.CONFLICT and self.conflict is None:
            raise ValueError("conflict candidate requires a conflict preview")
        if self.disposition is not CandidateDisposition.CONFLICT and self.conflict is not None:
            raise ValueError("only conflict candidates may carry a conflict preview")
        if self.operation is CandidateOperation.CORRECT and (
            self.target_memory_id is None or self.expected_revision is None
        ):
            raise ValueError("correction candidate requires target memory and revision")
        return self


class MemoryConfirmation(BaseModel):
    """Explicit user decision bound to the exact previewed content hash."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    confirmed: StrictBool
    content_hash: str = Field(min_length=1, max_length=128)
    resolution: ConflictResolution | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    alternative_semantic_key: str | None = Field(default=None, min_length=1, max_length=255)


class MemoryWriteResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: MemoryWriteAction
    memory: MemoryRecord | None = None
    content_hash: str = Field(default="", max_length=128)
    requires_confirmation: Literal[False] = False


class RememberCommand(BaseModel):
    """Parsed command payload; it is a form seed, never a write instruction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str
    requires_confirmation_form: Literal[True] = True


__all__ = [
    "CandidateDisposition",
    "CandidateOperation",
    "ConflictPreview",
    "ConflictResolution",
    "MemoryCandidate",
    "MemoryConfirmation",
    "MemoryValueBasis",
    "MemoryWriteAction",
    "MemoryWriteResult",
    "RememberCommand",
]
