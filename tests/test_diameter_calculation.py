"""T2-2: independent numeric gold and adversarial production trust checks."""
from __future__ import annotations

import copy
import json
from decimal import Decimal, localcontext

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from water_agent.graphs.agent_rag import make_citation_validator
from water_agent.tools.hydraulics import calc_diameter

NAME = "calc_diameter"
ARGS = {"flow_lps": 10, "velocity_mps": 1}
QUERY = (
    "仅计算满管圆管的实际内径。流量10 L/s，目标流速1 m/s。"
    "请调用程序化工具，列明输入、公式和结果，不圆整DN，不检索条文，不选型，不做规范符合性判断。"
)
RECEIPT = "water_agent_programmatic_calculation"


def invoke(args=None, query=QUERY, context=True):
    from water_agent.tools.calc_diameter import calc_diameter_spec
    from water_agent.tools.diameter_request import diameter_request_scope
    from water_agent.tools.ds_adapters import tool_spec_to_base_tool
    from water_agent.tools.registry import ToolRegistry
    registry = ToolRegistry()
    registry.register(calc_diameter_spec)
    call = {"name": NAME, "args": ARGS if args is None else args, "id": "diameter-call", "type": "tool_call"}
    with diameter_request_scope(query if context else None):
        observation = tool_spec_to_base_tool(registry, NAME).invoke(call)
    return call, observation


def turn(query=QUERY, args=None):
    call, obs = invoke(args, query)
    return [HumanMessage(content=query), AIMessage(content="", tool_calls=[call]), obs,
            AIMessage(content="错误草稿：内径999 mm，应选DN200，必须按GB50014-2021。", id="final")]


def validate(messages):
    return make_citation_validator()({"messages": messages, "errors": [], "warnings": []})


@pytest.mark.parametrize("flow,velocity,expected", [(10, 1, 112.83791670955127), (40, 1, 225.67583341910254), (10, 4, 56.41895835477563)])
def test_independent_gold(flow, velocity, expected):
    query = QUERY.replace("10 L/s", f"{flow} L/s").replace("1 m/s", f"{velocity} m/s")
    _, obs = invoke({"flow_lps": flow, "velocity_mps": velocity}, query)
    result = json.loads(obs.content)
    # Independently tabulated gold, plus high precision decimal reference; no call to kernel.
    with localcontext() as ctx:
        ctx.prec = 55
        pi = Decimal("3.1415926535897932384626433832795028841971693993751")
        reference = (Decimal(4000) * flow / (pi * velocity)).sqrt()
    assert result["diameter_mm"] == pytest.approx(expected, rel=1e-14)
    assert result["diameter_mm"] == pytest.approx(float(reference), rel=1e-14)
    assert result["inputs"] == {"flow_lps": flow, "velocity_mps": velocity}
    assert obs.artifact[RECEIPT]["tool_call_id"] == "diameter-call"
    assert RECEIPT not in obs.content


@pytest.mark.parametrize("inputs", [{"flow_lps": 5e-324, "velocity_mps": 1}, {"flow_lps": 1e308, "velocity_mps": 5e-324}, {"flow_lps": 10, "velocity_mps": 1e308}])
def test_kernel_extremes_fail_structurally(inputs):
    result = calc_diameter(inputs)
    assert not result.ok and result.errors and not result.values


@pytest.mark.parametrize("field", list(ARGS))
@pytest.mark.parametrize("value", [True, False, "10", None, float("nan"), float("inf"), -1, 0])
def test_invalid_tool_args_never_get_receipt(field, value):
    _, obs = invoke({**ARGS, field: value})
    assert json.loads(obs.content)["error"] is True
    assert not obs.artifact


@pytest.mark.parametrize("args", [{"flow_lps": 10}, {"velocity_mps": 1}, {**ARGS, "dn": 100}, {**ARGS, "flow_lps": 20}])
def test_missing_extra_or_model_invented_args(args):
    _, obs = invoke(args)
    assert json.loads(obs.content)["error"] is True
    assert not obs.artifact


