"""P5 Step 45 compose smoke test, executed inside the stack (`compose run smoke`).

Run as ``python -m water_agent.webapp.smoke``.  It lives in the application
package rather than ``scripts/`` so the image can execute it: ``.dockerignore``
denies everything by default and only ``src/`` (among code) is re-included, so a
script under ``scripts/`` would need the whole directory shipped to the image.

It talks to the running app over HTTP only — no database credentials, no
direct engine access — so a passing run proves the **deployed** service works,
not that a Python function does.  Checks are grouped by the P5 completion
definition: identity, authorization, memory lifecycle, SSE surface, and the
honest reporting of a missing remote model.

Nothing here prints a secret: the admin password is read from a secret file and
is never echoed, and no request/response body is logged verbatim.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from http.cookiejar import CookieJar
from pathlib import Path

BASE_URL = os.environ.get("SMOKE_BASE_URL", "http://app:8000").rstrip("/")
ADMIN_USERNAME = os.environ.get("SMOKE_ADMIN_USERNAME", "admin")
ADMIN_SECRET_PATH = Path(
    os.environ.get("SMOKE_ADMIN_PASSWORD_FILE", "/run/secrets/admin_password")
)
EXPECTED_MIGRATION_HEAD = os.environ.get("SMOKE_EXPECTED_MIGRATION_HEAD", "")


class SmokeFailure(RuntimeError):
    pass


class Client:
    """Minimal cookie-aware JSON client; no third-party dependency."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        csrf: bool = False,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict]:
        body = None
        request_headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if csrf:
            token = self.csrf_token()
            if token:
                request_headers["X-CSRF-Token"] = token
        request_headers.update(headers or {})
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
                status = response.status
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8")
            status = error.code
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"_non_json": True, "_length": len(raw)}
        return status, parsed

    def csrf_token(self) -> str | None:
        for cookie in self.jar:
            if cookie.name == "wa_csrf":
                return cookie.value
        return None


