"""水力计算确定性子图（P1，Step 2）。

编排边界（skill-hybrid-planner / ADR-01）：
- 参数解析与结果解释是"编排"职责，可交给 LLM；数值计算永远走 Step 1 程序化工具。
- 所有公式由输入显式指定，禁止静默切换；缺参返回结构化 ``missing_inputs``（要求澄清，
  禁止让模型猜测）；越界计算照常返回，但用 ``assumptions`` 标注并生成候选方案，随结果
  一并在回答中明示。

节点：parse_params → compute → validate → finish。
- parse_params：LLM/规则把用户话术解析为结构化参数；缺参输出 missing_inputs 清单。
- compute：程序化调用 Step 1 ``run_calc``（LLM 不参与数值运算）。
- validate：按口径表做物理/范围校验，越界标注 assumptions 并生成候选方案。
- finish：确定性生成回答（明示公式口径 / 越界提醒 / 候选方案 / 待澄清参数）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from water_agent.domain.state import STATUS_SUCCEEDED
from water_agent.tools.hydraulics import CalcResult, run_calc

# 各公式必要参数清单（与口径表 §1–5 缺参策略对齐）。
_REQUIRED_PARAMS: dict[str, list[str]] = {
    "velocity": ["flow_lps", "diameter_mm"],
    "head_loss_darcy_weisbach": ["length_m", "diameter_mm", "velocity_mps", "friction_factor"],
    "head_loss_hazen_williams": ["length_m", "diameter_mm", "flow_lps", "hazen_williams_c"],
    "head_loss_shevelev": ["length_m", "diameter_mm", "velocity_mps"],
    "diameter": ["flow_lps", "velocity_mps"],
    "slope_manning": ["flow_lps", "diameter_mm", "fill_ratio", "manning_n"],
    "pump_head": ["static_head_m", "friction_head_m", "local_head_m", "residual_head_m"],
}

# 给水/排水流速约束（口径表 §1；用于越界候选方案，与 hydraulics._VELOCITY_CONSTRAINTS 同源）
_WATER_SUPPLY = (0.6, 1.5)

# 计算意图识别的信号：单位/管径模式 + 强计算名词
_CALC_PATTERNS = [
    r"DN\s*\d+",  # DN150
    r"\d+(?:\.\d+)?\s*L\s*/\s*s",  # 20L/s
    r"\d+(?:\.\d+)?\s*m³?/h",  # 立方米/小时
    r"\d+(?:\.\d+)?\s*m\s*/\s*s",  # m/s
    r"h\s*/\s*d\s*=",  # h/d= 充满度
]
_CALC_KEYWORDS = [
    "流速", "管径", "沿程", "水头损失", "坡度", "扬程", "舍维列夫",
    "海曾", "达西", "曼宁", "充满度", "摩阻", "粗糙系数",
    "Darcy", "Hazen", "Shevelev", "Manning", "WILLIAMS",
]


class CalcState(TypedDict):
    """计算子图内部状态（与 WaterAgentState 解耦，本步不占用共享状态）。"""

    query: str
    intent: str
    formula: str | None
    params: dict[str, Any]
    calc_result: CalcResult | None
    missing_inputs: list[str]
    assumptions: list[str]
    errors: list[str]
    candidates: list[str]
    answer: str
    status: str
    step_count: int


@dataclass(frozen=True, slots=True)
class ParseOutcome:
    """话术解析结果：公式 + 结构化参数 + 缺失参数清单。"""

    formula: str | None
    params: dict[str, Any]
    missing_inputs: list[str]


def _first_float(query: str, pattern: str) -> float | None:
    match = re.search(pattern, query, re.IGNORECASE)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None


def _infer_formula(query: str) -> str | None:
    if "管径" in query:
        return "diameter"
    if "坡度" in query or "曼宁" in query or "Manning" in query:
        return "slope_manning"
    if "扬程" in query:
        return "pump_head"
    if "海曾" in query or "Hazen" in query or "WILLIAMS" in query:
        return "head_loss_hazen_williams"
    if "达西" in query or "Darcy" in query:
        return "head_loss_darcy_weisbach"
    if "沿程" in query or "水头损失" in query or "舍维列夫" in query or "Shevelev" in query:
        return "head_loss_shevelev"
    if "流速" in query or "Flow" in query:
        return "velocity"
    return None


def parse_calc_query(query: str) -> ParseOutcome:
    """规则式话术解析器：离线确定性替身，真实 LLM 参数理解在 Step 7 接入。

    从话术中提取管径/流量/长度/流速/充满度/曼宁 n/摩阻 f/C 等参数并推断公式。
    ``scenario`` 按 排水/污水 → drainage、给水 → water_supply 推断（默认给水）。
    """
    q = query.strip()
    params: dict[str, Any] = {}

    diameter = _first_float(q, r"DN\s*(\d+(?:\.\d+)?)")
    if diameter is not None:
        params["diameter_mm"] = diameter
    flow = _first_float(q, r"Q\s*=\s*(\d+(?:\.\d+)?)\s*L\s*/\s*s")
    if flow is None:
        flow = _first_float(q, r"流量\s*(\d+(?:\.\d+)?)\s*L\s*/\s*s")
    if flow is not None:
        params["flow_lps"] = flow
    length = _first_float(q, r"L\s*=\s*(\d+(?:\.\d+)?)\s*m\b") or _first_float(q, r"管长\s*(\d+(?:\.\d+)?)\s*m")
    if length is not None:
        params["length_m"] = length
    velocity = _first_float(q, r"v\s*=\s*(\d+(?:\.\d+)?)\s*m\s*/\s*s") or _first_float(q, r"流速\s*(\d+(?:\.\d+)?)\s*m\s*/\s*s")
    if velocity is not None:
        params["velocity_mps"] = velocity
    fill = _first_float(q, r"h\s*/\s*d\s*=\s*(\d+(?:\.\d+)?)")
    if fill is not None:
        params["fill_ratio"] = fill
    manning = _first_float(q, r"n\s*=\s*(\d+(?:\.\d+)?)")
    if manning is not None:
        params["manning_n"] = manning
    friction = _first_float(q, r"f\s*=\s*(\d+(?:\.\d+)?)")
    if friction is not None:
        params["friction_factor"] = friction
    hazen_c = _first_float(q, r"C\s*=\s*(\d+(?:\.\d+)?)")
    if hazen_c is not None:
        params["hazen_williams_c"] = hazen_c
    static = _first_float(q, r"几何扬程\s*(\d+(?:\.\d+)?)\s*米?")
    if static is not None:
        params["static_head_m"] = static
    friction_head = _first_float(q, r"沿程\s*(\d+(?:\.\d+)?)\s*米?")
    if friction_head is not None:
        params["friction_head_m"] = friction_head
    local = _first_float(q, r"局部\s*(\d+(?:\.\d+)?)\s*米?")
    if local is not None:
        params["local_head_m"] = local
    residual = _first_float(q, r"剩余\s*(\d+(?:\.\d+)?)\s*米?")
    if residual is not None:
        params["residual_head_m"] = residual

    if "排水" in q or "污水" in q:
        params.setdefault("scenario", "drainage")
    elif "给水" in q:
        params.setdefault("scenario", "water_supply")

    formula = _infer_formula(q)
    missing: list[str] = []
    if formula in _REQUIRED_PARAMS:
        missing = [
            key for key in _REQUIRED_PARAMS[formula] if params.get(key) is None or params.get(key) == ""
        ]
    return ParseOutcome(formula=formula, params=params, missing_inputs=missing)


def detect_intent(query: str) -> str:
    """计算意图 vs RAG 意图的确定性路由。命中单位/管径模式或强计算名词 → calc，否则 rag。"""
    q = query.strip()
    if not q:
        return "rag"
    if any(re.search(pattern, q, re.IGNORECASE) for pattern in _CALC_PATTERNS):
        return "calc"
    lowered = q.lower()
    if any(keyword.lower() in lowered for keyword in _CALC_KEYWORDS):
        return "calc"
    return "rag"


def _velocity_candidates(scenario: str, velocity_mps: float) -> list[str]:
    """给水/排水流速越界时的候选方案（口径表 §1 越界策略）。"""
    if scenario == "drainage":
        if velocity_mps < 0.6:
            return ["减小管径或增大流量，使排水管道最小设计流速 ≥0.6 m/s（GB 50014）"]
        if velocity_mps > 5.0:
            return ["增大管径或减小流量，降低流速；非金属管不宜 >5 m/s（金属管可放宽至 10 m/s，GB 50014）"]
        return []
    min_v, max_v = _WATER_SUPPLY
    if velocity_mps < min_v:
        return ["减小管径或增大流量，使给水管常用流速回到 0.6–1.5 m/s"]
    if velocity_mps > max_v:
        return ["增大管径或减小流量，使给水管常用流速回到 0.6–1.5 m/s"]
    return []


def _build_candidates(formula: str, params: dict[str, Any], values: dict[str, float]) -> list[str]:
    """按口径表生成越界候选方案。"""
    if formula == "velocity":
        scenario = str(params.get("scenario") or "water_supply")
        velocity = values.get("velocity_mps")
        if velocity is None:
            return []
        return _velocity_candidates(scenario, velocity)
    return []


def build_calc_graph(parser=parse_calc_query):
    """构建水力计算确定性子图。

    ``parser``：话术 → 结构化参数的函数（默认规则解析器；Step 7 可替换为 LLM 解析），
    签名 ``(query: str) -> ParseOutcome``。
    """
    graph = StateGraph(CalcState)

    def parse_params_node(state: CalcState) -> dict:
        params = dict(state.get("params") or {})
        formula = state.get("formula")

        if not params and not formula:
            outcome = parser(state.get("query", ""))
            formula = outcome.formula
            params = dict(outcome.params)

        if formula in _REQUIRED_PARAMS:
            missing = [
                key
                for key in _REQUIRED_PARAMS[formula]
                if params.get(key) is None or params.get(key) == ""
            ]
        else:
            missing = []

        status = "missing_inputs" if missing else "running"
        return {
            "formula": formula,
            "params": params,
            "missing_inputs": missing,
            "status": status,
            "step_count": state.get("step_count", 0) + 1,
        }

    def route_after_parse(state: CalcState) -> str:
        if state.get("missing_inputs"):
            return "finish"
        if not state.get("formula"):
            # 无法确定公式（口径外），直接收口为非法输入
            return "finish"
        return "compute"

    def compute_node(state: CalcState) -> dict:
        formula = state.get("formula") or ""
        calc_result = run_calc(formula, state.get("params") or {})
        return {"calc_result": calc_result, "step_count": state.get("step_count", 0) + 1}

    def validate_node(state: CalcState) -> dict:
        calc_result = state.get("calc_result")
        formula = state.get("formula") or ""
        if calc_result is None:
            return {"status": "failed", "errors": state.get("errors", []) + ["计算未执行"]}
        if not calc_result.ok:
            if calc_result.missing_inputs:
                return {
                    "status": "missing_inputs",
                    "missing_inputs": calc_result.missing_inputs,
                    "errors": state.get("errors", []) + list(calc_result.errors),
                }
            return {
                "status": "invalid_input",
                "errors": state.get("errors", []) + list(calc_result.errors),
            }
        candidates = _build_candidates(formula, state.get("params") or {}, calc_result.values)
        return {
            "status": STATUS_SUCCEEDED,
            "assumptions": list(calc_result.assumptions),
            "errors": state.get("errors", []),
            "candidates": candidates,
        }

    def finish_node(state: CalcState) -> dict:
        status = state.get("status", STATUS_SUCCEEDED)
        formula = state.get("formula") or ""
        if status == "missing_inputs":
            keys = "、".join(sorted(set(state.get("missing_inputs", []))))
            answer = (
                f"缺少必要参数：{keys}。请补充后再计算。"
                f"（公式口径：{formula or '未指定'}；禁止私自假设缺失参数）"
            )
            return {"answer": answer, "status": "missing_inputs"}
        if status == "invalid_input":
            errors = "；".join(state.get("errors", [])) or "输入非法"
            return {"answer": f"输入非法：{errors}", "status": "invalid_input"}
        if status == "failed":
            errors = "；".join(state.get("errors", [])) or "计算失败"
            return {"answer": f"计算失败：{errors}", "status": "failed"}

        calc_result = state.get("calc_result")
        values = ""
        if calc_result is not None:
            values = "；".join(f"{key}={value:.4g}" for key, value in calc_result.values.items())
        answer = f"按口径表公式 {formula} 计算：{values}。"
        if calc_result is not None and calc_result.assumptions:
            answer += "注意：" + "；".join(calc_result.assumptions) + "。"
        if state.get("candidates"):
            answer += "候选方案：" + "；".join(state["candidates"])
        return {"answer": answer, "status": STATUS_SUCCEEDED}

    graph.add_node("parse", parse_params_node)
    graph.add_node("compute", compute_node)
    graph.add_node("validate", validate_node)
    graph.add_node("finish", finish_node)

    graph.add_edge(START, "parse")
    graph.add_conditional_edges("parse", route_after_parse, {"compute": "compute", "finish": "finish"})
    graph.add_edge("compute", "validate")
    graph.add_edge("validate", "finish")
    graph.add_edge("finish", END)

    return graph.compile()