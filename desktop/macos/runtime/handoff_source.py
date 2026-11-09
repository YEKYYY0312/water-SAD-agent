"""Quiet, bounded source worker for a local operator credential handoff."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SLOTS = ("WATER_AGENT_LLM_API_KEY", "WATER_AGENT_EMBEDDING_API_KEY")
MAX_PAYLOAD_BYTES = 65536
PUBLIC_CODES = frozenset(
    {
        "HANDOFF_UNSAFE_FILE",
        "HANDOFF_INVALID_BINDING",
        "HANDOFF_SOURCE_MISSING",
        "HANDOFF_BINDING_MISMATCH",
        "HANDOFF_SOURCE_UNAVAILABLE",
        "HANDOFF_INVALID_PAYLOAD",
        "HANDOFF_TARGET_MANAGED",
        "HANDOFF_WRITE_FAILED",
        "HANDOFF_ROLLBACK_FAILED",
        "HANDOFF_LOCAL_DOCKER_REQUIRED",
        "HANDOFF_DOCKER_FAILED",
        "HANDOFF_UNSAFE_CONTAINER",
        "HANDOFF_SOURCE_TIMEOUT",
        "HANDOFF_CLEANUP_FAILED",
        "HANDOFF_ARGUMENTS_INVALID",
        "HANDOFF_FAILED",
        "HANDOFF_APP_RUNNING",
        "HANDOFF_UNSAFE_COMMAND",
        "HANDOFF_NOT_DIFFERENT",
        "HANDOFF_TTY_REQUIRED",
        "HANDOFF_ENTRY_FAILED",
    }
)


class HandoffError(RuntimeError):
    """Only fixed public codes may cross the process boundary."""

    def __init__(self, code: str):
        super().__init__(code if code in PUBLIC_CODES else "HANDOFF_FAILED")


def private_read(path: Path, *, missing_ok: bool = False) -> bytes | None:
    try:
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError
        if not path.exists() and missing_ok:
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > MAX_PAYLOAD_BYTES
            ):
                raise ValueError
            blocks = []
            length = 0
            while length <= MAX_PAYLOAD_BYTES:
                block = os.read(fd, min(8192, MAX_PAYLOAD_BYTES + 1 - length))
                if not block:
                    break
                blocks.append(block)
                length += len(block)
            data = b"".join(blocks)
            after = os.fstat(fd)
            if (
                len(data) > MAX_PAYLOAD_BYTES
                or len(data) != info.st_size
                or after.st_mtime_ns != info.st_mtime_ns
                or after.st_nlink != 1
                or after.st_size != info.st_size
            ):
                raise ValueError
            return data
        finally:
            os.close(fd)
    except (OSError, ValueError):
        raise HandoffError("HANDOFF_UNSAFE_FILE") from None


def validate_binding(data: Any) -> dict[str, Any]:
    try:
        from water_agent.generation.endpoints import validate_model_name, validate_panel_base_url

        if (
            not isinstance(data, dict)
            or set(data) != {"version", "volume", "image", "chat", "embedding"}
            or type(data["version"]) is not int
            or data["version"] != 1
            or not isinstance(data["volume"], str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", data["volume"])
            or not isinstance(data["image"], str)
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", data["image"])
        ):
            raise ValueError
        cleaned = dict(data)
        for kind in ("chat", "embedding"):
            pair = data[kind]
            if (
                not isinstance(pair, dict)
                or set(pair) != {"base_url", "model"}
                or any(not isinstance(v, str) for v in pair.values())
            ):
                raise ValueError
            base_url = validate_panel_base_url(pair["base_url"])
            if urlsplit(base_url).query:
                raise ValueError
            cleaned[kind] = {"base_url": base_url, "model": validate_model_name(pair["model"])}
        return cleaned
    except (ValueError, TypeError, KeyError):
        raise HandoffError("HANDOFF_INVALID_BINDING") from None


def write_private(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_suffix(".pending")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(json.dumps(data, ensure_ascii=False).encode())
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def produce(binding: dict[str, Any], root: Path, output: Path) -> None:
    """Read only the two dedicated slots after file and panel checks."""
    from water_agent.config import credentials
    from water_agent.generation.endpoints import (
        read_panel_endpoint,
        resolve_chat_endpoint,
        resolve_embedding_endpoint,
    )

    binding = validate_binding(binding)
    if not root.is_dir() or root.is_symlink():
        raise HandoffError("HANDOFF_UNSAFE_FILE")
    captured = {}
    for name in (*SLOTS, "panel"):
        filename = credentials.CREDENTIALS[name] if name != "panel" else "llm_endpoint.json"
        if not (root / filename).exists():
            raise HandoffError(
                "HANDOFF_BINDING_MISMATCH" if name == "panel" else "HANDOFF_SOURCE_MISSING"
            )
        captured[filename] = private_read(root / filename)
    snapshot = dict(os.environ)
    # The production resolver reads a private snapshot obtained with no-follow
    # FDs, so a concurrent source rename cannot turn its Path.read_text into a
    # symlink/FIFO read after the preflight check.
    source_snapshot = Path(tempfile.mkdtemp(prefix="source-", dir=output))
    try:
        for filename, content in captured.items():
            if content is None:
                raise HandoffError("HANDOFF_SOURCE_MISSING")
            fd = os.open(source_snapshot / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
        os.environ.clear()
        os.environ.update(
            {
                "WATER_AGENT_CREDENTIALS_DIR": str(source_snapshot),
                "WATER_AGENT_EMBEDDING_BASE_URL": binding["embedding"]["base_url"],
                "WATER_AGENT_EMBEDDING_MODEL": binding["embedding"]["model"],
            }
        )
        if read_panel_endpoint() != (binding["chat"]["base_url"], binding["chat"]["model"]):
            raise HandoffError("HANDOFF_BINDING_MISMATCH")
        values = {name: credentials.resolve_secret(name, getenv=lambda _: None) for name in SLOTS}
        if not all(values.values()):
            raise HandoffError("HANDOFF_SOURCE_MISSING")
        for name, value in values.items():
            if not isinstance(value, str):
                raise HandoffError("HANDOFF_SOURCE_MISSING")
            credentials.validate_credential_value(value)
            if credentials.credential_status(name, getenv=lambda _: None).source != "volume":
                raise HandoffError("HANDOFF_SOURCE_UNAVAILABLE")
        chat, embedding = resolve_chat_endpoint(), resolve_embedding_endpoint()
        for kind, endpoint, slot in (("chat", chat, SLOTS[0]), ("embedding", embedding, SLOTS[1])):
            if {"base_url": endpoint.base_url, "model": endpoint.model} != binding[
                kind
            ] or endpoint.api_key_name != slot:
                raise HandoffError("HANDOFF_BINDING_MISMATCH")
        write_private(
            output / "payload.json",
            {
                "version": 1,
                "chat": binding["chat"],
                "embedding": binding["embedding"],
                "values": values,
            },
        )
        write_private(output / "status.json", {"ok": True})
    finally:
        os.environ.clear()
        os.environ.update(snapshot)
        import shutil

        shutil.rmtree(source_snapshot)


def main() -> None:
    # -I -S avoids site/PYTHON startup hooks. Silence FDs before any production
    # or third-party import, even if that dependency unexpectedly prints.
    fd = os.open(os.devnull, os.O_WRONLY)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    os.close(fd)
    import logging
    import site
    import sys
    import time

    logging.disable(logging.CRITICAL)
    # Python 3.12 -S does not initialize the venv prefix. The supported pinned
    # water-agent image installs its dependencies here (Dockerfile contract).
    sys.path[:0] = ["/app/src", "/app/.venv/lib/python3.12/site-packages", *site.getsitepackages()]
    output = Path("/handoff")
    try:
        produce(json.loads(sys.argv[1]), Path("/var/lib/water-agent-credentials"), output)
    except BaseException as exc:  # noqa: BLE001 -- cancellation must not print a secret-bearing traceback
        code = str(exc) if isinstance(exc, HandoffError) else "HANDOFF_SOURCE_UNAVAILABLE"
        try:
            write_private(output / "status.json", {"ok": False, "code": code})
        except BaseException:  # noqa: BLE001 -- no alternative output channel is safe
            return
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        time.sleep(0.2)


if __name__ == "__main__":
    main()
