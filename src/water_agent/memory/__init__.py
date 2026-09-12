"""P5 Step 39 explicit long-term memory primitives."""

from water_agent.memory.contracts import (
    ALLOWED_STATUS_TRANSITIONS,
    MEMORY_ERROR_CODES,
    MemoryConflictRequiresConfirmationError,
    MemoryError,
    MemoryEvent,
    MemoryEventType,
    MemoryExpiredError,
    MemoryForgottenError,
    MemoryKind,
    MemoryKindInvalidError,
    MemoryNotFoundError,
    MemoryOrigin,
    MemoryRecord,
    MemoryRevisionConflictError,
    MemoryScopeDeniedError,
    MemoryScopeType,
    MemoryStatus,
    MemoryStoreUnavailableError,
    MemoryUsage,
)
from water_agent.memory.repository import MemoryRepository
from water_agent.memory.service import MemoryService

__all__ = [
    "ALLOWED_STATUS_TRANSITIONS",
    "MEMORY_ERROR_CODES",
    "MemoryConflictRequiresConfirmationError",
    "MemoryError",
    "MemoryEvent",
    "MemoryEventType",
    "MemoryExpiredError",
    "MemoryForgottenError",
    "MemoryKind",
    "MemoryKindInvalidError",
    "MemoryNotFoundError",
    "MemoryOrigin",
    "MemoryRecord",
    "MemoryRepository",
    "MemoryRevisionConflictError",
    "MemoryScopeDeniedError",
    "MemoryScopeType",
    "MemoryService",
    "MemoryStatus",
    "MemoryStoreUnavailableError",
    "MemoryUsage",
]
