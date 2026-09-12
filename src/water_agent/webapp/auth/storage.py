"""Select the Step 37 memory stores or their Step 38 PostgreSQL versions."""

from __future__ import annotations

import os

from water_agent.storage.database import Database, DatabaseNotConfiguredError
from water_agent.webapp.auth.db_store import PostgresSessionStore, PostgresUserStore
from water_agent.webapp.auth.session_store import set_session_store
from water_agent.webapp.auth.user_store import set_user_store


def configure_database_stores(database: Database | None = None) -> Database | None:
    """Install PostgreSQL auth stores when ``DATABASE_URL`` is configured.

    Returning ``None`` keeps unit tests and the existing in-memory development
    mode intact.  Production Compose always supplies the URL and therefore
    cannot silently use the process-local stores.
    """

    if database is None and not os.getenv("DATABASE_URL"):
        return None
    try:
        database = database or Database.from_environment()
    except DatabaseNotConfiguredError:
        return None
    set_user_store(PostgresUserStore(database))
    set_session_store(PostgresSessionStore(database))
    return database


__all__ = ["configure_database_stores"]
