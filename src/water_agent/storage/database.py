"""SQLAlchemy engine, pool and transaction boundary for the Web app.

Secrets are read only from ``*_FILE`` values and are never included in error
messages or health responses.  The class deliberately exposes a small API so
repositories cannot accidentally create independent engines or transactions.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

MIGRATION_HEAD = "0006_step50_dxf_uploads"
CHECKPOINT_MIGRATION_HEAD = 9


class DatabaseNotConfiguredError(RuntimeError):
    """No Web database URL was configured."""


class DatabaseNotReadyError(RuntimeError):
    """The database is reachable but migrations are not at the expected head."""


class DatabasePasswordFileError(RuntimeError):
    """``DATABASE_PASSWORD_FILE`` is set but yields no usable password.

    Deliberately not a ``DatabaseNotConfiguredError``: callers treat that one as
    "no database, use the in-memory stores", which would hide a broken secret.
    """


def _read_password_file(path_value: str | None) -> str | None:
    # An unset variable means "the URL carries its own credentials".  A set
    # variable whose file is unreadable or empty is a deployment error: falling
    # back to a password-less connection turns it into an opaque auth failure
    # (CI 2026-09-29: a 0600 secret owned by the runner, container uid 10001).
    if not path_value:
        return None
    try:
        value = Path(path_value).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise DatabasePasswordFileError(
            f"DATABASE_PASSWORD_FILE is not readable ({type(exc).__name__})"
        ) from None
    if not value:
        raise DatabasePasswordFileError("DATABASE_PASSWORD_FILE is empty")
    return value


def database_url_from_environment(
    environ: dict[str, str] | None = None,
) -> str | None:
    """Build a SQLAlchemy URL from non-secret configuration and a secret file.

    ``DATABASE_URL`` may omit its password when ``DATABASE_PASSWORD_FILE`` is
    present.  An explicit password in the URL remains supported for local
    development, but is never returned by diagnostics.
    """

    env = os.environ if environ is None else environ
    raw_url = env.get("DATABASE_URL", "").strip()
    if not raw_url:
        return None
    password = _read_password_file(env.get("DATABASE_PASSWORD_FILE"))
    if not password:
        return raw_url
    try:
        parsed = make_url(raw_url)
        if parsed.password is None:
            return parsed.set(password=password).render_as_string(hide_password=False)
    except (TypeError, ValueError):
        # Invalid URLs are reported by SQLAlchemy on engine construction.  Do
        # not echo either the URL or a secret while handling that failure.
        return raw_url
    return raw_url


@dataclass(frozen=True)
class DatabaseSettings:
    url: str = field(repr=False)
    pool_size: int = 5
    max_overflow: int = 10
    pool_timeout: float = 30.0
    pool_recycle: int = 1800


class Database:
    """One SQLAlchemy engine and session factory for a Web process."""

    def __init__(
        self,
        url: str | None = None,
        *,
        engine: Engine | None = None,
        pool_size: int = 5,
        max_overflow: int = 10,
        pool_timeout: float = 30.0,
        pool_recycle: int = 1800,
    ) -> None:
        if engine is None and not url:
            raise DatabaseNotConfiguredError("DATABASE_URL is not configured")
        resolved_engine_url = str(engine.url) if engine is not None else url
        if resolved_engine_url is None:
            raise DatabaseNotConfiguredError("could not resolve engine URL")
        self.settings = DatabaseSettings(
            url=resolved_engine_url,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout,
            pool_recycle=pool_recycle,
        )
        if engine is not None:
            self.engine = engine
        else:
            kwargs: dict[str, object] = {
                "pool_pre_ping": True,
                "pool_recycle": pool_recycle,
            }
            if not self.settings.url.startswith("sqlite"):
                kwargs.update(
                    pool_size=pool_size,
                    max_overflow=max_overflow,
                    pool_timeout=pool_timeout,
                )
            self.engine = create_engine(self.settings.url, **kwargs)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False, class_=Session)

    @classmethod
    def from_environment(cls) -> Database:
        return cls(database_url_from_environment())

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        """Yield a transaction-scoped session and roll back all failures."""

        session = self.sessions()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def ping(self) -> bool:
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return True
        except (OSError, RuntimeError, SQLAlchemyError):
            return False

    def migration_revision(self) -> str | None:
        try:
            inspector = inspect(self.engine)
            if not inspector.has_table("alembic_version"):
                return None
            with self.engine.connect() as connection:
                row = connection.execute(text("SELECT version_num FROM alembic_version")).first()
            return str(row[0]) if row else None
        except (OSError, RuntimeError, SQLAlchemyError):
            return None

    def checkpoint_migration_revision(self) -> int | None:
        try:
            inspector = inspect(self.engine)
            if not inspector.has_table("checkpoint_migrations"):
                return None
            with self.engine.connect() as connection:
                value = connection.execute(text("SELECT max(v) FROM checkpoint_migrations")).scalar_one()
            return int(value) if value is not None else None
        except (OSError, RuntimeError, SQLAlchemyError, TypeError, ValueError):
            return None

    def ready(self) -> bool:
        """Return true only when the DB is reachable and migration is current."""

        return (
            self.ping()
            and self.migration_revision() == MIGRATION_HEAD
            and self.checkpoint_migration_revision() == CHECKPOINT_MIGRATION_HEAD
        )

    def require_ready(self) -> None:
        if not self.ready():
            raise DatabaseNotReadyError("database or migration is not ready")

    def dispose(self) -> None:
        self.engine.dispose()
