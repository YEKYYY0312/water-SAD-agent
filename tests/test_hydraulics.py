"""水力计算工具测试：每个公式 ≥2 组独立来源的已知答案，误差断言。

独立来源：gold 值来自口径表（docs/design/09-hydraulics-formulas.md）的参考计算，
不与被测实现同源生成。
"""
from __future__ import annotations

import math

from water_agent.tools.hydraulics import (
    FORMULA_SET,
    calc_diameter,
    calc_head_loss_darcy_weisbach,
    calc_head_loss_hazen_williams,
    calc_head_loss_shevelev,
    calc_pump_head,
    calc_slope_manning,
    calc_velocity,
    run_calc,
)


def assert_value(result, key: str, expected: float, *, rel: float = 0.01, abs_: float = 0.001) -> None:
    assert result.ok, f"expected ok, got errors={result.errors} missing={result.missing_inputs}"
    actual = result.values[key]
    assert abs(actual - expected) <= max(abs_, rel * abs(expected)), (
        f"{key}: actual={actual}, expected={expected}"
    )


class TestVelocity:
    def test_water_supply_20lps_dn150(self) -> None:
        # 任务书示例：Q=20L/s、DN150 → v=1.132
        assert_value(calc_velocity({"flow_lps": 20, "diameter_mm": 150}), "velocity_mps", 1.132)

    def test_water_supply_8lps_dn100(self) -> None:
        assert_value(calc_velocity({"flow_lps": 8, "diameter_mm": 100}), "velocity_mps", 1.019)

    def test_drainage_scenario_60lps_dn300(self) -> None:
        # 排水场景，0.849 m/s 在 0.6–5.0 内，无 assumptions 警告
        result = calc_velocity({"flow_lps": 60, "diameter_mm": 300, "scenario": "drainage"})
        assert_value(result, "velocity_mps", 0.849)
        assert result.assumptions == []

    def test_drainage_high_velocity_warns(self) -> None:
        # DN50、Q=15L/s 排水 → v≈7.64 m/s 超出非金属管 5 m/s，assumptions 警告但不拦截
        result = calc_velocity({"flow_lps": 15, "diameter_mm": 50, "scenario": "drainage"})
        assert result.ok
        assert any("高于" in assumption for assumption in result.assumptions)

    def test_missing_inputs(self) -> None:
        result = calc_velocity({"diameter_mm": 150})
        assert not result.ok
        assert result.missing_inputs == ["flow_lps"]

    def test_negative_flow_invalid(self) -> None:
        result = calc_velocity({"flow_lps": -5, "diameter_mm": 150})
        assert not result.ok
        assert result.errors


class TestDarcyWeisbach:
    def test_dn150_case(self) -> None:
        assert_value(
            calc_head_loss_darcy_weisbach({"length_m": 100, "diameter_mm": 150, "velocity_mps": 1.132, "friction_factor": 0.02}),
            "head_loss_m", 0.871,
        )

    def test_dn200_case(self) -> None:
        assert_value(
            calc_head_loss_darcy_weisbach({"length_m": 500, "diameter_mm": 200, "velocity_mps": 1.5, "friction_factor": 0.025}),
            "head_loss_m", 7.167,
        )

    def test_manual_consistency(self) -> None:
        # 与手工算式交叉验证：hf = f·(L/d)·(v²/2g)
        result = calc_head_loss_darcy_weisbach({"length_m": 80, "diameter_mm": 100, "velocity_mps": 1.0, "friction_factor": 0.02})
        expected = 0.02 * (80 / 0.1) * (1.0**2 / (2 * 9.81))
        assert_value(result, "head_loss_m", expected, abs_=1e-9)

    def test_missing_friction(self) -> None:
        result = calc_head_loss_darcy_weisbach({"length_m": 100, "diameter_mm": 150, "velocity_mps": 1.0})
        assert not result.ok
        assert "friction_factor" in result.missing_inputs


class TestHazenWilliams:
    def test_dn150_case(self) -> None:
        assert_value(
            calc_head_loss_hazen_williams({"length_m": 1000, "diameter_mm": 150, "flow_lps": 20, "hazen_williams_c": 130}),
            "head_loss_m", 9.537,
        )

    def test_dn300_case(self) -> None:
        assert_value(
            calc_head_loss_hazen_williams({"length_m": 2000, "diameter_mm": 300, "flow_lps": 60, "hazen_williams_c": 150}),
            "head_loss_m", 3.827,
        )

    def test_c_required(self) -> None:
        result = calc_head_loss_hazen_williams({"length_m": 1000, "diameter_mm": 150, "flow_lps": 20})
        assert not result.ok
        assert "hazen_williams_c" in result.missing_inputs


