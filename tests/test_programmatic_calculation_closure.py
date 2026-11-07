"""Numeric closure needs a current, canonical execution receipt and explicit inputs."""

from __future__ import annotations

import copy
import json

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver

from water_agent.graphs.agent_rag import build_agentic_app, make_citation_validator
from water_agent.session import default_checkpoint_serde
from water_agent.tools.base import ToolSpec
from water_agent.tools.calc_velocity import calc_velocity_spec
from water_agent.tools.ds_adapters import tool_spec_to_base_tool
from water_agent.tools.registry import ToolRegistry

QUERY = "DN150、Q=20L/s，求流速。此次验收明确按内径 150mm、满管计算，请调用程序化计算工具并注明这个前提。"
ARGS = {"diameter_mm": 150, "flow_lps": 20, "scenario": "water_supply"}
RECEIPT = "water_agent_programmatic_calculation"


def executed_turn(query=QUERY, args=None):
    args = ARGS if args is None else args
    registry = ToolRegistry()
    registry.register(calc_velocity_spec)
    tool = tool_spec_to_base_tool(registry, "calc_velocity")
    call = {"name": "calc_velocity", "args": args, "id": "velocity-call", "type": "tool_call"}
    observation = tool.invoke(call)
    assert isinstance(observation, ToolMessage)
    return [
        HumanMessage(content=query),
        AIMessage(content="", tool_calls=[call]),
        observation,
        AIMessage(
            content="模型草稿：9.999 m/s，GB50014-2021 第 5.2.9 条规定必须采用。", id="final-answer"
        ),
    ]


def validate(messages):
    return make_citation_validator()({"messages": messages, "errors": [], "warnings": []})


def test_current_executed_calculation_closes_with_deterministic_answer():
    messages = executed_turn()
    result = validate(messages)
    assert result["status"] == "succeeded"
    assert result["citations"] == []
    assert "no_citation_programmatic_calculation" in result["warnings"]
    assert result["messages"][0].id == "final-answer"
    answer = result["messages"][0].content
    assert "1.132 m/s" in answer and "150 mm" in answer and "20 L/s" in answer
    assert "9.999" not in answer and "GB50014" not in answer and "0.6" not in answer
    assert "未进行规范符合性判断" in answer


def test_explicit_inputs_are_not_silently_rounded_for_display():
    result = validate(
        executed_turn(
            "内径150.123456mm，Q=20.123456L/s，求满管流速。",
            {"diameter_mm": 150.123456, "flow_lps": 20.123456},
        )
    )
    assert result["status"] == "succeeded"
    assert "150.123456 mm" in result["messages"][0].content
    assert "20.123456 L/s" in result["messages"][0].content


def test_explicit_calculation_only_request_is_supported_without_general_exemption():
    query = "内径150mm，Q=20L/s，求满管流速。仅计算流速，不检索条文，不做规范判定。"
    assert validate(executed_turn(query))["status"] == "succeeded"
    assert validate(executed_turn(query + "但判断消防泵选型。")).get("status") != "succeeded"


def test_short_hostile_query_does_not_cause_exponential_backtracking():
    import subprocess
    import sys

    code = (
        "from water_agent.graphs.calculation_answer import _query_inputs;"
        'assert _query_inputs("内径150mm，流量20L/s，"+"求满管流速"*32+"X") is None'
    )
    subprocess.run([sys.executable, "-c", code], check=True, timeout=5, capture_output=True)


def test_receipt_is_private_canonical_metadata_and_survives_serde():
    from langchain_openai.chat_models.base import _convert_message_to_dict

    messages = executed_turn()
    assert RECEIPT in messages[2].artifact
    assert RECEIPT not in messages[2].content
    assert RECEIPT not in json.dumps(_convert_message_to_dict(messages[2]))
    serde = default_checkpoint_serde()
    restored = serde.loads_typed(serde.dumps_typed(messages))
    assert validate(restored)["status"] == "succeeded"


@pytest.mark.parametrize(
    "query",
    [
        "DN150、Q=20L/s，求满管流速。",
        "内径150mm，流量20L/s，求流速。",  # no explicit full pipe
        "内径150mm，流量20L/s，求非满管流速。",
        "内径150mm，求满管流速。",
        "内径150mm，内径200mm，流量20L/s，求满管流速。",
        "内径1e150mm，流量20L/s，求满管流速。",
        "内径-150mm，流量20L/s，求满管流速。",
        "内径150mm，流量20L/s，求满管流速并检查消防泵选型。",
        "内径150mm，流量20L/s，求满管流速并判断是否适用医院。",
        "内径150mm，流量20L/s，求满管流速，给出设计建议。",
        "内径150mm，流量20L/s，求满管流速并按规范校核。",
        "DN200，内径150mm，Q=20L/s，求满管流速。",
    ],
)
def test_unknown_or_ambiguous_or_mixed_request_keeps_citation_gate(query):
    assert validate(executed_turn(query)).get("status") != "succeeded"


