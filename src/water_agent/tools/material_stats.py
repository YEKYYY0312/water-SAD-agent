"""工具：material_stats（材料统计，P2）。

把管段表（段号/系统/管径/长度/材质）程序化聚合为按「材质+规格」分组的材料清单，
每行带来源管段明细（可追溯）。LLM 只负责理解输入，不参与数量运算。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from water_agent.materials.core import (
    MaterialInputError,
    PipeRun,
    aggregate_materials,
)
from water_agent.tools.base import ToolSpec


class PipeSegmentArgs(BaseModel):
    """一条管段（材料统计的最小输入单元）。"""

    model_config = ConfigDict(extra="forbid")

    segment_id: str = Field(min_length=1, max_length=64, description="管段唯一编号，如 S1")
    system: Literal["water_supply", "drainage"] = Field(
        ...,
        description="系统：water_supply 给水；drainage 排水/污水",
    )
    dn_mm: int = Field(gt=0, le=4000, description="公称管径 mm，如 150")
    length_m: float = Field(gt=0, le=1_000_000, description="管段长度 m，必须为正")
    material: str = Field(min_length=1, max_length=64, description="材质，如 球墨铸铁 / HDPE / PVC-U")


class MaterialStatsArgs(BaseModel):
    """材料统计输入：至少一条管段。"""

    model_config = ConfigDict(extra="forbid")

    segments: list[PipeSegmentArgs] = Field(min_length=1, description="管段表（1 条及以上）")


def _execute(args: MaterialStatsArgs) -> dict:
    runs = [
        PipeRun(
            segment_id=seg.segment_id,
            system=seg.system,
            dn_mm=seg.dn_mm,
            length_m=seg.length_m,
            material=seg.material,
        )
        for seg in args.segments
    ]
    table = aggregate_materials(runs)
    if not table.ok:
        raise MaterialInputError("材料统计输入不合法：" + "；".join(table.errors))
    return {
        "ok": True,
        "rows": [row.to_dict() for row in table.rows],
        "total_length_m": table.total_length_m,
        "assumptions": list(table.assumptions),
        "note": "数量 = 各管段长度之和（程序化）；每行来源见 rows[].segments/detail",
    }


material_stats_spec = ToolSpec(
    name="material_stats",
    description=(
        "把管段表按材质+管径聚合为材料清单（单位 m），每行返回来源管段号与逐段长度明细。"
        "Use when 需要统计给排水管道工程量/材料（各段有管径、长度、材质）或输出 Excel 前的数量汇总。"
        "Do not use for 水力计算数值（流速/损失/坡度等走 calc 工具），也不要用于缺少管径或长度的估算。"
    ),
    args_model=MaterialStatsArgs,
    execute=_execute,
)
