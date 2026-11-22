"""Canonical pump head tool; four explicitly supplied head components."""
from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from water_agent.tools.base import ToolSpec
from water_agent.tools.hydraulics import calc_pump_head
from water_agent.tools.pump_head_request import (
    application_note,
    current_pump_head_query,
    guard_pump_head_request,
    pump_head_args_match,
)

PUMP_HEAD_FORMULA = 'H = H_static + H_friction + H_local + H_residual'
PUMP_HEAD_UNITS = {key: 'm' for key in ('static_head_m','friction_head_m','local_head_m','residual_head_m','pump_head_m')}

class PumpHeadArgs(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    static_head_m: float = Field(ge=0, description='用户静扬程m')
    friction_head_m: float = Field(ge=0, description='用户已给沿程损失扬程m')
    local_head_m: float = Field(ge=0, description='用户已给局部损失扬程m')
    residual_head_m: float = Field(ge=0, description='用户末端余压扬程m')


def _payload(args: PumpHeadArgs, query: str) -> dict[str, Any]:
    inputs = args.model_dump()
    result = calc_pump_head(inputs)
    if not result.ok:
        return {'error': True, 'code': 'CALCULATION_INVALID', 'message': '；'.join(result.errors),
                'values': {}, 'errors': list(result.errors),
                'missing_inputs': result.missing_inputs, 'assumptions': list(result.assumptions),
                'calculation_note': application_note(query)}
    return {'pump_head_m': result.values['pump_head_m'], 'inputs': inputs,
            'formula': PUMP_HEAD_FORMULA, 'units': dict(PUMP_HEAD_UNITS),
            'assumptions': list(result.assumptions), 'calculation_note': application_note(query)}


def _execute(args: PumpHeadArgs) -> dict[str, Any]:
    return _payload(args, current_pump_head_query() or '')


calc_pump_head_spec = ToolSpec(
    name='calc_pump_head',
    description=('泵扬程pump head四分量相加H(m)。Use when 用户明确给定静扬程、沿程损失、局部损失、末端余压扬程各m，含0。'
                 'Do not use for 缺参/未知单位；不换算压力、不补0、不另算损失、不加裕量、不选泵；来源适用性未核验。'),
    args_model=PumpHeadArgs, guard=guard_pump_head_request, execute=_execute,
)


def _no_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, dict):
        return all(_no_bool(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return all(_no_bool(v) for v in value)
    return True


def verified_pump_head_result(args: dict[str, Any], payload: dict[str, Any], receipt: Any,
                             tool_call_id: str, query: str | None) -> bool:
    """The observer must supply its independently trusted current user text."""
    try:
        if not isinstance(query, str) or not _no_bool([args, payload, receipt]):
            return False
        validated = PumpHeadArgs.model_validate(args)
        if not isinstance(receipt, dict):
            return False
        value = payload.get('pump_head_m')
        return (bool(tool_call_id)
            and set(receipt) == {'tool_name', 'tool_call_id', 'args', 'result', 'query'}
            and receipt['tool_name'] == calc_pump_head_spec.name
            and receipt['tool_call_id'] == tool_call_id and receipt['query'] == query
            and pump_head_args_match(query, args)
            and PumpHeadArgs.model_validate(receipt['args']).model_dump() == validated.model_dump()
            and receipt['result'] == payload == _payload(validated, query)
            and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0)
    except (ValueError, TypeError, KeyError, OverflowError):
        return False
