"""水力计算工具族（P1）。

公式口径唯一来源：``docs/design/09-hydraulics-formulas.md``（ADR-01 落实）：
- 公式由输入显式指定（``HydraulicsFormula``），禁止静默切换；
- 缺参返回结构化 ``missing_inputs``，禁止猜测；
- 越界计算照常返回结果，但用 ``assumptions`` 标注并交由上层明示。

纯函数实现，先不依赖 Step 4 的 ToolRegistry；Step 4 后在此注册工具。
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

GRAVITY_DEFAULT = 9.81  # m/s²
SHEVELEV_SPEED_BREAKPOINT = 1.2  # m/s，舍维列夫公式分段点

HydraulicsFormula = Literal[
    "velocity",
    "head_loss_darcy_weisbach",
    "head_loss_hazen_williams",
    "head_loss_shevelev",
    "diameter",
    "slope_manning",
    "pump_head",
]

# 口径表 §6 公式清单；不在表中的公式不允许出现在输入中
FORMULA_SET: frozenset[str] = frozenset(
    {
        "velocity",
        "head_loss_darcy_weisbach",
        "head_loss_hazen_williams",
        "head_loss_shevelev",
        "diameter",
        "slope_manning",
        "pump_head",
    }
)

# 给水/排水场景流速约束（口径表 §1；非全局硬编码，随 scenario 显式区分）
_VELOCITY_CONSTRAINTS: dict[str, dict[str, float | str]] = {
    "water_supply": {"min_mps": 0.6, "max_mps": 1.5, "label": "给水常用区间 0.6–1.5 m/s"},
    "drainage": {"min_mps": 0.6, "max_mps": 5.0, "label": "排水：最小 0.6 m/s；非金属管最大 5 m/s（金属 10 m/s）"},
}


@dataclass(slots=True)
class CalcResult:
    """统一计算输出。``ok=True`` 时 ``values`` 有效；缺参/非法时 ``ok=False``。"""

    ok: bool
    values: dict[str, float] = field(default_factory=dict)
    missing_inputs: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)


def _missing(inputs: dict[str, Any], required: list[str]) -> list[str]:
    missing: list[str] = []
    for name in required:
        value = inputs.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(name)
    return missing


def _require(inputs: dict[str, Any], required: list[str]) -> CalcResult | None:
    missing = _missing(inputs, required)
    if missing:
        return CalcResult(ok=False, missing_inputs=missing)
    return None


def _positive_numbers(inputs: dict[str, Any], names: list[str]) -> list[str]:
    """返回非正（非法）的数值参数名。"""
    invalid: list[str] = []
    for name in names:
        value = inputs.get(name)
        if value is not None:
            try:
                if float(value) <= 0:
                    invalid.append(name)
            except (TypeError, ValueError):
                invalid.append(name)
    return invalid


def _numeric(inputs: dict[str, Any], name: str) -> float:
    return float(inputs[name])


def calc_velocity(inputs: dict[str, Any]) -> CalcResult:
    """满管流圆管断面平均流速：``v = 4Q/(πd²)``。"""
    bad = _require(inputs, ["flow_lps", "diameter_mm"])
    if bad is not None:
        return bad
    invalid = _positive_numbers(inputs, ["flow_lps", "diameter_mm"])
    if invalid:
        return CalcResult(ok=False, errors=[f"{name} 必须为正数" for name in invalid])

    flow_lps = _numeric(inputs, "flow_lps")
    diameter_mm = _numeric(inputs, "diameter_mm")
    flow_m3s = flow_lps / 1000.0
    diameter_m = diameter_mm / 1000.0
    area_m2 = math.pi * (diameter_m / 2.0) ** 2
    velocity = flow_m3s / area_m2

    scenario = str(inputs.get("scenario") or "water_supply")
    constraints = _VELOCITY_CONSTRAINTS.get(scenario)
    assumptions: list[str] = []
    if constraints is not None:
        min_mps = float(constraints["min_mps"])
        max_mps = float(constraints["max_mps"])
        label = str(constraints["label"])
        if velocity < min_mps:
            assumptions.append(f"流速 {velocity:.3f} m/s 低于{label}")
        elif velocity > max_mps:
            assumptions.append(f"流速 {velocity:.3f} m/s 高于{label}")
    else:
        assumptions.append(f"未知 scenario={scenario!r}，未应用流速约束")

    return CalcResult(ok=True, values={"velocity_mps": velocity}, assumptions=assumptions)


def calc_head_loss_darcy_weisbach(inputs: dict[str, Any]) -> CalcResult:
    """Darcy-Weisbach 沿程水头损失：``hf = f·(L/d)·(v²/2g)``。摩阻系数必须显式提供。"""
    bad = _require(inputs, ["length_m", "diameter_mm", "velocity_mps", "friction_factor"])
    if bad is not None:
        return bad
    invalid = _positive_numbers(inputs, ["length_m", "diameter_mm", "velocity_mps"])
    friction = inputs.get("friction_factor")
    if invalid or friction is None or float(friction) <= 0:
        return CalcResult(ok=False, errors=[f"{name} 必须为正数" for name in invalid] + ["friction_factor 必须为正数"])

    length_m = _numeric(inputs, "length_m")
    diameter_m = _numeric(inputs, "diameter_mm") / 1000.0
    velocity = _numeric(inputs, "velocity_mps")
    gravity = float(inputs.get("gravity_mps2") or GRAVITY_DEFAULT)
    head_loss = float(friction) * (length_m / diameter_m) * (velocity**2 / (2.0 * gravity))
    return CalcResult(ok=True, values={"head_loss_m": head_loss})


def calc_head_loss_hazen_williams(inputs: dict[str, Any]) -> CalcResult:
    """Hazen-Williams（SI 制）：``hf = 10.67·L·Q^1.852 / (C^1.852·d^4.8704)``。"""
    bad = _require(inputs, ["length_m", "diameter_mm", "flow_lps", "hazen_williams_c"])
    if bad is not None:
        return bad
    invalid = _positive_numbers(inputs, ["length_m", "diameter_mm", "flow_lps", "hazen_williams_c"])
    if invalid:
        return CalcResult(ok=False, errors=[f"{name} 必须为正数" for name in invalid])

    length_m = _numeric(inputs, "length_m")
    diameter_m = _numeric(inputs, "diameter_mm") / 1000.0
    flow_m3s = _numeric(inputs, "flow_lps") / 1000.0
    c = _numeric(inputs, "hazen_williams_c")
    head_loss = 10.67 * length_m * flow_m3s**1.852 / (c**1.852 * diameter_m**4.8704)
    return CalcResult(ok=True, values={"head_loss_m": head_loss})


def calc_head_loss_shevelev(inputs: dict[str, Any]) -> CalcResult:
    """舍维列夫（给水钢管/铸铁管）：v≥1.2 用 ``i=0.00107v²/d^1.3``；否则用低速分支。"""
    bad = _require(inputs, ["length_m", "diameter_mm", "velocity_mps"])
    if bad is not None:
        return bad
    invalid = _positive_numbers(inputs, ["length_m", "diameter_mm", "velocity_mps"])
    if invalid:
        return CalcResult(ok=False, errors=[f"{name} 必须为正数" for name in invalid])

    length_m = _numeric(inputs, "length_m")
    diameter_m = _numeric(inputs, "diameter_mm") / 1000.0
    velocity = _numeric(inputs, "velocity_mps")

    if velocity >= SHEVELEV_SPEED_BREAKPOINT:
        slope = 0.00107 * velocity**2 / diameter_m**1.3
    else:
        slope = 0.000912 * velocity**2 * (1 + 0.867 / velocity) ** 0.3 / diameter_m**1.3
    return CalcResult(ok=True, values={"head_loss_m": slope * length_m})


def calc_diameter(inputs: dict[str, Any]) -> CalcResult:
    """由设计流量与设计流速求管径：``d = sqrt(4Q/(πv))``。"""
    bad = _require(inputs, ["flow_lps", "velocity_mps"])
    if bad is not None:
        return bad
    invalid = _positive_numbers(inputs, ["flow_lps", "velocity_mps"])
    if invalid:
        return CalcResult(ok=False, errors=[f"{name} 必须为正数" for name in invalid])

    flow_m3s = _numeric(inputs, "flow_lps") / 1000.0
    velocity = _numeric(inputs, "velocity_mps")
    diameter_m = math.sqrt(4.0 * flow_m3s / (math.pi * velocity))
    return CalcResult(ok=True, values={"diameter_mm": diameter_m * 1000.0})


def calc_slope_manning(inputs: dict[str, Any]) -> CalcResult:
    """排水重力流非满流曼宁坡度：``S = (n·Q / (A·R^(2/3)))²``。"""
    bad = _require(inputs, ["flow_lps", "diameter_mm", "fill_ratio", "manning_n"])
    if bad is not None:
        return bad
    invalid = _positive_numbers(inputs, ["flow_lps", "diameter_mm", "manning_n"])
    if invalid:
        return CalcResult(ok=False, errors=[f"{name} 必须为正数" for name in invalid])

    fill_ratio = _numeric(inputs, "fill_ratio")
    if not (0 < fill_ratio < 1):
        return CalcResult(ok=False, errors=["fill_ratio 必须在 (0, 1) 开区间内"])

    diameter_m = _numeric(inputs, "diameter_mm") / 1000.0
    flow_m3s = _numeric(inputs, "flow_lps") / 1000.0
    manning_n = _numeric(inputs, "manning_n")

    theta = 2.0 * math.acos(1.0 - 2.0 * fill_ratio)
    area = (diameter_m**2 / 8.0) * (theta - math.sin(theta))
    wetted_perimeter = (diameter_m / 2.0) * theta
    hydraulic_radius = area / wetted_perimeter

    slope = (manning_n * flow_m3s / (area * hydraulic_radius ** (2.0 / 3.0))) ** 2
    return CalcResult(ok=True, values={"slope": slope})


def calc_pump_head(inputs: dict[str, Any]) -> CalcResult:
    """泵扬程：``H = H_static + H_friction + H_local + H_residual``。"""
    bad = _require(inputs, ["static_head_m", "friction_head_m", "local_head_m", "residual_head_m"])
    if bad is not None:
        return bad
    invalid = [name for name in ["static_head_m", "friction_head_m", "local_head_m", "residual_head_m"] if float(inputs[name]) < 0]
    if invalid:
        return CalcResult(ok=False, errors=[f"{name} 不允许为负数" for name in invalid])

    total = sum(_numeric(inputs, name) for name in ["static_head_m", "friction_head_m", "local_head_m", "residual_head_m"])
    return CalcResult(ok=True, values={"pump_head_m": total})


_CALC_FUNCTIONS: dict[str, Callable[[dict[str, Any]], CalcResult]] = {
    "velocity": calc_velocity,
    "head_loss_darcy_weisbach": calc_head_loss_darcy_weisbach,
    "head_loss_hazen_williams": calc_head_loss_hazen_williams,
    "head_loss_shevelev": calc_head_loss_shevelev,
    "diameter": calc_diameter,
    "slope_manning": calc_slope_manning,
    "pump_head": calc_pump_head,
}


def run_calc(formula: str, inputs: dict[str, Any]) -> CalcResult:
    """按显式公式口径分发计算。公式不在口径表 → 结构化工单错误。"""
    if formula not in FORMULA_SET:
        return CalcResult(
            ok=False,
            errors=[f"未知公式 {formula!r}；口径表允许：{sorted(FORMULA_SET)}"],
        )
    return _CALC_FUNCTIONS[formula](inputs)
