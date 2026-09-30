"""P2 测试：docgen/material 评测类型（loader 解析 / scorer 判分 / runner 端到端）。

- docgen 判分：结构合规 + 数值双阈值 + 引用必须/禁止 + 模板引用策略（不编造）；
- material 判分：行集合精确一致 + 数量双阈值 + 全段溯源覆盖；
- runner：机制层跑新评测集（离线确定性），非法输入用例按 expected_status 判。
"""
from __future__ import annotations

import json
from pathlib import Path

from water_agent.docgen import render_docgen_doc
from water_agent.domain.models import DocgenCase, MaterialCase
from water_agent.harness.loader import load_eval_cases
from water_agent.harness.runner import HarnessRunner
from water_agent.harness.scorer import score_case
from water_agent.materials import aggregate_materials

REPO_ROOT = Path(__file__).resolve().parents[1]


def _docgen_case(**overrides) -> DocgenCase:
    payload = {
        "id": "docgen-t-1",
        "type": "docgen",
        "query": "生成 DN150 给水管道计算书",
        "doc_type": "calculation_book",
        "calc_result": {"diameter_mm": 150, "flow_lps": 20, "velocity_mps": 1.131768},
        "citations": ["FIX 1.1.1"],
        "required_sections": ["工程概况", "设计依据", "计算参数与结果", "结论"],
        "required_values": {"velocity_mps": 1.131768},
        "citations_required": ["FIX 1.1.1"],
        "expected_status": "succeeded",
    }
    payload.update(overrides)
    return DocgenCase.model_validate(payload)


def _material_case(**overrides) -> MaterialCase:
    payload = {
        "id": "mat-t-1",
        "type": "material",
        "query": "",
        "segments": [
            {"segment_id": "S1", "system": "water_supply", "dn_mm": 100, "length_m": 150, "material": "球墨铸铁"},
            {"segment_id": "S2", "system": "water_supply", "dn_mm": 100, "length_m": 50, "material": "球墨铸铁"},
        ],
        "gold_rows": [{"material": "球墨铸铁", "spec": "DN100", "unit": "m", "quantity": 200}],
        "expected_status": "succeeded",
    }
    payload.update(overrides)
    return MaterialCase.model_validate(payload)


def _render_for(case: DocgenCase):
    return render_docgen_doc(
        query=case.query,
        doc_type=case.doc_type,
        calc_result=case.calc_result,
        citations=case.citations,
    )


def _score_render_of(case: DocgenCase):
    """渲染 + 判分同一路径（与 runner._run_docgen_case 同构）：渲染失败走 render_error。"""
    try:
        rendered = _render_for(case)
    except ValueError as exc:
        return score_case(case, rendered=None, render_error=str(exc))
    return score_case(case, rendered=rendered)


class TestLoader:
    def test_parses_docgen_and_material_records(self) -> None:
        cases = load_eval_cases(REPO_ROOT / "data" / "eval" / "docgen_p0.jsonl")
        assert cases and all(isinstance(c, DocgenCase) for c in cases)
        assert len(cases) == 11

        material_cases = load_eval_cases(REPO_ROOT / "data" / "eval" / "material_p0.jsonl")
        assert material_cases and all(isinstance(c, MaterialCase) for c in material_cases)
        assert len(material_cases) == 10

    def test_unknown_type_falls_back_to_rag_eval_case(self, tmp_path: Path) -> None:
        source = tmp_path / "unknown.jsonl"
        source.write_text(json.dumps({"id": "x1", "type": "rag", "query": "q"}), encoding="utf-8")
        cases = load_eval_cases(source)
        assert type(cases[0]).__name__ == "EvalCase"

    def test_extra_forbid_rejects_unknown_field(self, tmp_path: Path) -> None:
        source = tmp_path / "bad.jsonl"
        source.write_text(
            json.dumps({"id": "d1", "type": "docgen", "query": "q", "typo_field": 1}),
            encoding="utf-8",
        )
        try:
            load_eval_cases(source)
            raise AssertionError("应拒绝未知字段")
        except Exception as exc:  # noqa: BLE001
            assert "extra" in str(exc).lower() or "forbidden" in str(exc).lower() or "unexpected" in str(exc).lower()


