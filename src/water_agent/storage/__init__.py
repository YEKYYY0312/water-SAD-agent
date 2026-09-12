"""P5 Step 38 persistence primitives.

The storage package owns the Web database boundary.  CLI callers continue to
use :mod:`water_agent.session` and its SQLite checkpointer.
"""

from water_agent.storage.database import (
    Database,
    DatabaseNotConfiguredError,
    DatabaseNotReadyError,
    database_url_from_environment,
)

__all__ = [
    "Database",
    "DatabaseNotConfiguredError",
    "DatabaseNotReadyError",
    "database_url_from_environment",
]
