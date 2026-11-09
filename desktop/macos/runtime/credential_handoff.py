"""Explicit local operator handoff. Never imported or called by the Agent."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn
from uuid import uuid4

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
if TYPE_CHECKING:
    from .handoff_source import SLOTS, HandoffError, private_read, validate_binding
    from .paths import DesktopPaths, InstanceLock, private_directory, reject_symlinks
else:
    try:
        from .handoff_source import SLOTS, HandoffError, private_read, validate_binding
        from .paths import DesktopPaths, InstanceLock, private_directory, reject_symlinks
    except ImportError:
        from handoff_source import SLOTS, HandoffError, private_read, validate_binding
        from paths import DesktopPaths, InstanceLock, private_directory, reject_symlinks

DOCKER = "/Applications/Docker.app/Contents/Resources/bin/docker"
SOURCE_ROOT = "/var/lib/water-agent-credentials"
SOURCE_PYTHON = "/app/.venv/bin/python"
LABEL = "local.wateragent.credential-handoff"
# This dedicated exec stream is connected to a private host FD, never PIPE or
# the console. docker cp cannot archive tmpfs; no tar extraction is needed.
COPY_PROGRAM = r"""
import os,stat,sys
if sys.argv[1] not in ("status.json","payload.json"):raise SystemExit(2)
fd=os.open("/handoff/"+sys.argv[1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
try:
 info=os.fstat(fd)
 if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_nlink!=1 or stat.S_IMODE(info.st_mode)!=0o600 or info.st_size>65536:raise SystemExit(2)
 blocks=[];length=0
 while length<=65536:
  block=os.read(fd,min(8192,65537-length))
  if not block:break
  blocks.append(block);length+=len(block)
 data=b"".join(blocks);after=os.fstat(fd)
 if len(data)!=info.st_size or length>65536 or after.st_mtime_ns!=info.st_mtime_ns or after.st_size!=info.st_size or after.st_nlink!=1:raise SystemExit(2)
 offset=0
 while offset<len(data):
  written=os.write(1,data[offset:])
  if written<=0:raise SystemExit(2)
  offset+=written
finally:os.close(fd)
"""


def read_binding(path: Path) -> dict[str, Any]:
    try:
        data = private_read(path)
        if data is None:
            raise HandoffError("HANDOFF_UNSAFE_FILE")
        return validate_binding(json.loads(data))
    except (ValueError, TypeError):
        raise HandoffError("HANDOFF_INVALID_BINDING") from None


def check_store(root: Path) -> None:
    from water_agent.config.credentials import CREDENTIALS

    reject_symlinks(root)
    info = root.stat()
    if not root.is_dir() or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise HandoffError("HANDOFF_UNSAFE_FILE")
    for name in SLOTS:
        target = root / CREDENTIALS[name]
        private_read(target, missing_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        if temporary.exists() or temporary.is_symlink():
            raise HandoffError("HANDOFF_UNSAFE_FILE")


def install_payload(
    payload: Any, binding: dict[str, Any], *, require_different: bool = False, compare_only: bool = False
) -> dict[str, Any]:
    """Caller owns the desktop instance lock. Compensates ordinary failures only."""
    from water_agent.config import credentials

    root = credentials.credentials_dir()
    check_store(root)  # before resolve_secret can follow a file
    if any(os.environ.get(n + "_FILE", "").strip() for n in SLOTS):
        raise HandoffError("HANDOFF_TARGET_MANAGED")
    if any(credentials.credential_status(n).source not in {"volume", "none"} for n in SLOTS):
        raise HandoffError("HANDOFF_TARGET_MANAGED")
    try:
        if (
            not isinstance(payload, dict)
            or set(payload) != {"version", "chat", "embedding", "values"}
            or type(payload["version"]) is not int
            or payload["version"] != 1
        ):
            raise ValueError
        if any(payload[kind] != binding[kind] for kind in ("chat", "embedding")):
            raise HandoffError("HANDOFF_BINDING_MISMATCH")
        values = payload["values"]
        if not isinstance(values, dict) or set(values) != set(SLOTS):
            raise ValueError
        for value in values.values():
            if not isinstance(value, str) or credentials.validate_credential_value(value) != value:
                raise ValueError
    except (ValueError, TypeError, KeyError):
        raise HandoffError("HANDOFF_INVALID_PAYLOAD") from None
    old = {name: credentials.resolve_secret(name, getenv=lambda _: None) for name in SLOTS}
    if any((root / credentials.CREDENTIALS[n]).exists() and old[n] is None for n in SLOTS):
        raise HandoffError("HANDOFF_UNSAFE_FILE")
    different = {name: old[name] is not None and old[name] != values[name] for name in SLOTS}
    if require_different and not all(different.values()):
        raise HandoffError("HANDOFF_NOT_DIFFERENT")
    if compare_only:
        return {
            "ok": True, "written": False, "authentication_checked": False, "rotated": False,
            "different_from_previous": different,
            "matches_previous": {n: old[n] is not None and old[n] == values[n] for n in SLOTS},
        }
    try:
        for name in SLOTS:
            credentials.write_credential(name, values[name])
        check_store(root)
        if any(credentials.resolve_secret(n, getenv=lambda _: None) != values[n] for n in SLOTS):
            raise HandoffError("HANDOFF_WRITE_FAILED")
    except BaseException:  # noqa: BLE001 -- compensate cancellation/SystemExit as well as write errors
        failed = False
        for name in reversed(SLOTS):
            try:
                check_store(root)
                previous = old[name]
                if previous is None:
                    credentials.delete_credential(name)
                else:
                    credentials.write_credential(name, previous)
            except BaseException:  # noqa: BLE001 -- restoration errors never carry credential values outward
                failed = True
        try:
            check_store(root)
            for name in SLOTS:
                if credentials.resolve_secret(name, getenv=lambda _: None) != old[name]:
                    failed = True
                if old[name] is None and (root / credentials.CREDENTIALS[name]).exists():
                    failed = True
        except BaseException:  # noqa: BLE001 -- unsuccessful verification is an unsuccessful rollback
            failed = True
        raise HandoffError(
            "HANDOFF_ROLLBACK_FAILED" if failed else "HANDOFF_WRITE_FAILED"
        ) from None
    return {
        "ok": True, "slots": list(SLOTS), "authentication_checked": False,
        "rotated": False, "different_from_previous": different,
    }


class DockerTransport:
    def __init__(self, socket_path: Path, configuration: Path):
        if not socket_path.is_absolute() or ".." in socket_path.parts:
            raise HandoffError("HANDOFF_LOCAL_DOCKER_REQUIRED")
        try:
            socket_path = socket_path.resolve(strict=True)
            info = socket_path.stat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
                raise ValueError
        except (OSError, ValueError):
            raise HandoffError("HANDOFF_LOCAL_DOCKER_REQUIRED") from None
        self.prefix = [
            DOCKER,
            "--config",
            str(configuration),
            "--host",
            "unix://" + str(socket_path),
        ]
        self.env = {"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "LANG": "en_US.UTF-8"}

    def call(self, args: list[str], *, timeout: float = 10, optional: bool = False) -> bytes:
        if not args or args[0] not in {"create", "inspect", "volume", "start", "ps", "rm", "image"}:
            raise HandoffError("HANDOFF_UNSAFE_COMMAND")
        try:
            result = subprocess.run(
                self.prefix + args,
                env=self.env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            if result.returncode and not optional:
                raise HandoffError("HANDOFF_DOCKER_FAILED")
            return result.stdout if result.returncode == 0 else b""
        except (OSError, subprocess.SubprocessError):
            raise HandoffError("HANDOFF_DOCKER_FAILED") from None

    def copy_to_file(
        self, name: str, remote: str, destination: Path, *, optional: bool = False
    ) -> bool:
        if remote not in {"status.json", "payload.json"}:
            raise HandoffError("HANDOFF_UNSAFE_COMMAND")
        fd = None
        successful = False
        try:
            reject_symlinks(destination)
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.fchmod(fd, 0o600)
            argv = self.prefix + [
                "exec",
                "--user",
                "10001:10001",
                name,
                SOURCE_PYTHON,
                "-I",
                "-S",
                "-B",
                "-c",
                COPY_PROGRAM,
                remote,
            ]
            result = subprocess.run(
                argv,
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=fd,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            if result.returncode:
                if not optional:
                    raise HandoffError("HANDOFF_DOCKER_FAILED")
                return False
            os.fsync(fd)
            successful = True
            return True
        except (OSError, ValueError, subprocess.SubprocessError):
            raise HandoffError("HANDOFF_DOCKER_FAILED") from None
        finally:
            if fd is not None:
                os.close(fd)
                if not successful:
                    try:
                        destination.unlink(missing_ok=True)
                    except OSError:
                        raise HandoffError("HANDOFF_CLEANUP_FAILED") from None


def source_command(binding: dict[str, Any], name: str, nonce: str) -> list[str]:
    producer = Path(__file__).with_name("handoff_source.py").read_text()
    return [
        "create",
        "--pull",
        "never",
        "--name",
        name,
        "--label",
        f"{LABEL}={nonce}",
        "--user",
        "10001:10001",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--log-driver",
        "none",
        "--memory",
        "256m",
        "--pids-limit",
        "64",
        "--env",
        "LD_PRELOAD=",
        "--env",
        "LD_LIBRARY_PATH=",
        "--mount",
        f"type=volume,source={binding['volume']},target={SOURCE_ROOT},readonly",
        "--tmpfs",
        "/handoff:rw,noexec,nosuid,size=1m,mode=0700,uid=10001,gid=10001",
        "--entrypoint",
        SOURCE_PYTHON,
        binding["image"],
        "-I",
        "-S",
        "-B",
        "-c",
        producer,
        json.dumps(binding, separators=(",", ":")),
    ]


def verify_container(
    info: dict[str, Any], binding: dict[str, Any], nonce: str, *, source_writable: bool = False
) -> None:
    try:
        host, config = info["HostConfig"], info["Config"]
        mounts = info["Mounts"]
        if (
            info["Image"] != binding["image"]
            or config["Labels"].get(LABEL) != nonce
            or host["LogConfig"]["Type"] != "none"
            or host["NetworkMode"] != "none"
            or host["ReadonlyRootfs"] is not True
            or "ALL" not in host["CapDrop"]
            or host.get("CapAdd")
            or host.get("Privileged") is not False
            or host["Memory"] != 256 * 1024 * 1024
            or host["PidsLimit"] != 64
            or not any(
                s in {"no-new-privileges", "no-new-privileges=true"} for s in host["SecurityOpt"]
            )
            or config["User"] != "10001:10001"
            or config["Entrypoint"] != [SOURCE_PYTHON]
            or not any(
                m.get("Name") == binding["volume"]
                and m.get("Destination") == SOURCE_ROOT
                and m.get("RW") is source_writable
                for m in mounts
            )
            or set(host["Tmpfs"]) != {"/handoff"}
            or not {"noexec", "nosuid", "mode=0700", "uid=10001", "gid=10001"}.issubset(
                set(host["Tmpfs"]["/handoff"].split(","))
            )
            or any(m.get("Destination") not in {SOURCE_ROOT, "/handoff"} for m in mounts)
        ):
            raise ValueError
        blocked = {
            *SLOTS,
            *(n + "_FILE" for n in SLOTS),
            "LOCAL_MODEL_ENABLED",
            "WATER_AGENT_LLM_BASE_URL",
            "WATER_AGENT_LLM_MODEL",
            "WATER_AGENT_EMBEDDING_BASE_URL",
            "WATER_AGENT_EMBEDDING_MODEL",
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
        }
        for item in config.get("Env", []):
            key, _, value = item.partition("=")
            if value and (key in blocked or key.endswith(("_API_KEY", "_API_KEY_FILE"))):
                raise ValueError
    except (KeyError, TypeError, ValueError):
        raise HandoffError("HANDOFF_UNSAFE_CONTAINER") from None


def cleanup_source(docker: DockerTransport, name: str, nonce: str) -> None:
    """A mandatory daemon query distinguishes absent from unavailable."""
    args = ["ps", "--all", "--filter", f"label={LABEL}={nonce}", "--format", "{{.Names}}"]
    names = docker.call(args).decode().splitlines()
    if name not in names:
        return
    info = json.loads(docker.call(["inspect", name]))[0]
    if info["Config"]["Labels"].get(LABEL) != nonce:
        raise HandoffError("HANDOFF_CLEANUP_FAILED")
    docker.call(["rm", "--force", "--volumes", name])
    if name in docker.call(args).decode().splitlines():
        raise HandoffError("HANDOFF_CLEANUP_FAILED")


def fetch_payload(
    binding: dict[str, Any], staging: Path, docker: DockerTransport
) -> dict[str, Any]:
    nonce = uuid4().hex
    name = "wa-credential-handoff-" + nonce
    attempted = False
    try:
        if (
            docker.call(["volume", "inspect", "--format", "{{.Name}}", binding["volume"]])
            .decode()
            .strip()
            != binding["volume"]
        ):
            raise HandoffError("HANDOFF_SOURCE_UNAVAILABLE")
        attempted = True
        docker.call(source_command(binding, name, nonce))
        info = json.loads(docker.call(["inspect", name]))[0]
        verify_container(info, binding, nonce)
        docker.call(["start", name])
        deadline = time.monotonic() + 20
        status = staging / "status.json"
        while time.monotonic() < deadline:
            if docker.copy_to_file(name, "status.json", status, optional=True):
                break
            time.sleep(0.05)
        if not status.exists():
            raise HandoffError("HANDOFF_SOURCE_TIMEOUT")
        raw_status = private_read(status)
        if raw_status is None:
            raise HandoffError("HANDOFF_SOURCE_UNAVAILABLE")
        ready = json.loads(raw_status)
        if ready != {"ok": True}:
            code = ready.get("code") if isinstance(ready, dict) else None
            if code == "HANDOFF_SOURCE_MISSING":
                raise HandoffError(code)
            raise HandoffError("HANDOFF_SOURCE_UNAVAILABLE")
        target = staging / "payload.json"
        docker.copy_to_file(name, "payload.json", target)
        raw_payload = private_read(target)
        payload = json.loads(raw_payload) if raw_payload is not None else None
        if not isinstance(payload, dict):
            raise HandoffError("HANDOFF_INVALID_PAYLOAD")
        return payload
    except HandoffError:
        raise
    except BaseException:  # noqa: BLE001 -- fixed public error only, including cancellation
        raise HandoffError("HANDOFF_SOURCE_UNAVAILABLE") from None
    finally:
        if attempted:
            try:
                cleanup_source(docker, name, nonce)
            except BaseException:  # noqa: BLE001 -- no success after uncertain source cleanup
                raise HandoffError("HANDOFF_CLEANUP_FAILED") from None


def execute(
    paths: DesktopPaths,
    binding_path: Path,
    socket_path: Path,
    *,
    transport_factory: Callable[[Path, Path], DockerTransport] | None = None,
    require_different: bool = False,
    compare_only: bool = False,
) -> dict[str, Any]:
    if TYPE_CHECKING:
        from .launcher import isolated_environment
    else:
        from launcher import isolated_environment

    from water_agent.generation.endpoints import resolve_chat_endpoint, resolve_embedding_endpoint

    binding = read_binding(binding_path)
    lock = paths.root / "instance.lock"
    private_read(lock, missing_ok=True)
    with InstanceLock(lock):
        check_store(paths.credentials / "models")
        panel = paths.credentials / "models/llm_endpoint.json"
        private_read(panel, missing_ok=True)
        env = isolated_environment(paths, paths.root / "temporary")
        os.environ.clear()
        os.environ.update(env)
        # A non-secret projection of the post-import slots, used for endpoint
        # selection only. It is never passed to a provider or written as a key.
        projection = lambda n: "handoff-projection" if n in SLOTS else env.get(n)
        chat, embedding = (
            resolve_chat_endpoint(getenv=projection),
            resolve_embedding_endpoint(getenv=projection),
        )
        for kind, endpoint, slot in (("chat", chat, SLOTS[0]), ("embedding", embedding, SLOTS[1])):
            if {"base_url": endpoint.base_url, "model": endpoint.model} != binding[
                kind
            ] or endpoint.api_key_name != slot:
                raise HandoffError("HANDOFF_BINDING_MISMATCH")
        stage = Path(tempfile.mkdtemp(prefix=".handoff-", dir=paths.credentials))
        try:
            configuration = private_directory(stage / "docker-config")
            transport = (transport_factory or DockerTransport)(socket_path, configuration)
            payload = fetch_payload(binding, stage, transport)
        finally:
            try:
                shutil.rmtree(stage)
            except BaseException:  # noqa: BLE001 -- protected plaintext staging must not be silently left
                raise HandoffError("HANDOFF_CLEANUP_FAILED") from None
        if require_different or compare_only:
            return install_payload(
                payload, binding, require_different=require_different, compare_only=compare_only
            )
        return install_payload(payload, binding)


class SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise HandoffError("HANDOFF_ARGUMENTS_INVALID")


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    parser = SafeParser(description="Local credential handoff; no authentication or rotation")
    for flag in ("resources", "data-dir", "source-binding", "socket"):
        parser.add_argument("--" + flag, required=True, type=Path)
    parser.add_argument("--require-different", action="store_true")
    parser.add_argument("--compare-only", action="store_true")
    try:
        args = parser.parse_args(argv)
        paths = DesktopPaths.create(args.resources, args.data_dir)
        sys.path.insert(0, str(paths.resources / "backend/src"))
        result = execute(
            paths, args.source_binding, args.socket,
            require_different=args.require_different, compare_only=args.compare_only
        )
        print(json.dumps(result))
        return 0
    except BaseException as exc:  # noqa: BLE001 -- CLI never emits submitted input or dependency tracebacks
        code = str(exc) if isinstance(exc, HandoffError) else "HANDOFF_FAILED"
        if isinstance(exc, SystemExit) and exc.code == 0:
            return 0
        if isinstance(exc, RuntimeError) and str(exc) == "DESKTOP_ALREADY_RUNNING":
            code = "HANDOFF_APP_RUNNING"
        print(
            json.dumps(
                {
                    "ok": False,
                    "status": "skipped" if code == "HANDOFF_SOURCE_MISSING" else "blocked",
                    "code": code,
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
