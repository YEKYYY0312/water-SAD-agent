"""tool_exposure_p0 harness 自身（P7-S4-A01，``docs/design/16-p7-plugin-mcp.md`` §7）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from water_agent.harness import tool_exposure_eval as tx

DATASET = tx.DEFAULT_DATASET


@pytest.fixture(scope="module")
def cases() -> dict[str, tx.ExposureCase]:
    loaded, problems = tx.load_cases(DATASET)
    assert problems == []
    return {case.id: case for case in loaded}


def _case(cases: dict[str, tx.ExposureCase], category: str, *, drawing: bool, mcp: bool) -> tx.ExposureCase:
    for case in cases.values():
        if (
            case.category == category
            and case.deployment.drawing is drawing
            and case.deployment.mcp is mcp
            and len(case.turns) == 1
        ):
            return case
    raise LookupError(category)


def test_harness_records_what_the_model_saw(cases, tmp_path: Path) -> None:
    """P7-S4-A01：harness 经生产会话运行，记下的是模型第一次调用实际绑定的工具与指引。"""

    case = _case(cases, "clause_qa", drawing=False, mcp=False)
    result = tx.run_case(case, tmp_path, strategy=None)
    assert result.error is None
    turn = result.turns[0]
    # 规范工具一定在：装配真的发生了，且指引是从系统提示里读出来的
    assert {"retrieve_clauses", "cite_clauses"} <= set(turn.exposed)
    assert {"retrieve_clauses", "cite_clauses"} <= set(turn.guide_tools)
    assert turn.schema_chars_first == turn.schema_chars_max > 0
    assert turn.status == "succeeded"


def test_script_must_be_consumed_exactly(cases, tmp_path: Path) -> None:
    """P7-S4-A01：脚本少用一步也是失败（与 agentic_p0 同一纪律）。"""

    case = _case(cases, "clause_qa", drawing=False, mcp=False)
    turn = case.turns[0].model_copy(
        update={"script": [*case.turns[0].script, tx.ScriptStep(content="多余的一步")]}
    )
    padded = case.model_copy(update={"turns": [turn]})
    result = tx.run_case(padded, tmp_path, strategy=None)
    assert any("恰好用完" in failure for failure in result.turns[0].failures)


def test_full_strategy_reproduces_f4_f5(cases, tmp_path: Path) -> None:
    """P7-S4-A01：全量暴露下 F4（schema 超 4500）与 F5（指引截掉读图 / MCP 工具）可复现。"""

    drawing = tx.run_case(_case(cases, "drawing", drawing=True, mcp=False), tmp_path, strategy="full")
    assert "read_drawing" in drawing.turns[0].exposed
    assert "read_drawing" in drawing.turns[0].not_in_guide
    assert drawing.turns[0].guide_truncated

    pressure = tx.run_case(_case(cases, "budget", drawing=True, mcp=True), tmp_path, strategy="full")
    assert pressure.turns[0].schema_chars_max > tx.SCHEMA_BUDGET


def test_missing_must_expose_is_a_load_problem(tmp_path: Path) -> None:
    """P7-S4-A01：缺 ``must_expose`` 的用例是加载问题，不能当成"什么都不需要"而通过。"""

    path = tmp_path / "bad.jsonl"
    path.write_text(
        json.dumps({"id": "x", "category": "c", "turns": [{"query": "q", "script": [{"content": "收到。"}]}]})
        + "\n"
        + json.dumps({"id": "x", "category": "c", "turns": [{"query": "q", "script": [{"content": "a"}], "must_expose": []}]})
        + "\n"
        + json.dumps({"id": "x", "category": "c", "turns": [{"query": "q", "script": [{"content": "a"}], "must_expose": []}]})
        + "\n",
        encoding="utf-8",
    )
    loaded, problems = tx.load_cases(path)
    assert [case.id for case in loaded] == ["x"]
    assert len(problems) == 2
    assert "must_expose" in problems[0]
    assert "重复" in problems[1]
    assert tx.main([str(path)]) == 2


def test_unmeasured_rates_are_not_a_pass() -> None:
    """P7-S4-A01：分母为 0 的速率记 None，且 None 判不达标（未测量不是通过）。"""

    metrics = tx.aggregate([], [])
    assert metrics["needed_tool_exposed_rate"] is None
    assert metrics["unexposed_call_executed_rate"] is None
    violations = tx.hard_violations(metrics)
    assert any(item.startswith("needed_tool_exposed_rate=None") for item in violations)
    assert any(item.startswith("unexposed_call_executed_rate=None") for item in violations)


def test_dataset_covers_every_category(cases) -> None:
    """P7-S4-A01：数据集覆盖执行 Prompt §1 第 1 条列出的全部类别，并有多轮与未开放调用用例。"""

    categories = {case.category for case in cases.values()}
    assert {
        "clause_qa",
        "calc",
        "materials",
        "drawing",
        "drawing_materials",
        "mcp",
        "budget",
        "multi_turn",
        "chitchat",
        "unexposed_call",
    } <= categories
    assert 30 <= len(cases) <= 40
    assert any(turn.expect_unexposed_call for case in cases.values() for turn in case.turns)
