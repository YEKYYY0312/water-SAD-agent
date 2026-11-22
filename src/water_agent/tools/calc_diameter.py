"""Canonical actual inside diameter tool; no nominal size selection."""
from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from water_agent.tools.base import ToolSpec
from water_agent.tools.diameter_request import guard_diameter_request
from water_agent.tools.hydraulics import calc_diameter

DIAMETER_FORMULA = "d = sqrt(4Q/(πv))"
DIAMETER_NOTE = "满管圆管；目标流速由用户给定，不自动圆整DN、不选管材，未进行规范符合性判断。"
DIAMETER_UNITS = {"flow_lps": "L/s", "velocity_mps": "m/s", "diameter_mm": "mm"}


class DiameterArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    flow_lps: float = Field(gt=0, title="Q", description="用户给定流量 L/s")
    velocity_mps: float = Field(gt=0, title="v", description="用户给定目标流速 m/s")


def _execute(args: DiameterArgs) -> dict:
    inputs = args.model_dump()
    result = calc_diameter(inputs)
    if not result.ok:
        return {"error": True, "code": "CALCULATION_INVALID", "message": "；".join(result.errors),
                "missing_inputs": result.missing_inputs}
    return {"diameter_mm": result.values["diameter_mm"], "formula": DIAMETER_FORMULA,
            "inputs": inputs, "units": dict(DIAMETER_UNITS), "assumptions": [],
            "calculation_note": DIAMETER_NOTE}


calc_diameter_spec = ToolSpec(
    name="calc_diameter",
    description=(
        "由流量和目标流速计算圆管实际内径(mm)。"
        "Use when 用户给定Q(L/s)、v(m/s)，d=sqrt(4Q/(πv))。"
        "Do not use for 缺参/单位不明；先澄清，不猜流速、不圆整DN、不选型或判合规。"
    ),
    args_model=DiameterArgs, guard=guard_diameter_request, execute=_execute,
)


def verified_diameter_result(
    args: dict[str, Any], payload: dict[str, Any], receipt: Any, tool_call_id: str,
) -> bool:
    """Verify this specific receipt contract; no arbitrary tool result is trusted."""
    try:
        validated = DiameterArgs.model_validate(args).model_dump()
        body_inputs = DiameterArgs.model_validate(payload.get("inputs")).model_dump()
        if not isinstance(receipt, dict):
            return False
        receipt_inputs = DiameterArgs.model_validate(receipt.get("args")).model_dump()
        value = payload.get("diameter_mm")
        return (
            bool(tool_call_id) and isinstance(receipt, dict)
            and set(receipt) == {"tool_name", "tool_call_id", "args", "result"}
            and receipt["tool_name"] == "calc_diameter"
            and receipt["tool_call_id"] == tool_call_id
            and receipt_inputs == validated == body_inputs
            and receipt["result"] == payload
            and set(payload) == {"diameter_mm", "inputs", "units", "formula",
                                 "assumptions", "calculation_note"}
            and payload["formula"] == DIAMETER_FORMULA
            and payload["units"] == DIAMETER_UNITS
            and payload["calculation_note"] == DIAMETER_NOTE
            and payload["assumptions"] == []
            and isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0
        )
    except (ValueError, TypeError, KeyError):
        return False