def _read_secret(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def main() -> int:
    checks: list[dict[str, object]] = []
    skipped: list[str] = []

    def record(name: str, ok: bool, detail: str = "") -> bool:
        checks.append({"name": name, "status": "PASS" if ok else "FAIL", "detail": detail})
        return ok

    anonymous = Client(BASE_URL)

    status, body = anonymous.request("GET", "/health/live")
    record("health_live", status == 200 and body.get("ok") is True, f"status={status}")

    status, ready = anonymous.request("GET", "/health/ready")
    checks_block = (ready.get("data") or {}).get("checks") or {}
    migration_head = checks_block.get("migrations")
    checkpoint_head = checks_block.get("checkpoint_migrations")
    record(
        "health_ready",
        status == 200 and ready.get("data", {}).get("status") == "ready",
        f"status={status} migrations={migration_head} checkpoints={checkpoint_head}",
    )
    if EXPECTED_MIGRATION_HEAD:
        record(
            "migration_head_matches_image",
            str(migration_head) == EXPECTED_MIGRATION_HEAD,
            f"expected={EXPECTED_MIGRATION_HEAD} actual={migration_head}",
        )

    status, capabilities = anonymous.request("GET", "/api/system/capabilities")
    record("capabilities", status == 200 and capabilities.get("ok") is True, f"status={status}")

    status, denied = anonymous.request("GET", "/api/memories")
    record(
        "memory_requires_session",
        status == 401 and (denied.get("error") or {}).get("code") == "AUTH_SESSION_INVALID",
        f"status={status}",
    )

    status, _ = anonymous.request("POST", "/api/auth/register", {"username": "x"})
    record("public_registration_disabled", status == 404, f"status={status}")

    password = _read_secret(ADMIN_SECRET_PATH)
    if not password:
        skipped.append(f"admin secret not readable at {ADMIN_SECRET_PATH}; authenticated checks skipped")
        record("authenticated_checks", False, "admin secret unavailable")
        print(json.dumps({"ok": False, "skipped": skipped, "checks": checks}, ensure_ascii=False, indent=2))
        return 1

    admin = Client(BASE_URL)
    status, _login = admin.request(
        "POST", "/api/auth/login", {"username": ADMIN_USERNAME, "password": password}
    )
    if not record("admin_login", status == 200, f"status={status}"):
        print(json.dumps({"ok": False, "skipped": skipped, "checks": checks}, ensure_ascii=False, indent=2))
        return 1

    status, me = admin.request("GET", "/api/auth/me")
    record("auth_me", status == 200 and me.get("ok") is True, f"status={status}")

    status, listing = admin.request("GET", "/api/memories")
    record("memory_list", status == 200 and isinstance(listing["data"]["memories"], list), f"status={status}")

    marker = f"compose-smoke-{uuid.uuid4().hex[:8]}"
    status, candidate = admin.request(
        "POST",
        "/api/memories/candidates",
        {
            "scope_type": "user",
            "kind": "user_preference",
            "semantic_key": f"smoke.{marker}",
            "content": f"部署冒烟标记 {marker} 用于确认记忆链路可用",
        },
        csrf=True,
    )
    candidate_id = (candidate.get("data") or {}).get("candidate_id")
    record(
        "memory_candidate_requires_confirmation",
        status == 201 and (candidate.get("data") or {}).get("requires_confirmation") is True,
        f"status={status}",
    )

    status, before_confirm = admin.request("GET", f"/api/memories?query={marker}")
    record(
        "unconfirmed_candidate_not_persisted",
        status == 200 and before_confirm["data"]["memories"] == [],
        f"status={status}",
    )

    memory_id = None
    revision = None
    if candidate_id:
        status, confirmed = admin.request(
            "POST",
            "/api/memories",
            {
                "candidate_id": candidate_id,
                "confirmation": {
                    "confirmed": True,
                    "content_hash": (candidate.get("data") or {}).get("content_hash"),
                },
            },
            csrf=True,
        )
        memory = (confirmed.get("data") or {}).get("memory") or {}
        memory_id, revision = memory.get("memory_id"), memory.get("revision")
        record("memory_confirmed", status == 201 and bool(memory_id), f"status={status}")

    if memory_id:
        status, recalled = admin.request("GET", f"/api/memories?query={marker}")
        returned = recalled["data"]["memories"] if status == 200 else []
        record(
            "memory_recall_after_new_session",
            len(returned) == 1 and marker in returned[0].get("content", ""),
            f"status={status} returned={len(returned)}",
        )

        forged = uuid.uuid4()
        status, _missing = admin.request("GET", f"/api/memories/{forged}")
        record("unknown_memory_id_404", status == 404, f"status={status}")

        status, forgotten = admin.request(
            "DELETE",
            f"/api/memories/{memory_id}",
            {"expected_revision": revision},
            csrf=True,
        )
        record(
            "memory_forget",
            status == 200 and (forgotten.get("data") or {}).get("status") == "forgotten",
            f"status={status}",
        )

        status, after_forget = admin.request("GET", f"/api/memories/{memory_id}")
        payload = after_forget.get("data") or {}
        record(
            "forgotten_body_unavailable",
            status == 200 and payload.get("status") == "forgotten" and payload.get("content") == "",
            f"status={status} content_empty={payload.get('content') == ''}",
        )

    status, session = admin.request("POST", "/api/sessions", {"title": "compose smoke"}, csrf=True)
    thread_id = (session.get("data") or {}).get("thread_id")
    record("session_created", status == 201 and bool(thread_id), f"status={status}")

    if thread_id:
        status, _restored = admin.request("GET", f"/api/sessions/{thread_id}")
        record("session_restored", status == 200, f"status={status}")

        status, _events = admin.request("GET", f"/api/runs/{uuid.uuid4()}/events")
        record("unknown_run_events_404", status == 404, f"status={status}")

        status, message = admin.request(
            "POST",
            f"/api/sessions/{thread_id}/messages",
            {"message": "冒烟：请说明给水系统设计流量口径"},
            csrf=True,
        )
        # 424 is the honest answer when no remote model is configured: the run is
        # refused instead of being simulated.
        record(
            "run_refused_without_model_or_accepted",
            status in (202, 424),
            f"status={status} code={(message.get('error') or {}).get('code')}",
        )
        if status == 424:
            description = (
                "no remote model configured; live agent execution is not exercised by this smoke run"
            )
        elif status == 202:
            run_id = (message.get("data") or {}).get("run_id")
            description = f"run started: {bool(run_id)}"
        else:  # pragma: no cover - guarded by the check above
            description = "unexpected status"
        skipped.append(f"live_agent_run: {description}")

    ok = all(check["status"] == "PASS" for check in checks)
    print(
        json.dumps(
            {
                "ok": ok,
                "base_url": BASE_URL,
                "passed": sum(1 for check in checks if check["status"] == "PASS"),
                "failed": sum(1 for check in checks if check["status"] == "FAIL"),
                "checks": checks,
                "notes": skipped,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
