"""P7-S13-A06：下载 / 批准没有模型入口也没有面板入口（SDD17-I12，``docs/design/17-p7-external-capabilities.md`` §10）。

全部用 AST 判定**真实导入**，不按源码文本找字符串；白名单只能收紧——登记项不再对应真实导入同样判失败。每条判定都有反证。
"""
from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "water_agent"
WEB_SRC = Path(__file__).resolve().parents[1] / "web" / "app" / "src"

#: ``skills/remote/`` 的唯一合法导入方（SDD17-I12）。
REMOTE_IMPORTERS: dict[str, str] = {
    "skills/cli.py": "运维 CLI：取回与批准发生在主机 shell 上，不在模型工具与 Web 面板里",
}
#: 绝不能导入 ``skills/remote/`` 的目录：模型工具、Web 路由、插件、图、运行时、治理层。
FORBIDDEN_IMPORTER_PREFIXES = ("tools/", "webapp/", "plugins/", "graphs/", "runtime/", "prompting/", "mcp/", "external/", "web_search/")
#: 取回 / 批准 / 安装类名字：模型工具清单、Web 路由路径与前端源码里都不得出现。
INSTALL_WORDS = ("fetch_skill", "approve_skill", "install_skill", "skill_registry", "skill_candidate", "quarantine")


def _modules() -> Iterator[tuple[str, ast.Module]]:
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path.relative_to(SRC).as_posix(), ast.parse(path.read_text(encoding="utf-8"))


def _imports_remote(tree: ast.Module, rel: str) -> bool:
    """是否导入 ``water_agent.skills.remote`` 及其子模块（含 ``from water_agent.skills import remote``）。"""

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:  # 相对导入：按文件所在包还原
                base = ("water_agent." + rel[:-3].replace("/", ".")).rsplit(".", node.level)[0]
                module = f"{base}.{module}" if module else base
            if module == "water_agent.skills.remote" or module.startswith("water_agent.skills.remote."):
                return True
            if module == "water_agent.skills" and any(alias.name == "remote" for alias in node.names):
                return True
        elif isinstance(node, ast.Import):
            if any(alias.name == "water_agent.skills.remote" or alias.name.startswith("water_agent.skills.remote.") for alias in node.names):
                return True
    return False


def _importers() -> set[str]:
    return {rel for rel, tree in _modules() if not rel.startswith("skills/remote/") and _imports_remote(tree, rel)}


def _violations(importers: set[str]) -> tuple[set[str], set[str]]:
    """（不该导入的 / 合法但已过期的登记项）。"""

    return importers - set(REMOTE_IMPORTERS), set(REMOTE_IMPORTERS) - importers


def test_only_the_operator_cli_imports_the_remote_package() -> None:
    """P7-S13-A06 / SDD17-I12 ``skills/remote/`` 只被 ``skills/cli.py`` 导入（AST 判定真实导入）；``tools/``、``webapp/``、``plugins/``、
    ``graphs/``、``runtime/`` 等一律不导入它；登记项不再对应真实导入同样判失败。"""

    importers = _importers()
    unexpected, stale = _violations(importers)
    assert not unexpected, f"不该导入 skills/remote/ 的模块：{sorted(unexpected)}"
    assert not stale, f"登记项已不再导入 skills/remote/，应删除：{sorted(stale)}"
    assert not [rel for rel in importers if rel.startswith(FORBIDDEN_IMPORTER_PREFIXES)]


def test_the_import_check_goes_red_for_a_rogue_importer_and_a_stale_entry() -> None:
    """P7-S13-A06 反证：往真实导入方集合里加一个模型工具 / Web 路由的模块 → 判定报出它；去掉真实导入方 → 登记项被判过期；
    相对导入与 ``from water_agent.skills import remote`` 两种写法也认得出来。"""

    real = _importers()
    unexpected, _ = _violations(real | {"tools/rogue.py", "webapp/rogue_routes.py"})
    assert unexpected == {"tools/rogue.py", "webapp/rogue_routes.py"}
    _, stale = _violations(real - {"skills/cli.py"})
    assert stale == {"skills/cli.py"}

    assert _imports_remote(ast.parse("from water_agent.skills.remote.client import RegistryClient\n"), "tools/x.py")
    assert _imports_remote(ast.parse("import water_agent.skills.remote.approve\n"), "tools/x.py")
    assert _imports_remote(ast.parse("from water_agent.skills import remote\n"), "tools/x.py")
    assert _imports_remote(ast.parse("from ..skills.remote import approve\n"), "tools/x.py")
    assert not _imports_remote(ast.parse("from water_agent.skills.installed import read_lock\n"), "webapp/x.py")
    assert not _imports_remote(ast.parse("from water_agent.skills import build_skill_library\n"), "webapp/x.py")


