"""calc harness 测试：mini 评测集跑通 + loader 路由 + 数值判分。"""
from __future__ import annotations

import json

from water_agent.domain.models import CalcCase
from water_agent.harness.loader import load_eval_bundle
from water_agent.harness.runner import HarnessRunner
from water_agent.harness.scorer import score_calc_case
from water_agent.tools.hydraulics import run_calc

MINI_CASES = [
    {
        "id": "mini-0001",
        "type": "calc",
        "variant": "programmatic",
        "query": "DN150、Q=20L/s，求流速",
        "formula": "velocity",
        "input": {"diameter_mm": 150, "flow_lps": 20},
        "gold": {"velocity_mps": 1.132},
        "tolerances": {"relative": 0.01, "absolute": 0.001},
    },
    {
        "id": "mini-0002",
        "type": "calc",
        "variant": "programmatic",
        "query": "求流速但缺流量",
        "formula": "velocity",
        "input": {"diameter_mm": 150},
        "gold": {},
        "tolerances": {"relative": 0.01, "absolute": 0.001},
        "expected_status": "missing_inputs",
    },
    {
        "id": "mini-0003",
        "type": "calc",
        "variant": "programmatic",
        "query": "充满度越界",
        "formula": "slope_manning",
        "input": {"flow_lps": 30, "diameter_mm": 300, "fill_ratio": 1.0, "manning_n": 0.013},
        "gold": {},
        "tolerances": {"relative": 0.01, "absolute": 0.001},
        "expected_status": "invalid_input",
    },
]


def _write_mini(path) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for case in MINI_CASES:
            handle.write(json.dumps(case, ensure_ascii=False) + "\n")


def test_loader_routes_calc_case(tmp_path) -> None:
    mini = tmp_path / "mini.jsonl"
    _write_mini(mini)
    bundle = load_eval_bundle(mini, strict=True)
    assert len(bundle.cases) == 3
    assert all(isinstance(case, CalcCase) for case in bundle.cases)
    assert bundle.cases[0].formula == "velocity"
    assert bundle.cases[0].gold == {"velocity_mps": 1.132}


def test_runner_programmatic_calc(tmp_path) -> None:
    mini = tmp_path / "mini.jsonl"
    _write_mini(mini)
    runner = HarnessRunner(eval_path=mini, report_dir=tmp_path / "reports", trace_dir=tmp_path / "traces")
    report, report_path, load_issues = runner.run()
    assert load_issues == []
    assert report.total == 3
    assert report.passed == 3
    assert report.failed == 0
    assert report_path.exists()


def test_score_calc_numeric_tolerance() -> None:
    case = CalcCase.model_validate(MINI_CASES[0])
    ok = score_calc_case(case, calc_result=run_calc(case.formula, case.input))
    assert ok.passed
    assert ok.actual_status == "succeeded"

    # 阈值内通过 / 阈值外失败：直接验证 _within_tolerance 行为
    from water_agent.domain.models import CalcTolerances
    from water_agent.harness.scorer import _within_tolerance

    tol = CalcTolerances(relative=0.01, absolute=0.001)
    assert _within_tolerance(1.132, 1.132, tol)
    assert _within_tolerance(1.1315, 1.132, tol)  # 相对误差 < 0.01
    assert not _within_tolerance(1.15, 1.132, tol)  # 误差 1.6% 超阈


def test_score_calc_missing_inputs() -> None:
    case = CalcCase.model_validate(MINI_CASES[1])
    result = score_calc_case(case, calc_result=run_calc(case.formula, case.input))
    assert result.passed
    assert result.actual_status == "missing_inputs"
    assert result.failure_reason == ""


def test_score_calc_invalid_input() -> None:
    case = CalcCase.model_validate(MINI_CASES[2])
    result = score_calc_case(case, calc_result=run_calc(case.formula, case.input))
    assert result.passed
    assert result.actual_status == "invalid_input"


def test_mismatched_status_fails() -> None:
    case = CalcCase.model_validate(MINI_CASES[1])  # 期望 missing_inputs
    calc_result = run_calc(case.formula, {"diameter_mm": 150, "flow_lps": 20})  # 实际 succeeded
    result = score_calc_case(case, calc_result=calc_result)
    assert not result.passed
    assert "expected status missing_inputs, got succeeded" in result.failure_reason
