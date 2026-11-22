"""Canonical Hazen–Williams tool; the existing kernel owns the formula."""
from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from water_agent.tools.base import ToolSpec
from water_agent.tools.hazen_request import application_note, guard_hazen_request, hazen_args_match
from water_agent.tools.hydraulics import calc_head_loss_hazen_williams

HAZEN_FORMULA = 'hf = 10.67 · L · Q^1.852 / (C^1.852 · d^4.8704)'
HAZEN_UNITS = {'length_m':'m', 'diameter_mm':'mm', 'flow_lps':'L/s', 'hazen_williams_c':'无量纲', 'head_loss_m':'m'}


class HazenArgs(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    length_m: float = Field(gt=0, title='L', description='用户给定管长m')
    diameter_mm: float = Field(gt=0, title='d', description='用户给定实际内径mm')
    flow_lps: float = Field(gt=0, title='Q', description='用户给定流量L/s')
    hazen_williams_c: float = Field(gt=0, title='C', description='用户给定C，无量纲，无默认')


def _execute(args: HazenArgs) -> dict:
    inputs = args.model_dump()
    result = calc_head_loss_hazen_williams(inputs)
    if not result.ok:
        return {'error':True, 'code':'CALCULATION_INVALID', 'message':'；'.join(result.errors), 'missing_inputs':result.missing_inputs}
    return {'head_loss_m':result.values['head_loss_m'], 'inputs':inputs, 'formula':HAZEN_FORMULA,
            'units':dict(HAZEN_UNITS), 'assumptions':[], 'calculation_note':application_note()}


calc_head_loss_hazen_williams_spec = ToolSpec(
    name='calc_head_loss_hazen_williams',
    description=('Hazen–Williams计算沿程水头损失(m)。Use when 明确该公式及L(m)、实际d(mm)、Q(L/s)、C(无量纲)。'
                 'Do not use for 缺参/单位不明；不猜C、不由DN推内径、不换公式；满管有压给水经验近似，需披露适用前提。'),
    args_model=HazenArgs, guard=guard_hazen_request, execute=_execute,
)


def _no_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, dict):
        return all(_no_bool(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return all(_no_bool(v) for v in value)
    return True


def verified_hazen_result(args: dict[str, Any], payload: dict[str, Any], receipt: Any,
                          tool_call_id: str, query: str | None = None) -> bool:
    """Validate only this explicit canonical receipt; bool cannot equal a number."""
    try:
        if not _no_bool([args, payload, receipt]):
            return False
        validated = HazenArgs.model_validate(args).model_dump()
        if not isinstance(receipt, dict):
            return False
        value = payload.get('head_loss_m')
        return (bool(tool_call_id) and set(receipt) == {'tool_name','tool_call_id','args','result','query'}
            and receipt['tool_name'] == calc_head_loss_hazen_williams_spec.name
            and receipt['tool_call_id'] == tool_call_id
            and isinstance(receipt['query'], str) and hazen_args_match(receipt['query'], args)
            and (query is None or receipt['query'] == query)
            and HazenArgs.model_validate(receipt['args']).model_dump() == validated
            and HazenArgs.model_validate(payload['inputs']).model_dump() == validated
            and receipt['result'] == payload
            and set(payload) == {'head_loss_m','inputs','formula','units','assumptions','calculation_note'}
            and payload['formula'] == HAZEN_FORMULA and payload['units'] == HAZEN_UNITS
            and payload['assumptions'] == [] and isinstance(payload['calculation_note'], str)
            and payload['calculation_note'] == application_note(receipt['query'])
            and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0)
    except (ValueError, TypeError, KeyError, OverflowError):
        return False
