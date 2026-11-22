"""水力计算工具（Step 8 → 可选 Step 22 反向校验）。

规格 `docs/design/09-hydraulics-formulas.md`（ADR-01 落实）：
- 公式由输入显式指定（`HydraulicsFormula`），禁止静默切换；
- 缺参返回结构化 `missing_inputs`，禁止猜测；
- 越界计算照常返回结果，但用 `assumptions` 标注并交由上层明示。

纯函数实现，先不依赖 Step 4 的 ToolRegistry；Step 4 后在此注册工具。
"""
from __future__ import annotations

import math
import sys
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
# 排水上限按管材区分（GB50014-2021 5.2.9）：塑料管/铸铁管 5.0 m/s，
# 混凝土管/钢管 10.0 m/s。 canonical 阈值表见 `drawing/rules.py::_MAX_VELOCITY_BY_MATERIAL`，
# 此处保留保守默认值 5.0（未知管材），有 `material` 输入时按管材精确判定。
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


def _to_float(value: Any) -> float | None:
    """安全取数（P1）：数值/数值字符串 → float；其余 → None。

    None / 空串 / 非数值 / NaN / ±inf / bool 一律返回 None，
    避免字符串等非法类型穿透 ``float()`` 导致调用方（CLI/图）裸崩。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _positive_numbers(inputs: dict[str, Any], names: list[str]) -> list[str]:
    """返回非正（非法）的数值参数名：非数值、NaN/inf、≤0 均非法。"""
    invalid: list[str] = []
    for name in names:
        value = inputs.get(name)
        if value is None:
            continue
        number = _to_float(value)
        if number is None or number <= 0:
            invalid.append(name)
    return invalid


def _non_negative_numbers(inputs: dict[str, Any], names: list[str]) -> list[str]:
    """返回负值/非数值的参数名；0 显式允许（口径表 §5：分量可为 0）。"""
    invalid: list[str] = []
    for name in names:
        value = inputs.get(name)
        if value is None:
            continue
        number = _to_float(value)
        if number is None or number < 0:
            invalid.append(name)
    return invalid


def _numeric(inputs: dict[str, Any], name: str) -> float:
    """校验通过后的安全取数；校验遗漏时抛出（内部契约错误，不应发生）。"""
    number = _to_float(inputs.get(name))
    if number is None:
        raise ValueError(f"{name} 不是有效数值（内部校验遗漏）")
    return number


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
    material_raw = inputs.get("material")
    material = material_raw if isinstance(material_raw, str) and material_raw.strip() else None

    constraints: dict[str, float | str] | None
    if scenario == "drainage" and material is not None:
        from water_agent.drawing.rules import get_max_velocity_for_material as _get_max_v

        _max_v = _get_max_v(material)
        constraints = {"min_mps": 0.6, "max_mps": _max_v, "label": f"排水（管材 {material}）最小 0.6 m/s、最大 {_max_v:g} m/s"}
    else:
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
    invalid = _positive_numbers(inputs, ["length_m", "diameter_mm", "velocity_mps", "friction_factor"])
    if invalid:
        return CalcResult(ok=False, errors=[f"{name} 必须为正数" for name in invalid])

    gravity_raw = inputs.get("gravity_mps2")
    if gravity_raw is None or (isinstance(gravity_raw, str) and not gravity_raw.strip()):
        gravity = GRAVITY_DEFAULT  # 口径表 §2.1 显式允许的默认值
    else:
        parsed_gravity = _to_float(gravity_raw)
        if parsed_gravity is None or parsed_gravity <= 0:
            return CalcResult(ok=False, errors=["gravity_mps2 必须为正数"])
        gravity = parsed_gravity

    length_m = _numeric(inputs, "length_m")
    diameter_m = _numeric(inputs, "diameter_mm") / 1000.0
    velocity = _numeric(inputs, "velocity_mps")
    friction = _numeric(inputs, "friction_factor")
    try:
        head_loss = friction * (length_m / diameter_m) * (velocity**2 / (2.0 * gravity))
    except (OverflowError, ZeroDivisionError):
        return CalcResult(ok=False, errors=["Darcy–Weisbach 计算溢出或单位换算下溢，请核对输入范围"])
    if not math.isfinite(head_loss) or head_loss <= 0:
        return CalcResult(ok=False, errors=["Darcy–Weisbach 结果不是有限正数，请核对输入范围"])
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
    try:
        flow_power, c_power, diameter_power = flow_m3s**1.852, c**1.852, diameter_m**4.8704
        numerator_scale = 10.67 * length_m
        numerator, denominator = numerator_scale * flow_power, c_power * diameter_power
        # A positive subnormal power can lose most significant digits while
        # the final value is finite. Fail closed outside normal float range.
        intermediates = (flow_m3s, diameter_m, flow_power, c_power, diameter_power,
                         numerator_scale, numerator, denominator)
        if any(not math.isfinite(v) or v < sys.float_info.min for v in intermediates):
            return CalcResult(ok=False, errors=["Hazen–Williams中间量溢出或下溢，请核对输入范围"])
        head_loss = numerator / denominator
    except (OverflowError, ZeroDivisionError):
        return CalcResult(ok=False, errors=["Hazen–Williams计算溢出或下溢，请核对输入范围"])
    if not math.isfinite(head_loss) or head_loss < sys.float_info.min:
        return CalcResult(ok=False, errors=["Hazen–Williams结果不是有限正数，请核对输入范围"])
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
    diameter_mm = _numeric(inputs, "diameter_mm")
    diameter_m = diameter_mm / 1000.0
    velocity = _numeric(inputs, "velocity_mps")

    # 口径表 §2.3 适用范围 DN50–DN1000：范围外照常计算，但 assumptions 标注越界
    assumptions: list[str] = []
    if diameter_mm < 50 or diameter_mm > 1000:
        assumptions.append(
            f"管径 {diameter_mm:.0f} mm 超出舍维列夫公式经验适用范围 DN50–DN1000，结果仅供参考"
        )

    try:
        velocity_squared, diameter_power = velocity**2, diameter_m**1.3
        intermediates = [length_m, diameter_m, velocity_squared, diameter_power]
        if velocity >= SHEVELEV_SPEED_BREAKPOINT:
            numerator = 0.00107 * velocity_squared
        else:
            reciprocal_term = 0.867 / velocity
            base = 1 + reciprocal_term
            correction = base**0.3
            scaled = 0.000912 * velocity_squared
            numerator = scaled * correction
            intermediates.extend((reciprocal_term, base, correction, scaled))
        intermediates.append(numerator)
        # Positive subnormal intermediates can lose significant precision even
        # when the final answer is finite: fail closed, without changing §2.3.
        if any(not math.isfinite(v) or v < sys.float_info.min for v in intermediates):
            return CalcResult(ok=False, errors=["舍维列夫中间量溢出或下溢，请核对输入范围"], assumptions=assumptions)
        slope = numerator / diameter_power
        head_loss = slope * length_m
        if any(not math.isfinite(v) or v < sys.float_info.min for v in (slope, head_loss)):
            return CalcResult(ok=False, errors=["舍维列夫结果溢出或下溢，请核对输入范围"], assumptions=assumptions)
    except (OverflowError, ZeroDivisionError):
        return CalcResult(ok=False, errors=["舍维列夫计算溢出或下溢，请核对输入范围"], assumptions=assumptions)
    return CalcResult(ok=True, values={"head_loss_m": head_loss}, assumptions=assumptions)


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
    denominator = math.pi * velocity
    if flow_m3s <= 0 or not math.isfinite(denominator):
        return CalcResult(ok=False, errors=["管径计算超出浮点可表示范围，请调整输入数量级。"])
    squared = 4.0 * flow_m3s / denominator
    if not math.isfinite(squared) or squared <= 0:
        return CalcResult(ok=False, errors=["管径计算超出浮点可表示范围，请调整输入数量级。"])
    diameter_mm = math.sqrt(squared) * 1000.0
    if not math.isfinite(diameter_mm) or diameter_mm <= 0:
        return CalcResult(ok=False, errors=["管径计算超出浮点可表示范围，请调整输入数量级。"])
    return CalcResult(ok=True, values={"diameter_mm": diameter_mm})


def calc_slope_manning(inputs: dict[str, Any]) -> CalcResult:
    """排水重力流非满流曼宁坡度：``S = (n·Q / (A·R^(2/3)))²``。"""
    bad = _require(inputs, ["flow_lps", "diameter_mm", "fill_ratio", "manning_n"])
    if bad is not None:
        return bad
    try:
        invalid = _positive_numbers(inputs, ["flow_lps", "diameter_mm", "manning_n"])
    except OverflowError:
        return CalcResult(ok=False, errors=["曼宁输入超出浮点表示范围。"])
    if invalid:
        return CalcResult(ok=False, errors=[f"{name} 必须为正数" for name in invalid])

    fill_ratio = _to_float(inputs.get("fill_ratio"))
    if fill_ratio is None or not (0 < fill_ratio < 1):
        return CalcResult(ok=False, errors=["fill_ratio 必须在 (0, 1) 开区间内"])

    # Equivalent half-angle geometry avoids 1-2r rounding to 1 near zero.
    # Near full, complement evaluation retains the small dry cap angle.
    try:
        diameter_m = _numeric(inputs, "diameter_mm") / 1000.0
        flow_m3s = _numeric(inputs, "flow_lps") / 1000.0
        manning_n = _numeric(inputs, "manning_n")
        theta = (4 * math.asin(math.sqrt(fill_ratio)) if fill_ratio <= .5 else
                 2 * math.pi - 4 * math.asin(math.sqrt(1-fill_ratio)))
        if theta < .5:
            # theta-sin(theta): alternating series, next term < 2e-19
            # relative to the leading term at theta=.5 (through theta^17).
            term = theta**3 / 6
            segment = term
            for k in range(2, 9):
                term *= -theta**2 / ((2*k)*(2*k+1))
                segment += term
        else:
            segment = theta - math.sin(theta)
        squared_d = diameter_m**2
        area = squared_d / 8 * segment
        wetted_perimeter = diameter_m / 2 * theta
        hydraulic_radius = area / wetted_perimeter
        radius_root = math.cbrt(hydraulic_radius)
        radius_power = radius_root**2
        numerator = manning_n * flow_m3s
        denominator = area * radius_power
        ratio = numerator / denominator
        slope = ratio**2
        import sys
        intermediates = (diameter_m, flow_m3s, manning_n, theta, segment,
                         squared_d, squared_d/8, diameter_m/2, area, wetted_perimeter,
                         hydraulic_radius, radius_root, radius_power, numerator, denominator, ratio, slope)
        if any(not math.isfinite(v) or v < sys.float_info.min for v in intermediates):
            raise ArithmeticError
        # The frozen absolute error contract is 1e-9 for large slopes;
        # reserve 128 output ulps for accumulated geometry/power/ratio rounding,
        # rather than checking final representability alone.
        if 128 * math.ulp(slope) > 1e-9:
            raise ArithmeticError
    except (ArithmeticError, ValueError):
        return CalcResult(ok=False, errors=["曼宁数值精度或浮点表示范围不足；合法充满度未钳制，请调整输入数量级。"])
    return CalcResult(ok=True, values={"slope": slope})


def calc_pump_head(inputs: dict[str, Any]) -> CalcResult:
    """泵扬程：``H = H_static + H_friction + H_local + H_residual``。"""
    bad = _require(inputs, ["static_head_m", "friction_head_m", "local_head_m", "residual_head_m"])
    if bad is not None:
        return bad
    invalid = _non_negative_numbers(inputs, ["static_head_m", "friction_head_m", "local_head_m", "residual_head_m"])
    if invalid:
        return CalcResult(ok=False, errors=[f"{name} 必须为非负数" for name in invalid])

    total = sum(_numeric(inputs, name) for name in ["static_head_m", "friction_head_m", "local_head_m", "residual_head_m"])
    if not math.isfinite(total):
        return CalcResult(ok=False, errors=["泵扬程合计超出有限数值范围"], assumptions=["仅给定四分量数学合计，工程适用性未核验"])
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
    """按显式公式口径分发计算。公式不在口径表时返回结构化工单错误。"""
    if formula not in FORMULA_SET:
        return CalcResult(
            ok=False,
            errors=[f"未知公式 {formula!r}；口径表允许：{sorted(FORMULA_SET)}"],
        )
    return _CALC_FUNCTIONS[formula](inputs)