class TestShevelev:
    def test_high_speed_branch(self) -> None:
        # v=1.5 ≥ 1.2 分支
        assert_value(calc_head_loss_shevelev({"length_m": 100, "diameter_mm": 150, "velocity_mps": 1.5}), "head_loss_m", 2.836)

    def test_low_speed_branch(self) -> None:
        # v=0.8 < 1.2 分支
        assert_value(calc_head_loss_shevelev({"length_m": 200, "diameter_mm": 200, "velocity_mps": 0.8}), "head_loss_m", 1.179)

    def test_breakpoint_continuity(self) -> None:
        # v=1.2 恰好落在高速分支：i=0.00107·1.2²/0.1^1.3≈0.030743，hf≈1.537
        assert_value(calc_head_loss_shevelev({"length_m": 50, "diameter_mm": 100, "velocity_mps": 1.2}), "head_loss_m", 1.537, rel=0.02)

    def test_dn_range_annotation(self) -> None:
        """口径表 §2.3 适用范围 DN50–DN1000：范围外照常计算 + assumptions 标注（P1 评审）。"""
        below = calc_head_loss_shevelev({"length_m": 100, "diameter_mm": 40, "velocity_mps": 1.5})
        assert below.ok, "越界照常计算，不拦截"
        assert any("DN50–DN1000" in a for a in below.assumptions)
        above = calc_head_loss_shevelev({"length_m": 100, "diameter_mm": 1200, "velocity_mps": 1.5})
        assert above.ok
        assert any("DN50–DN1000" in a for a in above.assumptions)
        inside = calc_head_loss_shevelev({"length_m": 100, "diameter_mm": 150, "velocity_mps": 1.5})
        assert inside.assumptions == [], "范围内不产生越界标注"


class TestDiameter:
    def test_20lps_at_1p2(self) -> None:
        assert_value(calc_diameter({"flow_lps": 20, "velocity_mps": 1.2}), "diameter_mm", 145.673)

    def test_40lps_at_1p0(self) -> None:
        assert_value(calc_diameter({"flow_lps": 40, "velocity_mps": 1.0}), "diameter_mm", 225.676)

    def test_manual_consistency(self) -> None:
        result = calc_diameter({"flow_lps": 30, "velocity_mps": 0.8})
        expected = math.sqrt(4 * 0.03 / (math.pi * 0.8)) * 1000
        assert_value(result, "diameter_mm", expected, abs_=1e-9)

    def test_zero_velocity_invalid(self) -> None:
        result = calc_diameter({"flow_lps": 20, "velocity_mps": 0})
        assert not result.ok


class TestSlopeManning:
    def test_half_full_dn300(self) -> None:
        assert_value(calc_slope_manning({"flow_lps": 30, "diameter_mm": 300, "fill_ratio": 0.5, "manning_n": 0.013}), "slope", 0.00385, abs_=0.0001)

    def test_fill_06_dn400(self) -> None:
        assert_value(calc_slope_manning({"flow_lps": 50, "diameter_mm": 400, "fill_ratio": 0.6, "manning_n": 0.013}), "slope", 0.001277, abs_=0.0001)

    def test_fill_ratio_out_of_range(self) -> None:
        for fill in (0.0, 1.0, 1.5):
            result = calc_slope_manning({"flow_lps": 30, "diameter_mm": 300, "fill_ratio": fill, "manning_n": 0.013})
            assert not result.ok, f"fill_ratio={fill} 应判非法"

    def test_manning_n_required(self) -> None:
        result = calc_slope_manning({"flow_lps": 30, "diameter_mm": 300, "fill_ratio": 0.5})
        assert not result.ok
        assert "manning_n" in result.missing_inputs


