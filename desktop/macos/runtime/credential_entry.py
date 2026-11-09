"""Local hidden entry into a new labelled source volume. Never writes App slots."""
from __future__ import annotations

import getpass
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
if TYPE_CHECKING:
    from . import credential_handoff as handoff
    from .paths import DesktopPaths, InstanceLock, private_directory, private_write
else:
    try:
        from . import credential_handoff as handoff
        from .paths import DesktopPaths, InstanceLock, private_directory, private_write
    except ImportError:
        import credential_handoff as handoff
        from paths import DesktopPaths, InstanceLock, private_directory, private_write


def entry_command(binding: dict[str, Any], name: str, nonce: str) -> list[str]:
    args = handoff.source_command(binding, name, nonce)
    args.insert(1, "--interactive")
    mount = args.index("--mount") + 1
    args[mount] = f"type=volume,source={binding['volume']},target={handoff.SOURCE_ROOT}"
    args[-2] = Path(__file__).with_name("credential_entry_worker.py").read_text()
    return args


def validate_session(data: Any) -> dict[str, Any]:
    try:
        if (
            not isinstance(data, dict)
            or set(data) != {"version", "nonce", "binding", "status", "resources", "socket"}
            or type(data["version"]) is not int or data["version"] != 1
            or not isinstance(data["nonce"], str)
            or not re.fullmatch(r"[a-f0-9]{32}", data["nonce"])
            or data["status"] != "created"
        ):
            raise ValueError
        binding = handoff.validate_binding(data["binding"])
        if binding["volume"] != "wa-a2-entry-" + data["nonce"]:
            raise ValueError
        for name in ("resources", "socket"):
            if not isinstance(data[name], str):
                raise TypeError
            path = Path(data[name])
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError
        return data
    except (ValueError, TypeError, KeyError):
        raise handoff.HandoffError("HANDOFF_ENTRY_FAILED") from None


def verify_entry(
    docker: handoff.DockerTransport, session: dict[str, Any], *, container_id: str | None = None
) -> str:
    session = validate_session(session)
    binding, nonce = session["binding"], session["nonce"]
    labels = json.loads(docker.call(["volume", "inspect", "--format", "{{json .Labels}}", binding["volume"]]))
    if labels.get(handoff.LABEL) != nonce:
        raise handoff.HandoffError("HANDOFF_UNSAFE_CONTAINER")
    info = json.loads(docker.call(["inspect", container_id or binding["volume"]]))[0]
    handoff.verify_container(info, binding, nonce, source_writable=True)
    args = entry_command(binding, binding["volume"], nonce)
    if (
        info["Config"].get("OpenStdin") is not True or info["Config"].get("Tty") is not False
        or info["Config"]["Cmd"] != args[args.index(binding["image"]) + 1:]
        or info["State"]["Status"] != "created"
        or not re.fullmatch(r"[a-f0-9]{64}", info.get("Id", ""))
        or container_id is not None and info["Id"] != container_id
    ):
        raise handoff.HandoffError("HANDOFF_UNSAFE_CONTAINER")
    return str(info["Id"])


def cleanup_entry(
    docker: handoff.DockerTransport, session: dict[str, Any], container_id: str, *, keep_volume: bool
) -> None:
    try:
        query = ["ps", "--all", "--no-trunc", "--filter", "id=" + container_id, "--format", "{{.ID}}"]
        if container_id in docker.call(query).decode().splitlines():
            docker.call(["rm", "--force", "--volumes", container_id])
        if container_id in docker.call(query).decode().splitlines():
            raise ValueError
        if not keep_volume:
            volume = session["binding"]["volume"]
            query_volume = ["volume", "ls", "--filter", "name=" + volume, "--format", "{{.Name}}"]
            if volume in docker.call(query_volume).decode().splitlines():
                labels = json.loads(docker.call(["volume", "inspect", "--format", "{{json .Labels}}", volume]))
                if labels.get(handoff.LABEL) != session["nonce"]:
                    raise ValueError
                docker.call(["volume", "rm", volume])
            if volume in docker.call(query_volume).decode().splitlines():
                raise ValueError
    except BaseException:  # noqa: BLE001 -- cleanup must not leak daemon or credential errors
        raise handoff.HandoffError("HANDOFF_CLEANUP_FAILED") from None


