"""工具：calc_velocity（程序化流速计算，Step 4）。

包装 Step 1 ``hydraulics.calc_velocity``（数值永远程序化），通过 pydantic schema
约束范围并用 Literal 约束场景枚举（防幻觉参数）。返回流速与场景越界 assumptions。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from water_agent.tools.base import ToolSpec
from water_agent.tools.hydraulics import calc_velocity as _hydraulic_calc_velocity


class VelocityArgs(BaseModel):
    """流速计算输入（口径表 §1）。"""

    model_config = ConfigDict(extra="forbid")

    diameter_mm: float = Field(gt=0, le=3000, description="管内径 mm，如 150")
    flow_lps: float = Field(gt=0, le=5000, description="流量 L/s，如 20")
    scenario: Literal["water_supply", "drainage"] = Field(
        default="water_supply",
        description="场景：water_supply 给水（常用 0.6–1.5 m/s）；drainage 排水",
    )


def _execute(args: VelocityArgs) -> dict:
    result = _hydraulic_calc_velocity(
        {
            "diameter_mm": args.diameter_mm,
            "flow_lps": args.flow_lps,
            "scenario": args.scenario,
        }
    )
    if not result.ok:
        raise RuntimeError("velocity 计算失败：" + ("；".join(result.errors) or "未知错误"))
    return {
        "velocity_mps": round(result.values["velocity_mps"], 3),
        "assumptions": list(result.assumptions),
    }


calc_velocity_spec = ToolSpec(
    name="calc_velocity",
    description=(
        "计算满管流圆管断面平均流速，返回 velocity_mps（m/s）及按场景判定的越界 "
        "assumptions（如高于/低于常用区间）。Use when 用户给出管径与流量求流速。"
        "Do not use for 非满管重力流（应改用排水坡度工具）。"
    ),
    args_model=VelocityArgs,
    execute=_execute,
)