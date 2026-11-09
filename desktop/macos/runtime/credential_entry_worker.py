"""Receiver for a new, empty operator-owned source volume; never an App importer."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

SLOTS = ("WATER_AGENT_LLM_API_KEY", "WATER_AGENT_EMBEDDING_API_KEY")


def seed(binding: dict[str, Any], values: Any, root: Path) -> None:
    from water_agent.config import credentials
    from water_agent.generation.endpoints import write_panel_endpoint

    info = root.lstat()
    if (
        not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700 or any(root.iterdir())
        or not isinstance(values, dict) or set(values) != set(SLOTS)
    ):
        raise ValueError("ENTRY_INVALID_SOURCE")
    for value in values.values():
        if not isinstance(value, str) or credentials.validate_credential_value(value) != value:
            raise ValueError("ENTRY_INVALID_VALUE")
    snapshot = dict(os.environ)
    os.environ.clear()
    os.environ["WATER_AGENT_CREDENTIALS_DIR"] = str(root)
    try:
        for name in SLOTS:
            credentials.write_credential(name, values[name])
        write_panel_endpoint(binding["chat"]["base_url"], binding["chat"]["model"])
        if any(credentials.resolve_secret(n, getenv=lambda _: None) != values[n] for n in SLOTS):
            raise ValueError("ENTRY_READBACK_FAILED")
    except BaseException:
        for name in SLOTS:
            credentials.delete_credential(name)
        (root / "llm_endpoint.json").unlink(missing_ok=True)
        raise
    finally:
        os.environ.clear()
        os.environ.update(snapshot)


def main() -> None:
    # Silence before importing production or third-party modules. Payload only
    # enters through the dedicated stdin stream; no stdout or error bodies.
    fd = os.open(os.devnull, os.O_WRONLY)
    os.dup2(fd, 1); os.dup2(fd, 2); os.close(fd)
    import logging
    import sys

    logging.disable(logging.CRITICAL)
    os.umask(0o077)
    sys.path[:0] = ["/app/src", "/app/.venv/lib/python3.12/site-packages"]
    try:
        data = sys.stdin.buffer.read(65537)
        if len(data) > 65536:
            raise ValueError
        seed(json.loads(sys.argv[1]), json.loads(data), Path("/var/lib/water-agent-credentials"))
    except BaseException:  # noqa: BLE001 -- worker must never emit secret-bearing tracebacks
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
