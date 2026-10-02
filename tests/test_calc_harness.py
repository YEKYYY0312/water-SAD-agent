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
    assert bundle.line_nos == [1, 2, 3]


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
    # P1：line_no 全链路贯通（loader → runner → report）
    assert [result.line_no for result in report.cases] == [1, 2, 3]


def test_runner_isolates_case_crash(tmp_path, monkeypatch) -> None:
    """P1 单用例隔离：任一条崩溃不得丢失整份报告，失败用例记录堆栈。"""
    mini = tmp_path / "mini_crash.jsonl"
    _write_mini(mini)

    import water_agent.graphs.calc as calc_graph_module

    original_run_calc = calc_graph_module.run_calc

    def boom(formula: str, inputs: dict):
        if inputs.get("diameter_mm") == 150 and formula == "velocity":
            raise RuntimeError("模拟计算子图崩溃")
        return original_run_calc(formula, inputs)

    monkeypatch.setattr(calc_graph_module, "run_calc", boom)
    runner = HarnessRunner(eval_path=mini, report_dir=tmp_path / "reports", trace_dir=tmp_path / "traces")
    report, report_path, load_issues = runner.run()

    assert load_issues == []
    assert report.total == 3
    assert report.passed == 2
    assert report.failed == 1
    crashed = next(result for result in report.cases if not result.passed)
    assert crashed.case_id == "mini-0001"
    assert crashed.actual_status == "error"
    assert "模拟计算子图崩溃" in crashed.failure_reason
    assert crashed.exception_trace
    assert [result.line_no for result in report.cases] == [1, 2, 3]
    assert report_path.exists()
    assert (tmp_path / "traces" / "mini-0001_error.trace").exists()


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


def test_expected_assumptions_check() -> None:
    # DN50,Q=5 给水 会产生 '高于' 的 assumption
    payload = {
        "id": "mini-0004",
        "type": "calc",
        "variant": "programmatic",
        "query": "DN50,Q=5L/s 给水 高流速",
        "formula": "velocity",
        "input": {"diameter_mm": 50, "flow_lps": 5, "scenario": "water_supply"},
        "gold": {"velocity_mps": 2.546479},
        "tolerances": {"relative": 0.01, "absolute": 0.001},
        "expected_status": "succeeded",
        "expected_assumptions": ["高于"],
    }
    case = CalcCase.model_validate(payload)
    calc_result = run_calc(case.formula, case.input)
    result = score_calc_case(case, calc_result=calc_result)
    assert result.passed

    # if we expect '低于' it should fail
    payload['expected_assumptions'] = ["低于"]
    case2 = CalcCase.model_validate(payload)
    res2 = score_calc_case(case2, calc_result=calc_result)
    assert not res2.passed
    assert "expected assumption missing: 低于" in res2.failure_reason


# ── review 2026-09-30：calc harness 必须测"子图"本身，不能靠回退重算掩盖子图缺陷 ──


class _GraphReportingSuccessWithoutResult:
    """模拟编排缺陷：子图跳过 compute，却把状态报成 succeeded。"""

    def invoke(self, state):
        return {**state, "status": "succeeded", "calc_result": None, "answer": "按口径表公式 velocity 计算：。"}


def test_runner_fails_a_graph_that_claims_success_without_a_result(tmp_path, monkeypatch) -> None:
    """此前 runner 在子图无 calc_result 时回退直调 run_calc 判分：路由坏掉、子图报
    "成功"却没有任何数值，calc_p0 仍 28/28（review 变异实测）。"""
    from water_agent.harness import runner as runner_mod

    mini = tmp_path / "mini.jsonl"
    _write_mini(mini)
    monkeypatch.setattr(runner_mod, "build_calc_graph", lambda: _GraphReportingSuccessWithoutResult())
    runner = HarnessRunner(eval_path=mini, report_dir=tmp_path / "reports", trace_dir=tmp_path / "traces")
    report, _, _ = runner.run()
    by_id = {result.case_id: result for result in report.cases}
    assert by_id["mini-0001"].passed is False
    assert "without a valid calc_result" in by_id["mini-0001"].failure_reason
    # 缺参/非法用例期望的状态也不是 succeeded：子图报错状态同样要判负
    assert by_id["mini-0002"].passed is False
    assert by_id["mini-0003"].passed is False


def test_calc_graph_never_finishes_as_success_without_a_valid_result() -> None:
    """finish 节点兜底：走到收口却没有有效计算结果（如路由跳过了 compute）→ failed。"""
    from water_agent.graphs.calc import finish_calc_state

    out = finish_calc_state({"status": "running", "formula": "velocity", "calc_result": None})
    assert out["status"] == "failed"
    assert "计算未执行" in out["answer"]


def test_score_calc_compares_the_exact_missing_inputs() -> None:
    """缺参用例要比"缺的是哪个参数"：一律报成缺 diameter_mm 也曾 28/28。"""
    case = CalcCase.model_validate({**MINI_CASES[1], "expected_missing_inputs": ["flow_lps"]})
    right = score_calc_case(case, calc_result=None, graph_status="missing_inputs", graph_missing_inputs=["flow_lps"])
    assert right.passed, right.failure_reason
    wrong = score_calc_case(case, calc_result=None, graph_status="missing_inputs", graph_missing_inputs=["diameter_mm"])
    assert not wrong.passed
    assert "missing_inputs" in wrong.failure_reason and "flow_lps" in wrong.failure_reason


def test_calc_p0_missing_input_cases_declare_the_missing_parameter() -> None:
    """数据集自身：每条 missing_inputs 用例都声明期望缺哪个参数（否则上一条检查形同虚设）。"""
    from pathlib import Path

    bundle = load_eval_bundle(Path(__file__).resolve().parents[1] / "data" / "eval" / "calc_p0.jsonl")
    missing_cases = [case for case in bundle.cases if case.expected_status == "missing_inputs"]
    assert missing_cases
    assert all(case.expected_missing_inputs for case in missing_cases), [c.id for c in missing_cases]


def test_runner_also_checks_the_run_calc_dispatcher(tmp_path, monkeypatch) -> None:
    """子图在解析阶段就按自己的表短路缺参，不经过 run_calc 分发表；而多 Agent 水力
    Agent 与 MCP 水力服务直接调 run_calc。分发器把缺参一律报成 diameter_mm 时，
    calc_p0 也必须变红（review 变异 calc_wrong_missing_param_inplace）。"""
    import water_agent.tools.hydraulics as hydraulics

    for name, fn in list(hydraulics._CALC_FUNCTIONS.items()):
        def wrong(inputs, _fn=fn):
            result = _fn(inputs)
            if result.missing_inputs:
                result.missing_inputs = ["diameter_mm"]
            return result

        monkeypatch.setitem(hydraulics._CALC_FUNCTIONS, name, wrong)
    mini = tmp_path / "mini.jsonl"
    mini.write_text(
        json.dumps({**MINI_CASES[1], "expected_missing_inputs": ["flow_lps"]}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    runner = HarnessRunner(eval_path=mini, report_dir=tmp_path / "reports", trace_dir=tmp_path / "traces")
    report, _, _ = runner.run()
    assert report.cases[0].passed is False
    assert "run_calc:" in report.cases[0].failure_reason
