"""Explicit local Docker acceptance using new synthetic volumes and profiles only."""

from __future__ import annotations

import json
import os
import secrets
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from desktop.macos.runtime import credential_handoff as handoff
from desktop.macos.runtime.paths import DesktopPaths, private_directory, private_write
from water_agent.config import credentials

CANARY_LABEL = "local.wateragent.canary"
SEED_PROGRAM = """import os,json,sys
from pathlib import Path
p=Path('/seed');data=json.loads(sys.stdin.read())
for name,value in data.items():
 fd=os.open(p/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(fd,'w') as stream:stream.write(value)
 os.chown(p/name,10001,10001)
os.chmod(p,0o700);os.chown(p,10001,10001)
"""
PUBLIC_PROBE = """import os,errno,json
result={"stdout_null":os.readlink("/proc/1/fd/1")=="/dev/null",
        "stderr_null":os.readlink("/proc/1/fd/2")=="/dev/null"}
try:
 fd=os.open("/var/lib/water-agent-credentials/write-probe",os.O_WRONLY|os.O_CREAT,0o600)
 os.close(fd);result["read_only_denied"]=False
except OSError as exc:result["read_only_denied"]=exc.errno==errno.EROFS
print(json.dumps(result))
"""


def run_canary(resources: Path, image: str, socket: Path, report_path: Path) -> dict[str, Any]:
    root = report_path.parent.resolve()
    regular = (Path.home() / "Library/Application Support/WaterAgent").resolve()
    if (
        root == Path.home()
        or root == regular
        or root.is_relative_to(regular)
        or root.is_relative_to(resources.resolve())
        or root.exists()
        and any(root.iterdir())
    ):
        raise handoff.HandoffError("HANDOFF_UNSAFE_FILE")
    private_directory(root)
    docker = handoff.DockerTransport(socket, private_directory(root / "docker-config"))
    nonce = uuid4().hex
    volume = "wa-a1-canary-" + nonce
    binding = handoff.validate_binding(
        {
            "version": 1,
            "volume": volume,
            "image": image,
            "chat": {
                "base_url": "https://model-endpoint.invalid/v1",
                "model": "deepseek-v4.1-flash",
            },
            "embedding": {
                "base_url": "https://model-endpoint.invalid/v1",
                "model": "text-embedding-3-large",
            },
        }
    )
    values = {name: "A1Synthetic_" + secrets.token_urlsafe(24) for name in handoff.SLOTS}
    checks: dict[str, bool] = {}
    observed: list[dict[str, Any]] = []
    commands: list[list[str]] = []
    source_owners: dict[str, str] = {}

    class ObservedTransport(handoff.DockerTransport):
        """Records public metadata without changing production execution."""

        def call(self, args: list[str], **kwargs: Any) -> bytes:
            commands.append(args)
            if args[0] == "create":
                created_name = args[args.index("--name") + 1]
                created_nonce = args[args.index("--label") + 1].split("=", 1)[1]
                source_owners[created_name] = created_nonce
            raw = super().call(args, **kwargs)
            if args[0] == "inspect":
                info = json.loads(raw)[0]
                observed.append(
                    {
                        "log_driver": info["HostConfig"]["LogConfig"]["Type"],
                        "network": info["HostConfig"]["NetworkMode"],
                        "read_only": info["HostConfig"]["ReadonlyRootfs"],
                        "source_rw": next(
                            m["RW"] for m in info["Mounts"] if m.get("Name") == volume
                        ),
                        "entrypoint": info["Config"]["Entrypoint"],
                        "uid": info["Config"]["User"],
                    }
                )
            return raw

        def copy_to_file(self, name: str, remote: str, destination: Path, **kwargs: Any) -> bool:
            commands.append(["private-copy", name, remote, str(destination)])
            copied = super().copy_to_file(name, remote, destination, **kwargs)
            if copied and remote == "payload.json":
                # This probe reads FD link names and tests a non-secret file
                # creation. Its stdout contains booleans, never payload bytes.
                result = subprocess.run(
                    self.prefix
                    + ["exec", name, handoff.SOURCE_PYTHON, "-I", "-S", "-B", "-c", PUBLIC_PROBE],
                    env=self.env,
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                if result.returncode:
                    raise handoff.HandoffError("HANDOFF_SOURCE_UNAVAILABLE")
                probe = json.loads(result.stdout)
                if set(probe) != {"stdout_null", "stderr_null", "read_only_denied"}:
                    raise handoff.HandoffError("HANDOFF_SOURCE_UNAVAILABLE")
                checks.update(probe)
            return copied

    volume_attempted = False
    seed_attempted = False
    seed_name = "wa-a1-canary-seed-" + nonce
    status: dict[str, Any] = {"ok": False, "real_credentials_used": False, "model_requests": 0}
    try:
        volume_attempted = True
        docker.call(["volume", "create", "--label", CANARY_LABEL + "=" + nonce, volume])
        labels = json.loads(
            docker.call(["volume", "inspect", "--format", "{{json .Labels}}", volume])
        )
        if labels.get(CANARY_LABEL) != nonce:
            raise handoff.HandoffError("HANDOFF_CLEANUP_FAILED")
        seed = {credentials.CREDENTIALS[name]: value for name, value in values.items()}
        seed["llm_endpoint.json"] = json.dumps(binding["chat"])
        args = [
            "run",
            "--rm",
            "--name",
            seed_name,
            "--label",
            CANARY_LABEL + "=" + nonce,
            "--label",
            handoff.LABEL + "=" + nonce,
            "--pull",
            "never",
            "-i",
            "--user",
            "0:0",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "CHOWN",
            "--log-driver",
            "none",
            "--entrypoint",
            handoff.SOURCE_PYTHON,
            "--mount",
            "type=volume,source=" + volume + ",target=/seed",
            image,
            "-I",
            "-S",
            "-B",
            "-c",
            SEED_PROGRAM,
        ]
        seed_attempted = True
        commands.append(args)
        initialized = subprocess.run(
            docker.prefix + args,
            input=json.dumps(seed).encode(),
            env=docker.env,
            capture_output=True,
            timeout=20,
            check=False,
        )
        if initialized.returncode:
            raise handoff.HandoffError("HANDOFF_SOURCE_UNAVAILABLE")
        checks["seed_stdout_empty"] = not initialized.stdout
        checks["seed_stderr_empty"] = not initialized.stderr
        paths = DesktopPaths.create(resources, root / "profile")
        private_write(paths.credentials / "models/llm_endpoint.json", json.dumps(binding["chat"]))
        private_write(
            paths.root / "config/embedding.json",
            json.dumps({**binding["embedding"], "retrieval_mode": "hybrid"}),
        )
        binding_path = root / "binding.json"
        private_write(binding_path, json.dumps(binding))
        result = handoff.execute(paths, binding_path, socket, transport_factory=ObservedTransport)
        checks["installed_values_match"] = all(
            credentials.resolve_secret(name, getenv=lambda _: None) == values[name]
            for name in handoff.SLOTS
        )
        checks["file_modes_600"] = all(
            stat.S_IMODE(
                (paths.credentials / "models" / credentials.CREDENTIALS[name]).stat().st_mode
            )
            == 0o600
            for name in handoff.SLOTS
        )
        checks["no_plaintext_in_argv"] = all(
            value not in repr(commands) for value in values.values()
        )
        checks["staging_removed"] = not list(paths.credentials.glob(".handoff-*"))
        checks["source_container_removed"] = not docker.call(
            ["ps", "--all", "--filter", "volume=" + volume, "--format", "{{.Names}}"]
        ).strip()
        status.update(ok=all(checks.values()), result=result)
    except BaseException as exc:  # noqa: BLE001 -- never print synthetic or real secret-bearing exceptions
        status["code"] = (
            str(exc) if isinstance(exc, handoff.HandoffError) else "CANARY_COMPONENT_FAILED"
        )
    finally:
        cleanup_ok = True
        for source_name, source_nonce in source_owners.items():
            try:
                handoff.cleanup_source(docker, source_name, source_nonce)
            except BaseException:  # noqa: BLE001 -- only owned-resource cleanup is retried
                cleanup_ok = False
        if seed_attempted:
            try:
                handoff.cleanup_source(docker, seed_name, nonce)
            except BaseException:  # noqa: BLE001 -- CLI timeout may leave the seed process alive
                cleanup_ok = False
        if volume_attempted:
            try:
                owned = docker.call(
                    [
                        "volume",
                        "ls",
                        "--filter",
                        "label=" + CANARY_LABEL + "=" + nonce,
                        "--format",
                        "{{.Name}}",
                    ]
                )
                if volume in owned.decode().splitlines():
                    labels = json.loads(
                        docker.call(["volume", "inspect", "--format", "{{json .Labels}}", volume])
                    )
                    if labels.get(CANARY_LABEL) != nonce:
                        raise handoff.HandoffError("HANDOFF_CLEANUP_FAILED")
                    docker.call(["volume", "rm", volume])
                status["canary_volume_removed"] = True
            except BaseException:  # noqa: BLE001 -- no success after uncertain fixture cleanup
                status["canary_volume_removed"] = False
                cleanup_ok = False
        status["canary_cleanup_ok"] = cleanup_ok
        if not cleanup_ok:
            status["ok"] = False
        status.update(checks=checks, actual_container_configuration=observed)
        report_path.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n")
    return status


def main() -> int:
    os.umask(0o077)
    parser = handoff.SafeParser(
        description="Synthetic Docker credential handoff acceptance; no production volume input"
    )
    for flag in ("resources", "socket", "out"):
        parser.add_argument("--" + flag, required=True, type=Path)
    parser.add_argument("--image", required=True)
    try:
        args = parser.parse_args()
        result = run_canary(args.resources, args.image, args.socket, args.out)
        print(json.dumps({"ok": result["ok"], "real_credentials_used": False, "model_requests": 0}))
        return 0 if result["ok"] else 1
    except BaseException as exc:  # noqa: BLE001 -- the canary CLI has the same no-echo boundary
        if isinstance(exc, SystemExit) and exc.code == 0:
            return 0
        print(
            json.dumps(
                {
                    "ok": False,
                    "code": str(exc)
                    if isinstance(exc, handoff.HandoffError)
                    else "CANARY_COMPONENT_FAILED",
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
