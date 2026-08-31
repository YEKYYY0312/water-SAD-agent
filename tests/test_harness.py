"""Tests for the JSONL evaluation harness."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from water_agent.domain.models import EvalCase
from water_agent.harness.doubles import BadCitationGenerator
from water_agent.harness.loader import EvalLoadError, load_eval_bundle, load_eval_cases
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