def submit_values(
    docker: handoff.DockerTransport, session: dict[str, Any], reader: Callable[[], dict[str, str]]
) -> None:
    container_id = verify_entry(docker, session)
    values: dict[str, str] = {}
    success = False
    try:
        values = reader()
        verify_entry(docker, session, container_id=container_id)
        # Immutable ID, checked again after human input. stdin is the only
        # secret stream; neither stdout nor stderr are captured or retained.
        result = subprocess.run(
            docker.prefix + ["start", "--attach", "--interactive", container_id],
            input=json.dumps(values).encode(), env=docker.env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False,
        )
        if result.returncode:
            raise handoff.HandoffError("HANDOFF_ENTRY_FAILED")
        success = True
    finally:
        values.clear()
        cleanup_entry(docker, session, container_id, keep_volume=success)


def read_values() -> dict[str, str]:
    from water_agent.config.credentials import validate_credential_value

    if not sys.stdin.isatty():
        raise handoff.HandoffError("HANDOFF_TTY_REQUIRED")
    print("仅填写 model-endpoint.invalid 新凭据；不会修改当前 App 或旧服务。")
    print("输入不会显示。不要使用官方 DeepSeek/百炼 Key。Ctrl-C 可取消。")
    if input("确认新凭据属于该中转端点，请输入 model-endpoint.invalid：").strip() != "model-endpoint.invalid":
        raise handoff.HandoffError("HANDOFF_BINDING_MISMATCH")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        chat = getpass.getpass("新对话 Key（隐藏输入）：")
        if chat != getpass.getpass("再次输入新对话 Key："):
            raise handoff.HandoffError("HANDOFF_ENTRY_FAILED")
        embedding = getpass.getpass("新 Embedding Key（同一把且有相应权限可直接回车）：")
        if embedding:
            if embedding != getpass.getpass("再次输入新 Embedding Key："):
                raise handoff.HandoffError("HANDOFF_ENTRY_FAILED")
        else:
            embedding = chat
        values = dict(zip(handoff.SLOTS, (chat, embedding), strict=True))
        for value in values.values():
            if validate_credential_value(value) != value:
                raise handoff.HandoffError("HANDOFF_ENTRY_FAILED")
        if len(json.dumps(values).encode()) > 65536:
            raise handoff.HandoffError("HANDOFF_ENTRY_FAILED")
        return values