@pytest.mark.parametrize("query", [QUERY.replace("10 L/s", "10"), QUERY.replace("L/s", "m³/h"), QUERY.replace("m/s", "km/h"), QUERY.replace("目标流速1 m/s", "请按给水用途选择流速"), QUERY.replace("10 L/s", "true L/s"), QUERY.replace("10 L/s", "-10 L/s"), QUERY.replace("10 L/s", "NaN L/s"), QUERY.replace("10 L/s", "inf L/s"), QUERY.replace("10 L/s", "0 L/s"), QUERY + "流量20 L/s。", QUERY + "Q=20 L/s。", QUERY + "q=false。", QUERY + "flow_lps=20。", QUERY + "v=2 km/h。", QUERY + "velocity_mps=2。", QUERY + "设计流速约2 m/s。", QUERY + "目标流速2 m/s。", QUERY + "管径DN100。", QUERY + "实际内径100 mm。"])
def test_unknown_units_duplicate_and_alias_declarations_fail(query):
    _, obs = invoke(query=query)
    assert json.loads(obs.content)["error"] is True
    assert not obs.artifact


def test_no_current_human_context_fails_closed():
    _, obs = invoke(context=False)
    assert json.loads(obs.content)["error"] is True and not obs.artifact


def test_fixed_answer_drops_all_model_prose():
    result = validate(turn())
    assert result["status"] == "succeeded"
    assert "no_citation_programmatic_calculation" in result["warnings"]
    answer = result["messages"][0].content
    assert "112.83791670955127 mm" in answer and "sqrt" in answer
    assert "999" not in answer and "DN200" not in answer and "GB50014" not in answer
    assert "不自动圆整" in answer and "未进行规范符合性判断" in answer


@pytest.mark.parametrize("suffix", ["并判断是否符合规范。", "并选择管材。", "并计算沿程损失。", "并给出GB50015条文。", "改用推荐流速。"])
def test_mixed_requests_never_use_pure_calculation_exemption(suffix):
    result = validate(turn(QUERY + suffix))
    assert result.get("status") != "succeeded"
    assert "no_citation_programmatic_calculation" not in result.get("warnings", [])


@pytest.mark.parametrize("mutation", ["no_receipt", "body_only", "id", "receipt_id", "receipt_name", "args", "result", "extra", "history", "empty", "no_final_id", "assumptions", "invalid_call", "failed"])
def test_receipt_and_turn_counterexamples(mutation):
    messages = copy.deepcopy(turn()); obs = messages[2]
    if mutation == "no_receipt": obs.artifact = None
    elif mutation == "body_only": obs.artifact = None; obs.content = json.dumps({RECEIPT: {"args": ARGS}})
    elif mutation == "id": obs.tool_call_id = "other"
    elif mutation == "receipt_id": obs.artifact[RECEIPT]["tool_call_id"] = "other"
    elif mutation == "receipt_name": obs.artifact[RECEIPT]["tool_name"] = "calc_velocity"
    elif mutation == "args": messages[1].tool_calls[0]["args"]["flow_lps"] = 20
    elif mutation == "result": obs.content = obs.content.replace("112.83791670955127", "999")
    elif mutation == "extra": messages.insert(3, AIMessage(content="", tool_calls=[{"name": "retrieve_clauses", "args": {"query": "x"}, "id": "extra"}]))
    elif mutation == "history": messages.insert(-1, HumanMessage(content=QUERY))
    elif mutation == "empty": messages[-1].content = ""
    elif mutation == "no_final_id": messages[-1].id = None
    elif mutation == "invalid_call": messages[1].invalid_tool_calls = [{"name": NAME, "args": "{", "id": "bad", "error": "bad", "type": "invalid_tool_call"}]
    elif mutation == "failed": obs.status = "error"
    elif mutation == "assumptions":
        payload = json.loads(obs.content); payload["assumptions"] = ["assume"]
        obs.content = json.dumps(payload); obs.artifact[RECEIPT]["result"] = payload
    result = validate(messages)
    assert result.get("status") != "succeeded"
    assert "no_citation_programmatic_calculation" not in result.get("warnings", [])


