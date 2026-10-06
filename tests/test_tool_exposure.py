"""按意图最小暴露（P7-S4，``docs/design/16-p7-plugin-mcp.md`` §3.6 / §7）。"""
from __future__ import annotations

import ast
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from water_agent.drawing.synth import build_synthetic_drawing
from water_agent.harness import tool_exposure_eval as tx
from water_agent.mcp.wiring import MCPGatewayAssembly, load_mcp_gateway, open_gateway_sessions
from water_agent.plugins import (
    AssemblyContext,
    McpGatewayPlugin,
    ToolAssembly,
    assemble_tools,
    builtin_plugins,
)
from water_agent.plugins.exposure import (
    CORE,
    EXPOSURE_ENV,
    ExposureRule,
    ToolExposurePolicy,
    ToolGroup,
    resolve_exposure_strategy,
)
from water_agent.retrieval.in_memory import InMemoryRetriever
from water_agent.runtime.governance.binding import local_request_context

EXPOSURE_MODULE = Path(__file__).resolve().parents[1] / "src/water_agent/plugins/exposure.py"
CORE_TOOLS = ("retrieve_clauses", "cite_clauses", "calc_velocity")


@pytest.fixture(scope="module")
def drawing_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("drawings")
    build_synthetic_drawing(root, drawing_id="P-01")
    return root


@pytest.fixture(scope="module")
def mcp_assembly(tmp_path_factory: pytest.TempPathFactory) -> Iterator[MCPGatewayAssembly]:
    config = tx._write_mcp_config(tmp_path_factory.mktemp("mcp") / "mcp.json")
    saved = os.environ.get("WATER_AGENT_MCP_CONFIG")
    os.environ["WATER_AGENT_MCP_CONFIG"] = str(config)
    try:
        assembly = open_gateway_sessions(load_mcp_gateway())
    finally:
        if saved is None:
            os.environ.pop("WATER_AGENT_MCP_CONFIG", None)
        else:
            os.environ["WATER_AGENT_MCP_CONFIG"] = saved
    try:
        yield assembly
    finally:
        assembly.close()


def _assemble(
    *,
    drawing: Path | None = None,
    mcp: MCPGatewayAssembly | None = None,
    strategy: str | None = "intent",
) -> ToolAssembly:
    return assemble_tools(
        AssemblyContext(
            retriever=InMemoryRetriever([]),
            drawing_root=str(drawing) if drawing is not None else "",
        ),
        base_principal=local_request_context(thread_id="exposure-test").principal,
        trace=None,
        plugins=builtin_plugins(mcp=McpGatewayPlugin.borrowing(mcp)),
        exposure_strategy=strategy,  # type: ignore[arg-type]
    )


def _definition(name: str, chars: int) -> dict:
    return {"type": "function", "function": {"name": name, "description": f"{name} 的说明。", "parameters": {"pad": "x" * chars}}}


def test_groups_come_from_contributions(drawing_root: Path, mcp_assembly: MCPGatewayAssembly) -> None:
    """P7-S4-A02：组来自插件贡献——检索 / 引用与流速为 core，材料与读图为 intent，MCP 每台 server 一组 ambient。"""

    groups = {group.group_id: group for group in _assemble(drawing=drawing_root, mcp=mcp_assembly).exposure.groups}
    assert list(groups) == ["clauses", "hydraulics", "materials", "drawing", "mcp:txfake"]
    assert groups["clauses"].tools == ("retrieve_clauses", "cite_clauses")
    assert groups["clauses"].rule.mode == groups["hydraulics"].rule.mode == "core"
    assert groups["materials"].rule.mode == groups["drawing"].rule.mode == "intent"
    assert groups["mcp:txfake"].rule.mode == "ambient"
    assert groups["mcp:txfake"].tools == ("mcp_txfake_echo_query", "mcp_txfake_lookup_project")
    # 图纸目录不可用时读图插件跳过，不成组
    assert "drawing" not in {group.group_id for group in _assemble().exposure.groups}


