"""One explicit Darcy–Weisbach calculation, using the existing hydraulic kernel."""
from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, Field

from water_agent.tools.base import ToolSpec
from water_agent.tools.darcy_request import darcy_default_values, guard_darcy_request
from water_agent.tools.hydraulics import calc_head_loss_darcy_weisbach

DARCY_FORMULA = "hf = f · (L/d) · v²/(2g)"


class DarcyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    length_m: float = Field(gt=0, title="L", description="管长 m")
    diameter_mm: float = Field(gt=0, title="d", description="实际内径 mm，非DN")
    velocity_mps: float = Field(gt=0, title="v", description="流速 m/s")
    friction_factor: float = Field(gt=0, title="f", description="用户给定Darcy f，无量纲")
    gravity_mps2: float = Field(default=9.81, gt=0, title="g", description="重力加速度 m/s²")


def _execute(args: DarcyArgs) -> dict:
    inputs = args.model_dump()
    result = calc_head_loss_darcy_weisbach(inputs)
    if not result.ok:
        return {"error": True, "code": "CALCULATION_INVALID", "message": "；".join(result.errors),
                "missing_inputs": result.missing_inputs}
    value = result.values.get("head_loss_m")
    if value is None or not math.isfinite(value) or value <= 0:
        return {"error": True, "code": "CALCULATION_INVALID", "message": "计算结果非有限正数。"}
    return {
        "head_loss_m": value,
        "formula": DARCY_FORMULA,
        "inputs": inputs,
        "units": {"length_m": "m", "diameter_mm": "mm", "velocity_mps": "m/s",
                  "friction_factor": "1", "gravity_mps2": "m/s²", "head_loss_m": "m"},
        "defaults_used": darcy_default_values(),
        "assumptions": [],
        "coefficient_note": "Darcy摩阻系数f由用户给定，未核验其工程来源；未进行规范符合性判断。",
    }


calc_head_loss_darcy_weisbach_spec = ToolSpec(
    name="calc_head_loss_darcy_weisbach",
    description=(
        "Darcy–Weisbach沿程损失(m)：hf=f·(L/d)·v²/(2g)。"
        "Use when 用户指定此公式及L(m)、实际内径(mm)、v(m/s)、Darcy f；g默认9.81。"
        "Do not use for 缺参/未知单位/DN推内径/Fanning；先澄清，不猜f、不作合规判断。"
    ),
    args_model=DarcyArgs,
    guard=guard_darcy_request,
    execute=_execute,
)
