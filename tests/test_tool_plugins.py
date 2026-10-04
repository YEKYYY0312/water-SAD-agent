"""P7-S2：工具插件契约与单一装配入口（``docs/design/16-p7-plugin-mcp.md`` §3）。"""
from __future__ import annotations

import json
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from water_agent.drawing.synth import build_synthetic_drawing
from water_agent.mcp.config import MCPGatewayConfig, MCPServerConfig
from water_agent.mcp.wiring import MCPGatewayAssembly
from water_agent.plugins import (
    BUILTIN_PLUGIN_ORDER,
    AssemblyContext,
    ClausesPlugin,
    DrawingContribution,
    DrawingPlugin,
    McpContribution,
    McpGatewayPlugin,
    PluginContribution,
    assemble_tools,
    builtin_plugins,
)
from water_agent.retrieval.loader import load_clauses_jsonl
from water_agent.retrieval.trivium import TriviumRetriever
from water_agent.runtime.governance.contracts import Capability, Principal
from water_agent.runtime.governance.inventory import clear_mcp_tool_bindings
from water_agent.tools.calc_velocity import calc_velocity_spec

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "data" / "fixtures" / "rag_fixture.jsonl"
FAKE_SERVER = REPO_ROOT / "tests" / "fixtures" / "fake_mcp_server.py"
BOGUS_ARTICLE = "GB00000-0000 9.9.9"


@pytest.fixture(autouse=True)
def _reset_bindings():
    clear_mcp_tool_bindings()
    yield
    clear_mcp_tool_bindings()


@pytest.fixture
def ctx() -> AssemblyContext:
    retriever = TriviumRetriever(db_path=None, fallback_clauses=load_clauses_jsonl(FIXTURE))
    return AssemblyContext(retriever=retriever, drawing_root="")


def _fake_config(server_id: str = "plugintest") -> MCPGatewayConfig:
    return MCPGatewayConfig(
        servers=(
            MCPServerConfig(
                server_id=server_id,
                command=sys.executable,
                args=(str(FAKE_SERVER), "--mode", "ok"),
                allow=("echo_query", "lookup_project"),
            ),
        )
    )


def _principal() -> Principal:
    return Principal.local("plugin-test")


class _StubPlugin:
    def __init__(self, plugin_id: str, contribution: PluginContribution | None = None,
                 error: Exception | None = None) -> None:
        self.plugin_id = plugin_id
        self._contribution = contribution
        self._error = error

    def contribute(self, ctx: AssemblyContext) -> PluginContribution:
        if self._error is not None:
            raise self._error
        assert self._contribution is not None
        return self._contribution


# ---------------------------------------------------------------------------
# 内置插件
# ---------------------------------------------------------------------------


def test_clauses_plugin_couples_retrieve_and_cite(ctx: AssemblyContext) -> None:
    """P7-S2-A01 clauses 插件按 retrieve → cite 的顺序贡献两者，且两者共享"本轮检索到了什么"。"""

    contribution = ClausesPlugin().contribute(ctx)
    assert [tool.name for tool in contribution.tools] == ["retrieve_clauses", "cite_clauses"]

    assembly = assemble_tools(ctx, base_principal=_principal(), trace=None, plugins=[ClausesPlugin()])
    hits = assembly.registry.invoke("retrieve_clauses", query="给水管道设计秒流量", top_k=2)
    real = hits["hits"][0]["source_id"]
    cited = assembly.registry.invoke("cite_clauses", article_ids=[real, BOGUS_ARTICLE])
    assert cited["registered"] is False
    assert cited["invalid_ids"] == [BOGUS_ARTICLE]


def test_clauses_plugin_forwards_context_source(ctx: AssemblyContext) -> None:
    """P7-S2-A01 cite 的可追溯集合包含 ``cite_context_source`` 给出的条款（跨 run 的 thread 历史）。"""

    remembered = "GB50015-2019 3.1.1"
    with_source = AssemblyContext(
        retriever=ctx.retriever, cite_context_source=lambda: {remembered}, drawing_root=""
    )
    assembly = assemble_tools(with_source, base_principal=_principal(), trace=None,
                              plugins=[ClausesPlugin()])
    accepted = assembly.registry.invoke("cite_clauses", article_ids=[remembered])
    assert accepted["registered"] is True
    rejected = assembly.registry.invoke("cite_clauses", article_ids=[BOGUS_ARTICLE])
    assert rejected["invalid_ids"] == [BOGUS_ARTICLE]


