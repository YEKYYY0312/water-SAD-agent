"""水力计算子图测试（Step 2）。

覆盖：意图识别路由 / 缺参→missing_inputs（要求澄清，禁止猜测）/ 越界→assumptions+候选方案 /
正常→succeeded / 非法输入→invalid_input / harness calc 端到端跑分。
"""
from __future__ import annotations

import json

from water_agent.domain.state import STATUS_SUCCEEDED
from water_agent.graphs.calc import build_calc_graph, detect_intent, parse_calc_query
from water_agent.harness.runner import HarnessRunner


def _invoke(query: str, *, formula: str | None = None, params: dict | None = None) -> dict:
    app = build_calc_graph()
    state: dict = {
        "query": query,
        "formula": formula,
        "params": params or {},
        "calc_result": None,
        "missing_inputs": [],
        "assumptions": [],
        "errors": [],
        "candidates": [],
        "answer": "",
        "status": "running",
        "intent": "calc",
        "step_count": 0,
    }
    return app.invoke(state)


class TestDetectIntent:
    """计算意图 vs RAG 意图的路由判定。"""

    def test_calc_velocity_utterance(self) -> None:
        assert detect_intent("DN150、Q=20L/s，管道流速是多少？") == "calc"

    def test_calc_slope_utterance(self) -> None:
        assert detect_intent("DN300 排水管道，Q=30L/s，h/d=0.5，求坡度") == "calc"

    def test_rag_question(self) -> None:
        assert detect_intent("给水管道的设计秒流量如何确定") == "rag"

    def test_plain_question(self) -> None:
        assert detect_intent("消防用水量应如何计算？") == "rag"

    def test_empty_query_falls_back_to_rag(self) -> None:
        assert detect_intent("") == "rag"


class TestParseCalcQuery:
    """话术 → 结构化参数解析（LLM 参数理解的离线确定性替身）。"""

    def test_parse_velocity_utterance(self) -> None:
        outcome = parse_calc_query("DN150、Q=20L/s，管道流速是多少？")
        assert outcome.formula == "velocity"
        assert outcome.params == {"diameter_mm": 150, "flow_lps": 20}
        assert outcome.missing_inputs == []

    def test_parse_missing_flow_reports_it(self) -> None:
        outcome = parse_calc_query("DN150 求流速")
        assert outcome.formula == "velocity"
        assert outcome.params == {"diameter_mm": 150}
        assert "flow_lps" in outcome.missing_inputs

    def test_parse_drainage_slope(self) -> None:
        outcome = parse_calc_query("DN300 排水管道，Q=30L/s，h/d=0.5，n=0.013，求坡度")
        assert outcome.formula == "slope_manning"
        assert outcome.params["diameter_mm"] == 300
        assert outcome.params["flow_lps"] == 30
        assert outcome.params["fill_ratio"] == 0.5
        assert outcome.params["manning_n"] == 0.013
        assert outcome.params["scenario"] == "drainage"


class TestCalcGraphNormal:
    """正常计算路径：公式显式指定、结果与 Step 1 一致、口径记录进回答。"""

    def test_velocity_injected(self) -> None:
        result = _invoke("求流速", formula="velocity", params={"diameter_mm": 150, "flow_lps": 20})
        assert result["status"] == STATUS_SUCCEEDED
        assert abs(result["calc_result"].values["velocity_mps"] - 1.132) < 0.001
        assert "velocity" in result["answer"], "回答必须明示公式口径"
        assert "1.132" in result["answer"], "回答必须包含计算结果"

    def test_velocity_from_utterance_parser(self) -> None:
        result = _invoke("DN150、Q=20L/s，管道流速是多少？")
        assert result["status"] == STATUS_SUCCEEDED
        assert result["params"]["diameter_mm"] == 150
        assert result["params"]["flow_lps"] == 20
        assert abs(result["calc_result"].values["velocity_mps"] - 1.132) < 0.001

    def test_slope_manning_injected(self) -> None:
        result = _invoke(
            "求坡度",
            formula="slope_manning",
            params={"flow_lps": 30, "diameter_mm": 300, "fill_ratio": 0.5, "manning_n": 0.013},
        )
        assert result["status"] == STATUS_SUCCEEDED
        assert abs(result["calc_result"].values["slope"] - 0.00385) < 0.0001


class TestCalcGraphMissingInputs:
    """缺参 → 结构化 missing_inputs，回答必须要求澄清而非猜测。"""

    def test_missing_param_injected(self) -> None:
        result = _invoke("求流速", formula="velocity", params={"diameter_mm": 150})
        assert result["status"] == "missing_inputs"
        assert "flow_lps" in result["missing_inputs"]
        assert "请补充" in result["answer"] or "缺少" in result["answer"]
        assert "flow_lps" in result["answer"]

    def test_missing_from_utterance(self) -> None:
        result = _invoke("求管道流速")
        assert result["status"] == "missing_inputs"
        assert result["missing_inputs"]

    def test_answer_must_not_guess_value(self) -> None:
        result = _invoke("求流速", formula="velocity", params={"diameter_mm": 150})
        assert result["status"] == "missing_inputs"
        # 缺失 flow_lps 时回答必须要求澄清，且明示"禁止猜测"而非编造一个数值
        assert "flow_lps" in result["answer"]
        assert "请补充" in result["answer"]
        assert "禁止" in result["answer"] and "假设" in result["answer"]


