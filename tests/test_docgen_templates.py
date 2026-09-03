"""P2 测试：文档模板引擎（计算书/设计说明结构化渲染、数值/引用纪律、结构合规）。"""
from __future__ import annotations

import pytest

from water_agent.docgen import (
    REQUIRED_SECTIONS,
    SUPPORTED_DOC_TYPES,
    RenderedDoc,
    check_docgen_self_check,
    check_structure,
    render_docgen_doc,
    validate_citation_policy,
)
from water_agent.domain.citation import CitationClaim


def _render(**kwargs) -> RenderedDoc:
    base = {
        "query": "生成 DN150 给水管道计算书",
        "doc_type": "calculation_book",
        "calc_result": {"diameter_mm": 150, "flow_lps": 20, "velocity_mps": 1.131768},
        "citations": ["FIX 1.1.1"],
    }
    base.update(kwargs)
    return render_docgen_doc(**base)


class TestRenderBasics:
    def test_calc_book_sections_and_title(self) -> None:
        doc = _render()
        assert doc.title == "# 计算书"
        keys = doc.section_keys()
        assert keys == ["工程概况", "设计依据", "计算参数与结果", "结论"]
        ok, missing = check_structure(doc)
        assert ok and missing == []

    def test_required_sections_matches_doc_type_registry(self) -> None:
        for doc_type in SUPPORTED_DOC_TYPES:
            doc = _render(doc_type=doc_type, calc_result={"slope": 0.003})
            required = REQUIRED_SECTIONS[doc_type]
            ok, missing = check_structure(doc)
            assert ok and missing == []
            assert set(doc.section_keys()) == set(required), doc_type

    def test_draft_title_contains_round(self) -> None:
        doc = _render(draft_no=2)
        assert doc.title == "# 计算书草稿（第 2 稿）"

    def test_unknown_doc_type_raises(self) -> None:
        with pytest.raises(ValueError, match="未知文档类型"):
            _render(doc_type="spec_sheet")

    def test_notes_and_missing_inputs_render_without_numbers(self) -> None:
        doc = _render(calc_result={"missing_inputs": "flow_lps；velocity_mps"})
        assert doc.calc_values == {}
        assert "flow_lps" in doc.text

    def test_design_note_label(self) -> None:
        doc = _render(doc_type="design_note", calc_result=None, citations=["FIX 1.1.1"])
        assert "设计说明" in doc.title
        assert "设计参数与计算结果" in doc.section_keys()


class TestValuesAndCitations:
    def test_numeric_values_kept_exact_and_formatted_with_unit(self) -> None:
        doc = _render(calc_result={"diameter_mm": 150, "flow_lps": 20, "velocity_mps": 1.131768})
        assert doc.calc_values["velocity_mps"] == 1.131768  # 浮点原值（判分用）
        assert "velocity_mps = 1.13177 m/s" in doc.text  # 展示用（g 格式）

    def test_citations_passthrough_claim_objects(self) -> None:
        claims = [CitationClaim(source_id="FIX 1.1.1", declared_by="system")]
        doc = _render(citations=claims)
        assert doc.citation_ids == ["FIX 1.1.1"]
        assert "FIX 1.1.1" in doc.text

    def test_no_citations_renders_explicit_missing_note(self) -> None:
        doc = _render(citations=[])
        assert doc.citation_ids == []
        assert "未声明规范依据" in doc.text

    def test_citation_policy_clean_when_only_inputs_present(self) -> None:
        doc = _render(citations=["FIX 1.1.1", "FIX 3.2.1"])
        assert validate_citation_policy(doc) == []

    def test_citation_policy_flags_undeclared_id(self) -> None:
        # 手工篡改正文（模拟未来模板分支注入）：渲染后往正文塞未声明的条款
        doc = _render(citations=["FIX 1.1.1"])
        doc.sections[2].lines.append("- 依据 FIX 9.9.9 复核")  # 第 3 节（计算参数与结果）
        problems = validate_citation_policy(doc)
        assert any("FIX 9.9.9" in p for p in problems)

    def test_feedback_appears_only_when_provided(self) -> None:
        without = _render(feedback="")
        assert "用户反馈" not in without.text
        with_feedback = _render(feedback="补充管径表格", draft_no=2)
        assert "用户反馈（已吸收）" in with_feedback.text
        assert "补充管径表格" in with_feedback.text


class TestSelfCheck:
    """草稿自检（draft → self_check → approval）：结构/引用/数值完整性三项检查。"""

    def test_self_check_passes_for_valid_render(self) -> None:
        doc = _render()
        problems = check_docgen_self_check(doc, calc_result={"diameter_mm": 150, "flow_lps": 20, "velocity_mps": 1.131768})
        assert problems == []

    def test_self_check_flags_missing_canonical_section(self) -> None:
        doc = _render()
        doc.sections = [section for section in doc.sections if section.key != "结论"]
        problems = check_docgen_self_check(doc)
        assert any("缺少必需章节" in problem and "结论" in problem for problem in problems)

    def test_self_check_flags_undeclared_citation(self) -> None:
        doc = _render(citations=["FIX 1.1.1"])
        doc.sections[2].lines.append("- 依据 FIX 9.9.9 复核")  # 正文出现输入之外的条款
        problems = check_docgen_self_check(doc, calc_result={"velocity_mps": 1.0})
        assert any("FIX 9.9.9" in problem for problem in problems)

    def test_self_check_flags_non_finite_calc_value(self) -> None:
        """NaN/inf 不得进入 calc_values（自检是渲染层之外的双保险）。"""
        doc = _render()
        doc.calc_values["velocity_mps"] = float("nan")
        problems = check_docgen_self_check(doc, calc_result={"velocity_mps": float("nan")})
        assert any("非有限" in problem for problem in problems)

    def test_self_check_flags_silently_dropped_input(self) -> None:
        """calc_result 有输入但渲染后既无数值也无备注 = 静默丢弃，报结构化问题。"""
        doc = _render(calc_result=None)
        problems = check_docgen_self_check(doc, calc_result={"missing_inputs": ""})
        assert any("静默丢弃" in problem for problem in problems)

    def test_self_check_note_rows_do_not_fail_integrity(self) -> None:
        """缺参记录（备注非空）不触发"静默丢弃"误报。"""
        doc = _render(calc_result={"missing_inputs": "flow_lps"})
        assert doc.calc_values == {}
        assert doc.notes
        problems = check_docgen_self_check(doc, calc_result={"missing_inputs": "flow_lps"})
        assert problems == []


class TestMaterialSection:
    def test_material_rows_rendered_when_provided(self) -> None:
        rows = [
            {"material": "球墨铸铁", "spec": "DN150", "unit": "m", "quantity": 200.5, "detail": "S1(120.5m)、S2(80m)"}
        ]
        doc = _render(material_rows=rows)
        assert "材料统计" in doc.section_keys()
        assert "球墨铸铁 DN150" in doc.text
        assert "S1(120.5m)、S2(80m)" in doc.text