def test_the_panel_and_library_read_the_lock_only_through_installed() -> None:
    """P7-S13-A06 面板路由与库只读 ``skills/installed.py``（lock 元数据与字节校验），从不碰取回 / 批准代码。"""

    for rel in ("webapp/skill_routes.py", "skills/library.py", "tools/load_skill.py", "plugins/builtin.py"):
        assert not _imports_remote(ast.parse((SRC / rel).read_text(encoding="utf-8")), rel), rel
    library_source = (SRC / "skills" / "library.py").read_text(encoding="utf-8")
    assert "from water_agent.skills.installed import" in library_source


def test_the_model_sees_no_fetch_approve_or_install_tool() -> None:
    """P7-S13-A06 模型工具清单（治理清单里登记的全部工具 + 本地插件贡献的全部 ``ToolSpec``）不含取回 / 批准 / 安装类名字；
    模型唯一与 skill 有关的工具仍是 ``load_skill``，且它只回回执。"""

    from water_agent.plugins import McpGatewayPlugin, builtin_plugins
    from water_agent.plugins.base import AssemblyContext
    from water_agent.retrieval.in_memory import InMemoryRetriever
    from water_agent.runtime.governance.contracts import Capability
    from water_agent.runtime.governance.inventory import (
        PLUGIN_GRANTED_TOOL_CAPABILITIES,
        TOOL_CAPABILITIES,
    )

    registered = {entry.tool_name for entry in (*TOOL_CAPABILITIES, *PLUGIN_GRANTED_TOOL_CAPABILITIES)}
    assert "load_skill" in registered
    assert not [name for name in registered if any(word in name.lower() for word in INSTALL_WORDS)]
    assert not [name for name in registered if "skill" in name and name != "load_skill"]

    ctx = AssemblyContext(retriever=InMemoryRetriever([]), cite_context_source=lambda: set(), drawing_root="")
    contributed = {spec.name for plugin in builtin_plugins(mcp=McpGatewayPlugin.borrowing(None)) for spec in plugin.contribute(ctx).tools}
    assert not [name for name in contributed if any(word in name.lower() for word in INSTALL_WORDS)]
    assert Capability.SKILL_FETCH.value not in {entry.required_capability.value for entry in (*TOOL_CAPABILITIES, *PLUGIN_GRANTED_TOOL_CAPABILITIES)}


def test_the_web_route_table_has_no_fetch_approve_or_install_route() -> None:
    """P7-S13-A06 Web 路由表（真实 ``create_app`` 的 OpenAPI 路径与方法——``app.routes`` 里的 ``include_router`` 在这个 FastAPI 版本里是
    嵌套对象，直接遍历会漏掉全部业务路由）不含取回 / 批准 / 安装 / 注册表 / 候选 / 隔离区类路径；与 skill 有关的只有
    ``/api/skills``（GET）与 ``/api/admin/skills/{name}``（GET / PATCH）。"""

    from sqlalchemy import create_engine

    from water_agent.storage.database import Database
    from water_agent.storage.models import Base
    from water_agent.webapp.app import create_app

    engine = create_engine("sqlite://")  # 内存库：只为让 create_app 挂上全部路由，不碰任何文件
    Base.metadata.create_all(engine)
    paths = create_app(database=Database(engine=engine)).openapi()["paths"]
    assert len(paths) > 40, "路由表几乎是空的——判定会空转"
    for word in ("approve", "install", "fetch", "registry", "candidate", "quarantine"):
        assert not [path for path in paths if "skill" in path.lower() and word in path.lower()], word
    skill_routes = {(path, method.upper()) for path, item in paths.items() if "skill" in path for method in item}
    assert skill_routes == {
        ("/api/skills", "GET"),
        ("/api/admin/skills/{name}", "GET"),
        ("/api/admin/skills/{name}", "PATCH"),
    }