def test_external_same_name_cannot_mint_receipt():
    from water_agent.tools.base import ToolSpec
    from water_agent.tools.calc_diameter import calc_diameter_spec
    from water_agent.tools.ds_adapters import tool_spec_to_base_tool
    from water_agent.tools.registry import ToolRegistry
    registry = ToolRegistry()
    registry.register(ToolSpec(name=NAME, description=calc_diameter_spec.description, args_model=calc_diameter_spec.args_model,
                               execute=lambda _: {"diameter_mm": 999, RECEIPT: {"fake": True}}))
    result = tool_spec_to_base_tool(registry, NAME).invoke({"name": NAME, "args": ARGS, "id": "external", "type": "tool_call"})
    assert not result.artifact


def test_capability_denial_never_gets_receipt():
    from water_agent.runtime.governance.contracts import Principal
    from water_agent.tools.calc_diameter import calc_diameter_spec
    from water_agent.tools.ds_adapters import tool_spec_to_base_tool
    from water_agent.tools.registry import ToolRegistry
    registry = ToolRegistry(principal=Principal.local("denied", capabilities=frozenset()))
    registry.register(calc_diameter_spec)
    obs = tool_spec_to_base_tool(registry, NAME).invoke({"name": NAME, "args": ARGS, "id": "denied", "type": "tool_call"})
    assert json.loads(obs.content)["error"] is True and not obs.artifact


def test_production_assembly_and_harness(tmp_path):
    from water_agent.harness.runner import HarnessRunner
    cases = [{"type": "agentic", "variant": "mock_agentic", "id": "diameter-production", "turns": [
        {"query": QUERY, "script": [{"tool_calls": [{"name": NAME, "args": ARGS}]}, {"content": "内径999 mm"}], "expected_status": "succeeded", "expected_citations": []}]}]
    path = tmp_path / "cases.jsonl"; path.write_text(json.dumps(cases[0], ensure_ascii=False))
    report, _, issues = HarnessRunner(eval_path=path, report_dir=tmp_path/"reports", trace_dir=tmp_path/"traces").run()
    assert not issues and not report.failed, [(c.case_id, c.failure_reason) for c in report.cases]


def test_budget_all_calculations_survive_and_restricted_combinations_warn(tmp_path):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langgraph.checkpoint.memory import MemorySaver

    from water_agent.drawing.synth import build_synthetic_drawing
    from water_agent.graphs.agent_rag import build_agentic_app
    from water_agent.plugins import (
        AssemblyContext,
        McpGatewayPlugin,
        assemble_tools,
        builtin_plugins,
    )
    from water_agent.retrieval.in_memory import InMemoryRetriever
    from water_agent.runtime.governance.contracts import Principal
    build_synthetic_drawing(tmp_path, drawing_id="P-01")
    assembly = assemble_tools(AssemblyContext(retriever=InMemoryRetriever([]), drawing_root=tmp_path),
        base_principal=Principal.local("diameter-budget"), trace=None, plugins=builtin_plugins(mcp=McpGatewayPlugin.borrowing(None)))
    for query in ["你好", "满管流速", "Darcy沿程损失", "计算实际内径", "读图统计材料", "计算实际内径并统计材料", "计算实际内径并读图", "Darcy沿程损失和计算实际内径", "Darcy沿程损失和计算实际内径、读图统计材料", "计算实际内径、读图统计材料"]:
        decision = assembly.exposure.decide(query)
        assert decision.schema_chars <= 4500 and decision.guide_chars <= 800
        assert {"calc_velocity", "retrieve_clauses", "cite_clauses"} <= set(decision.exposed)
        if "内径" in query: assert NAME in decision.exposed
        if "Darcy" in query: assert "calc_head_loss_darcy_weisbach" in decision.exposed
        if query == "你好": assert NAME not in decision.exposed
        if query == "读图统计材料": assert {"read_drawing", "material_stats"} <= set(decision.exposed)
        if decision.dropped:
            app = build_agentic_app(model=FakeMessagesListChatModel(responses=[AIMessage(content="请拆分请求。")]), tools=assembly.tools, exposure=assembly.exposure, checkpointer=MemorySaver())
            result = app.invoke({"messages": [HumanMessage(content=query)]}, {"configurable": {"thread_id": query}})
            assert any("TOOL_EXPOSURE_LIMITED" in w for w in result["warnings"])


