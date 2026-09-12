"""P5 Step 37: in-memory session store.

Step 38 replaces this with a PostgreSQL auth_sessions table.
"""
from __future__ import annotations

import threading
from uuid import UUID

from water_agent.webapp.auth.models import SessionRecord


class SessionNotFoundError(KeyError):
    pass


class SessionStore:
    """In-memory session store keyed by session_id string."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionRecord] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, session_id: str) -> SessionRecord:
        record = self._sessions.get(session_id)
        if record is None:
            raise SessionNotFoundError(session_id)
        return record

    def list_for_user(self, user_id: UUID) -> list[SessionRecord]:
        return [s for s in self._sessions.values() if s.user_id == user_id]

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def create(self, user_id: UUID) -> SessionRecord:
        with self._lock:
            record = SessionRecord(user_id=user_id)
            self._sessions[record.session_id] = record
            return record

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def delete_all_for_user(self, user_id: UUID) -> None:
        with self._lock:
            to_remove = [sid for sid, s in self._sessions.items() if s.user_id == user_id]
            for sid in to_remove:
                del self._sessions[sid]


# Module-level singleton; Step 38 replaces with DB-backed instance.
_store = SessionStore()


def get_session_store() -> SessionStore:
    """Return the active session store (FastAPI dependency-injection target)."""
    return _store
