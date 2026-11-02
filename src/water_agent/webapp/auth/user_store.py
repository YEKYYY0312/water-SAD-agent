"""P5 Step 37: in-memory user store.

Step 38 replaces this with SQLAlchemy + PostgreSQL. The interface contract is
stable so routes and tests stay unchanged at migration time.

Thread-safety note: Python's GIL makes dict reads/writes atomic for simple
operations; this is sufficient for the single-process development server.
A production PostgreSQL store provides real ACID semantics.
"""
from __future__ import annotations

import threading
from typing import Protocol
from uuid import UUID

from water_agent.webapp.auth.models import UserRecord, UserRole
from water_agent.webapp.auth.password import hash_password


class UserNotFoundError(KeyError):
    pass


class UsernameTakenError(ValueError):
    pass


class LastActiveAdminError(RuntimeError):
    """Deactivating this account would leave the deployment without an active admin."""


class UserStoreProtocol(Protocol):
    def get_by_id(self, user_id: UUID) -> UserRecord: ...

    def get_by_username(self, username: str) -> UserRecord: ...

    def has_admin(self) -> bool: ...

    def list_users(self) -> list[UserRecord]: ...

    def create_user(
        self,
        username: str,
        plaintext_password: str,
        role: UserRole,
        must_change_password: bool = False,
    ) -> UserRecord: ...

    def update_password_hash(self, user_id: UUID, new_hash: str) -> None: ...

    def reset_password_hash(self, user_id: UUID, new_hash: str) -> None: ...

    def set_must_change_password(self, user_id: UUID, value: bool) -> None: ...

    def set_active(self, user_id: UUID, *, active: bool) -> None: ...

    def deactivate(self, user_id: UUID) -> None:
        """Deactivate unless it would remove the last active admin (atomic check + write)."""
        ...


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

    def reset_password_hash(self, user_id: UUID, new_hash: str) -> None:
        with self._lock:
            record = self.get_by_id(user_id)
            self._by_id[user_id] = record.model_copy(
                update={"password_hash": new_hash, "must_change_password": True}
            )

    def set_must_change_password(self, user_id: UUID, value: bool) -> None:
        with self._lock:
            record = self.get_by_id(user_id)
            self._by_id[user_id] = record.model_copy(update={"must_change_password": value})

    def set_active(self, user_id: UUID, *, active: bool) -> None:
        with self._lock:
            record = self.get_by_id(user_id)
            updated = record.model_copy(update={"is_active": active})
            self._by_id[user_id] = updated

    def deactivate(self, user_id: UUID) -> None:
        with self._lock:
            record = self.get_by_id(user_id)
            others_active = any(
                other.user_id != user_id and other.role is UserRole.ADMIN and other.is_active
                for other in self._by_id.values()
            )
            if record.role is UserRole.ADMIN and record.is_active and not others_active:
                raise LastActiveAdminError(str(user_id))
            self._by_id[user_id] = record.model_copy(update={"is_active": False})


# Module-level singleton; Step 38 will replace with a DB-backed instance.
_store: UserStoreProtocol = UserStore()


def get_user_store() -> UserStoreProtocol:
    """Return the active user store (FastAPI dependency-injection target)."""
    return _store


def set_user_store(store: UserStoreProtocol) -> None:
    """Replace the process-local store during Web app assembly/tests."""
    global _store
    _store = store
