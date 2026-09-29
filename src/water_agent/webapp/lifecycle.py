"""P5 Step 38: migration and bootstrap lifecycle commands for Compose.

Step 37 replaces the Step 36 bootstrap placeholder with a real bootstrap-admin
command that reads the initial admin password from /run/secrets/admin_password.
Migration failures are surfaced as a non-zero process exit; Compose therefore
never starts the app against a partially migrated schema.
"""
from __future__ import annotations

import argparse
import sys

from alembic.config import Config
from alembic.util import CommandError
from sqlalchemy.exc import SQLAlchemyError

from alembic import command
from water_agent.storage.database import DatabasePasswordFileError


def main() -> int:
    parser = argparse.ArgumentParser(description="P5 Step 38 lifecycle commands")
    parser.add_argument(
        "action",
        choices=("migrate", "migrate-placeholder", "bootstrap-admin"),
    )
    action = parser.parse_args().action

    if action == "migrate-placeholder":
        print("Step 38: use 'migrate'; the placeholder is retained only for old local scripts.")
        return 0

    if action == "migrate":
        try:
            command.upgrade(Config("alembic.ini"), "head")
        except DatabasePasswordFileError as exc:
            print(f"database migration failed: {exc}", file=sys.stderr)
            return 1
        except (OSError, RuntimeError, SQLAlchemyError, CommandError) as exc:
            # Only the exception class: driver messages carry host/user details.
            print(f"database migration failed: {type(exc).__name__}", file=sys.stderr)
            return 1
        print("database migration complete")
        return 0

    # bootstrap-admin: real implementation
    from water_agent.webapp.auth.bootstrap import run_bootstrap
    return run_bootstrap()


if __name__ == "__main__":
    raise SystemExit(main())
