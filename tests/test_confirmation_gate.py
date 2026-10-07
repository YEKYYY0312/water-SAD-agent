"""P7-S7：危险操作人工确认门（``docs/design/16-p7-plugin-mcp.md`` §3.7、§7 S7）。

需确认的工具在 ToolNode 里是 ``return_direct`` 延迟工具（不执行、内层循环随即结束），外层图的
``confirmation_gate`` 挂起；批准后凭一次性凭证经 ``ToolRegistry.invoke_confirmed`` 执行一次。
端到端用例走生产 ``AgenticSession(confirmation=True)`` + 脚本模型 + ``fake_mcp_server.py --mode
stateful``（``bump`` 是有副作用的进程内计数器），证明"批准恰好执行一次、拒绝零次"。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

from water_agent.graphs.agent_rag import AgenticRAGState, _BudgetChatModel
from water_agent.graphs.confirmation import MAX_CONFIRMATIONS_PER_TURN, STATUS_WAITING_CONFIRMATION
from water_agent.harness.doubles import ScriptedChatModel
from water_agent.mcp.config import MCP_CONFIG_VERSION
from water_agent.plugins import AssemblyContext, PluginContribution, assemble_tools
from water_agent.plugins.exposure import bound_tool_definition
from water_agent.retrieval.trivium import TriviumRetriever
from water_agent.runtime.agentic import AgenticSession
from water_agent.runtime.governance.contracts import Capability, Principal, confirmation_args_hash
from water_agent.runtime.governance.inventory import McpToolBinding, register_mcp_tool_binding
from water_agent.session import default_checkpoint_serde
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


# ---------------------------------------------------------------------------
# 外层确认门：生产 AgenticSession(confirmation=True) + 脚本模型 + fake server（stateful）
# ---------------------------------------------------------------------------

FAKE_SERVER = Path(__file__).resolve().parent / "fixtures" / "fake_mcp_server.py"
SERVER = "s7gate"
BUMP = f"mcp_{SERVER}_bump"


@pytest.fixture
def gate_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """一台要求人工确认的 server（只放行 ``bump``）；本机覆盖层 / 暴露策略 / 图纸目录一律清掉。"""

    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "version": MCP_CONFIG_VERSION,
                "servers": [
                    {
                        "server_id": SERVER,
                        "command": sys.executable,
                        "args": [str(FAKE_SERVER), "--mode", "stateful"],
                        "allow": ["bump"],
                        "human_confirmation": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    for key in ("WATER_AGENT_MCP_OVERLAY", "WATER_AGENT_TOOL_EXPOSURE", "DRAWING_ROOT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WATER_AGENT_MCP_CONFIG", str(config))
    return tmp_path


def _session_args(workdir: Path) -> argparse.Namespace:
    # 与 HarnessRunner._agentic_args 同形：mock 生成器、不走真实索引、不注册图纸工具
    return argparse.Namespace(
        db_path=None,
        dimensions=None,
        rerank="local",
        rerank_model=None,
        rerank_base_url=None,
        rerank_api_key=None,
        top_k=5,
        recall_top_k=20,
        hybrid_alpha=0.7,
        model="deepseek-chat",
        base_url=None,
        api_key_env="DEEPSEEK_API_KEY",
        api_key=None,
        mock_generator=True,
        session_dir=str(workdir / "sessions"),
        trace_dir=str(workdir / "traces"),
        stream=False,
        drawing_root="",
    )


def _open_session(
    stack: ExitStack,
    workdir: Path,
    steps: list[dict[str, Any]],
    *,
    saver: Any | None = None,
    thread_id: str | None = None,
) -> tuple[AgenticSession, ScriptedChatModel]:
    model = ScriptedChatModel(steps=steps)
    session = AgenticSession(
        _session_args(workdir),
        stack=stack,
        thread_id=thread_id or uuid4().hex,
        checkpointer=saver if saver is not None else InMemorySaver(serde=default_checkpoint_serde()),
        model=model,
        retriever=TriviumRetriever(db_path=None, fallback_clauses=[]),
        confirmation=True,
    )
    return session, model


def _peek(session: AgenticSession) -> int:
    """直接读 fake server 进程内的计数器（不经注册表，只读，不改变它）。"""

    client = session.mcp_assembly.sessions[0].client
    assert client is not None
    return int(client.call_tool("peek", {})["structured_content"]["counter"])


def _bump(note: str) -> dict[str, Any]:
    return {"name": BUMP, "args": {"note": note}}


def _decide(request: dict[str, Any], approved: bool, reason: str | None = None) -> dict[str, Any]:
    return {"confirmation_id": request["confirmation_id"], "approved": approved, "reason": reason}


def _tool_payload(messages: list[Any], name: str = BUMP) -> list[dict[str, Any]]:
    return [json.loads(m.content) for m in messages if isinstance(m, ToolMessage) and m.name == name]


def test_approve_executes_exactly_once(gate_workdir: Path) -> None:
    """P7-S7-A03：挂起时副作用未发生（计数 0、模型只被调了一次）；批准后恰好执行一次（计数 1），
    执行的就是批准的参数，模型下一步看到真实结果、看不到待确认占位消息。"""

    with ExitStack() as stack:
        session, model = _open_session(
            stack,
            gate_workdir,
            [{"tool_calls": [_bump("approve-note")]}, {"content": "已按你的确认把计数器加一。"}],
        )
        assert BUMP in session.mcp_tool_names
        first = asyncio.run(session.aask("请把计数器加一", run_id=uuid4().hex))
        assert first["status"] == STATUS_WAITING_CONFIRMATION, first
        request = first["confirmation"]
        assert request["tool_name"] == BUMP
        assert request["server_id"] == SERVER
        assert request["remote_tool"] == "bump"
        assert request["arguments"] == {"note": "approve-note"}
        assert first["answer"] == ""
        assert _peek(session) == 0
        assert model.state["calls"] == 1

        final = asyncio.run(session.aresume(_decide(request, True)))
        assert final["status"] == "succeeded", final
        assert _peek(session) == 1
        assert model.state["calls"] == 2
        seen = model.state["received"][1]
        results = _tool_payload(seen)
        assert len(results) == 1
        assert results[0]["structured_content"]["counter"] == 1
        assert results[0]["structured_content"]["note"] == "approve-note"
        assert all("pending_confirmation" not in str(m.content) for m in seen)
        assert session.pending_confirmation() is None
        log = session.app.get_state(session.config).values["confirmation_log"]
        assert [entry["decision"] for entry in log] == ["approved"]
        assert log[0]["result_code"] == "ok"


def test_reject_executes_nothing(gate_workdir: Path) -> None:
    """P7-S7-A03：拒绝零次执行（计数仍为 0），待确认消息同 id 替换为拒绝 observation，模型据此作答。"""

    with ExitStack() as stack:
        session, model = _open_session(
            stack,
            gate_workdir,
            [{"tool_calls": [_bump("reject-note")]}, {"content": "你拒绝了这次操作，计数器没有改动。"}],
        )
        first = asyncio.run(session.aask("请把计数器加一", run_id=uuid4().hex))
        request = first["confirmation"]
        final = asyncio.run(session.aresume(_decide(request, False, "不需要")))
        assert final["status"] == "succeeded", final
        assert _peek(session) == 0
        rejected = _tool_payload(model.state["received"][1])
        assert len(rejected) == 1
        assert rejected[0]["code"] == "TOOL_CONFIRMATION_REJECTED"
        assert rejected[0]["error"] is True
        messages = session.app.get_state(session.config).values["messages"]
        ids = [m.id for m in messages if isinstance(m, ToolMessage) and m.name == BUMP]
        assert len(ids) == len(set(ids)) == 1, "拒绝是同 id 替换，不是追加"
        log = session.app.get_state(session.config).values["confirmation_log"]
        assert log[0]["decision"] == "rejected"
        assert log[0]["result_code"] == "not_executed"


def test_invalid_resume_does_not_poison_the_session(gate_workdir: Path) -> None:
    """P7-S7-A03：非法 resume 值（``"yes"``、``confirmation_id`` 不匹配、``approved`` 不是严格 bool）
    不抛异常、不算决定：会话仍挂起并带 error，之后合法值照常生效、恰好执行一次。

    同一条 AI 消息里两条需确认调用：两次挂起、逐条决定，两次决定之间模型不被调用。"""

    with ExitStack() as stack:
        session, model = _open_session(
            stack,
            gate_workdir,
            [{"tool_calls": [_bump("one"), _bump("two")]}, {"content": "第一次已执行，第二次被你拒绝。"}],
        )
        first = asyncio.run(session.aask("请把计数器加两次", run_id=uuid4().hex))
        request = first["confirmation"]
        assert request["arguments"] == {"note": "one"}

        for bad in (
            "yes",
            {"confirmation_id": "not-the-current-one", "approved": True, "reason": None},
            {"confirmation_id": request["confirmation_id"], "approved": "true", "reason": None},
        ):
            again = asyncio.run(session.aresume(bad))  # type: ignore[arg-type]
            assert again["status"] == STATUS_WAITING_CONFIRMATION, again
            assert again["confirmation"]["confirmation_id"] == request["confirmation_id"]
            assert again["confirmation"].get("error"), bad
            assert _peek(session) == 0
        assert session.app.get_state(session.config).values.get("confirmation_log") in (None, [])

        second = asyncio.run(session.aresume(_decide(request, True)))
        assert second["status"] == STATUS_WAITING_CONFIRMATION
        assert second["confirmation"]["arguments"] == {"note": "two"}
        assert second["confirmation"]["confirmation_id"] != request["confirmation_id"]
        assert "error" not in second["confirmation"]
        assert _peek(session) == 1
        assert model.state["calls"] == 1, "两条确认之间不得调模型"

        final = asyncio.run(session.aresume(_decide(second["confirmation"], False)))
        assert final["status"] == "succeeded", final
        assert _peek(session) == 1
        payloads = _tool_payload(model.state["received"][1])
        assert payloads[0]["structured_content"]["counter"] == 1
        assert payloads[1]["code"] == "TOOL_CONFIRMATION_REJECTED"
        log = session.app.get_state(session.config).values["confirmation_log"]
        assert [entry["decision"] for entry in log] == ["approved", "rejected"]


def test_fourth_confirmation_in_a_turn_fails(gate_workdir: Path) -> None:
    """P7-S7-A03：每轮最多 3 次确认；第 4 次请求不再挂起，本轮 ``failed``（结构化错误），未执行。"""

    with ExitStack() as stack:
        session, model = _open_session(
            stack, gate_workdir, [{"tool_calls": [_bump(f"n{i}")]} for i in range(4)]
        )
        result = asyncio.run(session.aask("一直加一", run_id=uuid4().hex))
        for _ in range(MAX_CONFIRMATIONS_PER_TURN):
            assert result["status"] == STATUS_WAITING_CONFIRMATION, result
            result = asyncio.run(session.aresume(_decide(result["confirmation"], True)))
        assert result["status"] == "failed", result
        assert any("TOOL_CONFIRMATION_LIMIT" in error for error in result["errors"])
        assert _peek(session) == MAX_CONFIRMATIONS_PER_TURN
        assert model.state["calls"] == 4
        assert session.pending_confirmation() is None


def test_confirmation_audit_has_no_raw_arguments(gate_workdir: Path) -> None:
    """P7-S7-A04：``confirmation_log`` 随 checkpoint 持久化、跨轮累积（每轮新建 session，同一 thread）；
    trace 里有 requested / decided / executed 三类事件、``query`` 为空；参数只记摘要、主体只记 hash，
    原因经输出护栏脱敏并截断到 200 字。参数原文不出现在 trace 与审计记录里。"""

    saver = InMemorySaver(serde=default_checkpoint_serde())
    thread_id = uuid4().hex
    secret_note = "raw-arg-NOTE-7f3a"
    phone = "13812345678"
    traces: list[dict[str, Any]] = []
    for turn, approved in enumerate((True, False)):
        with ExitStack() as stack:
            session, _ = _open_session(
                stack,
                gate_workdir,
                [{"tool_calls": [_bump(secret_note)]}, {"content": "已处理。"}],
                saver=saver,
                thread_id=thread_id,
            )
            first = asyncio.run(session.aask(f"第 {turn + 1} 次", run_id=uuid4().hex))
            reason = f"联系人 {phone}。" + "很长的说明" * 80
            final = asyncio.run(session.aresume(_decide(first["confirmation"], approved, reason)))
            assert final["status"] == "succeeded", final
            snapshot = session.recorder.snapshot()
            assert snapshot is not None
            traces.append(snapshot)
            state = session.app.get_state(session.config).values

    log = state["confirmation_log"]
    assert [entry["decision"] for entry in log] == ["approved", "rejected"]
    expected_hash = confirmation_args_hash({"note": secret_note})
    for entry in log:
        assert entry["args_hash"] == expected_hash
        assert len(entry["principal_hash"]) == 16
        assert phone not in entry["reason"]
        assert len(entry["reason"]) <= 200
        assert set(entry) == {
            "confirmation_id", "server_id", "tool_name", "args_hash", "principal_hash",
            "decision", "reason", "decided_at", "result_code",
        }
    assert secret_note not in json.dumps(log, ensure_ascii=False)

    for snapshot, executed in zip(traces, (True, False), strict=True):
        events = [e for e in snapshot["events"] if e["node_name"] == "confirmation"]
        kinds = [e["event_type"] for e in events]
        expected = ["confirmation_requested", "confirmation_decided"]
        assert kinds == ([*expected, "confirmation_executed"] if executed else expected)
        assert all(e["query"] == "" for e in events)
        assert secret_note not in json.dumps(snapshot, ensure_ascii=False)
        assert phone not in json.dumps(snapshot, ensure_ascii=False)
