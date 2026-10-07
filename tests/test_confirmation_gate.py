"""P7-S7：危险操作人工确认门（``docs/design/16-p7-plugin-mcp.md`` §3.7、§7 S7）。

需确认的工具在 ToolNode 里是 ``return_direct`` 延迟工具（不执行、内层循环随即结束），外层图的
``confirmation_gate`` 挂起；批准后凭一次性凭证经 ``ToolRegistry.invoke_confirmed`` 执行一次。
端到端用例走生产 ``AgenticSession(confirmation=True)`` + 脚本模型 + ``fake_mcp_server.py --mode
stateful``（``bump`` 是有副作用的进程内计数器），证明"批准恰好执行一次、拒绝零次"。
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from pydantic import BaseModel

from water_agent.graphs.agent_rag import AgenticRAGState, _BudgetChatModel
from water_agent.harness.doubles import ScriptedChatModel
from water_agent.plugins import AssemblyContext, PluginContribution, assemble_tools
from water_agent.plugins.exposure import bound_tool_definition
from water_agent.runtime.governance.contracts import Capability, Principal
from water_agent.runtime.governance.inventory import McpToolBinding, register_mcp_tool_binding
from water_agent.tools.base import ToolSpec
from water_agent.tools.ds_adapters import (
    PENDING_CONFIRMATION_ARTIFACT,
    PENDING_CONFIRMATION_STATUS,
    tool_spec_to_base_tool,
)

_DESCRIPTION = (
    "合成 fixture 工具（P7-S7 测试用）。Use when 测试确认门的装配与执行语义。"
    "Do not use for 任何真实问答。"
)


class _NoteArgs(BaseModel):
    note: str = ""


def _counting_spec(name: str, counter: dict[str, int]) -> ToolSpec:
    def execute(args: _NoteArgs) -> dict[str, Any]:
        counter[name] = counter.get(name, 0) + 1
        return {"ok": True, "note": args.note, "count": counter[name]}

    return ToolSpec(name=name, description=_DESCRIPTION, args_model=_NoteArgs, execute=execute)


class _FixturePlugin:
    def __init__(self, specs: tuple[ToolSpec, ...]) -> None:
        self.plugin_id = "s7_fixture"
        self._specs = specs

    def contribute(self, ctx: AssemblyContext) -> PluginContribution:
        del ctx
        return PluginContribution(
            plugin_id=self.plugin_id,
            tools=self._specs,
            grants=frozenset({Capability.MCP_INVOKE}),
        )


class _NoRetriever:
    def retrieve(self, query: str, top_k: int = 5) -> list[Any]:  # pragma: no cover - 不会被调用
        del query, top_k
        return []


@pytest.fixture
def fixture_assembly() -> Iterator[tuple[Any, dict[str, int]]]:
    """一台合成 server ``s7a01``：``bump`` 要求确认、``read`` 不要求（运行时绑定只增不删，独立 id）。"""

    for remote, confirm in (("bump", True), ("read", False)):
        register_mcp_tool_binding(
            McpToolBinding(
                tool_name=f"mcp_s7a01_{remote}",
                server_id="s7a01",
                remote_tool=remote,
                capability=Capability.MCP_INVOKE,
                requires_human_confirmation=confirm,
            )
        )
    counter: dict[str, int] = {}
    specs = (
        _counting_spec("mcp_s7a01_bump", counter),
        _counting_spec("mcp_s7a01_read", counter),
    )
    assembly = assemble_tools(
        AssemblyContext(retriever=_NoRetriever()),  # type: ignore[arg-type]
        base_principal=Principal.local("s7-a01"),
        trace=None,
        plugins=[_FixturePlugin(specs)],
    )
    yield assembly, counter


def test_deferred_tool_never_executes_and_ends_the_inner_loop(
    fixture_assembly: tuple[Any, dict[str, int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """P7-S7-A01：要求确认的工具在 ToolNode 里是 ``return_direct`` 延迟工具——模型看到的定义与普通
    工具逐字相同；执行时不调用注册表、不碰 MCP（计数为 0），只回带 ``confirmation_id`` 的待确认结果
    （标记在 artifact 里）；内层 react 循环在它之后结束、不再调模型。

    同一条消息里并行的不需确认的工具照常执行一次（ToolNode 按 ``Send`` 并行执行，模型并行发出的调用
    之间本就没有顺序约定）——这是实测行为，写进决策记录。
    """

    assembly, counter = fixture_assembly
    assert assembly.confirmation_tools == frozenset({"mcp_s7a01_bump"})
    tools = {tool.name: tool for tool in assembly.tools}
    deferred = tools["mcp_s7a01_bump"]
    assert deferred.return_direct is True
    assert tools["mcp_s7a01_read"].return_direct is False
    reference = tool_spec_to_base_tool(assembly.registry, "mcp_s7a01_bump")
    assert bound_tool_definition(deferred) == bound_tool_definition(reference)
    assert getattr(deferred, "registry", None) is assembly.registry

    invoked: list[str] = []
    real_invoke = assembly.registry.invoke

    def spy_invoke(name: str, **args: Any) -> dict[str, Any]:
        invoked.append(name)
        return real_invoke(name, **args)

    monkeypatch.setattr(assembly.registry, "invoke", spy_invoke)

    message = deferred.invoke(
        {"type": "tool_call", "name": "mcp_s7a01_bump", "args": {"note": "a"}, "id": "call-direct"}
    )
    assert isinstance(message, ToolMessage)
    payload = json.loads(message.content)
    assert payload["status"] == PENDING_CONFIRMATION_STATUS
    assert payload["tool"] == "mcp_s7a01_bump"
    assert message.artifact == {PENDING_CONFIRMATION_ARTIFACT: payload["confirmation_id"]}
    assert counter.get("mcp_s7a01_bump", 0) == 0
    assert invoked == []

    from langgraph.prebuilt import create_react_agent

    scripted = ScriptedChatModel(
        steps=[
            {
                "tool_calls": [
                    {"name": "mcp_s7a01_read", "args": {"note": "r"}},
                    {"name": "mcp_s7a01_bump", "args": {"note": "b"}},
                ]
            }
        ]
    )
    holder: dict[str, Any] = {"turns": 0}
    budget = _BudgetChatModel(inner=scripted, max_tool_turns=5, holder=holder)
    inner = create_react_agent(
        model=budget,
        tools=assembly.tools,
        state_schema=AgenticRAGState,
        checkpointer=False,
    )
    result = inner.invoke({"messages": [HumanMessage(content="问题")]})

    assert scripted.state["calls"] == 1, "延迟工具之后内层循环不得再调模型"
    assert counter.get("mcp_s7a01_bump", 0) == 0
    assert counter.get("mcp_s7a01_read", 0) == 1
    assert invoked == ["mcp_s7a01_read"]
    tool_messages = {m.name: m for m in result["messages"] if isinstance(m, ToolMessage)}
    pending = tool_messages["mcp_s7a01_bump"]
    assert json.loads(pending.content)["status"] == PENDING_CONFIRMATION_STATUS
    assert PENDING_CONFIRMATION_ARTIFACT in (pending.artifact or {})