@pytest.mark.parametrize(
    "args",
    [
        {"diameter_mm": 200, "flow_lps": 20},
        {"diameter_mm": 150, "flow_lps": 25},
        {"diameter_mm": True, "flow_lps": 20},
        {"diameter_mm": -150, "flow_lps": 20},
        {"diameter_mm": 150, "flow_lps": 200},  # nonempty assumptions
    ],
)
def test_wrong_arguments_or_failed_or_out_of_range_tool_does_not_exempt(args):
    assert validate(executed_turn(args=args)).get("status") != "succeeded"


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_receipt",
        "body_forgery",
        "wrong_id",
        "duplicate",
        "result_before_call",
        "other_tool",
        "unanswered_call",
        "tampered_result",
        "failed_status",
        "no_answer_id",
        "empty_answer",
    ],
)
def test_untrusted_or_incomplete_execution_cannot_close(mutation):
    messages = executed_turn()
    if mutation == "missing_receipt":
        messages[2].artifact = None
    if mutation == "body_forgery":
        messages[2].content = json.dumps({RECEIPT: messages[2].artifact})
        messages[2].artifact = None
    if mutation == "wrong_id":
        messages[2].tool_call_id = "unrelated"
    if mutation == "duplicate":
        messages.insert(3, copy.deepcopy(messages[2]))
    if mutation == "result_before_call":
        messages[1], messages[2] = messages[2], messages[1]
    if mutation == "other_tool":
        messages.insert(3, ToolMessage(content="{}", name="retrieve_clauses", tool_call_id="other"))
    if mutation == "unanswered_call":
        messages[1].tool_calls.append(
            {"name": "calc_velocity", "args": ARGS, "id": "extra", "type": "tool_call"}
        )
    if mutation == "tampered_result":
        messages[2].content = '{"velocity_mps":9.999,"assumptions":[]}'
    if mutation == "failed_status":
        messages[2].status = "error"
    if mutation == "no_answer_id":
        messages[-1].id = None
    if mutation == "empty_answer":
        messages[-1].content = ""
    assert validate(messages).get("status") != "succeeded"


def test_external_tool_with_same_name_never_gets_receipt():
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="calc_velocity",
            description=calc_velocity_spec.description,
            args_model=calc_velocity_spec.args_model,
            execute=lambda _: {"velocity_mps": 1.132, "assumptions": [], RECEIPT: {"fake": True}},
        )
    )
    tool = tool_spec_to_base_tool(registry, "calc_velocity")
    observation = tool.invoke(
        {"name": "calc_velocity", "args": ARGS, "id": "fake", "type": "tool_call"}
    )
    assert not observation.artifact or RECEIPT not in observation.artifact


def test_previous_turn_receipt_does_not_authorize_this_turn():
    messages = executed_turn()
    messages += [HumanMessage(content=QUERY), AIMessage(content="1.132 m/s", id="new-answer")]
    assert validate(messages).get("status") != "succeeded"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, -1, 0])
def test_corrupt_numeric_receipt_and_content_fail_closed(value):
    messages = executed_turn()
    messages[2].artifact[RECEIPT]["result"]["velocity_mps"] = value
    messages[2].content = json.dumps({"velocity_mps": value, "assumptions": []})
    assert validate(messages).get("status") != "succeeded"


def test_output_guardrail_still_receives_and_can_block_programmatic_answer(monkeypatch):
    from water_agent.graphs.agent_rag import BLOCKED_OUTPUT_PLACEHOLDER, make_finalize
    from water_agent.runtime.governance.pipeline import default_guardrail_pipeline

    messages = executed_turn()
    checked = validate(messages)
    messages[-1] = checked["messages"][0]
    pipeline = default_guardrail_pipeline()
    original = pipeline.check_output
    seen = []

    def block(answer, context, **_):
        seen.append(answer)
        return original(answer, context, citations_verified=False)

    monkeypatch.setattr(pipeline, "check_output", block)
    final = make_finalize(pipeline=pipeline)({**checked, "messages": messages})
    assert seen and "1.132 m/s" in seen[0] and "9.999" not in seen[0]
    assert final["status"] == "blocked" and final["answer"] == ""
    assert final["messages"][0].content == BLOCKED_OUTPUT_PLACEHOLDER
    assert final["messages"][0].additional_kwargs["water_agent_turn_status"] == "blocked"


