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


def test_compose_gives_traces_a_writable_volume_outside_the_backup_scope() -> None:
    """trace 必须有可写落点，且不能落进备份包。

    镜像把 ``/app/data`` 打成只读（Dockerfile 的 ``chmod -R a-w``），``data/traces``
    连镜像都没进（``.dockerignore``）。不配 ``WATER_AGENT_TRACE_DIR`` 时每次运行
    收尾都会 ``OSError: trace write failed`` —— 恰好在最需要现场的那一刻什么都不留。

    落点同时不能在 ``/var/lib/water-agent`` 之内：``scripts/backup.sh`` 用
    ``tar -C /var/lib/water-agent .`` 打整个目录，而 trace 含用户问题与答案原文
    （docs/design/p5-release-data-inventory.md 的 E 类），不该随用户文件备份流转。
    """
    compose = (REPO_ROOT / "compose.yml").read_text(encoding="utf-8")
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    backup = (REPO_ROOT / "scripts" / "backup.sh").read_text(encoding="utf-8")

    assert "WATER_AGENT_TRACE_DIR: /var/lib/water-agent-traces" in compose
    assert "app_traces:/var/lib/water-agent-traces" in compose
    assert "\n  app_traces:\n" in compose, "named volume 必须在顶层 volumes 注册"
    assert "app_traces:/var/lib/water-agent/" not in compose

    # 备份脚本打包的根目录不得是 trace 落点的祖先
    assert "tar -cf - -C /var/lib/water-agent ." in backup, "备份范围变了，请重新确认 trace 落点"

    # 卷挂载前镜像里必须已有这个目录且归 wateragent：Docker 只在首次填充空卷时
    # 从镜像抄属主与权限，镜像里没有的路径会变成 root 所有、非 root 用户写不进去。
    def _line(marker: str) -> str:
        matches = [line for line in dockerfile.splitlines() if marker in line]
        assert len(matches) == 1, f"Dockerfile 里 {marker!r} 命中 {len(matches)} 行，断言已失焦"
        return matches[0]

    assert "/var/lib/water-agent-traces" in _line("mkdir -p /var/lib/water-agent-credentials")
    assert "/var/lib/water-agent-traces" in _line("chown -R wateragent:wateragent")
    assert "/var/lib/water-agent-traces" in _line("chmod 700"), "trace 含用户原文，不得对其他用户可读"


def test_mcp_admin_layer_volume_is_backed_up() -> None:
    """P7-S6-A08 Web 扩展面板的第三层 MCP 配置有可写的卷，且在备份范围内；凭据值仍只在 app_credentials。

    层文件只有启停与工具白名单（没有密钥），所以挂在 ``/var/lib/water-agent`` 之下、随
    ``scripts/backup.sh`` 的 ``tar -C /var/lib/water-agent .`` 一起备份。镜像根文件系统只读，层文件只能
    落在卷里；挂载点必须在镜像里预建并归 wateragent，否则 named volume 首次挂载是 root 所有。
    """
    compose = (REPO_ROOT / "compose.yml").read_text(encoding="utf-8")
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    backup = (REPO_ROOT / "scripts" / "backup.sh").read_text(encoding="utf-8")

    assert "app_mcp_admin:/var/lib/water-agent/mcp-admin" in compose
    assert "\n  app_mcp_admin:\n" in compose, "named volume 必须在顶层 volumes 注册"
    assert "WATER_AGENT_MCP_WEB_LAYER: /var/lib/water-agent/mcp-admin/web_layer.json" in compose
    assert "tar -cf - -C /var/lib/water-agent ." in backup, "备份范围变了，请重新确认层文件仍被备份"

    # 环境变量与卷挂载在同一个服务里（app），不是只有冒烟或别的服务拿到
    app_block = compose.split("\n  app:\n", 1)[1].split("\n  smoke:\n", 1)[0]
    assert "WATER_AGENT_MCP_WEB_LAYER:" in app_block
    assert "app_mcp_admin:/var/lib/water-agent/mcp-admin" in app_block

    def _line(marker: str) -> str:
        matches = [line for line in dockerfile.splitlines() if marker in line]
        assert len(matches) == 1, f"Dockerfile 里 {marker!r} 命中 {len(matches)} 行，断言已失焦"
        return matches[0]

    assert "/var/lib/water-agent/mcp-admin" in _line("mkdir -p /var/lib/water-agent/uploads")
    assert "/var/lib/water-agent " in _line("chown -R wateragent:wateragent"), "chown -R 覆盖 mcp-admin"

    # 凭据槽位的值仍只在 app_credentials 卷，那个卷不在备份范围内
    assert "app_credentials:/var/lib/water-agent-credentials" in compose
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
        # data/indexes/ 刻意**不**在这个列表里：它由 .dockerignore 末尾的 re-include
        # 打进镜像（否则每次检索都回落 fixture）。那条规则的归属见
        # tests/test_container_smoke.py::TestProbePremises。
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
