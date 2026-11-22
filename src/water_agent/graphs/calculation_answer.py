"""Conservative projection of one locally executed full-pipe velocity result.

No LLM prose is accepted as numeric/normative evidence. Unknown query grammar
returns None and leaves the ordinary citation gate in charge.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Sequence
from decimal import Decimal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.messages.tool import ToolCall

from water_agent.tools.calc_diameter import DIAMETER_FORMULA, verified_diameter_result
from water_agent.tools.calc_hazen import verified_hazen_result
from water_agent.tools.calc_head_loss import DARCY_FORMULA, DarcyArgs
from water_agent.tools.calc_manning import verified_manning_result
from water_agent.tools.calc_pump_head import verified_pump_head_result
from water_agent.tools.calc_shevelev import SHEVELEV_HIGH, SHEVELEV_LOW, verified_shevelev_result
from water_agent.tools.calc_velocity import VelocityArgs
from water_agent.tools.darcy_request import (
    darcy_args_match,
    darcy_default_values,
    pure_darcy_request,
)
from water_agent.tools.diameter_request import diameter_args_match, pure_diameter_request
from water_agent.tools.ds_adapters import PROGRAMMATIC_CALC_ARTIFACT
from water_agent.tools.hazen_request import hazen_args_match, pure_hazen_request
from water_agent.tools.manning_request import pure_manning_request
from water_agent.tools.pump_head_request import pure_pump_head_request
from water_agent.tools.shevelev_request import pure_shevelev_request

WARNING_PROGRAMMATIC_CALCULATION = "no_citation_programmatic_calculation"
_NUMBER = r"([0-9]+(?:\.[0-9]+)?)"
_DIAMETER = re.compile(
    r"(?:管内径|内径)\s*(?:[=:：]|为)?\s*" + _NUMBER + r"\s*(?:mm|毫米)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_FLOW = re.compile(
    r"(?:流量|Q)\s*(?:[=:：]|为)?\s*" + _NUMBER + r"\s*(?:L/s|升/秒)(?![A-Za-z0-9])", re.IGNORECASE
)
_DN = re.compile(r"\bDN\s*" + _NUMBER + r"\b", re.IGNORECASE)
_PHRASES = tuple(
    sorted(
        (
            "求满管流速",
            "求平均流速",
            "求流速",
            "计算满管流速",
            "计算流速",
            "满管计算",
            "按满管",
            "满管",
            "圆管",
            "此次验收",
            "明确",
            "按",
            "请调用程序化计算工具",
            "请调用计算工具",
            "并注明这个前提",
            "仅计算流速",
            "不检索条文",
            "不做规范判定",
            "请",
            "计算",
            "求",
            "的",
            "为",
            "是多少",
            "流速",
        ),
        key=len,
        reverse=True,
    )
)


def _pure_remainder(text: str) -> bool:
    # Deterministic longest-token consumption: overlapping phrases in a repeated
    # regex admit exponential backtracking on a short hostile suffix.
    position = 0
    while position < len(text):
        if text[position].isspace() or text[position] in "，,。、：:；;？?！!（）()":
            position += 1
            continue
        token = next((phrase for phrase in _PHRASES if text.startswith(phrase, position)), None)
        if token is None:
            return False
        position += len(token)
    return True


def _query_inputs(query: str) -> tuple[Decimal, Decimal] | None:
    query = unicodedata.normalize("NFKC", query)
    if "满管" not in query or "流速" not in query:
        return None
    diameters, flows = _DIAMETER.findall(query), _FLOW.findall(query)
    if len(diameters) != 1 or len(flows) != 1:
        return None
    diameter, flow = Decimal(diameters[0]), Decimal(flows[0])
    if diameter <= 0 or flow <= 0 or any(Decimal(n) != diameter for n in _DN.findall(query)):
        return None
    remainder = _DN.sub("", _FLOW.sub("", _DIAMETER.sub("", query)))
    return (diameter, flow) if _pure_remainder(remainder) else None


def render_programmatic_velocity(messages: Sequence[BaseMessage]) -> str | None:
    """Return a trusted fixed answer only for a complete, current calculation turn."""
    human_index = next(
        (i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage)), None
    )
    if human_index is None or not isinstance(messages[human_index].content, str):
        return None
    query = messages[human_index].content
    inputs = _query_inputs(query) if isinstance(query, str) else None
    if inputs is None:
        return None
    turn = list(messages[human_index + 1 :])
    calls: list[tuple[int, ToolCall]] = [
        (i, call)
        for i, message in enumerate(turn)
        if isinstance(message, AIMessage)
        for call in message.tool_calls
    ]
    observations = [
        (i, message) for i, message in enumerate(turn) if isinstance(message, ToolMessage)
    ]
    if len(calls) != 1 or len(observations) != 1 or not turn:
        return None
    call_index, call = calls[0]
    result_index, observation = observations[0]
    final = turn[-1]
    if (
        call.get("name") != "calc_velocity"
        or not call.get("id")
        or observation.name != "calc_velocity"
        or observation.tool_call_id != call["id"]
        or observation.status != "success"
        or not call_index < result_index < len(turn) - 1
        or not isinstance(final, AIMessage)
        or final.tool_calls
        or final.invalid_tool_calls
        or not final.id
        or not isinstance(final.content, str)
        or not final.content.strip()
    ):
        return None
    artifact = observation.artifact
    if not isinstance(artifact, dict) or set(artifact) != {PROGRAMMATIC_CALC_ARTIFACT}:
        return None
    receipt = artifact[PROGRAMMATIC_CALC_ARTIFACT]
    if not isinstance(receipt, dict) or set(receipt) != {"args", "result"}:
        return None
    try:
        args = call["args"]
        if any(type(args.get(key)) not in (int, float) for key in ("diameter_mm", "flow_lps")):
            return None
        validated = VelocityArgs.model_validate(args)
        if validated.material is not None or validated.model_dump() != receipt["args"]:
            return None
        payload = json.loads(observation.content) if isinstance(observation.content, str) else None
        if not isinstance(payload, dict) or set(payload) != {"velocity_mps", "assumptions"}:
            return None
        velocity = payload["velocity_mps"]
        if (
            payload != receipt["result"]
            or payload["assumptions"] != []
            or type(velocity) not in (int, float)
            or not math.isfinite(velocity)
            or velocity <= 0
            or (Decimal(str(validated.diameter_mm)), Decimal(str(validated.flow_lps))) != inputs
        ):
            return None
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    return (
        "## 满管流速计算结果\n\n"
        f"- 管内径：{str(validated.diameter_mm).removesuffix('.0')} mm\n"
        f"- 流量：{str(validated.flow_lps).removesuffix('.0')} L/s\n"
        f"- 流速：{str(velocity).removesuffix('.0')} m/s\n\n"
        "计算前提：采用用户明确给出的内径，按满管圆管断面计算，公式 v = Q / A。\n\n"
        "数值来自 calc_velocity 程序化计算；此摘要由程序生成。"
        "本次仅计算流速，未进行规范符合性判断，也未判定具体用途的适用流速区间。"
    )


def render_programmatic_darcy(messages: Sequence[BaseMessage]) -> str | None:
    """Only one current canonical successful execution may replace model prose."""
    human_index = next((i for i in range(len(messages) - 1, -1, -1)
                        if isinstance(messages[i], HumanMessage)), None)
    if human_index is None:
        return None
    query = messages[human_index].content
    if not isinstance(query, str) or not pure_darcy_request(query):
        return None
    turn = list(messages[human_index + 1:])
    calls = [(i, call) for i, m in enumerate(turn) if isinstance(m, AIMessage) for call in m.tool_calls]
    observations = [(i, m) for i, m in enumerate(turn) if isinstance(m, ToolMessage)]
    if (len(calls) != 1 or len(observations) != 1 or not turn
            or any(isinstance(m, AIMessage) and m.invalid_tool_calls for m in turn)):
        return None
    ci, call = calls[0]
    oi, obs = observations[0]
    final = turn[-1]
    name = "calc_head_loss_darcy_weisbach"
    if (call.get("name") != name or not call.get("id") or obs.name != name
            or obs.tool_call_id != call["id"] or obs.status != "success"
            or not ci < oi < len(turn) - 1
            or not isinstance(final, AIMessage) or final.tool_calls or not final.id
            or not isinstance(final.content, str) or not final.content.strip()):
        return None
    artifact = obs.artifact
    if not isinstance(artifact, dict) or set(artifact) != {PROGRAMMATIC_CALC_ARTIFACT}:
        return None
    receipt = artifact[PROGRAMMATIC_CALC_ARTIFACT]
    if (not isinstance(receipt, dict)
            or set(receipt) != {"tool_name", "tool_call_id", "args", "result"}
            or receipt["tool_name"] != name or receipt["tool_call_id"] != call["id"]):
        return None
    try:
        args = DarcyArgs.model_validate(call["args"])
        if args.model_dump() != receipt["args"] or not darcy_args_match(query, args.model_dump())[0]:
            return None
        payload = json.loads(obs.content) if isinstance(obs.content, str) else None
        if (not isinstance(payload, dict) or payload != receipt["result"]
                or set(payload) != {"head_loss_m", "formula", "inputs", "units", "defaults_used",
                                    "assumptions", "coefficient_note"}
                or payload["inputs"] != args.model_dump() or payload["formula"] != DARCY_FORMULA
                or payload["assumptions"] != []):
            return None
        value = payload["head_loss_m"]
        defaults = darcy_default_values(query)
        if (type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                or payload["defaults_used"] != defaults):
            return None
    except (ValueError, TypeError, KeyError, AttributeError):
        return None

    def number(value: float) -> str:
        return str(value).removesuffix(".0")

    gravity_note = "采用工具显式默认值" if defaults else "用户明确给定，未采用默认值"
    return (
        "## Darcy–Weisbach 沿程水头损失计算结果\n\n"
        f"- 管长 L：{number(args.length_m)} m\n"
        f"- 实际内径 d：{number(args.diameter_mm)} mm\n"
        f"- 流速 v：{number(args.velocity_mps)} m/s\n"
        f"- Darcy 摩阻系数 f：{number(args.friction_factor)}（无量纲）\n"
        f"- 重力加速度 g：{number(args.gravity_mps2)} m/s²（{gravity_note}）\n\n"
        f"公式：{DARCY_FORMULA}；d 从 mm 换算为 m 后代入。\n\n"
        f"沿程水头损失 hf：{number(value)} m。\n\n"
        "计算前提：满管有压圆管，实际内径来自用户明确输入，未由 DN 推定。"
        "Darcy 摩阻系数 f 由用户给定，未核验其工程来源，未自动推导系数。"
        "数值来自 calc_head_loss_darcy_weisbach 程序化工具，此摘要由程序生成；"
        "未进行规范符合性判断。"
    )


def render_programmatic_diameter(messages: Sequence[BaseMessage]) -> str | None:
    """Current, single canonical diameter execution, projected as fixed prose."""
    human_index = next((i for i in range(len(messages) - 1, -1, -1)
                        if isinstance(messages[i], HumanMessage)), None)
    if human_index is None:
        return None
    query = messages[human_index].content
    if not isinstance(query, str) or not pure_diameter_request(query):
        return None
    turn = list(messages[human_index + 1:])
    calls = [(i, c) for i, m in enumerate(turn) if isinstance(m, AIMessage) for c in m.tool_calls]
    observations = [(i, m) for i, m in enumerate(turn) if isinstance(m, ToolMessage)]
    if (not turn or len(calls) != 1 or len(observations) != 1
            or any(isinstance(m, AIMessage) and m.invalid_tool_calls for m in turn)):
        return None
    ci, call = calls[0]
    oi, obs = observations[0]
    final = turn[-1]
    if (call.get("name") != "calc_diameter" or obs.name != "calc_diameter"
            or not call.get("id") or obs.tool_call_id != call["id"] or obs.status != "success"
            or not ci < oi < len(turn) - 1 or not isinstance(final, AIMessage)
            or final.tool_calls or not final.id or not isinstance(final.content, str)
            or not final.content.strip()):
        return None
    if not isinstance(obs.artifact, dict) or set(obs.artifact) != {PROGRAMMATIC_CALC_ARTIFACT}:
        return None
    try:
        payload = json.loads(obs.content) if isinstance(obs.content, str) else None
        if (not isinstance(payload, dict) or not diameter_args_match(query, call["args"])
                or not verified_diameter_result(call["args"], payload,
                                               obs.artifact[PROGRAMMATIC_CALC_ARTIFACT], call["id"])):
            return None
    except (ValueError, TypeError, KeyError):
        return None
    inputs = payload["inputs"]
    return (
        "## 圆管实际内径计算结果\n\n"
        f"- 流量 Q：{str(inputs['flow_lps']).removesuffix('.0')} L/s\n"
        f"- 目标流速 v：{str(inputs['velocity_mps']).removesuffix('.0')} m/s\n\n"
        f"公式：{DIAMETER_FORMULA}；Q 从 L/s 换算为 m³/s，d 从 m 换算为 mm。\n\n"
        f"实际计算内径 d：{payload['diameter_mm']} mm。\n\n"
        "计算前提：满管圆管，流量与目标流速均由用户明确给定。"
        "不自动圆整DN、不选择管材，不根据用途推定目标流速；未进行规范符合性判断。"
        "数值来自 calc_diameter 程序化工具，此摘要由程序生成。"
    )


def render_programmatic_hazen(messages: Sequence[BaseMessage]) -> str | None:
    """Current, single canonical diameter execution, projected as fixed prose."""
    human_index = next((i for i in range(len(messages) - 1, -1, -1)
                        if isinstance(messages[i], HumanMessage)), None)
    if human_index is None:
        return None
    query = messages[human_index].content
    if not isinstance(query, str) or not pure_hazen_request(query):
        return None
    turn = list(messages[human_index + 1:])
    calls = [(i, c) for i, m in enumerate(turn) if isinstance(m, AIMessage) for c in m.tool_calls]
    observations = [(i, m) for i, m in enumerate(turn) if isinstance(m, ToolMessage)]
    if (not turn or len(calls) != 1 or len(observations) != 1
            or any(isinstance(m, AIMessage) and m.invalid_tool_calls for m in turn)):
        return None
    ci, call = calls[0]
    oi, obs = observations[0]
    final = turn[-1]
    if (call.get("name") != "calc_head_loss_hazen_williams" or obs.name != "calc_head_loss_hazen_williams"
            or not call.get("id") or obs.tool_call_id != call["id"] or obs.status != "success"
            or not ci < oi < len(turn) - 1 or not isinstance(final, AIMessage)
            or final.tool_calls or not final.id or not isinstance(final.content, str)
            or not final.content.strip()):
        return None
    if not isinstance(obs.artifact, dict) or set(obs.artifact) != {PROGRAMMATIC_CALC_ARTIFACT}:
        return None
    try:
        payload = json.loads(obs.content) if isinstance(obs.content, str) else None
        if (not isinstance(payload, dict) or not hazen_args_match(query, call["args"])
                or not verified_hazen_result(call["args"], payload,
                                               obs.artifact[PROGRAMMATIC_CALC_ARTIFACT], call["id"], query)):
            return None
    except (ValueError, TypeError, KeyError):
        return None
    inputs = payload["inputs"]
    return (
        "## Hazen–Williams 沿程水头损失计算结果\n\n"
        f"- 管长 L：{inputs['length_m']} m\n"
        f"- 实际内径 d：{inputs['diameter_mm']} mm\n"
        f"- 流量 Q：{inputs['flow_lps']} L/s\n"
        f"- Hazen–Williams系数 C：{inputs['hazen_williams_c']}（无量纲，由用户给定）\n\n"
        f"公式：{payload['formula']}；Q从L/s换算为m³/s，d从mm换算为m。\n\n"
        f"沿程水头损失 hf：{payload['head_loss_m']} m。\n\n"
        f"{payload['calculation_note']}\n\n"
        "数值来自calc_head_loss_hazen_williams程序化工具，此摘要由程序生成；未进行规范符合性判断。"
    )


def render_programmatic_shevelev(messages: Sequence[BaseMessage]) -> str | None:
    """Single current Shevelev execution, retaining its expected applicability warnings."""
    human_index = next((i for i in range(len(messages) - 1, -1, -1)
                        if isinstance(messages[i], HumanMessage)), None)
    if human_index is None:
        return None
    query = messages[human_index].content
    if not isinstance(query, str) or not pure_shevelev_request(query):
        return None
    turn = list(messages[human_index + 1:])
    calls = [(i, c) for i, m in enumerate(turn) if isinstance(m, AIMessage) for c in m.tool_calls]
    observations = [(i, m) for i, m in enumerate(turn) if isinstance(m, ToolMessage)]
    if (not turn or len(calls) != 1 or len(observations) != 1
            or any(isinstance(m, AIMessage) and m.invalid_tool_calls for m in turn)):
        return None
    ci, call = calls[0]
    oi, obs = observations[0]
    final = turn[-1]
    if (call.get("name") != "calc_head_loss_shevelev" or obs.name != "calc_head_loss_shevelev"
            or not call.get("id") or obs.tool_call_id != call["id"] or obs.status != "success"
            or not ci < oi < len(turn) - 1 or not isinstance(final, AIMessage)
            or final.tool_calls or not final.id or not isinstance(final.content, str)
            or not final.content.strip()):
        return None
    if not isinstance(obs.artifact, dict) or set(obs.artifact) != {PROGRAMMATIC_CALC_ARTIFACT}:
        return None
    try:
        payload = json.loads(obs.content) if isinstance(obs.content, str) else None
        if (not isinstance(payload, dict) or not verified_shevelev_result(call["args"], payload,
                                               obs.artifact[PROGRAMMATIC_CALC_ARTIFACT], call["id"], query)):
            return None
    except (ValueError, TypeError, KeyError):
        return None
    inputs = payload["inputs"]
    return (
        "## 舍维列夫（Shevelev）沿程水头损失计算结果\n\n"
        f"- 管长 L：{inputs['length_m']} m\n"
        f"- 实际内径 d：{inputs['diameter_mm']} mm\n"
        f"- 流速 v：{inputs['velocity_mps']} m/s\n\n"
        f"程序所选分支：{payload['branch']}（按原始输入比较，分支判定前不圆整）。\n"
        f"分段条件v>=1.2 m/s：{SHEVELEV_HIGH}。\n"
        f"分段条件v<1.2 m/s：{SHEVELEV_LOW}。\n"
        f"本次采用公式：{payload['formula']}；d从mm换算为m，i单位m/m，hf单位m。\n\n"
        f"沿程水头损失 hf：{payload['head_loss_m']} m。\n\n"
        f"{payload['calculation_note']}\n\n"
        + ("内核警示：" + "；".join(payload['assumptions']) + "。\n\n" if payload['assumptions'] else "")
        + "数值来自calc_head_loss_shevelev程序化工具，此摘要由程序生成；未进行规范符合性判断。"
    )


def render_programmatic_manning(messages: Sequence[BaseMessage]) -> str | None:
    """Single current Shevelev execution, retaining its expected applicability warnings."""
    human_index = next((i for i in range(len(messages) - 1, -1, -1)
                        if isinstance(messages[i], HumanMessage)), None)
    if human_index is None:
        return None
    query = messages[human_index].content
    if not isinstance(query, str) or not pure_manning_request(query):
        return None
    turn = list(messages[human_index + 1:])
    calls = [(i, c) for i, m in enumerate(turn) if isinstance(m, AIMessage) for c in m.tool_calls]
    observations = [(i, m) for i, m in enumerate(turn) if isinstance(m, ToolMessage)]
    if (not turn or len(calls) != 1 or len(observations) != 1
            or any(isinstance(m, AIMessage) and m.invalid_tool_calls for m in turn)):
        return None
    ci, call = calls[0]
    oi, obs = observations[0]
    final = turn[-1]
    if (call.get("name") != "calc_slope_manning" or obs.name != "calc_slope_manning"
            or not call.get("id") or obs.tool_call_id != call["id"] or obs.status != "success"
            or not ci < oi < len(turn) - 1 or not isinstance(final, AIMessage)
            or final.tool_calls or not final.id or not isinstance(final.content, str)
            or not final.content.strip()):
        return None
    if not isinstance(obs.artifact, dict) or set(obs.artifact) != {PROGRAMMATIC_CALC_ARTIFACT}:
        return None
    try:
        payload = json.loads(obs.content) if isinstance(obs.content, str) else None
        if (not isinstance(payload, dict) or not verified_manning_result(call["args"], payload,
                                               obs.artifact[PROGRAMMATIC_CALC_ARTIFACT], call["id"], query)):
            return None
    except (ValueError, TypeError, KeyError):
        return None
    inputs = payload["inputs"]
    return (
        "## 曼宁（Manning）非满管圆管参考坡度\n\n"
        f"- 流量 Q：{inputs['flow_lps']} L/s\n"
        f"- 实际内径 d：{inputs['diameter_mm']} mm\n"
        f"- 充满度 r=h/d：{inputs['fill_ratio']}\n"
        f"- 用户给定 SI 曼宁 n：{inputs['manning_n']} s/m^(1/3)\n\n"
        f"几何关系和公式：{payload['formula']}。θ为圆心角，半满时θ=π。"
        "Q从L/s换算为m³/s，d从mm换算为m；未采用重力加速度g。\n\n"
        f"参考坡度 S：{payload['slope']} m/m。\n\n"
        f"{payload['calculation_note']}\n\n"
        + ("内核警示：" + "；".join(payload['assumptions']) + "。\n\n" if payload['assumptions'] else "")
        + "数值来自calc_slope_manning程序化工具，此摘要由程序生成；未由DN推定实际内径。"
    )


def render_programmatic_pump_head(messages: Sequence[BaseMessage]) -> str | None:
    """Single current pump head execution with explicit source/applicability disclosure."""
    human_index = next((i for i in range(len(messages) - 1, -1, -1)
                        if isinstance(messages[i], HumanMessage)), None)
    if human_index is None:
        return None
    query = messages[human_index].content
    if not isinstance(query, str) or not pure_pump_head_request(query):
        return None
    turn = list(messages[human_index + 1:])
    calls = [(i, c) for i, m in enumerate(turn) if isinstance(m, AIMessage) for c in m.tool_calls]
    observations = [(i, m) for i, m in enumerate(turn) if isinstance(m, ToolMessage)]
    if (not turn or len(calls) != 1 or len(observations) != 1
            or any(isinstance(m, AIMessage) and m.invalid_tool_calls for m in turn)):
        return None
    ci, call = calls[0]
    oi, obs = observations[0]
    final = turn[-1]
    if (call.get("name") != "calc_pump_head" or obs.name != "calc_pump_head"
            or not call.get("id") or obs.tool_call_id != call["id"] or obs.status != "success"
            or not ci < oi < len(turn) - 1 or not isinstance(final, AIMessage)
            or final.tool_calls or not final.id or not isinstance(final.content, str)
            or not final.content.strip()):
        return None
    if not isinstance(obs.artifact, dict) or set(obs.artifact) != {PROGRAMMATIC_CALC_ARTIFACT}:
        return None
    try:
        payload = json.loads(obs.content) if isinstance(obs.content, str) else None
        if (not isinstance(payload, dict) or not verified_pump_head_result(call["args"], payload,
                                               obs.artifact[PROGRAMMATIC_CALC_ARTIFACT], call["id"], query)):
            return None
    except (ValueError, TypeError, KeyError):
        return None
    inputs = payload["inputs"]
    return (
        "## 泵扬程四分量参考合计\n\n"
        f"- 静扬程：{inputs['static_head_m']} m\n"
        f"- 沿程损失扬程：{inputs['friction_head_m']} m\n"
        f"- 局部损失扬程：{inputs['local_head_m']} m\n"
        f"- 末端余压扬程：{inputs['residual_head_m']} m\n\n"
        f"公式：{payload['formula']}。\n\n"
        f"完整精度参考合计 H：{payload['pump_head_m']} m。\n\n"
        f"{payload['calculation_note']}\n\n"
        + ("内核警示：" + "；".join(payload['assumptions']) + "。\n\n" if payload['assumptions'] else "")
        + "数值来自calc_pump_head程序化工具，此摘要由程序生成。"
    )
