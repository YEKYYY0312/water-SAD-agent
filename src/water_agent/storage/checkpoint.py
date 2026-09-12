"""LangGraph PostgreSQL checkpointer factory.

The official ``langgraph-checkpoint-postgres`` saver is reused; this module
only owns pool lifecycle and readiness checks.  It never invokes
``PostgresSaver.setup`` because Alembic is the sole schema authority.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy.engine import make_url

from water_agent.session import default_checkpoint_serde
from water_agent.storage.database import Database


def _psycopg_conninfo(url: str) -> str:
    parsed = make_url(url)
    if parsed.drivername.startswith("postgresql+"):
        parsed = parsed.set(drivername="postgresql")
    return parsed.render_as_string(hide_password=False)


@contextmanager
def postgres_checkpointer(
    database: Database,
    *,
    min_size: int = 1,
    max_size: int = 5,
    pipeline: bool = False,
) -> Iterator[PostgresSaver]:
    """Yield a ready saver backed by a bounded psycopg connection pool."""

    if pipeline:
        raise ValueError("pipeline mode requires a single psycopg connection, not a pool")
    database.require_ready()
    pool = ConnectionPool(
        conninfo=_psycopg_conninfo(database.settings.url),
        min_size=min_size,
        max_size=max_size,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    try:
        pool.open(wait=True)
        saver = PostgresSaver(cast(Any, pool), serde=default_checkpoint_serde())
        # Runtime schema setup is forbidden: checkpoint schema is owned by Alembic.
        yield saver
    finally:
        pool.close()


__all__ = ["postgres_checkpointer"]