def prepare(paths: DesktopPaths, directory: Path, image: str, socket: Path) -> Path:
    if TYPE_CHECKING:
        from .launcher import isolated_environment
    else:
        from launcher import isolated_environment

    from water_agent.generation.endpoints import resolve_chat_endpoint, resolve_embedding_endpoint

    if directory.exists() or directory.is_relative_to(paths.root) or paths.root.is_relative_to(directory):
        raise handoff.HandoffError("HANDOFF_UNSAFE_FILE")
    private_directory(directory)
    handoff.check_store(paths.credentials / "models")
    handoff.private_read(paths.credentials / "models/llm_endpoint.json")
    env = isolated_environment(paths, paths.root / "temporary")
    os.environ.clear(); os.environ.update(env)
    projection = lambda n: "entry-projection" if n in handoff.SLOTS else env.get(n)
    chat, embedding = resolve_chat_endpoint(getenv=projection), resolve_embedding_endpoint(getenv=projection)
    if chat.api_key_name != handoff.SLOTS[0] or embedding.api_key_name != handoff.SLOTS[1]:
        raise handoff.HandoffError("HANDOFF_BINDING_MISMATCH")
    nonce = uuid4().hex
    binding = handoff.validate_binding({
        "version": 1, "volume": "wa-a2-entry-" + nonce, "image": image,
        "chat": {"base_url": chat.base_url, "model": chat.model},
        "embedding": {"base_url": embedding.base_url, "model": embedding.model},
    })
    session = {"version": 1, "nonce": nonce, "binding": binding, "status": "created", "resources": str(paths.resources), "socket": str(socket)}
    docker = handoff.DockerTransport(socket, private_directory(directory / "docker-config"))
    docker.call(["volume", "create", "--label", handoff.LABEL + "=" + nonce, binding["volume"]])
    try:
        docker.call(entry_command(binding, binding["volume"], nonce))
        verify_entry(docker, session)
    except BaseException:
        handoff.cleanup_source(docker, binding["volume"], nonce)
        labels = json.loads(docker.call(["volume", "inspect", "--format", "{{json .Labels}}", binding["volume"]]))
        if labels.get(handoff.LABEL) == nonce:
            docker.call(["volume", "rm", binding["volume"]])
        raise
    private_write(directory / "session.json", json.dumps(session))
    private_write(directory / "source-binding.json", json.dumps(binding))
    command = directory / "填写新凭据.command"
    args = [str(paths.resources / "python/bin/python3.12"), "-I", "-B", str(paths.resources / "runtime/credential_entry.py"), "fill", "--directory", str(directory)]
    private_write(command, "#!/bin/sh\nexec /usr/bin/env -i HOME=" + shlex.quote(str(Path.home())) + " PATH=/usr/bin:/bin LANG=en_US.UTF-8 " + shlex.join(args) + "\n")
    command.chmod(0o700)
    return command


def fill(directory: Path) -> None:
    # Load only this executable's own bundle, before binding validation imports
    # the production URL/model validators. Never choose code from session data.
    current_resources = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(current_resources / "backend/src"))
    info = directory.lstat()
    if not directory.is_dir() or info.st_uid != os.geteuid() or (info.st_mode & 0o777) != 0o700:
        raise handoff.HandoffError("HANDOFF_UNSAFE_FILE")
    handoff.private_read(directory / "entry.lock", missing_ok=True)
    with InstanceLock(directory / "entry.lock"):
        raw = handoff.private_read(directory / "session.json")
        session = validate_session(json.loads(raw or b"null"))
        resources = Path(session["resources"])
        if Path(__file__).resolve().parent != (resources / "runtime").resolve():
            raise handoff.HandoffError("HANDOFF_BINDING_MISMATCH")
        sys.path.insert(0, str(resources / "backend/src"))
        docker = handoff.DockerTransport(Path(session["socket"]), private_directory(directory / "docker-config"))
        try:
            submit_values(docker, session, read_values)
        except BaseException:
            session["status"] = "blocked"
            private_write(directory / "session.json", json.dumps(session))
            raise
        session["status"] = "ready"
        private_write(directory / "session.json", json.dumps(session))
        print("新凭据已写入独立受保护源卷。App尚未更新。请返回Codex告知：填写完成。")


def main() -> int:
    os.umask(0o077); logging.disable(logging.CRITICAL)
    parser = handoff.SafeParser(description="Local hidden credential entry; never updates App")
    parser.add_argument("action", choices=("prepare", "fill"))
    parser.add_argument("--directory", type=Path, required=True)
    for name in ("resources", "data-dir", "socket"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--image")
    try:
        args = parser.parse_args()
        if args.action == "fill":
            fill(args.directory)
        else:
            if any(v is None for v in (args.resources, args.data_dir, args.socket, args.image)):
                raise handoff.HandoffError("HANDOFF_ARGUMENTS_INVALID")
            sys.path.insert(0, str(args.resources / "backend/src"))
            command = prepare(DesktopPaths(args.resources, args.data_dir), args.directory, args.image, args.socket)
            print(json.dumps({"ok": True, "command": str(command), "credentials_entered": False}))
        return 0
    except BaseException as exc:  # noqa: BLE001 -- never echo submitted secrets or tracebacks
        code = str(exc) if isinstance(exc, handoff.HandoffError) else "HANDOFF_ENTRY_FAILED"
        print(json.dumps({"ok": False, "code": code}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