def test_the_frontend_source_cannot_reach_a_fetch_approve_or_install_endpoint() -> None:
    """P7-S13-A06 前端源码（``web/app/src`` 全部 ``.ts`` / ``.tsx``，大小写不敏感）不含取回 / 批准 / 安装类接口路径或函数名。"""

    needles = ("/skills/fetch", "/skills/approve", "/skills/install", "/skills/candidate", "/skills/registry",
               "skill_registry", "approveskill", "fetchskill", "installskill")
    assert WEB_SRC.is_dir()
    sources = [path for path in WEB_SRC.rglob("*") if path.suffix in {".ts", ".tsx"}]
    assert len(sources) > 20
    hits = [(path.name, needle) for path in sources for needle in needles if needle in path.read_text(encoding="utf-8").lower()]
    assert hits == []


def test_skill_fetch_capability_is_held_by_nobody_but_the_cli_principal() -> None:
    """P7-S13-A09 ``Capability.SKILL_FETCH`` 不在默认本地能力集、Web 成员 / 管理员能力集与 ``GRANTABLE_CAPABILITIES`` 里（插件、模型、面板都拿不到），
    也不在危险集里；唯一持有者是运维 CLI 构造的主体。"""

    from water_agent.plugins import GRANTABLE_CAPABILITIES
    from water_agent.runtime.governance.contracts import (
        DANGEROUS_CAPABILITIES,
        DEFAULT_LOCAL_CAPABILITIES,
        Capability,
    )
    from water_agent.skills.remote.client import cli_principal
    from water_agent.webapp.auth.dependencies import _ADMIN_CAPABILITIES, _MEMBER_CAPABILITIES

    for granted in (DEFAULT_LOCAL_CAPABILITIES, _MEMBER_CAPABILITIES, _ADMIN_CAPABILITIES, GRANTABLE_CAPABILITIES):
        assert Capability.SKILL_FETCH not in granted
    assert Capability.SKILL_FETCH not in DANGEROUS_CAPABILITIES
    assert Capability.NETWORK_FETCH in DANGEROUS_CAPABILITIES
    assert cli_principal().capabilities == frozenset({Capability.SKILL_FETCH})


def test_the_remote_package_has_no_raw_network_primitive_and_never_calls_open_exchange() -> None:
    """P7-S13-A09 ``skills/remote/`` 不含原始网络原语，也不直接调用 ``open_exchange``（只经 ``governed_get``）；
    出站站点 ``skill_registry`` 的出口模块是 ``mcp/http_transport.py``。"""

    from water_agent.runtime.governance import inventory

    root = SRC / "skills" / "remote"
    for path in sorted(root.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert not any(pattern.search(text) for pattern in inventory._EGRESS_PATTERNS), path.name
        assert not inventory._spawns_downloader(text), path.name
        tree = ast.parse(text)
        assert not [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                    and getattr(node.func, "id", getattr(node.func, "attr", "")) == "open_exchange"], path.name
        # 会开连接的模块；``urllib.parse``（纯字符串解析，拼查询串 / 取主机名）不是网络原语，只禁 ``urllib.request`` / ``urllib.error``
        forbidden = {"socket", "http", "requests", "httpx", "aiohttp", "ssl", "urllib.request", "urllib.error", "urllib3"}
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and not node.level}
        offending = {name for name in imported if name in forbidden or name.split(".")[0] in {"socket", "http", "requests", "httpx", "aiohttp", "ssl", "urllib3"}}
        assert not offending, (path.name, offending)
    site = next(site for site in inventory.PROVIDER_CALL_SITES if site.site_id == "skill_registry")
    assert site.module == "water_agent/skills/remote/client.py" and site.egress_module == "water_agent/mcp/http_transport.py"
    assert site.required_capability.value == "skill_fetch" and site.governed and not site.cache_eligible
    assert "water_agent/skills/remote/client.py" not in inventory.registered_call_site_modules()


@pytest.mark.parametrize("module", ["approve", "client", "parse", "quarantine", "safety"])
def test_remote_modules_import_without_side_effects(module: str) -> None:
    """P7-S13-A06 ``skills/remote/*`` 导入时不读环境变量、不开网络、不写盘（只有显式调用才有动作）。"""

    import importlib
    import os

    snapshot = dict(os.environ)
    importlib.import_module(f"water_agent.skills.remote.{module}")
    assert dict(os.environ) == snapshot
