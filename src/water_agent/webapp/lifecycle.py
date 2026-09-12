"""P5 Step 37: lifecycle commands for Compose.

Step 37 replaces the Step 36 bootstrap placeholder with a real bootstrap-admin
command that reads the initial admin password from /run/secrets/admin_password.
The migrate placeholder is retained until Step 38 delivers schema migrations.
"""
from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="P5 Step 37 lifecycle commands")
    parser.add_argument(
        "action",
        choices=("migrate-placeholder", "bootstrap-admin"),
    )
    action = parser.parse_args().action

    if action == "migrate-placeholder":
        print("Step 37: no database migrations are defined; Step 38 owns schema migration.")
        return 0

    # bootstrap-admin: real implementation
    from water_agent.webapp.auth.bootstrap import run_bootstrap
    return run_bootstrap()


if __name__ == "__main__":
    raise SystemExit(main())
