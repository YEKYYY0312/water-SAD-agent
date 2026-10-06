"""P7-S2：插件化的结构不变量（``docs/design/16-p7-plugin-mcp.md`` §4，P7-I01…I07）。

全部用 AST 判定**真实调用**，不按源码文本找字符串：``ToolRegistry(`` 这样的字样也出现在
docstring 与注释里（``observability.py``、``detectors.py``），文本 grep 会误报；反过来，
把调用改写成别名也骗不过按调用结点判定的检查。白名单条目若已不对应任何真实调用，同样
判失败——白名单只能收紧，不能留下"曾经豁免过"的空位。
"""
from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "water_agent"

#: 唯一的面向模型装配点。
ASSEMBLY_MODULE = "plugins/assembly.py"

#: 不面向模型的内部注册表（P7-I01 白名单），每条都要说明为什么不算第二条装配路径。
INTERNAL_REGISTRY_SITES: dict[str, str] = {
    "mcp/memory_adapter.py": "Memory MCP 适配器的私有注册表，只做记忆搜索，工具从不交给模型",
    "runtime/governance/scenarios.py": "治理评测场景，只调用 calc_velocity 验证护栏与 trace",
    "harness/memory_eval.py": "memory_p0 评测，比对记忆注入前后的工具清单与提示词不变",
    "harness/local_model_compat.py": "本地模型兼容性探针，刻意逐个绑定单个工具",
}

#: P7-I02：除装配入口外，只有兼容性探针可以自己把 ToolSpec 转成 LangChain 工具。
BASE_TOOL_SITES = frozenset({ASSEMBLY_MODULE, "harness/local_model_compat.py"})

#: P7-I07：只有这两条装配路径构图（装配快照的捕获点前提）。
AGENTIC_APP_CALLERS = frozenset({"runtime/agentic.py", "harness/runner.py"})


def _modules() -> Iterator[tuple[str, ast.Module]]:
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(SRC).as_posix()
        yield rel, ast.parse(path.read_text(encoding="utf-8"))


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _callers(name: str) -> set[str]:
    return {
        rel
        for rel, tree in _modules()
        if any(isinstance(node, ast.Call) and _call_name(node) == name for node in ast.walk(tree))
    }


def _module(rel: str) -> ast.Module:
    return ast.parse((SRC / rel).read_text(encoding="utf-8"))


def _assigned_literal(rel: str, name: str) -> Any:
    for node in ast.walk(_module(rel)):
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if value is not None and any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return ast.literal_eval(value)
    raise AssertionError(f"{rel} 里没有找到 {name} 的字面量赋值")


@pytest.mark.parametrize(
    "invariant",
    ["P7-I01", "P7-I02", "P7-I03", "P7-I04", "P7-I05", "P7-I06", "P7-I07"],
)
def test_invariants_hold(invariant: str) -> None:
    """P7-S2-A08 插件化的七条结构不变量（P7-I01…I07）都成立。"""

    if invariant == "P7-I01":
        callers = _callers("ToolRegistry")
        allowed = {ASSEMBLY_MODULE, *INTERNAL_REGISTRY_SITES}
        assert callers <= allowed, f"在装配入口之外构造了 ToolRegistry：{sorted(callers - allowed)}"
        stale = set(INTERNAL_REGISTRY_SITES) - callers
        assert not stale, f"白名单条目已不再构造 ToolRegistry，应删除：{sorted(stale)}"
        assert ASSEMBLY_MODULE in callers
    elif invariant == "P7-I02":
        callers = _callers("tool_spec_to_base_tool")
        assert callers == BASE_TOOL_SITES, sorted(callers)
    elif invariant == "P7-I03":
        callers = _callers("assemble_tools")
        assert {"runtime/agentic.py", "harness/runner.py"} <= callers, sorted(callers)
    elif invariant == "P7-I04":
        assert _assigned_literal("plugins/builtin.py", "BUILTIN_PLUGIN_ORDER") == (
            "clauses", "hydraulics", "materials", "drawing", "mcp_gateway"
        )
        from water_agent.plugins import BUILTIN_PLUGIN_ORDER, McpGatewayPlugin, builtin_plugins

        plugins = builtin_plugins(mcp=McpGatewayPlugin.borrowing(None))
        assert tuple(plugin.plugin_id for plugin in plugins) == BUILTIN_PLUGIN_ORDER
    elif invariant == "P7-I05":
        offenders = sorted(
            rel for rel in _callers("ToolSpec") if rel.startswith("plugins/")
        )
        assert not offenders, f"plugins/ 里构造了 ToolSpec（治理清单扫描不到）：{offenders}"
    elif invariant == "P7-I06":
        assert _assigned_literal_grantable() == {"MCP_INVOKE"}
        from water_agent.plugins import GRANTABLE_CAPABILITIES
        from water_agent.runtime.governance.contracts import Capability

        assert GRANTABLE_CAPABILITIES == frozenset({Capability.MCP_INVOKE})
    elif invariant == "P7-I07":
        callers = _callers("build_agentic_app")
        assert callers == AGENTIC_APP_CALLERS, sorted(callers)
    else:  # pragma: no cover - 参数化列表与分支一一对应
        raise AssertionError(invariant)


def test_production_app_receives_exposure_policy() -> None:
    """P7-S4-A08 两处生产构图都把本次装配的暴露策略交给图（AST 判定真实调用）。

    少传一处不会报错，只会静默退回全量暴露——所以要结构性地锁住。
    """

    for rel in sorted(AGENTIC_APP_CALLERS):
        calls = [
            node
            for node in ast.walk(_module(rel))
            if isinstance(node, ast.Call) and _call_name(node) == "build_agentic_app"
        ]
        assert calls, rel
        for call in calls:
            assert "exposure" in {kw.arg for kw in call.keywords}, f"{rel} 构图时没传 exposure="


def _assigned_literal_grantable() -> set[str]:
    """``GRANTABLE_CAPABILITIES = frozenset({Capability.X, ...})`` 里出现的能力名。"""

    for node in ast.walk(_module("plugins/base.py")):
        if isinstance(node, ast.AnnAssign | ast.Assign):
            targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            if any(isinstance(t, ast.Name) and t.id == "GRANTABLE_CAPABILITIES" for t in targets):
                return {
                    attr.attr
                    for attr in ast.walk(node)
                    if isinstance(attr, ast.Attribute)
                    and isinstance(attr.value, ast.Name)
                    and attr.value.id == "Capability"
                }
    raise AssertionError("plugins/base.py 里没有 GRANTABLE_CAPABILITIES")


def test_session_assembly_matches_baseline() -> None:
    """P7-S2-A06 会话路径零行为变化：会话侧全部 profile 与入仓基线逐字段一致。

    S2 的入仓基线里，会话侧 profile 与 S1 合入后的基线完全相同（范围守卫的 ``frozen_json``
    在 PR 上核对这一点）；这里在本地再采一次会话侧，与基线比对。
    """

    from water_agent.harness import tool_assembly_snapshot as snap

    session_profiles = [profile for profile in snap.PROFILES if profile.side == "session"]
    head = snap.collect_snapshot(session_profiles)
    baseline = snap.load_snapshot(snap.DEFAULT_BASELINE)
    lines = snap.diff_snapshots(
        {"profiles": {p.name: baseline["profiles"][p.name] for p in session_profiles}},
        {"profiles": head["profiles"]},
    )
    assert lines == [], "\n".join(lines)
