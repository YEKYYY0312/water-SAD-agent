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
    assert "image: ollama/ollama:0.13.3" in compose
    assert "LOCAL_MODEL_ENABLED: ${LOCAL_MODEL_ENABLED:-false}" in compose
    assert "Step 46 local-model integration is not implemented" not in compose
    assert "local_model_data:/root/.ollama" in compose
    assert "postgres_data:" in compose
    assert "postgres_data:/var/lib/postgresql" in compose
    assert "postgres_data:/var/lib/postgresql/data" not in compose
    assert "/health/live" in compose
    assert "/health/ready" in compose


def test_compose_wires_the_llm_credential_sources() -> None:
    """凭据的三层来源里，第 2、3 层由 compose 提供，缺一层 Web 端就配不上模型。

    这一组断言存在的理由：这两个 secret 的命名约定早在 secrets/README.md 里
    写好，却长期没有接进 compose，于是文档教的 `printf > secrets/deepseek_api_key`
    完全无效——容器内根本看不到那个文件。
    """
    compose = (REPO_ROOT / "compose.yml").read_text(encoding="utf-8")

    # 第 2 层：Compose secret，只读挂载
    assert "deepseek_api_key:" in compose
    assert "dashscope_api_key:" in compose
    assert "file: ./secrets/deepseek_api_key" in compose
    assert "file: ./secrets/dashscope_api_key" in compose
    assert "DEEPSEEK_API_KEY_FILE: /run/secrets/deepseek_api_key" in compose
    assert "DASHSCOPE_API_KEY_FILE: /run/secrets/dashscope_api_key" in compose

    # 第 3 层：设置面板写入的可写卷
    assert "app_credentials:/var/lib/water-agent-credentials" in compose
    assert "\n  app_credentials:\n" in compose, "named volume 必须在顶层 volumes 注册"

    # 凭据卷不得落在 /var/lib/water-agent 之内：scripts/backup.sh 打包该目录
    # 整体，挂进去会把密钥明文写进备份包。
    assert "app_credentials:/var/lib/water-agent/" not in compose


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
