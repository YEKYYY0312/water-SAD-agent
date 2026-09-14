"""测试材料导出的路径校验（Step 15 P1 补全）。"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from water_agent.materials.core import MaterialRow, MaterialTable
from water_agent.materials.export import write_materials_csv, write_materials_xlsx


@pytest.fixture
def sample_table() -> MaterialTable:
    """测试用材料清单。"""
    return MaterialTable(
        rows=[
            MaterialRow(
                material="PVC-U",
                spec="DN100",
                unit="m",
                quantity=15.5,
                segments=["seg1", "seg2"],
                detail="测试管段",
            )
        ],
        total_length_m=15.5,
        assumptions=[],
    )


class TestPathValidation:
    """路径校验：拒绝相对路径和路径遍历。"""

    def test_xlsx_rejects_relative_path(self, sample_table: MaterialTable) -> None:
        """相对路径被拒绝（绝对路径要求）。"""
        with pytest.raises(ValueError, match="必须为绝对路径"):
            write_materials_xlsx(sample_table, "output/materials.xlsx")

    def test_csv_rejects_relative_path(self, sample_table: MaterialTable) -> None:
        """CSV 导出同样拒绝相对路径。"""
        with pytest.raises(ValueError, match="必须为绝对路径"):
            write_materials_csv(sample_table, "materials.csv")

    def test_xlsx_rejects_path_traversal(self, sample_table: MaterialTable) -> None:
        """包含 .. 的路径被拒绝（路径遍历防御）。"""
        # 相对路径会先被"必须为绝对路径"拒绝
        with pytest.raises(ValueError, match="必须为绝对路径"):
            write_materials_xlsx(sample_table, "../sensitive/materials.xlsx")

    def test_csv_rejects_path_traversal(self, sample_table: MaterialTable) -> None:
        """CSV 导出拒绝路径遍历。"""
        # 相对路径会先被"必须为绝对路径"拒绝
        with pytest.raises(ValueError, match="必须为绝对路径"):
            write_materials_csv(sample_table, "../../etc/passwd")

    def test_xlsx_rejects_absolute_path_with_traversal(self, sample_table: MaterialTable) -> None:
        """绝对路径中包含 .. 也被拒绝。"""
        with pytest.raises(ValueError, match="路径遍历"):
            write_materials_xlsx(sample_table, "/tmp/../etc/passwd")

    def test_csv_rejects_absolute_path_with_traversal(self, sample_table: MaterialTable) -> None:
        """CSV 绝对路径中的 .. 被拒绝。"""
        with pytest.raises(ValueError, match="路径遍历"):
            write_materials_csv(sample_table, "/home/user/../../../etc/shadow")

    def test_xlsx_accepts_absolute_path(self, sample_table: MaterialTable) -> None:
        """绝对路径通过校验并成功写入。"""
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "materials.xlsx"
            result = write_materials_xlsx(sample_table, path)
            assert result.exists()
            assert result.suffix == ".xlsx"

    def test_csv_accepts_absolute_path(self, sample_table: MaterialTable) -> None:
        """CSV 绝对路径通过校验。"""
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "materials.csv"
            result = write_materials_csv(sample_table, path)
            assert result.exists()
            assert result.suffix == ".csv"

    def test_nested_directory_creation(self, sample_table: MaterialTable) -> None:
        """支持创建嵌套目录。"""
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "nested" / "output" / "materials.xlsx"
            result = write_materials_xlsx(sample_table, path)
            assert result.exists()
            assert result.parent.name == "output"

