"""Step 36 deployment files keep the Web skeleton isolated and publish-safe."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_compose_declares_the_required_default_and_optional_services() -> None:
    compose = (REPO_ROOT / "compose.yml").read_text(encoding="utf-8")

    for service in ("postgres:", "migrate:", "bootstrap-admin:", "app:"):
        assert service in compose
    assert "profiles: [production]" in compose
    assert "reverse-proxy:" in compose
    assert "profiles: [local-model]" in compose
    assert "local-model:" in compose
    assert "postgres_data:" in compose
    assert "/health/live" in compose
    assert "/health/ready" in compose


def test_container_uses_a_non_root_runtime_user_and_a_multistage_build() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "AS frontend" in dockerfile
    assert "AS python-build" in dockerfile
    assert "USER wateragent" in dockerfile
    assert "npm ci" in dockerfile
    assert "web/app/dist" in dockerfile


def test_docker_context_excludes_secrets_and_non_publishable_runtime_data() -> None:
    excluded = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")

    for pattern in (
        ".env*",
        "secrets/",
        "data/traces/",
        "data/eval/reports/",
        "data/eval/traces/",
        "data/sessions/",
        "data/indexes/",
        "*.db",
        "web/app/node_modules/",
        "web/app/dist/",
    ):
        assert pattern in excluded
