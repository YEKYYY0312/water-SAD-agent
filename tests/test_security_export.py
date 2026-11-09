"""材料导出输出边界：外部文本不得成为电子表格公式。"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest
from openpyxl import load_workbook

from water_agent.materials.core import MaterialRow, MaterialTable
from water_agent.materials.export import write_materials_csv, write_materials_xlsx


def _text_table(text: str) -> MaterialTable:
    return MaterialTable(
        rows=[MaterialRow(material=text, spec=text, unit=text, quantity=15.5, detail=text)],
        total_length_m=15.5,
        assumptions=[text],
    )


@pytest.mark.parametrize(
    "text",
    [
        "=1+1",
        '=HYPERLINK("https://example.invalid/", "材料")',
        "+1+1",
        "-1+1",
        "@SUM(1,1)",
        "\t=1+1",
        "\r=1+1",
        "\n=1+1",
        " =1+1",
        "#N/A",
        "#REF!",
        "00150",
        "PVC-U",
    ],
)
def test_xlsx_external_text_is_literal_on_both_sheets(tmp_path: Path, text: str) -> None:
    table = _text_table(text)
    original = table.to_dict()
    path = write_materials_xlsx(table, tmp_path / "materials.xlsx")
    workbook = load_workbook(path, data_only=False)
    try:
        sheet = workbook["材料清单"]
        for address in ("B2", "C2", "D2", "F2"):
            assert sheet[address].data_type == "s", address
            assert sheet[address].value == text, address
        summary = workbook["统计说明"]
        assert summary["B3"].data_type == "s"
        assert summary["B3"].value == f"{text} {text}"
        assert summary["C3"].data_type == "s"
        assert summary["C3"].value == f"数量 15.5 {text}；{text}"
        assert summary["C4"].data_type == "s"
        assert summary["C4"].value == text
        for cell, value in ((sheet["A2"], 1), (sheet["E2"], 15.5),
                            (summary["B1"], 1), (summary["B2"], 15.5)):
            assert cell.data_type == "n"
            assert cell.value == value
        assert sheet["E2"].number_format == "0.000"
        assert all(cell.data_type != "f" for page in workbook
                   for row in page.iter_rows() for cell in row)
    finally:
        workbook.close()
    assert table.to_dict() == original


@pytest.mark.parametrize(
    ("field", "sheet_name", "address"),
    [("material", "材料清单", "B2"), ("spec", "材料清单", "C2"),
     ("unit", "材料清单", "D2"), ("detail", "材料清单", "F2"),
     ("assumptions", "统计说明", "C4")],
)
def test_xlsx_each_external_field_cannot_be_a_formula(
    tmp_path: Path, field: str, sheet_name: str, address: str
) -> None:
    table = _text_table("普通文本")
    if field == "assumptions":
        table.assumptions = ["=1+1"]
    else:
        setattr(table.rows[0], field, "=1+1")
    workbook = load_workbook(write_materials_xlsx(table, tmp_path / "field.xlsx"))
    try:
        cell = workbook[sheet_name][address]
        assert cell.data_type == "s"
        assert cell.value == "=1+1"
    finally:
        workbook.close()


def test_registry_material_statistics_web_xlsx_keeps_untrusted_text_literal(tmp_path: Path) -> None:
    from water_agent.tools.material_stats import material_stats_spec
    from water_agent.tools.registry import ToolRegistry
    from water_agent.webapp.export_routes import _render_xlsx

    registry = ToolRegistry()
    registry.register(material_stats_spec)
    result = registry.invoke(
        "material_stats",
        segments=[{"segment_id": "=1+1", "system": "water_supply", "dn_mm": 100,
                   "length_m": 15.5, "material": "=1+1"}],
    )
    assert result["ok"] is True
    workbook = load_workbook(
        _render_xlsx({"material_table": result}, "材料统计", tmp_path / "web.xlsx"),
        data_only=False,
    )
    try:
        sheet = workbook["材料清单"]
        assert sheet["B2"].data_type == "s"
        assert sheet["B2"].value == "=1+1"
        assert sheet["F2"].data_type == "s"
        assert sheet["F2"].value == "=1+1(15.5m)"
        assert sheet["E2"].data_type == "n"
        assert sheet["E2"].value == 15.5
        assert workbook["统计说明"]["B3"].data_type == "s"
        assert workbook["统计说明"]["B3"].value == "=1+1 DN100"
    finally:
        workbook.close()


@pytest.mark.parametrize(
    "text",
    ["=1+1", "+1+1", "-1+1", "@SUM(1,1)", "-15.5", "+15.5",
     '\t=HYPERLINK("https://example.invalid/", "材料")', "\r=1+1", "\n=1+1",
     "\t材料", "\r材料", "\n材料", "\x00=1+1", "\x1f=1+1", "\x7f=1+1",
     "\x85=1+1", " =1+1", "  +1+1", " \t材料", " \x00=1+1", "\u00a0=1+1",
     "\ufeff=1+1", "\u200b=1+1", '=HYPERLINK("https://example.invalid/a,b", "材料")'],
)
def test_csv_neutralizes_dangerous_text_in_materials_and_summary(
    tmp_path: Path, text: str
) -> None:
    table = _text_table(text)
    original = table.to_dict()
    path = write_materials_csv(table, tmp_path / "materials.csv")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    escaped = "'" + text
    assert rows[1] == ["1", escaped, escaped, escaped, "15.5", escaped]
    assert rows[2] == []
    assert rows[3][1] == "1"
    assert rows[4][1] == "15.5"
    assert rows[5] == ["材料", "'" + f"{text} {text}", f"数量 15.5 {text}；{text}"]
    assert rows[6] == ["提示", "人工核对", escaped]
    assert table.to_dict() == original


@pytest.mark.parametrize("text", ["PVC-U", "00150", "15.5", "普通文本", "", "  普通文本",
                                 "'已有前缀", "材料\n第二行", "材料,规格", "管道=1+1"])
def test_csv_keeps_benign_text_and_real_numbers_unchanged(tmp_path: Path, text: str) -> None:
    table = _text_table(text)
    table.rows[0].quantity = -15.5
    table.total_length_m = -15.5
    path = write_materials_csv(table, tmp_path / "benign.csv")
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[1] == ["1", text, text, text, "-15.5", text]
    assert rows[3][1] == "1"
    assert rows[4][1] == "-15.5"
    assert rows[5] == ["材料", f"{text} {text}", f"数量 -15.5 {text}；{text}"]
    assert rows[6] == ["提示", "人工核对", text]
