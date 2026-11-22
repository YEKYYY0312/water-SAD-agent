"""Canonical Shevelev tool; only the existing hydraulics kernel calculates hf."""
from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from water_agent.tools.base import ToolSpec
from water_agent.tools.hydraulics import calc_head_loss_shevelev
from water_agent.tools.shevelev_request import (
    application_note,
    current_shevelev_query,
    guard_shevelev_request,
    shevelev_args_match,
)

SHEVELEV_HIGH = 'i = 0.00107 · v² / d^1.3; hf = i · L'
SHEVELEV_LOW = 'i = 0.000912 · v² · (1 + 0.867/v)^0.3 / d^1.3; hf = i · L'
SHEVELEV_UNITS = {'length_m': 'm', 'diameter_mm': 'mm', 'velocity_mps': 'm/s', 'head_loss_m': 'm'}


class ShevelevArgs(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    length_m: float = Field(gt=0, title='L', description='用户给定管长m')
    diameter_mm: float = Field(gt=0, title='d', description='用户给定实际内径mm')
    velocity_mps: float = Field(gt=0, title='v', description='用户给定流速m/s')


def _payload(args: ShevelevArgs, query: str) -> dict[str, Any]:
    inputs = args.model_dump()
    result = calc_head_loss_shevelev(inputs)
    if not result.ok:
        return {'error': True, 'code': 'CALCULATION_INVALID', 'message': '；'.join(result.errors),
                'missing_inputs': result.missing_inputs, 'assumptions': list(result.assumptions),
                'calculation_note': application_note(query)}
    high = args.velocity_mps >= 1.2
    return {'head_loss_m': result.values['head_loss_m'], 'inputs': inputs,
            'branch': 'v>=1.2' if high else 'v<1.2', 'formula': SHEVELEV_HIGH if high else SHEVELEV_LOW,
            'units': dict(SHEVELEV_UNITS), 'assumptions': list(result.assumptions),
            'calculation_note': application_note(query)}


def _execute(args: ShevelevArgs) -> dict[str, Any]:
    return _payload(args, current_shevelev_query() or '')


calc_head_loss_shevelev_spec = ToolSpec(
    name='calc_head_loss_shevelev',
    description=('舍维列夫Shevelev计算沿程水头损失(m)。Use when 明确该公式及L(m)、实际d(mm)、v(m/s)。'
                 'Do not use for 缺参/单位不明；不由DN猜内径、不由流量推流速。'
                 '程序按v>=1.2或v<1.2分支；经验近似、适用性未核验，保留警示。'),
    args_model=ShevelevArgs, guard=guard_shevelev_request, execute=_execute,
)


def _no_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, dict):
        return all(_no_bool(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return all(_no_bool(v) for v in value)
    return True


def verified_shevelev_result(args: dict[str, Any], payload: dict[str, Any], receipt: Any,
                             tool_call_id: str, query: str | None) -> bool:
    """The observer must supply its independently trusted current user text."""
    try:
        if not isinstance(query, str) or not _no_bool([args, payload, receipt]):
            return False
        validated = ShevelevArgs.model_validate(args)
        if not isinstance(receipt, dict):
            return False
        value = payload.get('head_loss_m')
        return (bool(tool_call_id)
            and set(receipt) == {'tool_name', 'tool_call_id', 'args', 'result', 'query', 'branch'}
            and receipt['tool_name'] == calc_head_loss_shevelev_spec.name
            and receipt['tool_call_id'] == tool_call_id and receipt['query'] == query
            and shevelev_args_match(query, args)
            and ShevelevArgs.model_validate(receipt['args']).model_dump() == validated.model_dump()
            and receipt['branch'] == payload['branch']
            and receipt['result'] == payload == _payload(validated, query)
            and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0)
    except (ValueError, TypeError, KeyError, OverflowError):
        return False