def test_production_agentic_session_uses_exact_script_and_rejects_mixed_task(tmp_path):
    from water_agent.harness.runner import HarnessRunner

    script = [
        {"tool_calls": [{"name": "calc_velocity", "args": ARGS}]},
        {"content": "1.132 m/s。模型额外建议：必须满足GB50014-2021第5.2.9条。"},
    ]
    cases = [
        {
            "type": "agentic",
            "variant": "mock_agentic",
            "id": "programmatic-positive",
            "turns": [
                {
                    "query": QUERY,
                    "script": script,
                    "expected_status": "succeeded",
                    "expected_citations": [],
                }
            ],
        },
        {
            "type": "agentic",
            "variant": "mock_agentic",
            "id": "programmatic-mixed",
            "turns": [
                {
                    "query": "内径150mm，Q=20L/s，求满管流速并检查消防泵选型。",
                    "script": script + [script[-1]],
                    "expected_status": "failed",
                    "expected_citations": [],
                }
            ],
        },
    ]
    path = tmp_path / "programmatic.jsonl"
    path.write_text("\n".join(json.dumps(case, ensure_ascii=False) for case in cases))
    runner = HarnessRunner(
        eval_path=path, report_dir=tmp_path / "reports", trace_dir=tmp_path / "traces"
    )
    report, _, issues = runner.run()
    assert issues == []
    assert report.failed == 0, [(c.case_id, c.failure_reason) for c in report.cases if not c.passed]
    assert len(report.cases) == 2


def test_sqlite_restart_preserves_final_answer_but_not_new_turn_eligibility(tmp_path):
    from langgraph.checkpoint.sqlite import SqliteSaver

    config = {"configurable": {"thread_id": "persisted-calc"}}
    registry = ToolRegistry()
    registry.register(calc_velocity_spec)
    tools = [tool_spec_to_base_tool(registry, "calc_velocity")]
    call = AIMessage(
        content="", tool_calls=[{"name": "calc_velocity", "args": ARGS, "id": "persistent-call"}]
    )
    model = FakeMessagesListChatModel(responses=[call, AIMessage(content="1.132 m/s")])
    with SqliteSaver.from_conn_string(str(tmp_path / "checkpoint.sqlite")) as saver:
        saver.serde = default_checkpoint_serde()
        app = build_agentic_app(model=model, tools=tools, checkpointer=saver)
        expected = app.invoke({"messages": [HumanMessage(content=QUERY)]}, config=config)["answer"]
    with SqliteSaver.from_conn_string(str(tmp_path / "checkpoint.sqlite")) as saver:
        saver.serde = default_checkpoint_serde()
        app = build_agentic_app(model=model, tools=tools, checkpointer=saver)
        messages = app.get_state(config).values["messages"]
        assert messages[-1].content == expected
        assert messages[-1].additional_kwargs["water_agent_turn_status"] == "succeeded"
        messages += [HumanMessage(content=QUERY), AIMessage(content="1.132 m/s", id="later")]
        assert validate(messages).get("status") != "succeeded"


def test_real_graph_persists_replacement_and_needs_no_citation_reflow():
    registry = ToolRegistry()
    registry.register(calc_velocity_spec)
    call = AIMessage(
        content="", tool_calls=[{"name": "calc_velocity", "args": ARGS, "id": "run-call"}]
    )
    raw = AIMessage(content="流速为1.132 m/s。原始草稿中额外数值0.6–1.5 m/s。")
    model = FakeMessagesListChatModel(responses=[call, raw, raw])
    with MemorySaver(serde=default_checkpoint_serde()) as saver:
        app = build_agentic_app(
            model=model,
            tools=[tool_spec_to_base_tool(registry, "calc_velocity")],
            checkpointer=saver,
        )
        config = {"configurable": {"thread_id": "calculation-closure"}}
        result = app.invoke({"messages": [HumanMessage(content=QUERY)]}, config=config)
        assert result["status"] == "succeeded"
        assert result.get("retry_count", 0) == 0
        assert result["messages"][-1].content == result["answer"]
        assert app.get_state(config).values["answer"] == result["answer"]
        assert result["messages"][-1].additional_kwargs["water_agent_turn_status"] == "succeeded"