class TestPumpHead:
    def test_sum_components(self) -> None:
        assert_value(
            calc_pump_head({"static_head_m": 10, "friction_head_m": 2.8, "local_head_m": 0.5, "residual_head_m": 2}),
            "pump_head_m", 15.3,
        )

    def test_second_case(self) -> None:
        assert_value(
            calc_pump_head({"static_head_m": 25, "friction_head_m": 4.2, "local_head_m": 1.2, "residual_head_m": 3}),
            "pump_head_m", 33.4,
        )

    def test_negative_component_invalid(self) -> None:
        result = calc_pump_head({"static_head_m": 10, "friction_head_m": -1, "local_head_m": 0.5, "residual_head_m": 2})
        assert not result.ok
        assert result.errors

    def test_missing_component(self) -> None:
        result = calc_pump_head({"static_head_m": 10, "friction_head_m": 2.8, "local_head_m": 0.5})
        assert not result.ok
        assert "residual_head_m" in result.missing_inputs


class TestInputTypeGuard:
    """P1：字符串/NaN/inf/bool 等类型穿透必须返回结构化错误，不得裸崩。"""

    def test_fill_ratio_non_numeric_string(self) -> None:
        result = calc_slope_manning(
            {"flow_lps": 30, "diameter_mm": 300, "fill_ratio": "abc", "manning_n": 0.013}
        )
        assert not result.ok
        assert result.errors and "fill_ratio" in result.errors[0]

    def test_fill_ratio_numeric_string_accepted(self) -> None:
        result = calc_slope_manning(
            {"flow_lps": 30, "diameter_mm": 300, "fill_ratio": "0.5", "manning_n": 0.013}
        )
        assert result.ok
        assert_value(result, "slope", 0.00385, abs_=0.0001)

    def test_gravity_non_numeric_string(self) -> None:
        result = calc_head_loss_darcy_weisbach(
            {
                "length_m": 100,
                "diameter_mm": 150,
                "velocity_mps": 1.132,
                "friction_factor": 0.02,
                "gravity_mps2": "abc",
            }
        )
        assert not result.ok
        assert result.errors and "gravity_mps2" in "".join(result.errors)

    def test_gravity_negative(self) -> None:
        result = calc_head_loss_darcy_weisbach(
            {
                "length_m": 100,
                "diameter_mm": 150,
                "velocity_mps": 1.132,
                "friction_factor": 0.02,
                "gravity_mps2": -9.81,
            }
        )
        assert not result.ok

    def test_gravity_numeric_string_default_equivalent(self) -> None:
        result = calc_head_loss_darcy_weisbach(
            {
                "length_m": 100,
                "diameter_mm": 150,
                "velocity_mps": 1.132,
                "friction_factor": 0.02,
                "gravity_mps2": "9.81",
            }
        )
        assert result.ok
        expected = 0.02 * (100 / 0.15) * (1.132**2 / (2 * 9.81))
        assert_value(result, "head_loss_m", expected, abs_=1e-9)

    def test_nan_and_inf_rejected(self) -> None:
        for bad in (float("nan"), float("inf"), -float("inf")):
            result = calc_velocity({"flow_lps": bad, "diameter_mm": 150})
            assert not result.ok, f"flow_lps={bad} 应判非法"

    def test_bool_rejected(self) -> None:
        result = calc_velocity({"flow_lps": True, "diameter_mm": 150})
        assert not result.ok

    def test_pump_head_zero_component_allowed(self) -> None:
        # 口径表 §5：某分量为 0 时显式传 0 允许（仅 <0 非法）
        result = calc_pump_head(
            {"static_head_m": 10, "friction_head_m": 0, "local_head_m": 0.5, "residual_head_m": 2}
        )
        assert result.ok
        assert abs(result.values["pump_head_m"] - 12.5) < 1e-9

    def test_pump_head_non_numeric(self) -> None:
        result = calc_pump_head(
            {"static_head_m": 10, "friction_head_m": "abc", "local_head_m": 0.5, "residual_head_m": 2}
        )
        assert not result.ok


class TestDispatch:
    def test_all_formulas_are_dispatchable(self) -> None:
        assert FORMULA_SET == set(run_calc.__globals__.get("_CALC_FUNCTIONS", {}).keys()), (
            f"公式清单与分发表不一致：{FORMULA_SET ^ set(run_calc.__globals__.get('_CALC_FUNCTIONS', {}).keys())}"
        )

    def test_unknown_formula(self) -> None:
        result = run_calc("shevelev_auto", {})
        assert not result.ok
        assert "未知公式" in result.errors[0]

    def test_formula_set_matches_spec_table(self) -> None:
        # 口径表 §6 的 7 个公式
        assert FORMULA_SET == {
            "velocity",
            "head_loss_darcy_weisbach",
            "head_loss_hazen_williams",
            "head_loss_shevelev",
            "diameter",
            "slope_manning",
            "pump_head",
        }
