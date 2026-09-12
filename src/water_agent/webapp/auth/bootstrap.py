"""P5 Step 37: bootstrap-admin one-shot command.

Reads the initial admin password ONLY from /run/secrets/admin_password.
Never accepts the password from command-line arguments or environment variables.

Idempotent: exits cleanly if an active admin already exists.
"""
from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from water_agent.webapp.auth import audit
from water_agent.webapp.auth.models import UserRole
from water_agent.webapp.auth.password import PasswordTooShortError
from water_agent.webapp.auth.storage import configure_database_stores
from water_agent.webapp.auth.user_store import UsernameTakenError, get_user_store

# The one and only path from which the initial admin password may be read.
ADMIN_SECRET_PATH = Path("/run/secrets/admin_password")
DEFAULT_ADMIN_USERNAME = "admin"


def _read_secret(path: Path) -> str:
    """Read secret file, strip trailing whitespace, never return empty string."""
    if not path.is_file():
        raise FileNotFoundError(
            f"Admin password secret not found at {path}. "
            "Mount the file as a Docker secret or create it manually for development."
        )
    content = path.read_text(encoding="utf-8").rstrip("\n\r ")
    if not content:
        raise ValueError(f"Admin password secret at {path} is empty.")
    return content


def run_bootstrap(
    *,
    secret_path: Path = ADMIN_SECRET_PATH,
    username: str = DEFAULT_ADMIN_USERNAME,
) -> int:
    """Create the initial admin user if none exists.  Return 0 on success.

    This function is the ONLY place the admin password secret is read.
    It must never log the password value — only its existence.
    """
    try:
        db = configure_database_stores()
        if db is None:
            print("bootstrap-admin: ERROR — database unavailable.", file=sys.stderr)
            return 1
        user_store = get_user_store()
        has_admin = user_store.has_admin()
    except (OSError, RuntimeError, SQLAlchemyError):
        print("bootstrap-admin: ERROR — database unavailable.", file=sys.stderr)
        return 1

    if has_admin:
        print("bootstrap-admin: an active admin already exists; skipping (idempotent).")
        audit.audit_bootstrap_admin_skipped()
        return 0

    try:
        password = _read_secret(secret_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"bootstrap-admin: ERROR — {exc}", file=sys.stderr)
        return 1

    try:
        user = user_store.create_user(
            username=username,
            plaintext_password=password,
            role=UserRole.ADMIN,
            must_change_password=False,
        )
    except PasswordTooShortError as exc:
        print(f"bootstrap-admin: ERROR — {exc}", file=sys.stderr)
        return 1
    except UsernameTakenError:
        # Shouldn't happen after has_admin() check, but guard anyway
        print(f"bootstrap-admin: ERROR — username {username!r} already exists.", file=sys.stderr)
        return 1

    # Log only the user_id, never the password.
    print(f"bootstrap-admin: admin user created (user_id={user.user_id}).")
    audit.audit_bootstrap_admin_created(user_id=str(user.user_id))
    return 0


def main() -> int:
    return run_bootstrap()


if __name__ == "__main__":
    raise SystemExit(main())
