"""材料统计导出（P2）：材料清单 → Excel(.xlsx) / CSV。

- ``.xlsx`` 用 openpyxl 生成（已入 pyproject 依赖）；不可用时给出安装指引的错误，
  不静默降级、不产出空文件；
- 内容与引擎输出一一对应：第一张表 = 材料清单（含来源明细列），第二张表 = 统计说明
  （汇总 + 逐段来源明细），保证"清单数值与计算一致、可追溯来源"。
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from water_agent.materials.core import PIPE_UNIT, MaterialTable

# 材料清单表头（机器可读：序号从 1 起，数值列保持数值单元格）
COLUMNS = ["序号", "材料名称", "规格", "单位", "数量", "来源说明（管段明细）"]

_XLSX_INSTALL_HINT = (
    "导出 .xlsx 需要 openpyxl：pip/uv 安装 water-agent 依赖后重试"
    "（uv sync 或 pip install -e '.[dev]'；openpyxl>=3.1,<4 已入 pyproject）"
)


def _rows_for_excel(table: MaterialTable) -> list[list[Any]]:
    """材料清单行（与表头 COLUMNS 对齐）。"""
    output: list[list[Any]] = []
    for index, row in enumerate(table.rows, start=1):
        output.append([index, row.material, row.spec, row.unit, row.quantity, row.detail])
    return output


def _summary_rows(table: MaterialTable) -> list[list[Any]]:
    """统计说明页内容：项目 / 数值 / 说明。"""
    rows: list[list[Any]] = [
        ["材料行数", len(table.rows), "按（材质, 规格）分组后的行数"],
        ["管道总长度", round(table.total_length_m, 3), f"单位：{PIPE_UNIT}（全部管段长度之和）"],
    ]
    for row in table.rows:
        rows.append(["材料", f"{row.material} {row.spec}", f"数量 {row.quantity} {row.unit}；{row.detail}"])
    if table.assumptions:
        for note in table.assumptions:
            rows.append(["提示", "人工核对", note])
    return rows


def write_materials_xlsx(
    table: MaterialTable,
    path: str | Path,
    *,
    title: str = "给排水材料统计表",
) -> Path:
    """材料清单导出为 .xlsx（两个 sheet：材料清单 + 统计说明）。返回写入路径。"""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        from openpyxl import Workbook  # type: ignore[import-untyped]
        from openpyxl.styles import Alignment, Font, PatternFill  # type: ignore[import-untyped]
        from openpyxl.utils import get_column_letter  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover — 依赖缺失兜底
        raise ImportError(_XLSX_INSTALL_HINT) from exc

    wb = Workbook()
    sheet = wb.active
    sheet.title = "材料清单"

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    wrap = Alignment(vertical="top", wrap_text=True)

    sheet.append(COLUMNS)
    for cell in sheet[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center", horizontal="center")

    for row in _rows_for_excel(table):
        sheet.append(row)

    widths = [8, 18, 12, 8, 12, 60]
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = wrap
            if isinstance(cell.value, float):
                cell.number_format = "0.000"
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:F{sheet.max_row}"

    summary = wb.create_sheet("统计说明")
    for row in _summary_rows(table):
        summary.append(row)
    for index, width in enumerate([16, 20, 80], start=1):
        summary.column_dimensions[get_column_letter(index)].width = width
    for row in summary.iter_rows(min_row=1):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)

    wb.save(destination)
    return destination


def write_materials_csv(table: MaterialTable, path: str | Path) -> Path:
    """材料清单导出为 CSV（utf-8-sig，Excel 直接打开中文不乱码）。返回写入路径。"""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        for row in _rows_for_excel(table):
            writer.writerow(row)
        writer.writerow([])
        for summary_row in _summary_rows(table):
            writer.writerow(summary_row)
    return destination