class TestCalcGraphOutOfRange:
    """越界 → 计算照常返回（succeeded）+ assumptions 标注 + 候选方案 + 回答明示。"""

    def test_velocity_above_upper(self) -> None:
        # DN50、Q=5L/s → v≈2.55 m/s，超过给水上限 1.5 m/s
        result = _invoke("求流速", formula="velocity", params={"diameter_mm": 50, "flow_lps": 5})
        assert result["status"] == STATUS_SUCCEEDED
        assert result["assumptions"], "越界必须标注 assumptions"
        assert any("高于" in a for a in result["assumptions"])
        assert result["candidates"], "越界必须生成候选方案"
        assert any("高于" in a for a in result["assumptions"]), "回答必须明示越界"

    def test_velocity_below_lower(self) -> None:
        # DN300、Q=10L/s → v≈0.14 m/s，低于给水下限 0.6 m/s
        result = _invoke("求流速", formula="velocity", params={"diameter_mm": 300, "flow_lps": 10})
        assert result["status"] == STATUS_SUCCEEDED
        assert any("低于" in a for a in result["assumptions"])
        assert result["candidates"]

    def test_in_range_no_assumptions(self) -> None:
        result = _invoke(
            "求流速",
            formula="velocity",
            params={"diameter_mm": 300, "flow_lps": 60, "scenario": "drainage"},
        )
        assert result["status"] == STATUS_SUCCEEDED
        assert result["assumptions"] == []


class TestCalcGraphInvalid:
    """非法输入 → invalid_input，错误文本可追溯。"""

    def test_negative_flow(self) -> None:
        result = _invoke("求流速", formula="velocity", params={"diameter_mm": 150, "flow_lps": -5})
        assert result["status"] == "invalid_input"
        assert result["errors"]

    def test_unknown_formula(self) -> None:
        result = _invoke("求流速", formula="shevelev_auto", params={})
        assert result["status"] == "invalid_input"
        assert result["errors"]

    def test_fill_ratio_out_of_range(self) -> None:
        result = _invoke(
            "求坡度",
            formula="slope_manning",
            params={"flow_lps": 30, "diameter_mm": 300, "fill_ratio": 1.0, "manning_n": 0.013},
        )
        assert result["status"] == "invalid_input"


class TestCalcGraphEndToEnd:
    """harness `type=="calc"` 端到端：计算子图跑真工具 + 程序化判分。"""

    def _write_mini(self, path) -> None:
        cases = [
            {
                "id": "g-0001",
                "type": "calc",
                "variant": "programmatic",
                "query": "DN150、Q=20L/s 求流速",
                "formula": "velocity",
                "input": {"diameter_mm": 150, "flow_lps": 20},
                "gold": {"velocity_mps": 1.132},
                "tolerances": {"relative": 0.01, "absolute": 0.001},
                "expected_status": "succeeded",
            },
            {
                "id": "g-0002",
                "type": "calc",
                "variant": "programmatic",
                "query": "求流速缺流量",
                "formula": "velocity",
                "input": {"diameter_mm": 150},
                "gold": {},
                "tolerances": {"relative": 0.01, "absolute": 0.001},
                "expected_status": "missing_inputs",
            },
        ]
        with path.open("w", encoding="utf-8") as handle:
            for case in cases:
                handle.write(json.dumps(case, ensure_ascii=False) + "\n")

    def test_runner_walks_calc_graph(self, tmp_path) -> None:
        mini = tmp_path / "mini.jsonl"
        self._write_mini(mini)
        runner = HarnessRunner(
            eval_path=mini,
            report_dir=tmp_path / "reports",
            trace_dir=tmp_path / "traces",
        )
        report, _, load_issues = runner.run()
        assert load_issues == []
        assert report.total == 2
        assert report.passed == 2
        assert report.failed == 0


class TestFormulaInference:
    """公式推断：看"求什么"而不是名词出现与否；识别不出公式必须收口 invalid_input。"""

    def test_diameter_word_does_not_hijack_velocity_question(self) -> None:
        outcome = parse_calc_query("管径DN150，流量Q=20L/s，求流速")
        assert outcome.formula == "velocity"
        assert outcome.missing_inputs == []

    def test_velocity_question_phrase_variants(self) -> None:
        for q in ("DN200 Q=30L/s 流速是多少", "DN200、Q=30L/s，管内流速？", "管径 DN200，流量 30L/s，计算流速"):
            assert parse_calc_query(q).formula == "velocity", q

    def test_diameter_target_with_flow_and_velocity(self) -> None:
        outcome = parse_calc_query("流量Q=20L/s，流速v=1.0m/s，求管径")
        assert outcome.formula == "diameter"
        assert outcome.missing_inputs == []

    def test_params_infer_diameter_when_velocity_given_and_diameter_absent(self) -> None:
        outcome = parse_calc_query("给水管流速v=1.0m/s、Q=20L/s，管径应取多少")
        assert outcome.formula == "diameter"

    def test_head_loss_wins_over_diameter_and_velocity_words(self) -> None:
        outcome = parse_calc_query("管径DN150，流速v=1.2m/s，L=100m，求沿程水头损失")
        assert outcome.formula == "head_loss_shevelev"

    def test_unrecognized_formula_is_invalid_input_not_success(self) -> None:
        result = _invoke("DN150 管道的摩阻系数取多少")
        assert result["status"] == "invalid_input"
        assert result["formula"] is None
        assert any("无法识别公式口径" in e for e in result["errors"])
        assert "无法识别" in result["answer"] or "输入非法" in result["answer"]