def test_decision_is_deterministic_and_pure(drawing_root: Path) -> None:
    """P7-S4-A02：同输入同输出、不受定义字典顺序影响；模块不 import 模型 / HTTP / random / time。"""

    policy = _assemble(drawing=drawing_root).exposure
    query = "根据图纸 P-01 统计管材用量"
    first = policy.decide(query)
    assert all(policy.decide(query) == first for _ in range(3))
    shuffled = ToolExposurePolicy(
        strategy="intent",
        groups=policy.groups,
        definitions=dict(reversed(list(policy._definitions.items()))),
        order=policy.order,
        guide_budget=policy.guide_budget,
    )
    assert shuffled.decide(query) == first

    banned = {"random", "time", "datetime", "httpx", "requests", "openai", "langchain_openai", "urllib"}
    tree = ast.parse(EXPOSURE_MODULE.read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not imported & banned, imported & banned
    assert not any("chat_model" in (getattr(node, "module", "") or "") for node in ast.walk(tree))


def test_intent_groups_open_only_on_matching_questions(drawing_root: Path) -> None:
    """P7-S4-A02：intent 组只在命中时开放；core 每轮都开。"""

    policy = _assemble(drawing=drawing_root).exposure
    plain = policy.decide("给水管道的设计秒流量如何确定")
    assert plain.exposed == CORE_TOOLS
    assert plain.unexposed == ("material_stats", "read_drawing")
    assert dict(plain.reasons) == {"clauses": "core", "hydraulics": "core"}
    both = policy.decide("从 P-01 的管线算出各规格管道的总长度")
    assert both.exposed == (*CORE_TOOLS, "material_stats", "read_drawing")
    assert dict(both.reasons)["drawing"] == dict(both.reasons)["materials"] == "intent"


def test_budget_drops_whole_groups_in_reverse_priority(drawing_root: Path, mcp_assembly: MCPGatewayAssembly) -> None:
    """P7-S4-A03：按优先级逐组入选、放不下的整组跳过；ambient（MCP）最先让位；指引不截断。"""

    policy = _assemble(drawing=drawing_root, mcp=mcp_assembly).exposure
    pressure = policy.decide("根据图纸 P-01 统计材料，并核对 DN100 管 6L/s 的流速")
    assert pressure.exposed == (*CORE_TOOLS, "material_stats", "read_drawing")
    assert pressure.dropped == (("mcp:txfake", "schema_budget"),)
    assert pressure.schema_chars <= 4500
    assert pressure.guide_chars is not None and pressure.guide_chars <= 800
    # 没有预算压力时 MCP 照常开放（整台 server 一起）
    light = policy.decide("查一下项目 A-12 的资料")
    assert {"mcp_txfake_echo_query", "mcp_txfake_lookup_project"} <= set(light.exposed)
    assert light.dropped == ()

    # 指引预算同样整组裁决：把预算压到只够 core 的指引，intent 组以 guide_budget 让位
    tight = ToolExposurePolicy(
        strategy="intent",
        groups=policy.groups,
        definitions=policy._definitions,
        order=policy.order,
        guide_budget=policy._measure(set(CORE_TOOLS))[1] + 5,
    )
    squeezed = tight.decide("根据图纸 P-01 统计管材用量")
    assert squeezed.exposed == CORE_TOOLS
    assert ("materials", "guide_budget") in squeezed.dropped
    assert ("drawing", "guide_budget") in squeezed.dropped


def test_core_over_budget_fails_at_assembly() -> None:
    """P7-S4-A03：core 自身就超预算是编程错误，构造期抛错，不降级。"""

    with pytest.raises(ValueError, match="core 组自身超预算"):
        ToolExposurePolicy(
            strategy="intent",
            groups=[ToolGroup("big", ("big",), CORE)],
            definitions={"big": _definition("big", 5000)},
            order=["big"],
            guide_budget=800,
        )
    # 每个已注册工具必须恰好属于一个组
    with pytest.raises(ValueError, match="恰好属于一个组"):
        ToolExposurePolicy(
            strategy="intent",
            groups=[ToolGroup("a", ("a",), CORE)],
            definitions={"a": _definition("a", 1), "b": _definition("b", 1)},
            order=["a", "b"],
            guide_budget=800,
        )


def test_previous_turn_tools_stay_exposed_for_one_turn(drawing_root: Path) -> None:
    """P7-S4-A04：上一轮实际调用过工具的组本轮保留（sticky）；没有调用就不保留。"""

    policy = _assemble(drawing=drawing_root).exposure
    follow = policy.decide("那换成 PE 管呢？", previous_turn_tools=["material_stats"])
    assert "material_stats" in follow.exposed
    assert dict(follow.reasons)["materials"] == "sticky"
    assert "material_stats" not in policy.decide("那换成 PE 管呢？").exposed


def test_rules_are_validated_at_construction() -> None:
    """P7-S4-A02：规则在构造期校验——非法正则、无触发条件的 intent、带关键词的 core 都抛错。"""

    import re

    with pytest.raises(re.error):
        ExposureRule(mode="intent", patterns=("(",))
    with pytest.raises(ValueError):
        ExposureRule(mode="intent")
    with pytest.raises(ValueError):
        ExposureRule(mode="core", keywords=("x",))
    # 关键词按 NFKC + 小写规范化：全角 / 大写都能命中
    rule = ExposureRule(mode="intent", keywords=("DXF",))
    assert rule.matches("打开 ｄｘｆ 文件".lower()) is False  # 调用方负责先规范化
    from water_agent.plugins.exposure import normalize

    assert rule.matches(normalize("打开 ＤＸＦ 文件"))


def test_full_strategy_matches_s3_behavior(drawing_root: Path, mcp_assembly: MCPGatewayAssembly) -> None:
    """P7-S4-A07：``full`` 策略开放全部已注册工具，不做预算裁决（与 S3 一样，MCP 时会超 4500）。"""

    assembly = _assemble(drawing=drawing_root, mcp=mcp_assembly, strategy="full")
    decision = assembly.exposure.decide("你好")
    assert decision.strategy == "full"
    assert decision.exposed == tuple(spec["function"]["name"] for spec in assembly.registry.list_tools())
    assert decision.dropped == ()
    assert decision.schema_chars > 4500
    assert decision.guide_chars is None


def test_invalid_strategy_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S4-A07：开关写错在装配期抛错，不静默回退成全量；缺省是 intent。"""

    assert resolve_exposure_strategy({}) == "intent"
    assert resolve_exposure_strategy({EXPOSURE_ENV: " full "}) == "full"
    monkeypatch.setenv(EXPOSURE_ENV, "all")
    with pytest.raises(ValueError, match=EXPOSURE_ENV):
        _assemble(strategy=None)
    monkeypatch.setenv(EXPOSURE_ENV, "full")
    assert _assemble(strategy=None).exposure.strategy == "full"


# ---------------------------------------------------------------------------
# 接线（生产 AgenticSession + 脚本模型）
# ---------------------------------------------------------------------------


def _run_session(
    tmp_path: Path,
    turns: list[tuple[str, list[dict]]],
    *,
    drawing: Path | None = None,
    stream: bool = True,
) -> list[tuple[object, dict]]:
    """逐轮跑生产会话（每轮新建 session、共用 checkpointer 与 thread，与 Web 同构）。"""

    import asyncio
    from contextlib import ExitStack
    from uuid import uuid4

    from langgraph.checkpoint.memory import InMemorySaver

    from water_agent.harness.doubles import ScriptedChatModel
    from water_agent.retrieval.loader import load_clauses_jsonl
    from water_agent.retrieval.trivium import TriviumRetriever
    from water_agent.runtime.agentic import AgenticSession
    from water_agent.session import default_checkpoint_serde

    saver = InMemorySaver(serde=default_checkpoint_serde())
    clauses = load_clauses_jsonl(tx.FIXTURE_CLAUSES)
    out: list[tuple[object, dict]] = []
    for number, (query, script) in enumerate(turns, start=1):
        model = ScriptedChatModel(steps=script, id_prefix=f"t{number}")
        with ExitStack() as stack:
            session = AgenticSession(
                tx._session_args(tmp_path, str(drawing) if drawing else ""),
                stack=stack,
                thread_id="exposure-wiring",
                checkpointer=saver,
                model=model,
                retriever=TriviumRetriever(db_path=None, fallback_clauses=clauses),
                mcp_enabled=False,
            )
            if stream:
                asyncio.run(session.aask(query, run_id=uuid4().hex, on_token=lambda _t: None))
            else:
                session.ask(query)
            out.append((model, session.recorder.snapshot() or {}))
    return out


def _bound(model: object) -> list[set[str]]:
    """每次调用绑定的工具名（集合：发给模型的顺序是字母序，见事实 F3）。"""
    return [set(tx.tool_names(tools)) for tools in model.state["tools"]]  # type: ignore[attr-defined]


def _events(trace: dict, event: str) -> list[dict]:
    return [item for item in trace.get("events", []) if item.get("event_type") == event]


@pytest.mark.parametrize("stream", [True, False])
def test_model_receives_only_exposed_tools(tmp_path: Path, drawing_root: Path, stream: bool) -> None:
    """P7-S4-A05：模型只收到本轮开放的工具（流式与同步两条路径），指引按本轮决定渲染，
    每轮一条 tool_exposure trace 事件且不含 query 原文；第二轮按新问题重新决定。"""

    ok = [{"content": "收到。"}]
    runs = _run_session(
        tmp_path,
        [("给水管道的设计秒流量如何确定", ok), ("根据图纸 P-01 统计管材用量", ok)],
        drawing=drawing_root,
        stream=stream,
    )
    (first_model, first_trace), (second_model, second_trace) = runs
    assert _bound(first_model) == [set(CORE_TOOLS)]
    guide = tx.guide_segment(first_model.state["received"][0])  # type: ignore[attr-defined]
    assert tx.guide_tools(guide) == list(CORE_TOOLS)
    assert "本轮未开放：material_stats、read_drawing" in guide
    events = _events(first_trace, "tool_exposure")
    assert len(events) == 1
    assert events[0]["query"] == ""
    assert events[0]["data"]["exposed"] == list(CORE_TOOLS)

    assert _bound(second_model) == [{*CORE_TOOLS, "material_stats", "read_drawing"}]
    assert len(_events(second_trace, "tool_exposure")) == 1


def test_reflow_reuses_the_turn_decision(tmp_path: Path, drawing_root: Path) -> None:
    """P7-S4-A05：引用校验回流再进 agent_loop 时复用本轮决定（不重复决定、不重复记事件）。"""

    normative = {"content": "给水管道流速不应大于 2 m/s。"}
    (model, trace), = _run_session(
        tmp_path, [("给水管道的设计秒流量如何确定", [normative, normative])], drawing=drawing_root
    )
    assert model.state["calls"] == 2  # type: ignore[attr-defined]  # 零引用回流一次
    assert _bound(model) == [set(CORE_TOOLS), set(CORE_TOOLS)]
    assert len(_events(trace, "tool_exposure")) == 1


def test_unexposed_call_executes_and_is_marked(tmp_path: Path, drawing_root: Path) -> None:
    """P7-S4-A06：模型调用本轮未开放的工具时照常经 ToolRegistry.invoke 执行（护栏与授权不变），
    trace 另记 unexposed_call；开放的工具调用不记。"""

    script = [
        {"tool_calls": [{"name": "read_drawing", "args": {"drawing_id": "P-01"}}]},
        {"tool_calls": [{"name": "calc_velocity", "args": {"diameter_mm": 100, "flow_lps": 5}}]},
        {"content": "收到。"},
    ]
    (model, trace), = _run_session(tmp_path, [("你好", script)], drawing=drawing_root)
    assert "read_drawing" not in _bound(model)[0]
    executed = [
        item["data"]["tool_name"]
        for item in _events(trace, "tool")
        if item.get("data", {}).get("executed") is True
    ]
    assert {"read_drawing", "calc_velocity"} <= set(executed)
    marked = [item["data"] for item in _events(trace, "unexposed_call")]
    assert marked == [{"tool_name": "read_drawing", "unexposed_call": True}]
    assert all(item["query"] == "" for item in _events(trace, "unexposed_call"))


def test_previous_turn_tools_reads_only_the_last_turn() -> None:
    """P7-S4-A04：sticky 只回看上一轮，运行时上下文前言不算一轮。"""

    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from water_agent.graphs.agent_rag import previous_turn_tools
    from water_agent.prompting.history import RUNTIME_CONTEXT_PREFIX

    def call(name: str, cid: str) -> list:
        return [
            AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": cid}]),
            ToolMessage(content="{}", name=name, tool_call_id=cid),
        ]

    messages = [
        HumanMessage(content=f"{RUNTIME_CONTEXT_PREFIX}\n前言"),
        HumanMessage(content="第一问"),
        *call("material_stats", "a"),
        AIMessage(content="答一"),
        HumanMessage(content="第二问"),
        AIMessage(content="答二"),
        HumanMessage(content="第三问"),
    ]
    assert previous_turn_tools(messages) == []
    assert previous_turn_tools(messages[:6]) == ["material_stats"]
    assert previous_turn_tools(messages[:2]) == []
