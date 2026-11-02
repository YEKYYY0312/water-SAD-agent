"""P5 Step 37: bootstrap-admin one-shot command.

Reads the initial admin password ONLY from /run/secrets/admin_password.
Never accepts the password from command-line arguments or environment variables.

Idempotent: exits cleanly if an active admin already exists, or if an admin
account with the bootstrap username already exists -- even a deactivated one.
Bootstrap is not a recovery path: it never reactivates an account and never
resets a password from the secret file.  Compose starts the app only after this
job exits 0, so a deactivated ``admin`` used to keep the app down for good.
"""
from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from water_agent.webapp.auth import audit
from water_agent.webapp.auth.models import UserRecord, UserRole
from water_agent.webapp.auth.password import PasswordTooShortError
from water_agent.webapp.auth.storage import configure_database_stores
from water_agent.webapp.auth.user_store import (
    UsernameTakenError,
    UserNotFoundError,
    get_user_store,
)

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


def _existing_account(user: UserRecord, username: str) -> int:
    """Outcome when ``username`` is already taken; the account is never modified."""
    if user.role is not UserRole.ADMIN:
        print(
            f"bootstrap-admin: ERROR — username {username!r} is taken by a non-admin account; "
            "no admin was created.",
            file=sys.stderr,
        )
        return 1
    if user.is_active:
        print(f"bootstrap-admin: admin {username!r} already exists; skipping (idempotent).")
        audit.audit_bootstrap_admin_skipped()
        return 0
    print(
        f"bootstrap-admin: WARNING — admin {username!r} already exists but is deactivated; "
        "it is not reactivated and its password is not reset (idempotent skip). "
        "No active admin remains: an operator must reactivate it in the database.",
        file=sys.stderr,
    )
    audit.audit_bootstrap_admin_skipped(reason="admin_exists_inactive")
    return 0


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

    # The name check comes before the secret is read: an existing account needs
    # neither, and the secret must never be applied to it.
    try:
        existing = user_store.get_by_username(username)
    except UserNotFoundError:
        existing = None
    except (OSError, RuntimeError, SQLAlchemyError):
        print("bootstrap-admin: ERROR — database unavailable.", file=sys.stderr)
        return 1
    if existing is not None:
        return _existing_account(existing, username)

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
        # Taken concurrently between the lookup above and this insert: apply the
        # same rule to whoever holds the name now.
        try:
            return _existing_account(user_store.get_by_username(username), username)
        except (UserNotFoundError, OSError, RuntimeError, SQLAlchemyError):
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