def test_drawing_plugin_passes_root_verbatim(
    ctx: AssemblyContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P7-S2-A02 drawing 插件原样透传 drawing_root：``""`` 关闭，``None`` 读 DRAWING_ROOT。"""

    drawings = tmp_path / "drawings"
    drawings.mkdir()
    build_synthetic_drawing(drawings, drawing_id="P-01")
    monkeypatch.setenv("DRAWING_ROOT", str(drawings))

    off = DrawingPlugin().contribute(ctx)
    assert off.status == "skipped" and off.tools == ()
    assert off.resolution.status == "NOT_CONFIGURED"

    from_env = DrawingPlugin().contribute(
        AssemblyContext(retriever=ctx.retriever, drawing_root=None)
    )
    assert from_env.status == "active"
    assert [tool.name for tool in from_env.tools] == ["read_drawing"]
    assert from_env.resolution.drawing_root == str(drawings)


# ---------------------------------------------------------------------------
# MCP 插件的两种来源
# ---------------------------------------------------------------------------


def test_mcp_owning_closes_on_stack_exit(ctx: AssemblyContext) -> None:
    """P7-S2-A03 owning：打开后立即把关闭登记到 stack，stack 退出时子进程被收掉。"""

    with ExitStack() as stack:
        plugin = McpGatewayPlugin.owning(_fake_config(), stack=stack)
        contribution = plugin.contribute(ctx)
        sessions = contribution.assembly.sessions
        assert [tool.name for tool in contribution.tools] == [
            "mcp_plugintest_echo_query", "mcp_plugintest_lookup_project"
        ]
        assert contribution.grants == {Capability.MCP_INVOKE}
        assert all(session.client is not None for session in sessions)
    assert sessions and all(session.client is None for session in sessions)


def test_mcp_owning_is_single_use(ctx: AssemblyContext) -> None:
    """P7-S2-A03 owning 只能装配一次：同一批子进程不能交给两次装配。"""

    with ExitStack() as stack:
        plugin = McpGatewayPlugin.owning(None, stack=stack)
        empty = plugin.contribute(ctx)
        assert empty.tools == () and empty.grants == frozenset()
        with pytest.raises(RuntimeError, match="只能装配一次"):
            plugin.contribute(ctx)


def test_mcp_borrowing_never_closes(ctx: AssemblyContext) -> None:
    """P7-S2-A03 borrowing 使用调用方已打开的装配，从不打开也不关闭。"""

    closed: list[bool] = []

    class _BorrowedAssembly:
        """记录 close 调用的装配替身（真实装配是 slots dataclass，不能逐实例打补丁）。"""

        sessions = (
            SimpleNamespace(
                result=SimpleNamespace(
                    tools=(calc_velocity_spec.model_copy(update={"name": "mcp_borrowed_echo"}),)
                )
            ),
        )

        def close(self) -> None:
            closed.append(True)

    assembly = _BorrowedAssembly()
    contribution = McpGatewayPlugin.borrowing(assembly).contribute(ctx)  # type: ignore[arg-type]
    assert [item.name for item in contribution.tools] == ["mcp_borrowed_echo"]
    assert contribution.assembly is assembly
    assert closed == []
    empty = McpGatewayPlugin.borrowing(None).contribute(ctx)
    assert empty.tools == () and isinstance(empty.assembly, MCPGatewayAssembly)


# ---------------------------------------------------------------------------
# 授权
# ---------------------------------------------------------------------------


def test_grant_only_with_contributed_tools(ctx: AssemblyContext) -> None:
    """P7-S2-A04 只有确实贡献了工具的插件，其 grants 才生效；否则原样返回基础主体。"""

    base = _principal()
    no_tools = _StubPlugin(
        "stub", PluginContribution(plugin_id="stub", grants=frozenset({Capability.MCP_INVOKE}))
    )
    assembly = assemble_tools(ctx, base_principal=base, trace=None, plugins=[no_tools])
    assert assembly.principal is base

    with_tools = _StubPlugin(
        "stub",
        PluginContribution(
            plugin_id="stub",
            tools=(calc_velocity_spec,),
            grants=frozenset({Capability.MCP_INVOKE}),
        ),
    )
    granted = assemble_tools(ctx, base_principal=base, trace=None, plugins=[with_tools]).principal
    assert Capability.MCP_INVOKE in granted.capabilities
    assert Capability.MCP_INVOKE not in base.capabilities


def test_grant_preserves_kind_and_auth_domain(ctx: AssemblyContext) -> None:
    """P7-S2-A04 授权保持主体的 kind / subject / auth_domain，只叠加能力。"""

    base = Principal.authenticated(
        subject="user:plugin-test",
        auth_domain="tenant:default",
        capabilities=frozenset({Capability.CALCULATE}),
    )
    plugin = _StubPlugin(
        "stub",
        PluginContribution(
            plugin_id="stub",
            tools=(calc_velocity_spec,),
            grants=frozenset({Capability.MCP_INVOKE}),
        ),
    )
    granted = assemble_tools(ctx, base_principal=base, trace=None, plugins=[plugin]).principal
    assert (granted.kind, granted.subject, granted.auth_domain) == (
        base.kind, base.subject, base.auth_domain
    )
    assert granted.capabilities == {Capability.CALCULATE, Capability.MCP_INVOKE}


def test_grants_outside_whitelist_rejected(ctx: AssemblyContext) -> None:
    """P7-S2-A04 插件申请白名单之外的能力（例如读图）一律拒绝——插件不是权限主体。"""

    plugin = _StubPlugin(
        "stub",
        PluginContribution(
            plugin_id="stub",
            tools=(calc_velocity_spec,),
            grants=frozenset({Capability.READ_DRAWING}),
        ),
    )
    with pytest.raises(ValueError, match="不可授予的能力"):
        assemble_tools(ctx, base_principal=_principal(), trace=None, plugins=[plugin])


# ---------------------------------------------------------------------------
# 装配语义
# ---------------------------------------------------------------------------


def test_plugin_exception_propagates(ctx: AssemblyContext) -> None:
    """P7-S2-A05 插件异常原样上抛，装配不吞错、不降级。"""

    boom = _StubPlugin("boom", error=RuntimeError("插件装配失败"))
    with pytest.raises(RuntimeError, match="插件装配失败"):
        assemble_tools(ctx, base_principal=_principal(), trace=None,
                       plugins=[ClausesPlugin(), boom])


def test_duplicate_plugin_ids_rejected(ctx: AssemblyContext) -> None:
    """P7-S2-A05 插件 id 重复、或贡献里的 id 与插件不符，都拒绝。"""

    with pytest.raises(ValueError, match="插件 id 重复"):
        assemble_tools(ctx, base_principal=_principal(), trace=None,
                       plugins=[ClausesPlugin(), ClausesPlugin()])
    liar = _StubPlugin("stub", PluginContribution(plugin_id="other"))
    with pytest.raises(ValueError, match="返回了 plugin_id"):
        assemble_tools(ctx, base_principal=_principal(), trace=None, plugins=[liar])


def test_tools_sorted_and_bound_to_registry(ctx: AssemblyContext) -> None:
    """P7-S2-A05 交给模型的工具按字母序、全部绑定同一个注册表；注册顺序按插件顺序。"""

    with ExitStack() as stack:
        assembly = assemble_tools(
            ctx,
            base_principal=_principal(),
            trace=None,
            plugins=builtin_plugins(mcp=McpGatewayPlugin.owning(None, stack=stack)),
        )
    names = [tool.name for tool in assembly.tools]
    assert names == sorted(names) == assembly.registry.list_all_names()
    assert all(getattr(tool, "registry", None) is assembly.registry for tool in assembly.tools)
    registered = [entry["function"]["name"] for entry in assembly.registry.list_tools()]
    assert registered == ["retrieve_clauses", "cite_clauses", "calc_velocity", "material_stats"]
    assert tuple(item.plugin_id for item in assembly.contributions) == BUILTIN_PLUGIN_ORDER
    assert assembly.contribution(DrawingContribution).status == "skipped"
    assert assembly.contribution(McpContribution).tools == ()
    assert assembly.registered("mcp_gateway") == []


# ---------------------------------------------------------------------------
# runner real-agentic 路径统一成会话语义（规格 §3.5）
# ---------------------------------------------------------------------------


def _runner(tmp_path: Path) -> Any:
    from water_agent.harness.runner import HarnessRunner

    eval_path = tmp_path / "unused.jsonl"
    eval_path.write_text("", encoding="utf-8")
    runner = HarnessRunner(eval_path=eval_path, report_dir=tmp_path / "r",
                           trace_dir=tmp_path / "t", fixture_path=FIXTURE)
    runner._real_retriever = TriviumRetriever(
        db_path=None, fallback_clauses=load_clauses_jsonl(FIXTURE)
    )
    return runner


class _Captured(Exception):
    pass


def _capture_runner_app(monkeypatch: pytest.MonkeyPatch, sink: dict[str, Any]) -> None:
    import water_agent.harness.runner as runner_module

    def capture(*args: Any, **kwargs: Any) -> Any:
        sink.update(kwargs)
        sink["chat_model_context"] = getattr(kwargs["model"], "request_context", None)
        raise _Captured()

    monkeypatch.setattr(runner_module, "build_agentic_app", capture)


def _mcp_config_file(tmp_path: Path, server_id: str) -> Path:
    path = tmp_path / "mcp_servers.json"
    path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "server_id": server_id,
                        "command": sys.executable,
                        "args": [str(FAKE_SERVER), "--mode", "ok"],
                        "allow": ["echo_query", "lookup_project"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def test_runner_mcp_opened_and_closed_per_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P7-S2-A07 runner：MCP 每条用例现开现关，用例结束（含异常结束）后不留子进程。"""

    import water_agent.plugins.mcp_gateway as gateway_module

    opened: list[Any] = []
    original_open = gateway_module.open_gateway_sessions

    def spy_open(config: Any) -> Any:
        assembly = original_open(config)
        opened.append(assembly)
        return assembly

    monkeypatch.setattr(gateway_module, "open_gateway_sessions", spy_open)
    monkeypatch.setenv("WATER_AGENT_MCP_CONFIG", str(_mcp_config_file(tmp_path, "runnerpc")))
    monkeypatch.delenv("DRAWING_ROOT", raising=False)
    sink: dict[str, Any] = {}
    _capture_runner_app(monkeypatch, sink)
    runner = _runner(tmp_path)
    runner._real_chat_model = SimpleNamespace(request_context=None, gateway=None)

    for _ in range(2):
        with pytest.raises(_Captured):
            runner._run_real_agentic_case(SimpleNamespace(id="c", query="q"))

    assert len(opened) == 2, "每条用例都要重新打开 MCP 装配，不复用模块级缓存"
    assert all(session.client is None for assembly in opened for session in assembly.sessions)
    assert "mcp_runnerpc_echo_query" in sink["prompt"]._tool_registry.list_all_names()


def test_runner_mcp_register_fault_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P7-S2-A07 runner：MCP 工具注册出错时异常上抛，不再悄悄降级为纯本地工具。"""

    from water_agent.tools.registry import ToolRegistry

    original_register = ToolRegistry.register

    def faulty(self: ToolRegistry, spec: Any) -> None:
        if spec.name.startswith("mcp_"):
            raise ValueError("注入的 MCP 注册故障")
        original_register(self, spec)

    monkeypatch.setattr(ToolRegistry, "register", faulty)
    monkeypatch.setenv("WATER_AGENT_MCP_CONFIG", str(_mcp_config_file(tmp_path, "runnerfault")))
    sink: dict[str, Any] = {}
    _capture_runner_app(monkeypatch, sink)
    runner = _runner(tmp_path)
    runner._real_chat_model = SimpleNamespace(request_context=None, gateway=None)

    with pytest.raises(ValueError, match="注入的 MCP 注册故障"):
        runner._run_real_agentic_case(SimpleNamespace(id="c", query="q"))
    assert sink == {}, "装配失败后不得继续构图"


def test_runner_chat_model_sees_granted_principal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P7-S2-A07 runner：chat model 拿到的是授权后的主体，与交给图的主体一致。"""

    monkeypatch.setenv("WATER_AGENT_MCP_CONFIG", str(_mcp_config_file(tmp_path, "runnergrant")))
    sink: dict[str, Any] = {}
    _capture_runner_app(monkeypatch, sink)
    runner = _runner(tmp_path)
    runner._real_chat_model = SimpleNamespace(request_context=None, gateway=None)

    with pytest.raises(_Captured):
        runner._run_real_agentic_case(SimpleNamespace(id="c", query="q"))

    graph_principal = sink["request_context"].principal
    assert Capability.MCP_INVOKE in graph_principal.capabilities
    assert sink["chat_model_context"].principal == graph_principal


def test_runner_cite_uses_thread_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S2-A07 runner：cite 的可追溯集合读本用例 thread 的 checkpoint（与会话路径同语义）。

    单轮用例里，进程内累积（retrieve 回填 ``context_updater``）与 checkpoint 两个来源给出的
    结果相同，端到端跑一遍分不出 runner 有没有接上 thread 来源（实测：把它改成 ``None``
    端到端用例照样通过）。这里直接拿 runner 交给 clauses 插件的 ``cite_context_source``：
    图构好之前返回空集合；用 runner 自己构的图往**本用例 thread** 写入一条检索结果后，
    它必须读到那条结果里的 source_id。
    """

    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    import water_agent.harness.runner as runner_module
    import water_agent.plugins.builtin as builtin_module

    monkeypatch.setattr(
        "water_agent.mcp.config.default_config_path", lambda **_: tmp_path / "absent.json"
    )
    sources: list[Any] = []
    original_check = builtin_module.build_cite_clauses_tool_with_context_check

    def spy_check(*args: Any, **kwargs: Any) -> Any:
        sources.append(kwargs.get("context_source"))
        return original_check(*args, **kwargs)

    monkeypatch.setattr(builtin_module, "build_cite_clauses_tool_with_context_check", spy_check)
    seen: dict[str, Any] = {}
    original_build = runner_module.build_agentic_app

    def spy_build(*args: Any, **kwargs: Any) -> Any:
        app = original_build(*args, **kwargs)
        original_invoke = app.invoke

        def invoke_and_probe(payload: Any, config: Any = None, **rest: Any) -> Any:
            context_source = sources[-1]
            seen["before_writes"] = context_source()
            app.update_state(
                config,
                {
                    "messages": [
                        HumanMessage(content="q"),
                        AIMessage(content="", tool_calls=[
                            {"name": "retrieve_clauses", "args": {"query": "q"}, "id": "t1"}
                        ]),
                        ToolMessage(
                            content=json.dumps({"ok": True, "hits": [{"source_id": "GB50015-2019 3.1.1"}]}),
                            name="retrieve_clauses",
                            tool_call_id="t1",
                        ),
                    ]
                },
            )
            seen["after_write"] = context_source()
            return original_invoke(payload, config=config, **rest)

        app.invoke = invoke_and_probe  # type: ignore[method-assign]
        return app

    monkeypatch.setattr(runner_module, "build_agentic_app", spy_build)
    runner = _runner(tmp_path)
    runner._real_chat_model = FakeMessagesListChatModel(
        responses=[AIMessage(content="设计秒流量按用水定额与用水人数计算。")]
    )
    from water_agent.domain.models import EvalCase

    runner._run_real_agentic_case(EvalCase(id="cite-ctx", query="q", variant="real"))

    assert len(sources) == 1 and sources[0] is not None, "runner 没有给 clauses 插件接 thread 来源"
    assert seen["before_writes"] == set()
    assert seen["after_write"] == {"GB50015-2019 3.1.1"}
