"""Bounded, process-local storage for unconfirmed memory candidates.

Capacity is accounted per user.  The store used to have only one global limit
and evicted the oldest candidate regardless of whose it was, so one user
previewing 256 memories evicted every other user's pending preview and their
confirmation answered ``MEMORY_NOT_FOUND``.  Now a user over quota only loses
their own oldest candidate.  The global limit stays as the memory bound for
many concurrent users; when it is reached, only the user holding the most
candidates loses one (the submitter first on a tie), never a lighter user.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
from uuid import UUID

from water_agent.memory.contracts import MemoryNotFoundError
from water_agent.memory.write_contracts import MemoryCandidate

#: Total candidates held by one process, across all users: the RAM bound.
PENDING_CANDIDATE_LIMIT = 256
#: Candidates one user may keep pending at once.  Far above interactive use
#: (preview, then confirm), and 8 users at full quota still fit the total.
PENDING_CANDIDATE_PER_USER_LIMIT = 32
PENDING_CANDIDATE_TTL_SECONDS = 15 * 60.0

_Owner = tuple[UUID, UUID]


@dataclass(frozen=True, slots=True)
class PendingCandidate:
    candidate: MemoryCandidate
    tenant_id: UUID
    actor_user_id: UUID
    expires_at_monotonic: float

    @property
    def owner(self) -> _Owner:
        return (self.tenant_id, self.actor_user_id)


class PendingCandidateStore:
    """Keep preview bodies out of persistent storage and bound RAM retention."""

    def __init__(
        self,
        *,
        limit: int = PENDING_CANDIDATE_LIMIT,
        per_user_limit: int = PENDING_CANDIDATE_PER_USER_LIMIT,
        ttl_seconds: float = PENDING_CANDIDATE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit <= 0 or per_user_limit <= 0 or ttl_seconds <= 0:
            raise ValueError("pending candidate limits must be positive")
        self._limit = limit
        self._per_user_limit = per_user_limit
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        # Insertion order is age order: the first entry of an owner is their oldest.
        self._items: dict[UUID, PendingCandidate] = {}
        self._lock = RLock()

    def put(self, candidate: MemoryCandidate, *, tenant_id: UUID, actor_user_id: UUID) -> None:
        owner: _Owner = (tenant_id, actor_user_id)
        with self._lock:
            self._purge_expired()
            while self._count(owner) >= self._per_user_limit:
                self._evict_oldest_of(owner)
            while len(self._items) >= self._limit:
                self._evict_oldest_of(self._heaviest_owner(submitter=owner))
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

    def _count(self, owner: _Owner) -> int:
        return sum(1 for item in self._items.values() if item.owner == owner)

    def _evict_oldest_of(self, owner: _Owner) -> None:
        oldest = next(
            candidate_id for candidate_id, item in self._items.items() if item.owner == owner
        )
        del self._items[oldest]

    def _heaviest_owner(self, *, submitter: _Owner) -> _Owner:
        """The owner to evict from when the total limit is reached.

        Only the owner holding the most candidates is eligible.  On a tie the
        submitter goes first, so nobody can push out a peer holding as many
        candidates as they do; otherwise the tied owner whose oldest candidate
        has waited longest goes first.  Counts follow insertion order, so the
        first maximal owner encountered is that longest-waiting one.
        """

        counts: dict[_Owner, int] = {}
        for item in self._items.values():
            counts[item.owner] = counts.get(item.owner, 0) + 1
        most = max(counts.values())
        if counts.get(submitter) == most:
            return submitter
        return next(owner for owner, count in counts.items() if count == most)

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
    "PENDING_CANDIDATE_PER_USER_LIMIT",
    "PENDING_CANDIDATE_TTL_SECONDS",
    "PendingCandidate",
    "PendingCandidateStore",
]
