"""Internal project specs: never contributed to model-visible plugin assembly."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from water_agent.multi_agent.project_calculation_contracts import (
    INPUT_UNITS,
    OUTPUTS,
    TOOL_NAMES,
    number,
)
from water_agent.tools.base import ToolSpec
from water_agent.tools.hydraulics import run_calc

_ACTIVE_SCOPE: ContextVar[Any] = ContextVar("project_node_scope", default=None)


class Velocity(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)
    flow_lps: float = Field(gt=0, le=5000)
    diameter_mm: float = Field(gt=0, le=3000)


class Darcy(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)
    length_m: float = Field(gt=0)
    diameter_mm: float = Field(gt=0, le=3000)
    velocity_mps: float = Field(gt=0)
    friction_factor: float = Field(gt=0)
    gravity_mps2: float = Field(gt=0)


class Pump(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)
    static_head_m: float = Field(ge=0)
    friction_head_m: float = Field(ge=0)
    local_head_m: float = Field(ge=0)
    residual_head_m: float = Field(ge=0)


def _guard(args: BaseModel) -> None:
    scope = _ACTIVE_SCOPE.get()
    if scope is None:
        raise ValueError("PROJECT_UNTRUSTED_SOURCE")
    scope.check(args.model_dump())


def _execute(index: int, args: BaseModel) -> dict:
    _guard(args)
    payload = args.model_dump()
    for value in payload.values():
        number(value)
    if index == 0:
        payload["scenario"] = "water_supply"
    result = run_calc(("velocity", "head_loss_darcy_weisbach", "pump_head")[index], payload)
    if not result.ok:
        return {"error": True, "code": "PROJECT_EXECUTION_FAILED"}
    key = OUTPUTS[index]
    value = number(result.values[key])
    if value < 0 or (index < 2 and value <= 0):
        return {"error": True, "code": "PROJECT_EXECUTION_FAILED"}
    return {"outputs": {key: value}, "assumptions": list(result.assumptions)}


project_velocity_spec = ToolSpec(
    name="project_calc_velocity",
    description="内部项目程序计算。Use when 服务确认计划且已解析受信参数。Do not use for 模型调用、未确认计划或未知来源；无默认值。",
    args_model=Velocity,
    guard=_guard,
    execute=lambda args: _execute(0, args),
)
project_darcy_spec = ToolSpec(
    name="project_calc_darcy",
    description="内部项目程序计算。Use when 服务确认计划且已解析受信参数。Do not use for 模型调用、未确认计划或未知来源；无默认值。",
    args_model=Darcy,
    guard=_guard,
    execute=lambda args: _execute(1, args),
)
project_pump_spec = ToolSpec(
    name="project_calc_pump_head",
    description="内部项目程序计算。Use when 服务确认计划且已解析受信参数。Do not use for 模型调用、未确认计划或未知来源；无默认值。",
    args_model=Pump,
    guard=_guard,
    execute=lambda args: _execute(2, args),
)
PROJECT_SPECS = (project_velocity_spec, project_darcy_spec, project_pump_spec)
assert tuple(s.name for s in PROJECT_SPECS) == TOOL_NAMES
assert len(INPUT_UNITS) == len(PROJECT_SPECS)
