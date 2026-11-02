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


def test_operator_skills_volume_is_read_only() -> None:
    """P7-S9-A09 运维 skill 目录是只读挂载的 ``app_skills`` 卷，不在备份范围（在 ``/var/lib/water-agent`` 之外）；
    开关与目录变量都只设在 app 服务上；内置 skill 随包分发（package-data），镜像里不靠 ``data/``。"""

    import tomllib

    compose = (REPO_ROOT / "compose.yml").read_text(encoding="utf-8")
    backup = (REPO_ROOT / "scripts" / "backup.sh").read_text(encoding="utf-8")
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    app_block = compose.split("\n  app:\n", 1)[1].split("\n  smoke:\n", 1)[0]
    assert '      WATER_AGENT_SKILLS: "on"\n' in app_block
    assert "      WATER_AGENT_SKILLS_DIR: /etc/water-agent/skills\n" in app_block
    assert "      - app_skills:/etc/water-agent/skills:ro\n" in app_block, "必须只读挂载"
    assert "\n  app_skills:\n" in compose, "named volume 必须在顶层 volumes 注册"
    # 只有 app 拿到：冒烟与一次性服务不开 skill。
    # P7-S13：``skills-admin``（profile ops 的一次性运维容器）也**只读**挂这个卷——同名预检（SKILL_NAME_TAKEN）要看得到运维目录里
    # 有哪些 skill。所以"挂载点恰好一个"放宽为"恰好两个，都是只读，且只在 app 与 skills-admin"；开关 ``WATER_AGENT_SKILLS:`` 仍只在 app。
    assert compose.count("WATER_AGENT_SKILLS:") == 1
    mounts = [line.strip() for line in compose.splitlines() if "app_skills:/" in line]
    assert mounts == ["- app_skills:/etc/water-agent/skills:ro"] * 2, "恰好两个挂载点，都必须只读"
    admin_block = compose.split("\n  skills-admin:\n", 1)[1].split("\n  # Smoke test", 1)[0]
    assert "app_skills:/etc/water-agent/skills:ro" in admin_block and "app_skills:/" not in compose.split("\n  smoke:\n", 1)[1].split("\n  reverse-proxy:", 1)[0]

    # 不在备份范围：备份打包的是 /var/lib/water-agent 整体
    assert "tar -cf - -C /var/lib/water-agent ." in backup, "备份范围变了，请重新确认 skill 卷仍不在其中"
    assert not "/etc/water-agent/skills".startswith("/var/lib/water-agent")

    # 内置 skill 随包分发；.dockerignore 收 src/，镜像里有这几份文件
    package_data = pyproject["tool"]["setuptools"]["package-data"]
    assert package_data["water_agent.skills"] == ["builtin/*/SKILL.md"]
    builtin = sorted((REPO_ROOT / "src" / "water_agent" / "skills" / "builtin").glob("*/SKILL.md"))
    assert len(builtin) == 4
    assert "!src/**" in (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")


def test_public_base_url_is_passed_to_the_app_only() -> None:
    """P7-S8-A08：回调地址的唯一来源 ``WEB_PUBLIC_BASE_URL`` 只传给 app 服务；缺省为空（面板不提供授权）。"""

    compose = (REPO_ROOT / "compose.yml").read_text(encoding="utf-8")
    app_block = compose.split("\n  app:\n", 1)[1].split("\n  smoke:\n", 1)[0]
    assert "      WEB_PUBLIC_BASE_URL: ${WEB_PUBLIC_BASE_URL:-}\n" in app_block
    assert compose.count("\n      WEB_PUBLIC_BASE_URL:") == 1, "只有 app 服务拿到它"


def test_web_search_is_off_by_default_and_its_key_never_travels_by_env() -> None:
    """P7-S12-A12 compose 给 app 透传联网搜索的开关 / provider / 基址 / 私网放行四个变量，**缺省为空 = off**（外发默认关，
    与 skills 不同）；密钥不经环境变量传（会出现在 ``docker compose config`` 里），在设置面板的只写槽位填写。四个变量与密钥
    槽位都被装配快照钉住。"""

    from water_agent.harness import tool_assembly_snapshot as snap

    compose = (REPO_ROOT / "compose.yml").read_text(encoding="utf-8")
    app_block = compose.split("\n  app:\n", 1)[1].split("\n  smoke:\n", 1)[0]
    for name in (
        "WATER_AGENT_WEB_SEARCH",
        "WATER_AGENT_WEB_SEARCH_PROVIDER",
        "WATER_AGENT_WEB_SEARCH_BASE_URL",
        "WATER_AGENT_WEB_SEARCH_ALLOW_PRIVATE",
    ):
        assert f"      {name}: ${{{name}:-}}\n" in app_block, name
        assert name in snap.PINNED_ENV_KEYS, name
    assert "WATER_AGENT_WEB_SEARCH: \"on\"" not in compose
    assert "WATER_AGENT_WEB_SEARCH_API_KEY:" not in compose
    assert "WATER_AGENT_WEB_SEARCH_API_KEY" in snap.PINNED_ENV_KEYS


def test_endpoint_variables_are_passed_through() -> None:
    """P7-S10-A09 compose 给 app 透传对话 / embedding 端点、兼容性报告、超时、价格与检索模式（缺省为空 = 现状：
    DeepSeek + 百炼 + auto）；冒烟服务拿到检索模式与 embedding 端点，好按同一配置断言 retrieval_mode。Key 一律
    不经环境变量传（会出现在 ``docker compose config`` 里），在设置面板的只写槽位填写。"""

    compose = (REPO_ROOT / "compose.yml").read_text(encoding="utf-8")
    app_block = compose.split("\n  app:\n", 1)[1].split("\n  smoke:\n", 1)[0]
    smoke_block = compose.split("\n  smoke:\n", 1)[1].split("\n  reverse-proxy:\n", 1)[0]
    for name in (
        "WATER_AGENT_LLM_BASE_URL",
        "WATER_AGENT_LLM_MODEL",
        "WATER_AGENT_LLM_COMPATIBILITY_REPORT",
        "WATER_AGENT_LLM_TIMEOUT_SECONDS",
        "WATER_AGENT_LLM_PRICE",
        "WATER_AGENT_EMBEDDING_BASE_URL",
        "WATER_AGENT_EMBEDDING_MODEL",
        "WATER_AGENT_RETRIEVAL_MODE",
    ):
        assert f"      {name}: ${{{name}:-}}\n" in app_block, name
    for name in ("WATER_AGENT_RETRIEVAL_MODE", "WATER_AGENT_EMBEDDING_BASE_URL", "WATER_AGENT_EMBEDDING_MODEL"):
        assert f"      {name}: ${{{name}:-}}\n" in smoke_block, name
    assert "WATER_AGENT_LLM_API_KEY:" not in compose
    assert "WATER_AGENT_EMBEDDING_API_KEY:" not in compose


def test_skills_download_is_an_ops_only_service_and_its_volumes_stay_out_of_the_backup() -> None:
    """P7-S13-A10 Skill 下载的部署形状：

    - ``skills-admin`` 在 ``profiles: [ops]`` 下（默认 ``up`` 不启动），复用 ``water-agent-app:local``（不新增构建服务）、
      只读根文件系统、不挂 secrets、不连 postgres；
    - 注册表基址（``WATER_AGENT_SKILL_REGISTRY_URL``）与私网放行**只**设在 ``skills-admin``，``app`` 看不到；``app`` 也**不挂隔离区**；
    - ``app`` 把安装区**只读**挂载并设 ``WATER_AGENT_SKILLS_INSTALLED_DIR``；
    - 三个新卷（安装区 / 隔离区 / 审计）都在 ``/var/lib/water-agent`` 之外，不进 ``scripts/backup.sh`` 的备份范围；
    - 新挂载点在镜像里预建并归 ``wateragent``（Docker 只在首次填充空卷时从镜像抄属主）：隔离区与审计 ``chmod 700``。
    """

    compose = (REPO_ROOT / "compose.yml").read_text(encoding="utf-8")
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    backup = (REPO_ROOT / "scripts" / "backup.sh").read_text(encoding="utf-8")

    admin_block = compose.split("\n  skills-admin:\n", 1)[1].split("\n  # Smoke test", 1)[0]
    assert "    profiles: [ops]\n" in admin_block
    assert "    image: water-agent-app:local\n" in admin_block and "build:" not in admin_block, "复用 app 镜像，不新增构建服务"
    assert "    read_only: true\n" in admin_block and "    tmpfs: [/tmp]\n" in admin_block
    assert "secrets:" not in admin_block and "DATABASE_URL" not in admin_block and "depends_on" not in admin_block
    assert 'command: ["python", "-m", "water_agent.skills.cli", "--help"]' in admin_block
    for line in (
        "      WATER_AGENT_SKILL_REGISTRY_URL: ${WATER_AGENT_SKILL_REGISTRY_URL:-}\n",
        "      WATER_AGENT_SKILL_REGISTRY_ALLOW_PRIVATE: ${WATER_AGENT_SKILL_REGISTRY_ALLOW_PRIVATE:-}\n",
        "      - app_skills_installed:/var/lib/water-agent-skills-installed\n",
        "      - skills_quarantine:/var/lib/water-agent-skills-quarantine\n",
        "      - skills_state:/var/lib/water-agent-skills-state\n",
        "      - app_skills:/etc/water-agent/skills:ro\n",
    ):
        assert line in admin_block, line

    app_block = compose.split("\n  app:\n", 1)[1].split("\n  skills-admin:\n", 1)[0]
    assert "      - app_skills_installed:/var/lib/water-agent-skills-installed:ro\n" in app_block, "app 只读挂载安装区"
    assert "      WATER_AGENT_SKILLS_INSTALLED_DIR: /var/lib/water-agent-skills-installed\n" in app_block
    for forbidden in ("SKILL_REGISTRY", "QUARANTINE", "skills_quarantine", "skills_state", "STATE_DIR"):
        assert forbidden not in app_block, f"app 不得拿到 {forbidden}"
    assert compose.count("\n      WATER_AGENT_SKILL_REGISTRY_URL: ") == 1, "注册表基址只出现在 skills-admin"
    assert "WATER_AGENT_SKILL_REGISTRY_URL" not in app_block
    assert compose.count("profiles: [ops]") == 1

    for volume in ("app_skills_installed", "skills_quarantine", "skills_state"):
        assert f"\n  {volume}:\n" in compose, f"{volume} 必须在顶层 volumes 注册"
    for fragment in ("app_skills_installed:/var/lib/water-agent/", "skills_quarantine:/var/lib/water-agent/", "skills_state:/var/lib/water-agent/"):
        assert fragment not in compose, "不得挂进 /var/lib/water-agent（会被 backup.sh 打进 user_files.tar）"
    assert "tar -cf - -C /var/lib/water-agent ." in backup, "备份范围变了，请重新确认三个 skills 卷仍不在其中"
    for mount in ("/var/lib/water-agent-skills-installed", "/var/lib/water-agent-skills-quarantine", "/var/lib/water-agent-skills-state"):
        assert not mount.startswith("/var/lib/water-agent/")

    def _line(marker: str) -> str:
        matches = [line for line in dockerfile.splitlines() if marker in line]
        assert len(matches) == 1, f"Dockerfile 里 {marker!r} 命中 {len(matches)} 行，断言已失焦"
        return matches[0]

    assert all(
        mount in _line("mkdir -p /var/lib/water-agent-skills-installed")
        for mount in ("water-agent-skills-installed", "water-agent-skills-quarantine", "water-agent-skills-state")
    )
    assert "water-agent-skills-state" in "\n".join(dockerfile.splitlines()[dockerfile.splitlines().index(_line("chown -R wateragent:wateragent")):][:2])
    chmod_block = dockerfile.split("&& chmod 700", 1)[1].split("\nCOPY", 1)[0]
    assert "skills-quarantine" in chmod_block and "skills-state" in chmod_block, "未批准文本与审计不得对其他用户可读"
    assert "skills-installed" not in chmod_block, "安装区要让 app 进程读到，不 chmod 700"
    # 只动了目录清单：基础镜像、USER、CMD 不变
    assert "USER wateragent" in dockerfile and 'CMD ["uvicorn", "water_agent.webapp.app:app"' in dockerfile


def test_skill_download_variables_are_pinned_by_the_assembly_snapshot() -> None:
    """P7-S13-A10 新的环境变量被装配快照钉住（评测机上残留的变量不会让快照漂）。"""

    from water_agent.harness.tool_assembly_snapshot import PINNED_ENV_KEYS

    for key in (
        "WATER_AGENT_SKILLS_INSTALLED_DIR",
        "WATER_AGENT_SKILLS_QUARANTINE_DIR",
        "WATER_AGENT_SKILL_REGISTRY_URL",
        "WATER_AGENT_SKILL_REGISTRY_ALLOW_PRIVATE",
    ):
        assert key in PINNED_ENV_KEYS, key

def test_mcp_registry_is_off_by_default_and_only_app_gets_it() -> None:
    """P7-S14-A08 compose 只给 app 透传 MCP 目录的基址与私网放行两个变量，**缺省为空 = 功能关闭**（外发默认关）；不新增服务、
    不新增卷（目录搜索是 app 内的只读外发，目的地由运维写死）；目录不收任何凭据，所以没有密钥槽位，也不经环境变量传任何密钥。
    两个变量都被装配快照钉住。"""

    from water_agent.harness import tool_assembly_snapshot as snap

    compose = (REPO_ROOT / "compose.yml").read_text(encoding="utf-8")
    app_block = compose.split("\n  app:\n", 1)[1].split("\n  smoke:\n", 1)[0]
    for name in ("WATER_AGENT_MCP_REGISTRY_URL", "WATER_AGENT_MCP_REGISTRY_ALLOW_PRIVATE"):
        assert f"      {name}: ${{{name}:-}}\n" in app_block, name
        assert compose.count(f"\n      {name}:") == 1, f"{name} 只有 app 服务拿到"
        assert name in snap.PINNED_ENV_KEYS, name
    assert "WATER_AGENT_MCP_REGISTRY_URL: \"" not in compose
    assert "MCP_REGISTRY_API_KEY" not in compose and "MCP_REGISTRY_TOKEN" not in compose
    assert "mcp_registry" not in compose.split("\nvolumes:\n", 1)[1], "不新增卷"
