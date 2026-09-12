"""P5 Step 37: in-memory user store.

Step 38 replaces this with SQLAlchemy + PostgreSQL. The interface contract is
stable so routes and tests stay unchanged at migration time.

Thread-safety note: Python's GIL makes dict reads/writes atomic for simple
operations; this is sufficient for the single-process development server.
A production PostgreSQL store provides real ACID semantics.
"""
from __future__ import annotations

import threading
from uuid import UUID

from water_agent.webapp.auth.models import UserRecord, UserRole
from water_agent.webapp.auth.password import hash_password


class UserNotFoundError(KeyError):
    pass


class UsernameTakenError(ValueError):
    pass


class UserStore:
    """In-memory user store keyed by user_id (UUID) and username."""

    def __init__(self) -> None:
        self._by_id: dict[UUID, UserRecord] = {}
        self._by_username: dict[str, UUID] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_by_id(self, user_id: UUID) -> UserRecord:
        try:
            return self._by_id[user_id]
        except KeyError:
            raise UserNotFoundError(str(user_id))

    def get_by_username(self, username: str) -> UserRecord:
        uid = self._by_username.get(username)
        if uid is None:
            raise UserNotFoundError(username)
        return self._by_id[uid]

    def has_admin(self) -> bool:
        return any(u.role is UserRole.ADMIN and u.is_active for u in self._by_id.values())

    def list_users(self) -> list[UserRecord]:
        return list(self._by_id.values())

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def create_user(
        self,
        username: str,
        plaintext_password: str,
        role: UserRole,
        must_change_password: bool = False,
    ) -> UserRecord:
        with self._lock:
            if username in self._by_username:
                raise UsernameTakenError(f"用户名 {username!r} 已被占用")
            record = UserRecord(
                username=username,
                password_hash=hash_password(plaintext_password),
                role=role,
                must_change_password=must_change_password,
            )
            self._by_id[record.user_id] = record
            self._by_username[username] = record.user_id
            return record

    def update_password_hash(self, user_id: UUID, new_hash: str) -> None:
        with self._lock:
            record = self.get_by_id(user_id)
            updated = record.model_copy(
                update={"password_hash": new_hash, "must_change_password": False}
            )
            self._by_id[user_id] = updated

    def set_active(self, user_id: UUID, *, active: bool) -> None:
        with self._lock:
            record = self.get_by_id(user_id)
            updated = record.model_copy(update={"is_active": active})
            self._by_id[user_id] = updated


# Module-level singleton; Step 38 will replace with a DB-backed instance.
_store = UserStore()


def get_user_store() -> UserStore:
    """Return the active user store (FastAPI dependency-injection target)."""
    return _store
