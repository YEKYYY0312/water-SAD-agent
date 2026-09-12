"""Bounded, process-local storage for unconfirmed memory candidates."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
from uuid import UUID

from water_agent.memory.contracts import MemoryNotFoundError
from water_agent.memory.write_contracts import MemoryCandidate

PENDING_CANDIDATE_LIMIT = 256
PENDING_CANDIDATE_TTL_SECONDS = 15 * 60.0


@dataclass(frozen=True, slots=True)
class PendingCandidate:
    candidate: MemoryCandidate
    tenant_id: UUID
    actor_user_id: UUID
    expires_at_monotonic: float


class PendingCandidateStore:
    """Keep preview bodies out of persistent storage and bound RAM retention."""

    def __init__(
        self,
        *,
        limit: int = PENDING_CANDIDATE_LIMIT,
        ttl_seconds: float = PENDING_CANDIDATE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit <= 0 or ttl_seconds <= 0:
            raise ValueError("pending candidate limits must be positive")
        self._limit = limit
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._items: dict[UUID, PendingCandidate] = {}
        self._lock = RLock()

    def put(self, candidate: MemoryCandidate, *, tenant_id: UUID, actor_user_id: UUID) -> None:
        with self._lock:
            self._purge_expired()
            while len(self._items) >= self._limit:
                self._items.pop(next(iter(self._items)))
            self._items[candidate.candidate_id] = PendingCandidate(
                candidate=candidate,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                expires_at_monotonic=self._clock() + self._ttl_seconds,
            )

    def get(self, candidate_id: UUID) -> PendingCandidate:
        with self._lock:
            self._purge_expired()
            try:
                return self._items[candidate_id]
            except KeyError as exc:
                raise MemoryNotFoundError("memory candidate not found or expired") from exc

    def pop(self, candidate_id: UUID) -> PendingCandidate | None:
        with self._lock:
            self._purge_expired()
            return self._items.pop(candidate_id, None)

    def _purge_expired(self) -> None:
        now = self._clock()
        expired = [
            candidate_id
            for candidate_id, item in self._items.items()
            if item.expires_at_monotonic <= now
        ]
        for candidate_id in expired:
            self._items.pop(candidate_id, None)


__all__ = [
    "PENDING_CANDIDATE_LIMIT",
    "PENDING_CANDIDATE_TTL_SECONDS",
    "PendingCandidate",
    "PendingCandidateStore",
]