class TestDocgenScoring:
    def test_passing_case(self) -> None:
        case = _docgen_case()
        result = score_case(case, rendered=_render_for(case))
        assert result.passed, result.failure_reason
        assert result.actual_status == "succeeded"
        assert "FIX 1.1.1" in result.actual_citations

    def test_missing_required_section_fails(self) -> None:
        case = _docgen_case(required_sections=["工程概况", "不存在章节"])
        result = score_case(case, rendered=_render_for(case))
        assert not result.passed
        assert "required sections missing" in result.failure_reason

    def test_canonical_required_sections_always_enforced(self) -> None:
        """case 必需章节清空也不绕过 canonical：I-3 恒查 REQUIRED_SEMENTS[doc_type]。"""
        case = _docgen_case(required_sections=[])
        rendered = _render_for(case)
        rendered.sections = [s for s in rendered.sections if s.key != "结论"]  # 模拟模板缺陷：丢必需章节
        result = score_case(case, rendered=rendered)
        assert not result.passed
        assert "required sections missing: ['结论']" in result.failure_reason

    def test_wrong_value_fails(self) -> None:
        case = _docgen_case(required_values={"velocity_mps": 2.0})
        result = score_case(case, rendered=_render_for(case))
        assert not result.passed and "velocity_mps" in result.failure_reason

    def test_required_citation_missing_fails(self) -> None:
        case = _docgen_case(citations_required=["FIX 9.9.9"])
        result = score_case(case, rendered=_render_for(case))
        assert not result.passed
        assert "required citation not rendered" in result.failure_reason

    def test_forbidden_citation_fails(self) -> None:
        case = _docgen_case(citations=["FIX 1.1.1", "FIX 4.4.4"], citations_forbidden=["FIX 4.4.4"])
        result = score_case(case, rendered=_render_for(case))
        assert not result.passed
        assert "forbidden citation rendered" in result.failure_reason

    def test_expected_invalid_input_case_passes(self) -> None:
        """渲染失败（未知 doc_type）+ expected_status=invalid_input → 判通过（invalid_input 可评测）。"""
        case = _docgen_case(doc_type="spec_sheet", citations=[], required_values={}, expected_status="invalid_input")
        result = _score_render_of(case)
        assert result.passed, result.failure_reason
        assert result.actual_status == "invalid_input"

    def test_unexpected_render_failure_fails(self) -> None:
        """渲染失败但 expected_status=succeeded → 判失败并带可见原因。"""
        case = _docgen_case(doc_type="spec_sheet")
        result = _score_render_of(case)
        assert not result.passed
        assert result.actual_status == "invalid_input"
        assert "未知文档类型" in result.failure_reason


