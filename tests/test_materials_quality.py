"""P2 材料质量套件：异常输入、舍入口径、溯源与 xlsx round-trip。"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from water_agent.domain.models import MaterialCase
from water_agent.harness.loader import load_eval_cases
from water_agent.harness.runner import HarnessRunner
from water_agent.materials import aggregate_materials, write_materials_xlsx

REPO_ROOT = Path(__file__).resolve().parents[1]
QUALITY_SET = REPO_ROOT / "data" / "eval" / "material_quality.jsonl"


def _segment(
    segment_id: str,
    *,
    system: str = "water_supply",
    dn_mm: int = 100,
    length_m: float = 10.0,
    material: str = "钢管",
) -> dict:
    return {
        "segment_id": segment_id,
        "system": system,
        "dn_mm": dn_mm,
        "length_m": length_m,
        "material": material,
    }


class TestMaterialQualityCases:
    @pytest.mark.parametrize("bad_length", [math.inf, -math.inf, math.nan])
    def test_nonfinite_lengths_are_structured_invalid_input(self, bad_length: float) -> None:
        table = aggregate_materials([_segment("NF-1", length_m=bad_length)])
        assert not table.ok
        assert table.rows == []
        assert any("有限" in error for error in table.errors)

    def test_missing_system_and_duplicate_segment_id_are_rejected(self) -> None:
        missing_system = _segment("M-1")
        missing_system.pop("system")
        missing_table = aggregate_materials([missing_system])
        duplicate_table = aggregate_materials(
            [_segment("DUP-1", length_m=10), _segment("DUP-1", length_m=20)]
        )
        assert not missing_table.ok and any("system" in error for error in missing_table.errors)
        assert not duplicate_table.ok and any("重复" in error for error in duplicate_table.errors)

    def test_mixed_system_rounding_and_provenance_are_preserved(self) -> None:
        segments = [
            _segment("W-1", system="water_supply", length_m=33.3334),
            _segment("D-1", system="drainage", length_m=33.3334),
        ]
        table = aggregate_materials(segments)
        assert table.ok
        assert len(table.rows) == 1
        row = table.rows[0]
        assert row.raw_quantity == pytest.approx(66.6668)
        assert row.quantity == 66.667
        assert row.segments == ["W-1", "D-1"]
        assert all(segment["segment_id"] in row.detail for segment in segments)

    def test_xlsx_round_trip_keeps_quantities_and_full_traceability(self, tmp_path: Path) -> None:
        from openpyxl import load_workbook

        segments = [
            _segment("W-1", system="water_supply", dn_mm=100, length_m=33.3334),
            _segment("D-1", system="drainage", dn_mm=100, length_m=33.3334),
            _segment("D-2", system="drainage", dn_mm=200, length_m=10.125, material="PVC-U"),
        ]
        table = aggregate_materials(segments)
        path = write_materials_xlsx(table, tmp_path / "material-quality.xlsx")

        workbook = load_workbook(path, data_only=True)
        sheet = workbook["材料清单"]
        rows = list(sheet.iter_rows(min_row=2, values_only=False))
        by_key = {(row[1].value, row[2].value): row for row in rows}
        steel = by_key[("钢管", "DN100")]
        pvc = by_key[("PVC-U", "DN200")]
        assert steel[4].value == 66.667
        assert steel[4].number_format == "0.000"
        assert pvc[4].value == 10.125
        detail_text = " ".join(str(row[5].value) for row in rows)
        assert all(segment["segment_id"] in detail_text for segment in segments)


class TestMaterialQualityHarness:
    def test_loader_parses_material_quality_cases(self) -> None:
        cases = load_eval_cases(QUALITY_SET)
        assert len(cases) == 7
        assert all(isinstance(case, MaterialCase) for case in cases)

    def test_material_quality_suite_all_green(self, tmp_path: Path) -> None:
        runner = HarnessRunner(
            eval_path=QUALITY_SET,
            report_dir=tmp_path / "reports",
            trace_dir=tmp_path / "traces",
        )
        report, report_path, issues = runner.run()
        assert issues == []
        assert report.total == 7
        assert report.passed == 7
        assert report.failed == 0
        assert report.skipped == 0
        assert report_path.exists()
