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

from water_agent.tools.calc_velocity import VelocityArgs
from water_agent.tools.ds_adapters import PROGRAMMATIC_CALC_ARTIFACT

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
