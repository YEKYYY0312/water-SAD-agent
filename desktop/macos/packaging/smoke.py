"""Acceptance controller for a built App, using disposable data and real PG.

This controller never targets the user's regular desktop profile or 8000/8001.
It does not configure a model Key or turn mechanism smoke into real LLM evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit


def require_disposable_root(path: Path) -> Path:
    regular = (Path.home() / "Library/Application Support/WaterAgent").resolve()
    resolved = path.resolve()
    if path.is_symlink() or resolved == Path.home().resolve():
        raise ValueError("smoke requires a dedicated disposable directory")
    if resolved == regular or regular in resolved.parents:
        raise ValueError("smoke cannot use the regular desktop profile")
    if resolved.exists() and (not resolved.is_dir() or any(resolved.iterdir())):
        raise ValueError("smoke data directory must be empty")
    return resolved


def safe_ready_url(value: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme != "http" or parts.hostname != "127.0.0.1"
        or parts.username is not None or parts.password is not None
        or parts.path or parts.query or parts.fragment
        or parts.port is None or not 1024 <= parts.port <= 65535
        or parts.port in {8000, 8001}
    ):
        raise ValueError("smoke only accepts its own random loopback endpoint")
    return value


class AppRuntime:
    def __init__(self, resources: Path, data: Path, *, restore: Path | None = None):
        self.resources, self.data = resources, data
        self.events: queue.Queue[dict] = queue.Queue()
        self.observed: list[dict] = []
        self.log = (data.parent / f"{data.name}-supervisor.stderr").open("wb")
        args = [str(resources / "python/bin/python3.12"), "-I", "-B",
                str(resources / "runtime/launcher.py"), "--resources", str(resources),
                "--data-dir", str(data)]
        if restore:
            args += ["--restore", str(restore)]
        env = {name: os.environ[name] for name in ("HOME", "TMPDIR", "USER") if name in os.environ}
        env.update(PATH="/usr/bin:/bin:/usr/sbin:/sbin", LANG="en_US.UTF-8")
        self.process = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
            text=True, encoding="utf-8", env=env, cwd=data.parent,
        )
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                self.events.put({"event": "error", "code": "NON_JSON_CONTROL_OUTPUT"})
                continue
            self.observed.append(event)
            self.events.put(event)

    def send(self, command: dict) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(command) + "\n")
        self.process.stdin.flush()

    def wait(self, wanted: str, *, timeout: float = 120) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                event = self.events.get(timeout=min(1, max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                if self.process.poll() is not None:
                    raise RuntimeError(f"supervisor exited before {wanted}")
                continue
            if event.get("event") == "error":
                raise RuntimeError(f"supervisor failure: {event.get('code', 'unknown')}")
            if event.get("event") == wanted:
                return event
        raise TimeoutError(f"supervisor did not emit {wanted}")

    def close(self, *, eof: bool = False) -> int:
        if self.process.poll() is None:
            if not eof:
                self.send({"command": "stop"})
            assert self.process.stdin is not None
            self.process.stdin.close()
        try:
            return self.process.wait(timeout=45)
        finally:
            self.log.close()


def run(app: Path, data_root: Path, output: Path) -> dict:
    """Exercise HTTP/storage over a real packaged runtime; no paid API calls."""
    from water_agent.webapp.smoke import Client, _drawing_roundtrip

    resources = app.resolve() / "Contents/Resources"
    root = require_disposable_root(data_root)
    root.parent.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    active: AppRuntime | None = None

    def record(name: str, ok: bool, **detail: object) -> None:
        results.append({"name": name, "ok": bool(ok), **detail})
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"checks": results}, ensure_ascii=False, indent=2))
        if not ok:
            raise RuntimeError(f"desktop acceptance failed: {name}")

    def ready(runtime: AppRuntime) -> str:
        return safe_ready_url(runtime.wait("ready")["url"])

    def sign_in(url: str, profile: Path) -> Client:
        from http.cookiejar import Cookie
        client = Client(url)
        secret = profile / "credentials/desktop-session.json"
        data = json.loads(secret.read_text())
        secret.unlink()
        for name, field in [("wa_session", "session_id"), ("wa_csrf", "csrf_token")]:
            client.jar.set_cookie(Cookie(
                version=0, name=name, value=data[field], port=None, port_specified=False,
                domain="127.0.0.1", domain_specified=False, domain_initial_dot=False,
                path="/", path_specified=True, secure=False, expires=int(data["expires_at"]),
                discard=False, comment=None, comment_url=None,
                rest={"HttpOnly": None} if name == "wa_session" else {}, rfc2109=False,
            ))
        code, _ = client.request("GET", "/api/auth/me")
        record("personal_session_without_password", code == 200, status=code)
        return client

    try:
        active = AppRuntime(resources, root)
        url = ready(active)
        client = Client(url)
        code, payload = client.request("GET", "/health/ready")
        record("real_postgres_and_migrations_ready", code == 200 and payload.get("data", {}).get("status") == "ready", checks=payload.get("data", {}).get("checks"))
        code, _ = client.request("GET", "/api/memories")
        record("anonymous_access_denied", code == 401, status=code)
        client = sign_in(url, root)
        code, payload = client.request("POST", "/api/sessions", {"title": "Mac 独立包持久化验收"}, csrf=True)
        thread = payload.get("data", {}).get("thread_id")
        record("session_created", code == 201 and bool(thread), status=code)
        code, payload = client.upload("/api/uploads", "桌面验收 中文.txt", "本项目采用闭式热水循环。".encode())
        upload = payload.get("data", {}).get("upload_id")
        record("chinese_filename_upload", code == 201 and bool(upload), status=code)
        code, _ = client.request("POST", f"/api/sessions/{thread}/messages", {"message": "请说明给水设计流量"}, csrf=True)
        record("no_inherited_model_key_or_fake_answer", code == 424, status=code)
        drawing_ok, detail = _drawing_roundtrip(client)
        record("drawing_generation_and_download", drawing_ok, detail=detail)

        env = {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
               "LANG": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
               "PYTHONPATH": str(resources / "backend/src"),
               "WATER_AGENT_CREDENTIALS_DIR": str(root / "credentials/models"),
               "WATER_AGENT_TRACE_DIR": str(root / "traces")}
        index_files = list((resources / "backend/data/indexes").rglob("*.db"))
        record("one_explicit_packaged_index", len(index_files) == 1)
        probe = subprocess.run([
            str(resources / "python/bin/python3.12"), "-B", "-m", "water_agent.cli",
            "给水管道的设计秒流量如何确定", "--mode", "agentic", "--mock-generator", "--json",
            "--db-path", str(index_files[0]), "--session-dir", str(root / "sessions-smoke")],
            capture_output=True, text=True, env=env, cwd=root / "work", timeout=180, check=False)
        (root.parent / "packaged-agentic-probe.json").write_text(probe.stdout)
        (root.parent / "packaged-agentic-probe.stderr").write_text(probe.stderr)
        payload = json.loads(probe.stdout)
        runtime_status = payload.get("retriever_runtime_status", {})
        record("packaged_real_index_and_citations_with_scripted_model",
               probe.returncode == 0 and payload.get("status") == "succeeded"
               and runtime_status.get("is_fallback") is False
               and runtime_status.get("text_index_ready") is True and bool(payload.get("citations")))
        code, _ = Client(url).request("POST", "/api/desktop/session", {})
        record("no_network_auto_login_endpoint", code == 404)

        backup = root.parent / (root.name + ".backup.tar")
        active.send({"command": "backup", "destination": str(backup)})
        active.wait("backup_complete")
        record("backup_created", backup.is_file() and backup.stat().st_size > 0)
        record("graceful_shutdown", active.close() == 0)
        active = None
        record("postgres_stopped", not (root / "postgres/postmaster.pid").exists())

        active = AppRuntime(resources, root)
        client = sign_in(ready(active), root)
        code, payload = client.request("GET", f"/api/sessions/{thread}")
        record("session_survived_restart", code == 200 and payload.get("data", {}).get("title") == "Mac 独立包持久化验收")
        code, _ = client.request("GET", f"/api/uploads/{upload}")
        record("upload_survived_restart", code == 200)
        record("stdin_eof_shutdown", active.close(eof=True) == 0)
        active = None
        record("postgres_stopped_after_eof", not (root / "postgres/postmaster.pid").exists())

        restored = require_disposable_root(root.parent / (root.name + " 已恢复"))
        active = AppRuntime(resources, restored, restore=backup)
        client = sign_in(ready(active), restored)
        code, _ = client.request("GET", f"/api/sessions/{thread}")
        record("backup_restore_session", code == 200)
        code, _ = client.request("GET", f"/api/uploads/{upload}")
        record("backup_restore_upload", code == 200)
        record("restored_runtime_shutdown", active.close() == 0)
        active = None
    finally:
        if active is not None:
            try:
                active.close(eof=True)
            except (OSError, TimeoutError, subprocess.TimeoutExpired):
                # Do not kill arbitrary processes; caller gets the failed evidence.
                record("cleanup_failed", False)
    report = {"ok": True, "app": str(app.resolve()), "checks": results,
              "passed": len(results), "real_llm_quality": "SKIP: no API credentials configured",
              "production_ports_touched": False}
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = run(args.app, args.data_root, args.out)
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": report["ok"], "passed": report["passed"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