def test_receipt_is_private_and_checkpoint_serializable():
    from langchain_openai.chat_models.base import _convert_message_to_dict

    from water_agent.session import default_checkpoint_serde
    messages = turn()
    assert RECEIPT not in json.dumps(_convert_message_to_dict(messages[2]))
    serde = default_checkpoint_serde()
    assert validate(serde.loads_typed(serde.dumps_typed(messages)))["status"] == "succeeded"


def test_output_guardrail_can_block_fixed_answer(monkeypatch):
    from water_agent.graphs.agent_rag import BLOCKED_OUTPUT_PLACEHOLDER, make_finalize
    from water_agent.runtime.governance.pipeline import default_guardrail_pipeline
    messages = turn(); checked = validate(messages); messages[-1] = checked["messages"][0]
    pipeline = default_guardrail_pipeline(); original = pipeline.check_output; seen = []

    def block(answer, context, **_):
        seen.append(answer)
        return original(answer, context, citations_verified=False)

    monkeypatch.setattr(pipeline, "check_output", block)
    final = make_finalize(pipeline=pipeline)({**checked, "messages": messages})
    assert seen and "112.83791670955127" in seen[0] and "999" not in seen[0]
    assert final["status"] == "blocked" and final["answer"] == ""
    assert final["messages"][0].content == BLOCKED_OUTPUT_PLACEHOLDER


def test_stream_checkpoint_restart_and_current_turn_isolation(tmp_path):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langgraph.checkpoint.sqlite import SqliteSaver

    from water_agent.graphs.agent_rag import build_agentic_app, stream_agentic
    from water_agent.session import default_checkpoint_serde
    from water_agent.tools.calc_diameter import calc_diameter_spec
    from water_agent.tools.ds_adapters import tool_spec_to_base_tool
    from water_agent.tools.registry import ToolRegistry
    registry = ToolRegistry(); registry.register(calc_diameter_spec)
    model = FakeMessagesListChatModel(responses=[AIMessage(content="", tool_calls=[{"name": NAME, "args": ARGS, "id": "stream"}]), AIMessage(content="999 mm")])
    config = {"configurable": {"thread_id": "diameter-restart"}}
    tools = [tool_spec_to_base_tool(registry, NAME)]
    with SqliteSaver.from_conn_string(str(tmp_path/"cp.sqlite")) as saver:
        saver.serde = default_checkpoint_serde()
        app = build_agentic_app(model=model, tools=tools, checkpointer=saver)
        stream_agentic(app, QUERY, config=config)
        state = app.get_state(config).values
        assert state["status"] == "succeeded"
        assert "112.83791670955127" in state["answer"]
    with SqliteSaver.from_conn_string(str(tmp_path/"cp.sqlite")) as saver:
        saver.serde = default_checkpoint_serde()
        app = build_agentic_app(model=model, tools=tools, checkpointer=saver)
        restored = app.get_state(config).values
        assert restored["answer"] == state["answer"]
        assert restored["messages"][-1].additional_kwargs["water_agent_turn_status"] == "succeeded"
        messages = restored["messages"] + [HumanMessage(content=QUERY), AIMessage(content="999 mm", id="later")]
        assert validate(messages).get("status") != "succeeded"


