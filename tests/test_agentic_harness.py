"""mock-agentic 机制层（agentic_p0）：生产 AgenticSession 装配 + 逐步脚本模型。

PR 门禁此前对生产 Web 走的 agentic 图没有任何 harness 级覆盖——RAG harness 的 mock
变体只跑 legacy 图（review 2026-09-30）。这里的用例都经 ``AgenticSession`` 运行；
每条"变异"测试破坏一处真实的 loop 机制，断言套件因此变红，证明它有区分力。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from water_agent.domain.models import AgenticCase
from water_agent.harness.doubles import ScriptedChatModel, ScriptExhausted
from water_agent.harness.loader import load_eval_bundle
from water_agent.harness.runner import HarnessRunner

REPO_ROOT = Path(__file__).resolve().parents[1]
SUITE = REPO_ROOT / "data" / "eval" / "agentic_p0.jsonl"


def _run(tmp_path: Path, eval_path: Path = SUITE):
    runner = HarnessRunner(eval_path=eval_path, report_dir=tmp_path / "reports", trace_dir=tmp_path / "traces")
    report, _, load_issues = runner.run()
    return report, load_issues


def _by_id(report) -> dict:
    return {case.case_id: case for case in report.cases}


def test_suite_loads_as_agentic_cases() -> None:
    bundle = load_eval_bundle(SUITE)
    assert bundle.issues == []
    assert bundle.cases and all(isinstance(case, AgenticCase) for case in bundle.cases)


def test_shipped_suite_is_all_green(tmp_path: Path) -> None:
    report, load_issues = _run(tmp_path)
    assert load_issues == []
    assert report.failed == 0, [(c.case_id, c.failure_reason) for c in report.cases if not c.passed]


def test_runs_through_the_production_session_one_per_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """装配走生产 AgenticSession（不是 runner 自己拼一套），多轮时每轮一个新 session。"""
    from water_agent.runtime import agentic

    created: list[str] = []
    original = agentic.AgenticSession.__init__

    def spy(self, *args, **kwargs):
        created.append(kwargs["thread_id"])
        original(self, *args, **kwargs)

    monkeypatch.setattr(agentic.AgenticSession, "__init__", spy)
    report, _ = _run(tmp_path)
    turns = sum(len(case.turns) for case in load_eval_bundle(SUITE).cases)
    assert len(created) == turns
    # 同一用例的各轮共用一个 thread（同 Web：thread 跨 run 持续）
    assert len(set(created)) == len(report.cases)


def test_disabling_the_citation_validator_turns_the_memory_citation_trap_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """变异：agentic 引用校验恒放行 → "不检索凭记忆引用"陷阱必须变红。"""
    from water_agent.domain.citation import CitationCheckResult
    from water_agent.graphs import agent_rag

    monkeypatch.setattr(
        agent_rag, "validate_citations", lambda cited, context, **_: CitationCheckResult(ok=True)
    )
    report, _ = _run(tmp_path)
    trap = _by_id(report)["agentic-p0-0007"]
    assert trap.passed is False
    assert "expected status failed, got succeeded" in trap.failure_reason


def test_dropping_the_per_turn_reset_leaks_citations_into_the_next_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """变异：不再逐轮归零无 reducer 的 state → 第二轮寒暄带着第一轮的引用，必须变红。"""
    from water_agent.graphs import agent_rag

    monkeypatch.setattr(agent_rag, "PER_TURN_RESET", {})
    report, _ = _run(tmp_path)
    assert _by_id(report)["agentic-p0-0008"].passed is False


def test_removing_the_zero_citation_reflow_is_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """变异：零引用不再回流 → 回流后补声明的用例失败，且脚本剩余步被报出。"""
    from water_agent.graphs import agent_rag

    monkeypatch.setattr(agent_rag, "EMPTY_CITATION_MAX_REFLOWS", 0)
    report, _ = _run(tmp_path)
    reflowed = _by_id(report)["agentic-p0-0003"]
    assert reflowed.passed is False
    assert "unused step" in reflowed.failure_reason


def test_a_loop_that_turns_once_more_than_scripted_fails_the_case(tmp_path: Path) -> None:
    """脚本耗尽（模型被多调一次）是行为差异，判失败而不是复用脚本继续跑。"""
    case = {
        "id": "short-script",
        "type": "agentic",
        "turns": [
            {
                "query": "给水管道的设计秒流量如何确定",
                "script": [
                    {"tool_calls": [{"name": "retrieve_clauses", "args": {"query": "给水管道的设计秒流量"}}]},
                    {"content": "设计秒流量按规范公式计算。"},
                ],
            }
        ],
    }
    path = tmp_path / "short.jsonl"
    path.write_text(json.dumps(case, ensure_ascii=False) + "\n", encoding="utf-8")
    report, _ = _run(tmp_path, path)
    assert report.cases[0].passed is False
    assert report.cases[0].actual_status == "error"
    assert "ScriptExhausted" in report.cases[0].failure_reason


def test_scripted_model_never_recycles_its_script() -> None:
    from langchain_core.messages import HumanMessage

    model = ScriptedChatModel(steps=[{"content": "只有一步"}])
    assert model._generate([HumanMessage(content="q")]).generations[0].message.content == "只有一步"
    with pytest.raises(ScriptExhausted):
        model._generate([HumanMessage(content="q")])
    assert model.state["calls"] == 2
