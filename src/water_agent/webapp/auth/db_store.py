"""PostgreSQL-backed implementations of the Step 37 auth store contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from water_agent.storage.database import Database
from water_agent.storage.models import AuthSession, User
from water_agent.webapp.auth.models import SessionRecord, UserRecord, UserRole
from water_agent.webapp.auth.password import hash_password
from water_agent.webapp.auth.session_store import SessionNotFoundError
from water_agent.webapp.auth.user_store import (
    LastActiveAdminError,
    UsernameTakenError,
    UserNotFoundError,
)


def _user(record: User) -> UserRecord:
    return UserRecord(
        user_id=UUID(record.user_id),
        username=record.username,
        password_hash=record.password_hash,
        role=UserRole(record.role),
        must_change_password=record.must_change_password,
        is_active=record.is_active,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _session(record: AuthSession) -> SessionRecord:
    return SessionRecord(
        session_id=record.session_id,
        user_id=UUID(record.user_id),
        csrf_token=record.csrf_token,
        created_at=record.created_at,
        expires_at=record.expires_at,
    )


class PostgresUserStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get_by_id(self, user_id: UUID) -> UserRecord:
        with self.database.transaction() as session:
            record = session.get(User, str(user_id))
            if record is None:
                raise UserNotFoundError(str(user_id))
            return _user(record)

    def get_by_username(self, username: str) -> UserRecord:
        with self.database.transaction() as session:
            record = session.execute(select(User).where(User.username == username)).scalar_one_or_none()
            if record is None:
                raise UserNotFoundError(username)
            return _user(record)

    def has_admin(self) -> bool:
        with self.database.transaction() as session:
            return session.execute(
                select(User.user_id).where(User.role == UserRole.ADMIN.value, User.is_active.is_(True)).limit(1)
            ).first() is not None

    def list_users(self) -> list[UserRecord]:
        with self.database.transaction() as session:
            return [_user(item) for item in session.execute(select(User).order_by(User.created_at)).scalars()]

    def create_user(
        self,
        username: str,
        plaintext_password: str,
        role: UserRole,
        must_change_password: bool = False,
    ) -> UserRecord:
        now = datetime.now(UTC)
        record = User(
            user_id=str(uuid4()),
            username=username,
            password_hash=hash_password(plaintext_password),
            role=role.value,
            must_change_password=must_change_password,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        try:
            with self.database.transaction() as session:
                session.add(record)
            return _user(record)
        except IntegrityError as exc:
            raise UsernameTakenError(username) from exc

    def update_password_hash(self, user_id: UUID, new_hash: str) -> None:
        with self.database.transaction() as session:
            record = session.get(User, str(user_id))
            if record is None:
                raise UserNotFoundError(str(user_id))
            record.password_hash = new_hash
            record.must_change_password = False
            record.updated_at = datetime.now(UTC)

    def reset_password_hash(self, user_id: UUID, new_hash: str) -> None:
        """Atomically rotate the hash and require a first-login password change."""
        with self.database.transaction() as session:
            record = session.get(User, str(user_id))
            if record is None:
                raise UserNotFoundError(str(user_id))
            record.password_hash = new_hash
            record.must_change_password = True
            record.updated_at = datetime.now(UTC)

    def set_must_change_password(self, user_id: UUID, value: bool) -> None:
        with self.database.transaction() as session:
            record = session.get(User, str(user_id))
            if record is None:
                raise UserNotFoundError(str(user_id))
            record.must_change_password = value
            record.updated_at = datetime.now(UTC)

    def set_active(self, user_id: UUID, *, active: bool) -> None:
        with self.database.transaction() as session:
            record = session.get(User, str(user_id))
            if record is None:
                raise UserNotFoundError(str(user_id))
            record.is_active = active
            record.updated_at = datetime.now(UTC)

    def deactivate(self, user_id: UUID) -> None:
        """Deactivate ``user_id`` unless it is the last active admin.

        Check and write share one transaction.  Every active admin row is locked
        ``FOR UPDATE`` in a fixed order first, so on PostgreSQL two admins
        deactivating each other concurrently serialize: the second re-reads the
        rows after the first commits, no longer sees the other admin as active,
        and is refused.  (SQLite ignores FOR UPDATE and serializes writers itself.)
        """
        with self.database.transaction() as session:
            active_admins = set(
                session.execute(
                    select(User.user_id)
                    .where(User.role == UserRole.ADMIN.value, User.is_active.is_(True))
                    .order_by(User.user_id)
                    .with_for_update()
                ).scalars()
            )
            record = session.get(User, str(user_id))
            if record is None:
                raise UserNotFoundError(str(user_id))
            if active_admins == {record.user_id}:
                raise LastActiveAdminError(str(user_id))
            record.is_active = False
            record.updated_at = datetime.now(UTC)


class PostgresSessionStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get(self, session_id: str) -> SessionRecord:
        with self.database.transaction() as session:
            record = session.get(AuthSession, session_id)
            if record is None:
                raise SessionNotFoundError(session_id)
            return _session(record)

    def list_for_user(self, user_id: UUID) -> list[SessionRecord]:
        with self.database.transaction() as session:
            records = session.execute(
                select(AuthSession).where(AuthSession.user_id == str(user_id)).order_by(AuthSession.created_at)
            ).scalars()
            return [_session(item) for item in records]

    def create(self, user_id: UUID) -> SessionRecord:
        value = SessionRecord(user_id=user_id)
        record = AuthSession(
            session_id=value.session_id,
            user_id=str(user_id),
            csrf_token=value.csrf_token,
            created_at=value.created_at,
            expires_at=value.expires_at,
        )
        with self.database.transaction() as session:
            session.add(record)
        return value

    def delete(self, session_id: str) -> None:
        with self.database.transaction() as session:
            session.execute(delete(AuthSession).where(AuthSession.session_id == session_id))

    def delete_all_for_user(self, user_id: UUID) -> None:
        with self.database.transaction() as session:
            session.execute(delete(AuthSession).where(AuthSession.user_id == str(user_id)))


__all__ = ["PostgresSessionStore", "PostgresUserStore"]
