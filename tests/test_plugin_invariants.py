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

#: P7-I07 的评测白名单：**为了验证图本身的行为**直接调用 ``build_agentic_app`` 的模块，每条要说明为什么不算第二条装配路径。
#: P7-S11 曾登记 ``harness/external_content_eval.py``（当时没有生产联网工具，只能直接构图 + 夹具工具）；P7-S12 的 ``web_search``
#: 接入生产装配后，那批用例改走 ``AgenticSession``（报告与 S11 入仓基线逐字相同），条目已删除。现在为空——新增条目需要评审。
EVAL_GRAPH_CALLERS: dict[str, str] = {}


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
        # P7-S9：末尾追加 skills（排在全部工具插件之后——它按先于它的插件贡献的工具核对 requires_tools，
        # 指引里列在最后、预算不够时与 MCP 一样整组让位）
        # P7-S12：web_search 插在 mcp_gateway 与 skills 之间——skills 仍在最后；web_search 是逐条消息才可能出现的 request 组，
        # 不参与 skills 的 requires_tools 核对
        assert _assigned_literal("plugins/builtin.py", "BUILTIN_PLUGIN_ORDER") == (
            "clauses", "hydraulics", "materials", "drawing", "mcp_gateway", "web_search", "skills"
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
        # P7-S12（17 号 SDD 待决 D2）：加入 WEB_SEARCH——"只读外发、目的地由运维写死"，与"目的地由模型决定"的
        # NETWORK_FETCH 语义不同；后者仍在危险集里，不可授予（下面再断言一次）
        assert _assigned_literal_grantable() == {"MCP_INVOKE", "WEB_SEARCH"}
        from water_agent.plugins import GRANTABLE_CAPABILITIES
        from water_agent.runtime.governance.contracts import DANGEROUS_CAPABILITIES, Capability

        assert GRANTABLE_CAPABILITIES == frozenset({Capability.MCP_INVOKE, Capability.WEB_SEARCH})
        assert not GRANTABLE_CAPABILITIES & DANGEROUS_CAPABILITIES
        assert Capability.NETWORK_FETCH in DANGEROUS_CAPABILITIES
    elif invariant == "P7-I07":
        callers = _callers("build_agentic_app")
        assert callers - set(EVAL_GRAPH_CALLERS) == AGENTIC_APP_CALLERS, sorted(callers)
        stale = set(EVAL_GRAPH_CALLERS) - callers
        assert not stale, f"评测白名单条目已不再构图，应删除：{sorted(stale)}"
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


# ---------------------------------------------------------------------------
# S11：外部内容底座的结构不变量（17 号 SDD 的 I08 / I09；docstring 里写不带 ``P7-`` 前缀的 ``SDD17-Ixx``，
# 因为 ``test_p7_traceability`` 只认 16 号规格里定义的 ``P7-I*``）
# ---------------------------------------------------------------------------

#: ``open_exchange(`` 的登记调用方（SDD17-I08）。每条都说明为什么它是合规的出站调用方；
#: 新增调用方 = 新增出站站点，必须同时进 ``PROVIDER_CALL_SITES``。
OPEN_EXCHANGE_CALLERS: dict[str, str] = {
    "mcp/http_client.py": "MCP Streamable HTTP 客户端，登记站点 mcp_streamable_http",
    "mcp/oauth.py": "MCP OAuth 授权端点，同属 mcp_streamable_http 站点",
    "generation/chat_model.py": "设置面板「获取模型」的 GET {base_url}/models，经 ProviderGateway.call(MODEL_LIST)，复用对话站点",
    "external/http_get.py": "受治理的 GET 内核（P7-S11）；S12 起为联网搜索 / skill 注册表 / MCP 目录的唯一出站入口",
}

#: 参数名里出现这些词的本地工具参数 = 把"去哪儿"交给了模型（SDD17-I09）。
FORBIDDEN_ARG_NAMES = frozenset({"url", "uri", "href", "endpoint", "host"})


def _calls_in(source: str, name: str) -> bool:
    return any(
        isinstance(node, ast.Call) and _call_name(node) == name for node in ast.walk(ast.parse(source))
    )


def _registry_violations(callers: set[str]) -> tuple[set[str], set[str]]:
    """（未登记的调用方, 已过期的登记项）——与 ``test_open_exchange_callers_are_registered`` 同一判定。"""

    return callers - set(OPEN_EXCHANGE_CALLERS), set(OPEN_EXCHANGE_CALLERS) - callers


def test_open_exchange_callers_are_registered() -> None:
    """P7-S11-A04 / SDD17-I08 ``open_exchange(`` 的调用方 ∈ 登记表（AST 判定真实调用）；登记项不再对应真实代码同样判失败。

    ``open_exchange`` 是出站内核而非原始网络原语，所以模块级静态扫描（``_EGRESS_PATTERNS``）管不到"谁能调用它"。
    这条不变量补上：任何新的调用方都必须先登记（并同时是治理清单里的出站站点），否则红灯。
    """

    unregistered, stale = _registry_violations(_callers("open_exchange"))
    assert not unregistered, f"未登记的 open_exchange 调用方：{sorted(unregistered)}"
    assert not stale, f"登记项已不再调用 open_exchange，应删除：{sorted(stale)}"


def test_open_exchange_registry_goes_red_for_an_unregistered_caller_and_a_stale_entry() -> None:
    """P7-S11-A04 / SDD17-I08 反证：往真实调用方集合里加一个未登记模块 → 判定报出它；去掉一个真实调用方 → 登记项被判过期。"""

    real = _callers("open_exchange")
    assert _calls_in("from x import open_exchange\nopen_exchange(1)\n", "open_exchange")
    assert not _calls_in("def open_exchange():\n    pass\n", "open_exchange"), "只定义不调用不算调用方"

    unregistered, _ = _registry_violations(real | {"plugins/rogue.py"})
    assert unregistered == {"plugins/rogue.py"}

    _, stale = _registry_violations(real - {"external/http_get.py"})
    assert stale == {"external/http_get.py"}


def _destination_offenders(specs: list[Any]) -> list[str]:
    offenders: list[str] = []
    for spec in specs:
        for name in getattr(spec.args_model, "model_fields", {}):
            if name.lower() in FORBIDDEN_ARG_NAMES:
                offenders.append(f"{spec.name}.{name}")
    return offenders


def _local_tool_specs() -> list[Any]:
    from water_agent.plugins import McpGatewayPlugin, builtin_plugins
    from water_agent.plugins.base import AssemblyContext
    from water_agent.retrieval.in_memory import InMemoryRetriever

    ctx = AssemblyContext(retriever=InMemoryRetriever([]), cite_context_source=lambda: set(), drawing_root="")
    specs: list[Any] = []
    for plugin in builtin_plugins(mcp=McpGatewayPlugin.borrowing(None)):
        if plugin.plugin_id == "mcp_gateway":  # 远程工具的参数由远端 schema 子集决定，另有安检
            continue
        specs.extend(plugin.contribute(ctx).tools)
    return specs


def test_local_tools_take_no_destination_argument() -> None:
    """P7-S11-A04 / SDD17-I09 本地 ``ToolSpec`` 的 ``args_model`` 不含 ``url`` / ``uri`` / ``href`` / ``endpoint`` / ``host`` 字段：
    模型只控制查询字符串，不控制目的地（出口由运维写死）。"""

    specs = _local_tool_specs()
    # 防止判定空转：确实遍历到了本地工具（检索 / 引用 / 流速至少三个）
    assert {"retrieve_clauses", "cite_clauses", "calc_velocity"} <= {spec.name for spec in specs}
    assert _destination_offenders(specs) == []


def test_destination_argument_check_catches_a_url_parameter() -> None:
    """P7-S11-A04 / SDD17-I09 反证：给一个带 ``url`` 参数的假工具，同一判定必须报出它。"""

    from pydantic import BaseModel

    class _Args(BaseModel):
        url: str

    class _Spec:
        name = "rogue_fetch"
        args_model = _Args

    assert _destination_offenders([_Spec()]) == ["rogue_fetch.url"]
