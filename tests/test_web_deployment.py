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

    assert "node:26.8.1-bookworm-slim@sha256:" in dockerfile
    assert "python:3.12.14-slim-bookworm@sha256:" in dockerfile
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


def test_production_proxy_requires_a_domain_and_sets_http_boundaries() -> None:
    compose = (REPO_ROOT / "compose.yml").read_text(encoding="utf-8")
    caddyfile = (REPO_ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")

    assert "caddy:2.11.1-alpine@sha256:" in compose
    assert "WEB_DOMAIN: ${WEB_DOMAIN:-}" in compose
    assert 'test -n "$$WEB_DOMAIN"' in compose
    assert '"80:80"' in compose
    assert '"443:443"' in compose
    assert "request_body" in caddyfile
    assert "max_size 16MB" in caddyfile
    assert "Content-Security-Policy" in caddyfile
    assert "Strict-Transport-Security" in caddyfile
