"""材料统计（P2）：管段表 → 材料清单（程序化聚合 + 可追溯来源）+ Excel/CSV 导出。"""

from water_agent.materials.core import (
    PIPE_UNIT,
    QUANTITY_PRECISION,
    MaterialInputError,
    MaterialRow,
    MaterialTable,
    PipeRun,
    aggregate_materials,
    material_rows_from_dicts,
)
from water_agent.materials.export import write_materials_csv, write_materials_xlsx

__all__ = [
    "PIPE_UNIT",
    "QUANTITY_PRECISION",
    "MaterialInputError",
    "MaterialRow",
    "MaterialTable",
    "PipeRun",
    "aggregate_materials",
    "material_rows_from_dicts",
    "write_materials_csv",
    "write_materials_xlsx",
]
