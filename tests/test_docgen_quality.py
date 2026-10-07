"""P2 docgen 质量层：受限 prose 润色、确定性自检与 real blocked 语义。"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from water_agent.docgen import (
    RealPolishTransport,
    build_polish_prompt,
    numeric_preservation_problems,
    render_docgen_doc,
    run_quality_polish,
    strip_polish_meta_lines,
)
from water_agent.domain.models import DocgenQualityCase
from water_agent.harness.loader import load_eval_cases
from water_agent.harness.runner import HarnessRunner

REPO_ROOT = Path(__file__).resolve().parents[1]
QUALITY_SET = REPO_ROOT / "data" / "eval" / "docgen_quality.jsonl"


def _base_doc():
    return render_docgen_doc(
        query="生成 DN150 给水管道计算书",
        doc_type="calculation_book",
        calc_result={"diameter_mm": 150, "flow_lps": 20, "velocity_mps": 1.131768},
        citations=["FIX 1.1.1"],
    )


def _identity_polish(_system: str, user: str) -> str:
    return "\n".join(strip_polish_meta_lines(user))


class TestQualityPolish:
    def test_prompt_metadata_cannot_be_confused_with_content_doc_type_line(self) -> None:
        prompt = build_polish_prompt(_base_doc())
        assert "[[DOC_META " in prompt
        assert "[[SECTION_META " in prompt
        assert "- 文档类型：计算书" in strip_polish_meta_lines(prompt)

    def test_compliant_paraphrase_preserves_programmatic_values_and_citations(self) -> None:
        base = _base_doc()

        def paraphrase(_system: str, user: str) -> str:
            text = "\n".join(strip_polish_meta_lines(user))
            return text.replace("项目 / 查询：", "项目说明：").replace(
                "供人工复核后确认", "应由专业人员复核确认"
            )

        verdict = run_quality_polish(base, polish=paraphrase)
        assert verdict.verdict == "pass", verdict.problems
        assert verdict.polished is not None
        assert verdict.polished.calc_values == base.calc_values
        assert verdict.polished.citation_ids == base.citation_ids
        assert verdict.used_real_model is False

    def test_numeric_mutation_is_rejected(self) -> None:
        base = _base_doc()

        def mutate_number(_system: str, user: str) -> str:
            text = "\n".join(strip_polish_meta_lines(user))
            return text.replace("velocity_mps = 1.13177", "velocity_mps = 9.999", 1)

        verdict = run_quality_polish(base, polish=mutate_number)
        assert verdict.verdict == "fail"
        assert any("数值被改写" in problem for problem in verdict.problems)

    def test_undeclared_citation_is_rejected(self) -> None:
        base = _base_doc()

        def inject_citation(_system: str, user: str) -> str:
            lines = strip_polish_meta_lines(user)
            lines[0] += "；另见 FIX 9.9.9"
            return "\n".join(lines)

        verdict = run_quality_polish(base, polish=inject_citation)
        assert verdict.verdict == "fail"
        assert any("未经声明的引用：FIX 9.9.9" in problem for problem in verdict.problems)

    def test_numeric_line_cannot_be_removed_while_preserving_line_count(self) -> None:
        base = _base_doc()

        def remove_numeric_line(_system: str, user: str) -> str:
            text = "\n".join(strip_polish_meta_lines(user))
            return text.replace(
                "- flow_lps = 20 L/s",
                "- 设计流量已经完成程序化核算",
                1,
            )

        verdict = run_quality_polish(base, polish=remove_numeric_line)
        assert verdict.verdict == "fail"
        assert any("数字被增删或移位" in problem for problem in verdict.problems)

    def test_existing_number_cannot_be_reused_on_another_line(self) -> None:
        base = _base_doc()

        def reuse_number(_system: str, user: str) -> str:
            lines = strip_polish_meta_lines(user)
            lines[1] += "，共 150 项"
            return "\n".join(lines)

        verdict = run_quality_polish(base, polish=reuse_number)
        assert verdict.verdict == "fail"
        assert any("数字被增删或移位" in problem for problem in verdict.problems)

    def test_line_count_damage_is_blocked_not_skip(self) -> None:
        base = _base_doc()

        def drop_line(_system: str, user: str) -> str:
            return "\n".join(strip_polish_meta_lines(user)[:-1])

        verdict = run_quality_polish(base, polish=drop_line)
        assert verdict.verdict == "blocked"
        assert verdict.skipped is False
        assert verdict.polished is None

    def test_missing_key_is_structured_skip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        verdict = run_quality_polish(_base_doc())
        assert verdict.verdict == "blocked"
        assert verdict.skipped is True
        assert "SKIP" in verdict.blocked_reason

    def test_real_transport_failure_is_blocked_not_skip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

        def unavailable(_system: str, _user: str) -> str:
            raise ConnectionError("network unavailable")

        verdict = run_quality_polish(
            _base_doc(),
            polish=unavailable,
            used_real_model=True,
        )
        assert verdict.verdict == "blocked"
        assert verdict.skipped is False
        assert verdict.used_real_model is False
        assert "ConnectionError" in verdict.blocked_reason

    def test_real_model_flag_requires_explicit_real_transport(self) -> None:
        verdict = run_quality_polish(
            _base_doc(),
            polish=_identity_polish,
            used_real_model=True,
        )
        assert verdict.verdict == "pass"
        assert verdict.used_real_model is True

    def test_real_transport_records_token_usage(self) -> None:
        class Response:
            def __init__(self) -> None:
                self.content = "polished"
                self.usage_metadata = {"input_tokens": 100, "output_tokens": 20}

        class FakeLLM:
            def invoke(self, _messages):
                return Response()

        transport = RealPolishTransport()
        transport._llm = FakeLLM()
        assert transport("system", "user") == "polished"
        assert transport.last_usage is not None
        assert transport.last_usage.input_tokens == 100
        assert transport.last_usage.output_tokens == 20
        assert transport.last_usage.total_tokens == 120

        class FailingLLM:
            def invoke(self, _messages):
                raise ConnectionError("network unavailable")

        transport._llm = FailingLLM()
        with pytest.raises(ConnectionError, match="network unavailable"):
            transport("system", "user")
        assert transport.last_usage is None


class TestNumericPreservationIdentifiers:
    """编号尾巴碰巧是溢出的科学计数法字面量（多 Agent CLI 偶发失败的根因）。

    管段 ID 是 ``seg-`` + 12 位 hex，hex 只由数字和一个 ``e`` 组成时（约 0.27%），
    ``seg-21634e590093`` 的尾巴被读成 2.1634e590097 → ``float`` 溢出为 inf。有限的同类尾巴
    （全数字 hex）一直靠底座白名单放行，溢出的那种却在查白名单之前就被判"非有限数值"。
    """

    _SOURCE_LINE = "- 1. steel DN200：25.0 m（来源：seg-21634e590093(25m)）"

    def test_identifier_copied_verbatim_from_the_base_is_not_an_invented_number(self) -> None:
        assert numeric_preservation_problems(self._SOURCE_LINE, {}, base_text=self._SOURCE_LINE) == []

    def test_overflowing_literal_absent_from_the_base_is_still_rejected(self) -> None:
        """守护（修复前后都成立）：底座里没有的溢出字面量照旧判非有限，白名单只认逐字出现过的 token。"""
        assert numeric_preservation_problems("- 另有 1e999 项", {}, base_text="- 另有若干项") == [
            "正文出现非有限数值：1e999"
        ]
        assert "正文出现非有限数值：21634e590093" in numeric_preservation_problems(self._SOURCE_LINE, {})


class TestDocgenQualityHarness:
    def test_loader_parses_quality_cases(self) -> None:
        cases = load_eval_cases(QUALITY_SET)
        assert len(cases) == 4
        assert all(isinstance(case, DocgenQualityCase) for case in cases)

    def test_offline_quality_suite_all_green(self, tmp_path: Path) -> None:
        runner = HarnessRunner(
            eval_path=QUALITY_SET,
            report_dir=tmp_path / "reports",
            trace_dir=tmp_path / "traces",
        )
        report, report_path, issues = runner.run()
        assert issues == []
        assert report.total == 4
        assert report.passed == 4
        assert report.failed == 0
        assert report.skipped == 0
        assert report_path.exists()
        stamp = report_path.stem.removeprefix("docgen_quality_")
        assert re.fullmatch(r"\d{8}T\d{12}Z(?:_\d+)?", stamp)

    def test_offline_negative_modes_prove_self_check_and_blocked_semantics(
        self, tmp_path: Path
    ) -> None:
        eval_path = tmp_path / "negative-quality.jsonl"
        eval_path.write_text(
            "\n".join(
                json.dumps(payload, ensure_ascii=False)
                for payload in [
                    {
                        "id": "q-fail",
                        "type": "docgen_quality",
                        "query": "q",
                        "doc_type": "calculation_book",
                        "calc_result": {"flow_lps": 20},
                        "citations": [],
                        "polish_mode": "mutate_number",
                        "expected_verdict": "fail",
                    },
                    {
                        "id": "q-blocked",
                        "type": "docgen_quality",
                        "query": "q",
                        "doc_type": "calculation_book",
                        "calc_result": {"flow_lps": 20},
                        "citations": [],
                        "polish_mode": "drop_line",
                        "expected_verdict": "blocked",
                    },
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        runner = HarnessRunner(
            eval_path=eval_path,
            report_dir=tmp_path / "reports",
            trace_dir=tmp_path / "traces",
        )
        report, _report_path, issues = runner.run()
        assert issues == []
        assert report.passed == 1
        assert report.failed == 1
        assert report.skipped == 0
        assert [result.actual_status for result in report.cases] == ["fail", "blocked"]
        assert report.cases[1].passed is False

    def test_real_missing_key_skips_without_retrieval_prerequisites(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        runner = HarnessRunner(
            eval_path=QUALITY_SET,
            variant="real",
            report_dir=tmp_path / "reports",
            trace_dir=tmp_path / "traces",
        )
        report, _report_path, issues = runner.run()
        assert issues == []
        assert report.skipped == report.total == 4
        assert report.failed == 0
        assert all(result.actual_status == "blocked" for result in report.cases)
        assert all(result.quality_used_real_model is False for result in report.cases)
        assert all("DEEPSEEK_API_KEY" in result.failure_reason for result in report.cases)
        assert all("DASHSCOPE_API_KEY" not in result.failure_reason for result in report.cases)

    def test_real_transport_failure_is_reported_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        runner = HarnessRunner(
            eval_path=QUALITY_SET,
            variant="real",
            report_dir=tmp_path / "reports",
            trace_dir=tmp_path / "traces",
        )

        def unavailable(_system: str, _user: str) -> str:
            raise ConnectionError("network unavailable")

        runner._real_polisher = unavailable
        report, _report_path, issues = runner.run()
        assert issues == []
        assert report.failed == report.total == 4
        assert report.skipped == 0
        assert all(result.actual_status == "blocked" for result in report.cases)
        assert all("ConnectionError" in result.failure_reason for result in report.cases)
        assert all(result.quality_output_text == "" for result in report.cases)