class TestMaterialScoring:
    def test_passing_case(self) -> None:
        case = _material_case()
        table = aggregate_materials(case.segments)
        result = score_case(case, table=table)
        assert result.passed, result.failure_reason

    def test_wrong_quantity_fails(self) -> None:
        case = _material_case(gold_rows=[{"material": "球墨铸铁", "spec": "DN100", "unit": "m", "quantity": 150}])
        table = aggregate_materials(case.segments)
        result = score_case(case, table=table)
        assert not result.passed and "DN100: actual=200.0, gold=150.0" in result.failure_reason

    def test_extra_row_fails(self) -> None:
        case = _material_case()
        table = aggregate_materials(
            [*case.segments, {"segment_id": "S3", "system": "water_supply", "dn_mm": 200, "length_m": 30, "material": "钢管"}]
        )
        result = score_case(case, table=table)
        assert not result.passed and "row set mismatch" in result.failure_reason

    def test_dropped_segment_fails(self) -> None:
        # 聚合结果若丢了一段（覆盖检查），必须判失败——防止静默丢弃输入
        case = _material_case()
        table = aggregate_materials(case.segments)
        table.rows[0].segments = ["S1"]  # 模拟引擎缺陷：S2 丢失
        result = score_case(case, table=table)
        assert not result.passed and "not covered" in result.failure_reason

    def test_invalid_input_case_passes_when_expected(self) -> None:
        case = _material_case(
            segments=[{"segment_id": "N1", "system": "water_supply", "dn_mm": 150, "length_m": -50, "material": "钢管"}],
            gold_rows=[],
            expected_status="invalid_input",
        )
        table = aggregate_materials(case.segments)
        result = score_case(case, table=table)
        assert result.passed, result.failure_reason
        assert result.actual_status == "invalid_input"

    def test_invalid_input_with_gold_rows_fails(self) -> None:
        """invalid_input 用例不得携带 gold_rows：非法输入不可能产出合法行，携带即数据矛盾。"""
        case = _material_case(
            segments=[{"segment_id": "N1", "system": "water_supply", "dn_mm": 150, "length_m": -50, "material": "钢管"}],
            gold_rows=[{"material": "钢管", "spec": "DN150", "unit": "m", "quantity": 50}],
            expected_status="invalid_input",
        )
        table = aggregate_materials(case.segments)
        result = score_case(case, table=table)
        assert not result.passed
        assert "gold_rows" in result.failure_reason

    def test_missing_detail_fails(self) -> None:
        """引擎缺陷模拟：行明细 detail 丢失 → 判失败（溯源必须逐段可见）。"""
        case = _material_case()
        table = aggregate_materials(case.segments)
        table.rows[0].detail = ""
        result = score_case(case, table=table)
        assert not result.passed
        assert "detail" in result.failure_reason

    def test_missing_segment_in_detail_fails(self) -> None:
        """引擎缺陷模拟：detail 缺某段（S2 不在明细里）→ 判失败。"""
        case = _material_case()
        table = aggregate_materials(case.segments)
        table.rows[0].detail = "S1(150m)"
        result = score_case(case, table=table)
        assert not result.passed
        assert "S2" in result.failure_reason

    def test_raw_quantity_drift_fails(self) -> None:
        """引擎缺陷模拟：raw_quantity 与组内长度和不一致 → 判失败（口径审计）。"""
        case = _material_case()
        table = aggregate_materials(case.segments)
        table.rows[0].raw_quantity = 999
        result = score_case(case, table=table)
        assert not result.passed
        assert "raw_quantity" in result.failure_reason

    def test_relabelled_row_with_wrong_quantity_fails(self) -> None:
        """引擎缺陷模拟：DN100 行被标成 DN125 且数量翻倍，行数不变 → 必须判失败。

        此前只比行数：gold 行在实际输出里找不到就 ``continue``，口径审计又按新标签查
        不到基准而跳过——规格与采购数量全错也判通过（review 2026-09-30 变异：
        material_p0 10/10、全量 pytest 绿、整条门禁放行）。
        """
        case = _material_case()
        table = aggregate_materials(case.segments)
        table.rows[0].spec = "DN125"
        table.rows[0].quantity *= 2
        table.rows[0].raw_quantity *= 2
        result = score_case(case, table=table)
        assert not result.passed
        assert "球墨铸铁 DN100" in result.failure_reason
        assert "DN125" in result.failure_reason

    def test_audit_flags_rows_without_a_segment_group(self) -> None:
        """口径审计：输出行的 (材质, 规格) 在管段表里根本不存在 → 判失败，而不是跳过。"""
        case = _material_case(gold_rows=[])
        table = aggregate_materials(case.segments)
        table.rows[0].material = "钢管"
        result = score_case(case.model_copy(update={"gold_rows": [
            {"material": "钢管", "spec": "DN100", "unit": "m", "quantity": 200},
        ]}), table=table)
        assert not result.passed
        assert "口径审计" in result.failure_reason


class TestRunnerEndToEnd:
    def test_docgen_p0_all_green(self, tmp_path: Path) -> None:
        runner = HarnessRunner(
            eval_path=REPO_ROOT / "data" / "eval" / "docgen_p0.jsonl",
            report_dir=tmp_path / "reports",
            trace_dir=tmp_path / "traces",
        )
        report, _report_path, issues = runner.run()
        assert issues == []
        assert report.total == 11 and report.passed == 11 and report.failed == 0

    def test_material_p0_all_green(self, tmp_path: Path) -> None:
        runner = HarnessRunner(
            eval_path=REPO_ROOT / "data" / "eval" / "material_p0.jsonl",
            report_dir=tmp_path / "reports",
            trace_dir=tmp_path / "traces",
        )
        report, _report_path, issues = runner.run()
        assert issues == []
        assert report.total == 10 and report.passed == 10 and report.failed == 0
