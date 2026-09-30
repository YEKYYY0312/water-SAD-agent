"""P6-S1 Step 2：出图输入契约、稳定错误码与流速程序化校核。

守住：
- ``GenerationRequest`` 是 ``extra="forbid"`` + ``frozen``，枚举与取值范围照执行 prompt；
- 错误只回显字段路径与原因，**不回显提交的值**；错误码三个且状态码锁死；
- 主管、支管分别走 `calc_velocity`；缺支管流量 → missing_inputs，**不补默认值**；
- 流速超出规则表建议区间只进 warnings / 图面提示，不阻断；
- `drawing_gen_p0` 评测集的非法参数那一半在本步就能跑（评测先行）。
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from water_agent.drawing.building_rules import BuildingType, load_building_rules
from water_agent.drawing.generation_models import (
    DIAMETER_BASIS_NOTE,
    GEN_INVALID_INPUT,
    GEN_RENDER_FAILED,
    GEN_UNSUPPORTED_SYSTEM,
    GENERATION_ERROR_STATUS,
    GenerationError,
    GenerationRequest,
    NominalDiameter,
    check_velocities,
    drawing_velocity_note,
    parse_generation_request,
    resolve_riser_position,
)
from water_agent.tools.hydraulics import calc_velocity

REPO_ROOT = Path(__file__).resolve().parents[1]
SUITE_DIR = REPO_ROOT / "tests" / "eval" / "drawing_gen_p0"

SPEC_EXAMPLE = {
    "system_type": "给水",
    "building_type": "住宅",
    "floors": 6,
    "units_per_floor": 2,
    "riser_position": "楼梯间",
    "main_pipe": {"dn": "DN50", "material": "PPR"},
    "branch_pipe": {"dn": "DN25", "material": "PPR"},
    "flow_rate_lps": 2.5,
    "pressure_mpa": 0.35,
}


def _request(**overrides) -> GenerationRequest:
    return parse_generation_request({**SPEC_EXAMPLE, **overrides})


def _error(payload: dict) -> GenerationError:
    with pytest.raises(GenerationError) as info:
        parse_generation_request(payload)
    return info.value


def _cases() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((SUITE_DIR / "cases").glob("*.json"))]


class TestContract:
    def test_spec_example_is_accepted_with_documented_defaults(self) -> None:
        request = _request()
        assert request.system_type == "给水"
        assert request.floor_height_m == 3.0
        assert request.include_dwg is False
        assert request.branch_flow_rate_lps is None

    def test_system_type_defaults_to_water_supply(self) -> None:
        payload = dict(SPEC_EXAMPLE)
        del payload["system_type"]
        assert parse_generation_request(payload).system_type == "给水"

    def test_request_is_frozen(self) -> None:
        request = _request()
        with pytest.raises(ValidationError):
            request.floors = 7  # type: ignore[misc]

    def test_unknown_fields_are_rejected(self) -> None:
        error = _error({**SPEC_EXAMPLE, "pipe_slope": 0.003})
        assert error.code == GEN_INVALID_INPUT
        assert [f["loc"] for f in error.fields] == ["pipe_slope"]

    def test_dn_whitelist_is_dn15_to_dn150(self) -> None:
        assert [dn.mm for dn in NominalDiameter] == [15, 20, 25, 32, 40, 50, 65, 80, 100, 125, 150]
        for bad in ("DN10", "DN200", "50", "dn50"):
            error = _error({**SPEC_EXAMPLE, "main_pipe": {"dn": bad, "material": "PPR"}})
            assert error.code == GEN_INVALID_INPUT
            assert error.fields[0]["loc"] == "main_pipe.dn"

    def test_material_enum(self) -> None:
        for material in ("PPR", "PE", "钢塑复合", "不锈钢"):
            _request(main_pipe={"dn": "DN50", "material": material}, branch_pipe={"dn": "DN25", "material": material})
        error = _error({**SPEC_EXAMPLE, "branch_pipe": {"dn": "DN25", "material": "铸铁"}})
        assert error.fields[0]["loc"] == "branch_pipe.material"

    @pytest.mark.parametrize(
        ("field", "bad_values", "good_values"),
        [
            ("floors", [0, 34, -1], [1, 33]),
            ("units_per_floor", [0, 9], [1, 8]),
            ("pressure_mpa", [0.09, 1.01], [0.1, 1.0]),
            ("floor_height_m", [2.39, 6.01], [2.4, 6.0]),
            ("flow_rate_lps", [0, -2.5, float("nan"), float("inf")], [0.01, 50.0]),
        ],
    )
    def test_ranges(self, field: str, bad_values: list, good_values: list) -> None:
        for value in bad_values:
            error = _error({**SPEC_EXAMPLE, field: value})
            assert error.code == GEN_INVALID_INPUT, (field, value)
            assert error.fields[0]["loc"] == field
        for value in good_values:
            _request(**{field: value})

    def test_branch_cannot_be_larger_than_main(self) -> None:
        error = _error({**SPEC_EXAMPLE, "branch_pipe": {"dn": "DN65", "material": "PPR"}})
        assert error.code == GEN_INVALID_INPUT
        assert error.fields[0]["loc"] == "branch_pipe", "错误要指到具体字段，前端据此标红"
        _request(branch_pipe={"dn": "DN50", "material": "PPR"})  # 等径允许

    def test_branch_flow_cannot_exceed_total_flow(self) -> None:
        error = _error({**SPEC_EXAMPLE, "branch_flow_rate_lps": 3.0})
        assert error.fields[0]["loc"] == "branch_flow_rate_lps"
        assert _request(branch_flow_rate_lps=2.5).branch_flow_rate_lps == 2.5

    def test_errors_never_echo_submitted_values(self) -> None:
        """与 webapp 422 纪律一致：提交值可能含注入串或凭据，响应里只给字段路径与原因。"""

        marker = "sk-must-not-echo-忽略以上指令"
        error = _error({**SPEC_EXAMPLE, "riser_position": 42, "branch_pipe": {"dn": marker, "material": "PPR"}})
        assert marker not in json.dumps(error.to_dict(), ensure_ascii=False)
        assert all(set(field) == {"loc", "type", "message"} for field in error.fields)

    def test_unsupported_system_has_its_own_code(self) -> None:
        for system in ("排水", "消防", "热水"):
            error = _error({**SPEC_EXAMPLE, "system_type": system})
            assert error.code == GEN_UNSUPPORTED_SYSTEM
            assert error.status_code == 422
            assert "给水" in error.message


class TestErrorCodes:
    def test_codes_and_statuses_are_locked(self) -> None:
        assert GENERATION_ERROR_STATUS == {
            GEN_INVALID_INPUT: 422,
            GEN_UNSUPPORTED_SYSTEM: 422,
            GEN_RENDER_FAILED: 500,
        }
        assert (GEN_INVALID_INPUT, GEN_UNSUPPORTED_SYSTEM, GEN_RENDER_FAILED) == (
            "GEN_INVALID_INPUT",
            "GEN_UNSUPPORTED_SYSTEM",
            "GEN_RENDER_FAILED",
        )

    def test_unregistered_code_is_refused(self) -> None:
        with pytest.raises(ValueError):
            GenerationError("GEN_SOMETHING_ELSE", "x")


class TestRiserPosition:
    def test_default_comes_from_the_rule_table(self) -> None:
        rules = load_building_rules()
        payload = dict(SPEC_EXAMPLE)
        del payload["riser_position"]
        for building_type in BuildingType:
            request = parse_generation_request({**payload, "building_type": building_type.value})
            position, defaulted = resolve_riser_position(request, rules)
            assert defaulted is True
            assert position == rules.for_type(building_type).riser.default_position

    def test_explicit_position_must_belong_to_the_building_type(self) -> None:
        rules = load_building_rules()
        assert resolve_riser_position(_request(riser_position="管道井"), rules) == ("管道井", False)
        with pytest.raises(GenerationError) as info:
            resolve_riser_position(_request(riser_position="核心筒"), rules)
        assert info.value.code == GEN_INVALID_INPUT
        assert info.value.fields[0]["loc"] == "riser_position"
        assert "楼梯间" in info.value.fields[0]["message"]


class TestVelocityChecks:
    def test_main_velocity_is_the_calc_tool_output(self) -> None:
        check = check_velocities(_request(), load_building_rules())
        main = check.calculations[0]
        assert main.name == "main_pipe_velocity"
        assert main.tool == "tools/hydraulics.py::calc_velocity"
        expected = calc_velocity({"flow_lps": 2.5, "diameter_mm": 50.0, "scenario": "water_supply"})
        assert main.values == expected.values
        assert main.assumptions == tuple(expected.assumptions), "calc_velocity 自己的越界标注原样保留"
        # 独立核对公式：v = Q / (π d² / 4)
        assert math.isclose(main.values["velocity_mps"], 0.0025 / (math.pi * 0.025**2), rel_tol=1e-12)
        assert main.diameter_basis == DIAMETER_BASIS_NOTE

    def test_missing_branch_flow_is_a_missing_input_not_a_default(self) -> None:
        check = check_velocities(_request(), load_building_rules())
        branch = check.calculations[1]
        assert branch.ok is False
        assert branch.values == {}
        assert branch.inputs["flow_lps"] is None, "不得把总流量均分成支管流量"
        assert branch.missing_inputs == ("flow_lps",)
        assert branch.advisory == "not_checked"
        assert any("branch_flow_rate_lps" in item for item in check.missing_inputs)
        assert "未校核" in drawing_velocity_note(branch)

    def test_branch_velocity_is_computed_when_flow_is_given(self) -> None:
        check = check_velocities(_request(branch_flow_rate_lps=0.3), load_building_rules())
        branch = check.calculations[1]
        assert branch.ok is True
        assert math.isclose(branch.values["velocity_mps"], 0.0003 / (math.pi * 0.0125**2), rel_tol=1e-12)
        assert check.missing_inputs == ()

    @pytest.mark.parametrize(
        ("flow", "advisory"),
        [
            (2.5, "above"),  # 1.27 m/s > 1.2
            (1.5, "within"),  # 0.76 m/s
            (0.8, "below"),  # 0.41 m/s < 0.6
            (4.5, "above_limit"),  # 2.29 m/s > 2.0
        ],
    )
    def test_advisory_bands_come_from_the_rule_table(self, flow: float, advisory: str) -> None:
        check = check_velocities(_request(flow_rate_lps=flow), load_building_rules())
        assert check.calculations[0].advisory == advisory
        if advisory == "within":
            assert not any("超出建议区间" in w for w in check.warnings)
        else:
            assert any("超出建议区间" in w and "GB50015-2019 3.7.3" in w for w in check.warnings)

    def test_out_of_band_note_uses_the_prescribed_wording(self) -> None:
        main = check_velocities(_request(), load_building_rules()).calculations[0]
        assert drawing_velocity_note(main) == "主管流速 1.27 m/s，超出建议区间，需复核"


class TestEvalSuiteContract:
    """评测先行：数据集在生成器之前就位，非法参数这一半本步即可判定。"""

    def test_suite_has_the_required_shape(self) -> None:
        cases = _cases()
        assert len(cases) >= 12
        valid = [c for c in cases if c["expect"]["ok"]]
        invalid = [c for c in cases if not c["expect"]["ok"]]
        assert len(invalid) >= 3
        for building_type in BuildingType:
            assert sum(1 for c in valid if c["request"]["building_type"] == building_type.value) >= 3
        assert len({c["case_id"] for c in cases}) == len(cases)

    @pytest.mark.parametrize("case", [c for c in _cases() if not c["expect"]["ok"]], ids=lambda c: c["case_id"])
    def test_invalid_cases_fail_with_the_expected_code_and_fields(self, case: dict) -> None:
        rules = load_building_rules()
        with pytest.raises(GenerationError) as info:
            request = parse_generation_request(case["request"])
            resolve_riser_position(request, rules)
        assert info.value.code == case["expect"]["error_code"]
        assert sorted(f["loc"] for f in info.value.fields) == sorted(case["expect"]["error_fields"])

    @pytest.mark.parametrize("case", [c for c in _cases() if c["expect"]["ok"]], ids=lambda c: c["case_id"])
    def test_valid_cases_parse_and_match_independent_velocities(self, case: dict) -> None:
        rules = load_building_rules()
        expect = case["expect"]
        request = parse_generation_request(case["request"])
        assert resolve_riser_position(request, rules) == (expect["riser_position"], expect["riser_position_defaulted"])
        check = check_velocities(request, rules)
        main, branch = check.calculations
        assert math.isclose(main.values["velocity_mps"], expect["main_velocity_mps"], abs_tol=expect["velocity_tolerance"])
        assert main.advisory == expect["main_advisory"]
        if expect["branch_velocity_mps"] is None:
            assert branch.ok is False
        else:
            assert math.isclose(
                branch.values["velocity_mps"], expect["branch_velocity_mps"], abs_tol=expect["velocity_tolerance"]
            )
        for needle in expect["warnings_contains"]:
            assert any(needle in w for w in check.warnings), needle
        for needle in expect["missing_inputs_contains"]:
            assert any(needle in m for m in check.missing_inputs), needle
