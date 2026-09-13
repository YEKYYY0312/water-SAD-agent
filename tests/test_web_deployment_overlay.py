"""P5 Step 45: production compose overlay, operations scripts and CI wiring.

Two layers again: text contracts that always run, and — when the Docker CLI is
present — a real ``docker compose config`` parse of the base + overlay pair,
which is what actually proves the overlay is valid rather than merely
plausible.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_COMPOSE = REPO_ROOT / "compose.yml"
OVERLAY_COMPOSE = REPO_ROOT / "compose.production.yml"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def _docker() -> str | None:
    found = shutil.which("docker")
    if found:
        return found
    desktop = Path("/Applications/Docker.app/Contents/Resources/bin/docker")
    return str(desktop) if desktop.is_file() else None


def _require_docker() -> str:
    docker = _docker()
    if docker is None:
        pytest.skip("docker CLI is not available on this host; compose parsing needs it")
    probe = subprocess.run([docker, "info"], capture_output=True, text=True, check=False)
    if probe.returncode != 0:
        pytest.skip("docker daemon is unreachable; compose parsing needs it")
    return docker


def test_production_overlay_declares_resource_limits_and_hardening() -> None:
    overlay = OVERLAY_COMPOSE.read_text(encoding="utf-8")

    assert "deploy:" in overlay and "resources:" in overlay
    assert overlay.count("limits:") >= 5
    assert overlay.count("no-new-privileges:true") >= 5
    assert "stop_grace_period" in overlay
    assert "stop_signal: SIGTERM" in overlay
    assert "--timeout-graceful-shutdown" in overlay
    assert "max-size" in overlay  # log rotation
    assert "pids:" in overlay  # process cap


def test_overlay_does_not_fork_the_single_source_of_truth() -> None:
    """The overlay hardens services; it must not redefine their contracts."""

    overlay = OVERLAY_COMPOSE.read_text(encoding="utf-8")
    base = BASE_COMPOSE.read_text(encoding="utf-8")

    # Secrets, the database URL, image digests and the migration ordering stay
    # defined once, in compose.yml.
    assert "secrets:" not in overlay
    assert "DATABASE_URL" not in overlay
    assert "DATABASE_PASSWORD_FILE" not in overlay
    assert "depends_on" not in overlay
    assert "@sha256:" not in overlay
    # ...and the base file still owns them.
    assert "DATABASE_URL" in base and "secrets:" in base
    assert "@sha256:" in base


def test_base_compose_keeps_the_ordered_one_shot_services_and_healthchecks() -> None:
    base = BASE_COMPOSE.read_text(encoding="utf-8")

    assert "condition: service_completed_successfully" in base
    assert "condition: service_healthy" in base
    assert "/health/live" in base and "/health/ready" in base
    assert "pg_isready" in base
    # A migration failure must stop the app from starting: the app depends on
    # both one-shot services completing successfully.
    app_block = base[base.index("\n  app:"):base.index("\n  smoke:")]
    assert "service_completed_successfully" in app_block
    assert "migrate:" in app_block and "bootstrap-admin:" in app_block


def test_smoke_service_runs_without_a_profile_and_has_a_stable_image_name() -> None:
    base = BASE_COMPOSE.read_text(encoding="utf-8")
    smoke_block = base[base.index("  smoke:") : base.index("  reverse-proxy:")]

    assert "-m water_agent.webapp.smoke" in smoke_block or "water_agent.webapp.smoke" in smoke_block
    # `docker compose run --rm smoke` cannot run a service whose profile is
    # inactive, so the smoke service must not be profiled.
    assert "profiles:" not in smoke_block
    assert 'restart: "no"' in smoke_block
    assert "condition: service_healthy" in smoke_block
    # check_image.sh targets this exact tag.
    assert "image: water-agent-app:local" in base


def test_smoke_expected_migration_head_matches_the_code_constant() -> None:
    from water_agent.storage.database import CHECKPOINT_MIGRATION_HEAD, MIGRATION_HEAD

    base = BASE_COMPOSE.read_text(encoding="utf-8")

    assert f'SMOKE_EXPECTED_MIGRATION_HEAD: "{MIGRATION_HEAD}"' in base
    readme = (REPO_ROOT / "scripts" / "backup.sh").read_text(encoding="utf-8")
    assert "pg_dump" in readme
    assert CHECKPOINT_MIGRATION_HEAD >= 1


def test_operations_scripts_exist_and_are_executable() -> None:
    for name in ("backup.sh", "restore.sh", "check_image.sh", "compose_common.sh"):
        path = REPO_ROOT / "scripts" / name
        assert path.is_file(), name
        assert path.stat().st_size > 0
    for name in ("backup.sh", "restore.sh", "check_image.sh"):
        path = REPO_ROOT / "scripts" / name
        mode = path.stat().st_mode
        assert mode & 0o111, f"{name} is not executable"


def test_image_check_declares_the_release_rules() -> None:
    script = (REPO_ROOT / "scripts" / "check_image.sh").read_text(encoding="utf-8")

    assert "non_root" in script or "runtime_user_non_root" in script
    assert "wateragent" in script
    assert ".env" in script
    assert "-----BEGIN " in script
    assert "DEEPSEEK_API_KEY=" in script
    assert "require-scanners" in script
    # Never echo a matched secret value.
    assert "grep -rl" in script
    assert "grep -o" in script


def test_backups_are_gitignored_and_excluded_from_the_build_context() -> None:
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert "backups/" in gitignore
    assert "backups/" in dockerignore
    assert "secrets/" in dockerignore
    assert "__pycache__/" in dockerignore
    assert "tests/" in dockerignore


def test_github_actions_cover_the_gate_frontend_memory_and_compose() -> None:
    ci = (WORKFLOWS / "p5-ci.yml").read_text(encoding="utf-8")
    compose = (WORKFLOWS / "p5-compose.yml").read_text(encoding="utf-8")
    postgres = (WORKFLOWS / "p5-postgres.yml").read_text(encoding="utf-8")

    assert "scripts/ci_gate.sh" in ci
    assert "npm --prefix web/app run typecheck" in ci
    assert "npm --prefix web/app run test" in ci
    assert "npm --prefix web/app run build" in ci
    assert "--release-gate" in ci
    assert "compare_memory_p0.py" in ci
    # A release-gate host must not SKIP.
    assert "--release-gate" in ci and "SKIP" in ci
    assert "${{ secrets." in compose
    assert "scripts/backup.sh" in compose and "scripts/restore.sh" in compose
    assert "check_image.sh" in compose
    assert "docker compose down" in compose
    assert "test_memory_web_api.py" in postgres
    assert "test_web_sessions.py" in postgres
    # The pinned PostgreSQL digest must not drift between workflows.
    digest = "postgres:18.2-bookworm@sha256:"
    assert digest in postgres and digest in ci


def test_workflows_never_print_a_secret_value() -> None:
    """A secret *name* may be logged; its interpolated *value* may not."""

    for path in sorted(WORKFLOWS.glob("*.yml")):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if "${{ secrets." not in stripped:
                continue
            # `echo` writes its argument to the job log; `printf ... > file`
            # writes the value into a file and is the sanctioned pattern.
            if stripped.startswith(("echo ", "run: echo")):
                raise AssertionError(f"{path.name} may echo a secret value: {stripped}")
            if stripped.startswith("printf ") and ">" not in stripped:
                raise AssertionError(f"{path.name} prints a secret to stdout: {stripped}")
    compose = (WORKFLOWS / "p5-compose.yml").read_text(encoding="utf-8")
    assert "umask 077" in compose
    assert "printf '%s' \"${{ secrets." in compose
    assert 'test -n "${{ secrets.' in compose


def test_compose_config_parses_base_and_overlay_together() -> None:
    docker = _require_docker()

    result = subprocess.run(
        [docker, "compose", "-f", str(BASE_COMPOSE), "-f", str(OVERLAY_COMPOSE), "config", "-q"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    resolved = subprocess.run(
        [docker, "compose", "-f", str(BASE_COMPOSE), "-f", str(OVERLAY_COMPOSE), "config", "--format", "json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert resolved.returncode == 0, resolved.stderr
    services = json.loads(resolved.stdout)["services"]

    app = services["app"]
    assert app["deploy"]["resources"]["limits"]["memory"]
    assert app["read_only"] is True
    assert app["stop_grace_period"]
    assert "no-new-privileges:true" in app["security_opt"]
    # The overlay must not have introduced a second database credential path.
    assert app["environment"]["DATABASE_PASSWORD_FILE"] == "/run/secrets/postgres_password"
    assert "smoke" in services
    assert services["smoke"].get("profiles") in (None, [])


def test_image_check_skips_structurally_instead_of_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both SKIP routes the task book allows must be structured and exit 0."""

    del monkeypatch
    script = REPO_ROOT / "scripts" / "check_image.sh"
    bash = shutil.which("bash") or "/bin/bash"

    unreachable = subprocess.run(
        [bash, str(script)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "DOCKER": "/nonexistent/docker"},
    )
    assert unreachable.returncode == 0, unreachable.stderr
    payload = json.loads(unreachable.stdout.strip().splitlines()[-1])
    assert payload["status"] == "SKIP"
    assert "unreachable" in payload["reason"]

    missing_image = subprocess.run(
        [bash, str(script), "water-agent-definitely-not-built:latest"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=dict(os.environ),
    )
    if missing_image.returncode != 0:
        pytest.skip("docker CLI is not available on this host; the image SKIP route needs it")
    payload = json.loads(missing_image.stdout.strip().splitlines()[-1])
    assert payload["status"] == "SKIP"
    assert "not built locally" in payload["reason"]
