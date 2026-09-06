"""Tests for the JSONL evaluation harness."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from water_agent.domain.models import EvalCase, EvalResult
from water_agent.harness.loader import EvalLoadError, load_eval_cases
from water_agent.harness.runner import HarnessRunner, main
from water_agent.harness.scorer import score_eval_case

REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL_PATH = REPO_ROOT / "data" / "eval" / "rag_p0.jsonl"
FIXTURE_PATH = REPO_ROOT / "data" / "fixtures" / "rag_fixture.jsonl"


class TestEvalLoader:
    def test_loads_valid_jsonl(self) -> None:
        cases = load_eval_cases(EVAL_PATH, strict=False)
        assert len(cases) == 12
        assert cases[0].id == "rag-p0-0001"

    def test_invalid_jsonl_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.jsonl"
        path.write_text('{"id": "ok", "type": "rag", "query": "测试"}\nnot-json\n', encoding="utf-8")
        with pytest.raises(EvalLoadError, match="invalid json"):
            load_eval_cases(path)

    def test_missing_required_field_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "missing.jsonl"
        path.write_text('{"id": "case-1", "type": "rag"}\n', encoding="utf-8")
        with pytest.raises(EvalLoadError, match="Field required"):
            load_eval_cases(path)


class TestScoring:
    def test_single_case_pass(self) -> None:
        case = EvalCase(id="c1", query="给水管道的设计秒流量", gold_articles_any=["FIX 1.1.1"])
        result = score_eval_case(
            case,
            actual_status="succeeded",
            actual_citations=["FIX 1.1.1"],
            context_articles=["FIX 1.1.1", "FIX 1.1.2"],
        )
        assert result.passed is True
        assert result.failure_reason == ""
        assert result.gold_articles_any_hit == ["FIX 1.1.1"]

    def test_gold_articles_any_required(self) -> None:
        case = EvalCase(id="c2", query="给水管道", gold_articles_any=["FIX 1.1.2"])
        result = score_eval_case(case, actual_status="succeeded", actual_citations=["FIX 1.1.1"], context_articles=["FIX 1.1.1"])
        assert result.passed is False
        assert "gold_articles_any" in result.failure_reason

    def test_gold_articles_all_required(self) -> None:
        case = EvalCase(id="c3", query="给水管道", gold_articles_all=["FIX 1.1.1", "FIX 2.1.1"])
        result = score_eval_case(case, actual_status="succeeded", actual_citations=["FIX 1.1.1"], context_articles=["FIX 1.1.1", "FIX 2.1.1"])
        assert result.passed is False
        assert "gold_articles_all missing" in result.failure_reason

    def test_forbidden_articles_fail(self) -> None:
        case = EvalCase(id="c4", query="给水管道", gold_articles_any=["FIX 1.1.1"], forbidden_articles=["FIX 2.1.2"])
        result = score_eval_case(case, actual_status="succeeded", actual_citations=["FIX 1.1.1", "FIX 2.1.2"], context_articles=["FIX 1.1.1", "FIX 2.1.2"])
        assert result.passed is False
        assert "forbidden_articles hit" in result.failure_reason

    def test_status_mismatch_fails(self) -> None:
        case = EvalCase(id="c5", query="给水管道", expected_status="failed")
        result = score_eval_case(case, actual_status="succeeded", actual_citations=[], context_articles=[])
        assert result.passed is False
        assert "expected status failed, got succeeded" in result.failure_reason

    def test_citation_must_come_from_context(self) -> None:
        case = EvalCase(id="c6", query="给水管道", gold_articles_any=["FIX 1.1.1"])
        result = score_eval_case(case, actual_status="succeeded", actual_citations=["FIX 1.1.1"], context_articles=["FIX 2.1.1"])
        assert result.passed is False
        assert "citations not present in retrieved context" in result.failure_reason


class _FixedOrderRetriever:
    """按固定 source_id 顺序返回夹具条款的检索器（query 无关，测试确定性）。

    刻意让 forbidden 条款与 gold 条款同时出现在召回里，验证 runner 不做任何剔除。
    """

    def __init__(self, source_ids: list[str]) -> None:
        from water_agent.retrieval.loader import load_clauses_jsonl

        clauses = load_clauses_jsonl(FIXTURE_PATH)
        self._by_id = {c.source_id: c for c in clauses}
        self._ids = [i for i in source_ids if i in self._by_id]

    def retrieve(self, query: str, top_k: int = 5):
        return [self._by_id[i] for i in self._ids[:top_k]]


class TestNoGoldLeakInRealAgenticRun:
    """P1/Step 10 评审：评测标签（forbidden_articles）不得泄漏进真实 agentic 检索输入。

    评审前的修复把 forbidden 条款做成 runner 层检索过滤器（_FilteringRetriever），
    等于用 gold 改写模型输入与检索指标——被评审判为泄漏。正确语义：
    - forbidden 条款若被真实召回，就原样进入模型上下文（模型可能引用它）；
    - 判分只在模型完成回答后由 scorer 执行（引用 forbidden → 判负）；
    - Recall@k/MRR 与 context_articles 基于真实检索返回结果，无 gold 后处理。
    """

    @pytest.fixture
    def real_offline(self, monkeypatch: pytest.MonkeyPatch):
        from water_agent.harness import runner as runner_mod

        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-dummy")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dummy")
        monkeypatch.setattr(runner_mod.HarnessRunner, "_probe_real_skip", lambda self: None)
        monkeypatch.setattr(
            runner_mod.HarnessRunner,
            "_build_real_retriever",
            lambda self: _FixedOrderRetriever(["FIX 1.1.1", "FIX 2.1.1", "FIX 1.1.2"]),
        )
        return runner_mod

    def _case_file(self, tmp_path: Path, *, forbidden: list[str], cited: list[str]) -> Path:

        case = {
            "id": "fb-1",
            "type": "rag",
            "variant": "real",
            "query": "新建地区的排水系统应如何确定排水体制？",
            "gold_articles_any": ["FIX 1.1.1"],
            "forbidden_articles": list(forbidden),
        }
        path = tmp_path / "fb.jsonl"
        path.write_text(json.dumps(case, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

    def _responses(self, cited: list[str]):
        from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
        from langchain_core.messages import AIMessage

        responses = [
            AIMessage(content="", tool_calls=[{"name": "retrieve_clauses", "args": {"query": "排水体制", "top_k": 5}, "id": "c1"}]),
        ]
        if cited:
            responses.append(AIMessage(content="", tool_calls=[{"name": "cite_clauses", "args": {"article_ids": list(cited)}, "id": "c2"}]))
        responses.append(AIMessage(content="新建地区应采用分流制排水系统，并采取雨污分流措施。"))
        return FakeMessagesListChatModel(responses=responses)

    def _run(self, tmp_path: Path, real_offline, monkeypatch: pytest.MonkeyPatch, *, forbidden: list[str], cited: list[str]):
        path = self._case_file(tmp_path, forbidden=forbidden, cited=cited)
        test_self = self
        monkeypatch.setattr(
            real_offline.HarnessRunner,
            "_build_real_chat_model",
            lambda runner: test_self._responses(cited),
        )
        runner = HarnessRunner(
            eval_path=path,
            report_dir=tmp_path / "r",
            trace_dir=tmp_path / "t",
            fixture_path=FIXTURE_PATH,
            variant="real",
            rerank="none",
        )
        report, _, _ = runner.run()
        return report.cases[0]

    def test_forbidden_clause_enters_model_context_unchanged(self, tmp_path, real_offline, monkeypatch) -> None:
        """forbidden 条款原样进入模型上下文：不剔除、不改序，模型没引用它 → 判分通过。"""
        case = self._run(tmp_path, real_offline, monkeypatch, forbidden=["FIX 2.1.1"], cited=["FIX 1.1.1"])
        assert case.passed is True, case.failure_reason
        assert case.context_articles == ["FIX 1.1.1", "FIX 2.1.1", "FIX 1.1.2"], (
            "检索上下文必须是真实召回顺序（含 forbidden 条款），禁止 runner 层 gold 后处理"
        )
        assert case.actual_citations == ["FIX 1.1.1"]
        assert case.forbidden_articles_hit == []
        # 检索指标按真实召回计算（gold 在 rank1）
        assert case.retrieval_metrics["recall_at_1"] == 1.0
        assert case.retrieval_metrics["mrr"] == 1.0

    def test_model_citing_forbidden_clause_fails_scoring(self, tmp_path, real_offline, monkeypatch) -> None:
        """模型引用 forbidden 条款 → scorer 在回答完成后判负（runner 不预过滤）。"""
        case = self._run(tmp_path, real_offline, monkeypatch, forbidden=["FIX 2.1.1"], cited=["FIX 2.1.1"])
        assert case.passed is False
        assert "forbidden_articles hit" in case.failure_reason
        assert "FIX 2.1.1" in case.context_articles, "即使被判负，上下文也要保留真实召回（泄漏修复后不剔除）"

    def test_retrieval_metrics_keep_true_rank_of_forbidden_clause(self) -> None:
        """检索指标保留 forbidden 条款的真实排名（scorer 层确定性验证）。"""
        case = EvalCase(
            id="rank-fb",
            query="排水体制",
            gold_articles_any=["FIX 1.1.1"],
            forbidden_articles=["FIX 2.1.1"],
        )
        result = score_eval_case(
            case,
            actual_status="succeeded",
            actual_citations=["FIX 1.1.1"],
            # forbidden 相关条款按真实召回占据 rank1，gold 在 rank2
            context_articles=["FIX 2.1.1", "FIX 1.1.1"],
        )
        assert result.passed is True
        assert result.retrieval_metrics["recall_at_1"] == 0.0
        assert result.retrieval_metrics["mrr"] == 0.5
        assert result.retrieval_metrics["recall_at_5"] == 1.0
        # 若像评审前那样把 forbidden 从召回里剔除，gold 会升到 rank1（recall@1=1, mrr=1），
        # 指标将不再是"真实检索质量"——上面的数值锁定了无后处理口径。
        assert result.forbidden_articles_hit == []


class TestCitationOkRateZeroCitation:
    """P1/Step 10 评审：零引用对存在 expected_articles 的 RAG 用例计 citation failure。"""

    def _result(self, *, expected: list[str], cited: list[str], ctx: list[str]) -> EvalResult:
        return EvalResult(
            case_id="c",
            passed=True,
            expected_status="succeeded",
            actual_status="succeeded",
            expected_articles=list(expected),
            actual_citations=list(cited),
            context_articles=list(ctx),
        )

    def test_zero_citation_with_expected_articles_is_citation_failure(self) -> None:
        from water_agent.harness.runner import citation_ok_for_result

        r = self._result(expected=["FIX 1.1.1"], cited=[], ctx=["FIX 1.1.1"])
        assert citation_ok_for_result(r) is False, "有 gold 引用要求的用例零引用必须计失败"

    def test_zero_citation_without_expected_articles_not_penalized(self) -> None:
        from water_agent.harness.runner import citation_ok_for_result

        r = self._result(expected=[], cited=[], ctx=[])
        assert citation_ok_for_result(r) is True, "不需要引用的用例不得被误伤"

    def test_citations_all_in_context_is_ok(self) -> None:
        from water_agent.harness.runner import citation_ok_for_result

        r = self._result(expected=["FIX 1.1.1"], cited=["FIX 1.1.1"], ctx=["FIX 1.1.1"])
        assert citation_ok_for_result(r) is True

    def test_citation_outside_context_not_ok(self) -> None:
        from water_agent.harness.runner import citation_ok_for_result

        r = self._result(expected=["FIX 1.1.1"], cited=["FAKE 0.0.0"], ctx=["FIX 1.1.1"])
        assert citation_ok_for_result(r) is False


class TestQualityVerdict:
    """质量层 judge 判定：阻断维度达标才算 quality_pass；judge 低分不得显示为通过。"""

    def test_all_blocking_dimensions_at_or_above_pass(self) -> None:
        from water_agent.harness.quality import judge_quality_verdict

        verdict, reasons = judge_quality_verdict(
            {"faithfulness": 4, "citation_coverage": 4, "no_fabrication": 5, "completeness": 2}
        )
        assert verdict is True
        assert reasons == [], f"非阻断维度（completeness）低分不影响阻断判定: {reasons}"

    def test_blocking_dimension_1of5_is_not_quality_pass(self) -> None:
        from water_agent.harness.quality import judge_quality_verdict

        verdict, reasons = judge_quality_verdict(
            {"faithfulness": 1, "citation_coverage": 5, "no_fabrication": 5, "clarity": 5}
        )
        assert verdict is False
        assert any("faithfulness=1" in r for r in reasons), f"失败原因必须点名低分维度: {reasons}"

    def test_missing_blocking_dimension_is_not_pass(self) -> None:
        from water_agent.harness.quality import judge_quality_verdict

        verdict, reasons = judge_quality_verdict({"faithfulness": 5, "no_fabrication": 5})
        assert verdict is False
        assert any("citation_coverage" in r for r in reasons)

    def test_empty_scores_means_not_judged(self) -> None:
        from water_agent.harness.quality import judge_quality_verdict

        verdict, reasons = judge_quality_verdict({})
        assert verdict is None
        assert reasons, "judge 未评分时必须给出未判定的原因"

    def test_low_non_blocking_dimension_does_not_block_pass(self) -> None:
        from water_agent.harness.quality import judge_quality_verdict

        verdict, _reasons = judge_quality_verdict(
            {"faithfulness": 5, "citation_coverage": 5, "no_fabrication": 5, "completeness": 1}
        )
        assert verdict is True


class TestHarnessRunner:
    def test_bundle_report_and_trace_id_association(self, tmp_path: Path) -> None:
        runner = HarnessRunner(
            eval_path=EVAL_PATH,
            report_dir=tmp_path / "reports",
            trace_dir=tmp_path / "traces",
            fixture_path=FIXTURE_PATH,
        )
        report, report_path, load_issues = runner.run()
        assert report.total == 12
        assert report.passed >= 8
        assert report.failed == report.total - report.passed
        assert report.pass_rate == pytest.approx(report.passed / report.total)
        assert report_path.exists()
        assert len(load_issues) == 1
        assert load_issues[0]["line_no"] == 13
        assert all(case.trace_id for case in report.cases)

    def test_report_file_is_valid_json(self, tmp_path: Path) -> None:
        runner = HarnessRunner(
            eval_path=EVAL_PATH,
            report_dir=tmp_path / "reports",
            trace_dir=tmp_path / "traces",
            fixture_path=FIXTURE_PATH,
        )
        report, report_path, _ = runner.run()
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        assert payload["total"] == report.total
        assert payload["passed"] == report.passed
        assert payload["failed"] == report.failed
        assert "created_at" in payload
        assert isinstance(payload["cases"], list)

    def test_failed_case_from_bad_citation_variant(self, tmp_path: Path) -> None:
        runner = HarnessRunner(
            eval_path=EVAL_PATH,
            report_dir=tmp_path / "reports",
            trace_dir=tmp_path / "traces",
            fixture_path=FIXTURE_PATH,
        )
        report, _, _ = runner.run()
        bad_case = next(case for case in report.cases if case.case_id == "rag-p0-0011")
        assert bad_case.passed is False
        assert bad_case.actual_status == "failed"
        assert bad_case.trace_id


class TestCliEntryPoint:
    def test_main_returns_nonzero_on_failed_eval(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        report_dir = tmp_path / "reports"
        trace_dir = tmp_path / "traces"
        exit_code = main([str(EVAL_PATH), "--report-dir", str(report_dir), "--trace-dir", str(trace_dir), "--fixture-path", str(FIXTURE_PATH)])
        out = capsys.readouterr()
        assert exit_code == 2
        assert '"ok": true' in out.out
        assert '"failed": 1' in out.out
        assert report_dir.exists()
        assert list(report_dir.glob("*.json"))

    def test_main_returns_nonzero_on_invalid_jsonl(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        bad = tmp_path / "bad.jsonl"
        bad.write_text('{"id": "case-1", "type": "rag"}\nnot-json\n', encoding="utf-8")
        exit_code = main([str(bad), "--report-dir", str(tmp_path / "reports"), "--trace-dir", str(tmp_path / "traces"), "--fixture-path", str(FIXTURE_PATH)])
        out = capsys.readouterr()
        assert exit_code == 2
        assert '"ok": true' in out.out
        assert '"load_issues"' in out.out
