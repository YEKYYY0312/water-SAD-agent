"""P2 测试：材料统计引擎（聚合/溯源/校验）与 Excel/CSV 导出。"""
from __future__ import annotations

import csv
import math
from pathlib import Path

import pytest

from water_agent.materials import (
    QUANTITY_PRECISION,
    PipeRun,
    aggregate_materials,
    write_materials_csv,
    write_materials_xlsx,
)


def _seg(segment_id: str, dn: int, length: float, material: str, system: str = "water_supply") -> dict:
    return {"segment_id": segment_id, "system": system, "dn_mm": dn, "length_m": length, "material": material}


class TestAggregation:
    def test_groups_by_material_and_dn_sums_lengths(self) -> None:
        table = aggregate_materials(
            [
                _seg("S1", 100, 150, "球墨铸铁"),
                _seg("S2", 100, 50, "球墨铸铁"),
                _seg("S3", 150, 60, "球墨铸铁"),
            ]
        )
        assert table.ok
        assert [(r.material, r.spec, r.quantity) for r in table.rows] == [
            ("球墨铸铁", "DN100", 200.0),
            ("球墨铸铁", "DN150", 60.0),
        ]
        assert table.total_length_m == 260.0

    def test_provenance_lists_every_segment_with_length(self) -> None:
        table = aggregate_materials([_seg("S1", 100, 120.5, "HDPE"), _seg("S2", 100, 80, "HDPE")])
        row = table.rows[0]
        assert row.segments == ["S1", "S2"]
        assert "S1(120.5m)" in row.detail and "S2(80m)" in row.detail

    def test_deterministic_row_order_and_rounding(self) -> None:
        table = aggregate_materials(
            [_seg("B", 200, 0.1, "钢管"), _seg("A", 100, 0.2, "钢管"), _seg("C", 50, 66.6667, "钢管")]
        )
        assert [r.spec for r in table.rows] == ["DN50", "DN100", "DN200"]  # (材质, 规格) 字典序
        assert table.rows[0].quantity == round(66.6667, QUANTITY_PRECISION) == 66.667
        assert table.rows[1].quantity == 0.2
        assert table.rows[2].quantity == 0.1

    def test_cross_system_same_material_dn_merges(self) -> None:
        table = aggregate_materials(
            [_seg("W1", 150, 120, "球墨铸铁", system="water_supply"), _seg("D1", 150, 80, "球墨铸铁", system="drainage")]
        )
        assert table.ok and len(table.rows) == 1
        assert table.rows[0].quantity == 200.0 and table.rows[0].segments == ["W1", "D1"]

    def test_unknown_material_flagged_as_assumption_not_error(self) -> None:
        table = aggregate_materials([_seg("X1", 100, 10, "钛合金")])
        assert table.ok
        assert any("钛合金" in a for a in table.assumptions)

    def test_empty_segments_errors(self) -> None:
        table = aggregate_materials([])
        assert not table.ok and table.rows == []
        assert any("空" in e for e in table.errors)

    def test_negative_length_errors(self) -> None:
        table = aggregate_materials([_seg("N1", 100, -5, "钢管")])
        assert not table.ok
        assert any("length_m" in e for e in table.errors)

    def test_duplicate_segment_id_errors(self) -> None:
        table = aggregate_materials([_seg("D1", 100, 10, "钢管"), _seg("D1", 100, 20, "钢管")])
        assert not table.ok
        assert any("重复" in e for e in table.errors)

    def test_unknown_system_errors(self) -> None:
        table = aggregate_materials([{**_seg("U1", 100, 10, "钢管"), "system": "fire_pipe"}])
        assert not table.ok
        assert any("未知系统" in e for e in table.errors)

    def test_missing_system_errors(self) -> None:
        table = aggregate_materials([{"segment_id": "M0", "dn_mm": 100, "length_m": 10, "material": "钢管"}])
        assert not table.ok
        assert any("system" in e for e in table.errors)

    def test_nonfinite_length_errors(self) -> None:
        nan_table = aggregate_materials([_seg("NAN", 100, math.nan, "钢管")])
        inf_table = aggregate_materials([_seg("INF", 100, math.inf, "钢管")])
        assert not nan_table.ok and any("有限" in e for e in nan_table.errors)
        assert not inf_table.ok and any("有限" in e for e in inf_table.errors)

    def test_invalid_dn_errors(self) -> None:
        float_table = aggregate_materials([{"segment_id": "D1", "system": "water_supply", "dn_mm": 150.5, "length_m": 10, "material": "钢管"}])
        text_table = aggregate_materials([{"segment_id": "D2", "system": "water_supply", "dn_mm": "abc", "length_m": 10, "material": "钢管"}])
        assert not float_table.ok and any("dn_mm" in e for e in float_table.errors)
        assert not text_table.ok and any("dn_mm" in e for e in text_table.errors)

    def test_accepts_pipe_run_objects_directly(self) -> None:
        runs = [PipeRun(segment_id="R1", system="water_supply", dn_mm=100, length_m=25.0, material="钢管")]
        table = aggregate_materials(runs)
        assert table.ok and table.rows[0].quantity == 25.0

    def test_missing_fields_errors(self) -> None:
        table = aggregate_materials([{"segment_id": "M1", "dn_mm": 100, "material": "钢管"}])  # 缺 length_m/system
        assert not table.ok
        assert any("length_m" in e for e in table.errors)
        assert any("system" in e for e in table.errors)


class TestExport:
    def test_xlsx_round_trip_columns_and_values(self, tmp_path: Path) -> None:
        from openpyxl import load_workbook

        table = aggregate_materials([_seg("S1", 150, 120.5, "球墨铸铁"), _seg("S2", 150, 80, "球墨铸铁")])
        path = write_materials_xlsx(table, tmp_path / "materials.xlsx")
        assert path.exists()

        wb = load_workbook(path)
        ws = wb["材料清单"]
        assert [c.value for c in ws[1]] == ["序号", "材料名称", "规格", "单位", "数量", "来源说明（管段明细）"]
        assert ws["B2"].value == "球墨铸铁"
        assert ws["C2"].value == "DN150"
        assert ws["E2"].value == 200.5  # 数值单元格（非文本）
        assert ws["F2"].value == "S1(120.5m)、S2(80m)"
        assert ws.max_row == 2  # 单行材料

        summary = wb["统计说明"]
        texts = [str(c.value) for row in summary.iter_rows() for c in row if c.value is not None]
        assert any("200.5" in text for text in texts)
        assert any("管道总长度" in text for text in texts)

    def test_csv_export_content(self, tmp_path: Path) -> None:
        table = aggregate_materials([_seg("S1", 100, 66.6667, "PE")])
        path = write_materials_csv(table, tmp_path / "materials.csv")
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
        assert rows[0][:4] == ["序号", "材料名称", "规格", "单位"]
        assert rows[1][1:5] == ["PE", "DN100", "m", "66.667"]

    def test_xlsx_missing_openpyxl_raises_clear_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """openpyxl 不可用时给安装指引错误（不静默降级、不产出空文件）。"""
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "openpyxl" or name.startswith("openpyxl."):
                raise ImportError("No module named 'openpyxl'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        table = aggregate_materials([_seg("S1", 100, 10, "钢管")])
        with pytest.raises(ImportError, match="openpyxl"):
            write_materials_xlsx(table, tmp_path / "nope.xlsx")
        assert not (tmp_path / "nope.xlsx").exists()