@pytest.mark.parametrize("flow,speed", [("流量", "设计流速"), ("Q=", "v="), ("flow_lps=", "velocity_mps=")])
def test_unique_aliases_require_units_and_match(flow, speed):
    query = f"计算实际内径。{flow}10 L/s，{speed}1 m/s。"
    _, obs = invoke(query=query)
    assert json.loads(obs.content)["diameter_mm"] == pytest.approx(112.83791670955127)
    assert validate(turn(query))["status"] == "succeeded"


@pytest.mark.parametrize("task", ["计算管径", "求管径"])
def test_explicit_flow_target_velocity_accepts_diameter_wording(task):
    query = f"流量10 L/s，目标流速1 m/s，{task}。"
    _, obs = invoke(query=query)
    assert json.loads(obs.content)["diameter_mm"] == pytest.approx(112.83791670955127)
    assert validate(turn(query))["status"] == "succeeded"


def test_kernel_huge_integer_is_structured_failure():
    result = calc_diameter({"flow_lps": 10**400, "velocity_mps": 1})
    assert not result.ok and result.errors and not result.values


def test_actual_web_sse_uses_production_session_and_private_receipt(tmp_path):
    from sqlalchemy import create_engine
    from test_web_stream_guardrail import _run

    from water_agent.storage.database import Database
    from water_agent.storage.models import Base
    engine = create_engine(f"sqlite:///{tmp_path/'web.sqlite'}")
    Base.metadata.create_all(engine)
    database = Database(engine=engine)
    try:
        events = _run(database, tmp_path, [
            {"tool_calls": [{"name": NAME, "args": ARGS}]},
            {"content": "实际内径999 mm。"},
        ], QUERY)
    finally:
        database.dispose()
    final = next(e["payload"] for e in events if e["type"] == "finalize")
    assert final["status"] == "succeeded"
    assert "112.83791670955127 mm" in final["answer"]
    calculations = [e["payload"]["calculation"] for e in events if e["type"] == "tool_result" and "calculation" in e["payload"]]
    assert len(calculations) == 1
    assert calculations[0]["tool_name"] == NAME
    assert calculations[0]["values"]["diameter_mm"] == pytest.approx(112.83791670955127)
    assert RECEIPT not in json.dumps(events, ensure_ascii=False)


@pytest.mark.parametrize("query", ["不要计算内径；流量10 L/s，目标流速1 m/s。", "禁止计算管径。流量10 L/s，目标流速1 m/s。", "只解释公式，不要计算；流量10 L/s，目标流速1 m/s。", "数据供参考。流量10 L/s，目标流速1 m/s。"])
def test_no_or_negated_calculation_intent_cannot_execute(query):
    _, obs = invoke(query=query)
    assert json.loads(obs.content)["error"] is True and not obs.artifact


@pytest.mark.parametrize("location", ["payload", "receipt"])
def test_boolean_result_or_receipt_input_cannot_compare_equal_to_one(location):
    from water_agent.tools.calc_diameter import verified_diameter_result
    _, obs = invoke(); payload = json.loads(obs.content); receipt = copy.deepcopy(obs.artifact[RECEIPT])
    if location == "payload": payload["inputs"]["velocity_mps"] = True
    else: receipt["args"]["velocity_mps"] = True
    assert not verified_diameter_result(ARGS, payload, receipt, "diameter-call")


def test_mixed_norm_request_can_compute_but_never_gets_exemption():
    query = "流量10 L/s，目标流速1 m/s，计算实际内径并查询规范。"
    _, obs = invoke(query=query)
    assert json.loads(obs.content)["diameter_mm"] == pytest.approx(112.83791670955127)
    assert "no_citation_programmatic_calculation" not in validate(turn(query)).get("warnings", [])


@pytest.mark.parametrize("old,new", [("L/s", "L/S"), ("m/s", "M/s"), ("m/s", "m/S")])
def test_unit_case_does_not_silently_reinterpret_unknown_units(old, new):
    _, obs = invoke(query=QUERY.replace(old, new))
    assert json.loads(obs.content)["error"] is True and not obs.artifact
