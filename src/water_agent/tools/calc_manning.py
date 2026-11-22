"""Canonical Manning tool; only the existing hydraulics kernel calculates S."""
from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from water_agent.tools.base import ToolSpec
from water_agent.tools.hydraulics import calc_slope_manning
from water_agent.tools.manning_request import (
    application_note,
    current_manning_query,
    guard_manning_request,
    manning_args_match,
)

MANNING_FORMULA = 'θ=2·acos(1−2r); A=d²·(θ−sinθ)/8; P=d·θ/2; R=A/P; S=[n·Q/(A·R^(2/3))]²'
MANNING_UNITS = {'flow_lps': 'L/s', 'diameter_mm': 'mm', 'fill_ratio': 'h/d',
                 'manning_n': 's/m^(1/3)', 'slope': 'm/m'}


class ManningArgs(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    flow_lps: float = Field(gt=0, title='Q', description='用户流量L/s')
    diameter_mm: float = Field(gt=0, title='d', description='实际内径mm')
    fill_ratio: float = Field(gt=0, lt=1, title='r', description='充满度h/d')
    manning_n: float = Field(gt=0, title='n', description='用户SI曼宁系数s/m^(1/3)')


def _payload(args: ManningArgs, query: str) -> dict[str, Any]:
    inputs = args.model_dump()
    result = calc_slope_manning(inputs)
    if not result.ok:
        return {'error': True, 'code': 'CALCULATION_INVALID', 'message': '；'.join(result.errors),
                'missing_inputs': result.missing_inputs, 'assumptions': list(result.assumptions),
                'calculation_note': application_note(query)}
    return {'slope': result.values['slope'], 'inputs': inputs,
            'formula': MANNING_FORMULA, 'units': dict(MANNING_UNITS),
            'assumptions': list(result.assumptions), 'calculation_note': application_note(query)}


def _execute(args: ManningArgs) -> dict[str, Any]:
    return _payload(args, current_manning_query() or '')


calc_slope_manning_spec = ToolSpec(
    name='calc_slope_manning',
    description=('曼宁Manning计算非满管圆管参考坡度S(m/m)。Use when 明确求坡度且四项用户输入Q(L/s)、实际d(mm)、r=h/d、SI n。'
                 'Do not use for 缺参/未知单位/满管有压/非圆/非均匀流；不由DN猜内径、不按材料猜n；适用性未核验。'),
    args_model=ManningArgs, guard=guard_manning_request, execute=_execute,
)


def _no_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, dict):
        return all(_no_bool(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return all(_no_bool(v) for v in value)
    return True


def verified_manning_result(args: dict[str, Any], payload: dict[str, Any], receipt: Any,
                             tool_call_id: str, query: str | None) -> bool:
    """The observer must supply its independently trusted current user text."""
    try:
        if not isinstance(query, str) or not _no_bool([args, payload, receipt]):
            return False
        validated = ManningArgs.model_validate(args)
        if not isinstance(receipt, dict):
            return False
        value = payload.get('slope')
        return (bool(tool_call_id)
            and set(receipt) == {'tool_name', 'tool_call_id', 'args', 'result', 'query'}
            and receipt['tool_name'] == calc_slope_manning_spec.name
            and receipt['tool_call_id'] == tool_call_id and receipt['query'] == query
            and manning_args_match(query, args)
            and ManningArgs.model_validate(receipt['args']).model_dump() == validated.model_dump()
            and receipt['result'] == payload == _payload(validated, query)
            and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0)
    except (ValueError, TypeError, KeyError, OverflowError):
        return False
