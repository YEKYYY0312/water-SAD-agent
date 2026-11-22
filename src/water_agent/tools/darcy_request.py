"""Finite explicit-input grammar for Darcy requests; unknown syntax fails closed."""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from typing import Any

_CURRENT_QUERY: ContextVar[str | None] = ContextVar("darcy_current_query", default=None)
_NUMBER = r"([0-9]+(?:\.[0-9]+)?)"
_FORMULA = re.compile(r"Darcy\s*[-–—−]\s*Weisbach", re.IGNORECASE)
_PATTERNS = {
    "length_m": re.compile(r"(?:管长|长度|\bL)\s*(?:[=:：]|为)?\s*" + _NUMBER + r"\s*(?:m|米)(?![A-Za-z0-9/.^])", re.IGNORECASE),
    "diameter_mm": re.compile(r"(?:实际内径|管内径|内径)\s*(?:[=:：]|为)?\s*" + _NUMBER + r"\s*(?:mm|毫米)(?![A-Za-z0-9/.^])", re.IGNORECASE),
    "velocity_mps": re.compile(r"(?:流速|\bv)\s*(?:[=:：]|为)?\s*" + _NUMBER + r"\s*(?:m/s|米/秒)(?![A-Za-z0-9/.^])", re.IGNORECASE),
    "friction_factor": re.compile(r"(?:Darcy\s*摩阻系数|Darcy\s*摩擦系数|摩阻系数|\bf)\s*(?:[=:：]|为)?\s*" + _NUMBER + r"\s*(?:无量纲)?\s*(?=$|[，,。；;、\n])", re.IGNORECASE),
    "gravity_mps2": re.compile(r"(?:重力加速度|\bg)\s*(?:[=:：]|为)?\s*" + _NUMBER + r"\s*(?:m/s\^?2|米/秒平方)(?![A-Za-z0-9/.^])", re.IGNORECASE),
}
_PURE_PHRASES = tuple(sorted((
    "仅按", "按", "公式计算", "公式", "计算", "满管有压圆管的沿程水头损失",
    "满管有压圆管", "满管有压", "满管", "有压圆管", "有压", "圆管", "的", "沿程水头损失",
    "摩阻系数是本次测试给定值", "摩阻系数由用户给定", "不需推导", "不推导摩阻系数",
    "请调用程序化工具", "请调用程序化计算工具", "请调用计算工具", "不检索条文",
    "不做选型或规范符合性判断", "不做规范符合性判断", "不做规范判定", "列明输入、公式和结果",
    "列出输入、公式和结果", "请", "求", "仅计算", "用户给定", "无量纲",
), key=len, reverse=True))


@contextmanager
def darcy_request_scope(query: str | None) -> Iterator[None]:
    """Trusted current human text, propagated to LangGraph tool executor threads."""
    token = _CURRENT_QUERY.set(query)
    try:
        yield
    finally:
        _CURRENT_QUERY.reset(token)


def parse_darcy_inputs(query: str) -> tuple[dict[str, Decimal] | None, str]:
    text = unicodedata.normalize("NFKC", query)
    if len(text) > 16000 or not _FORMULA.search(text):
        return None, "请明确选择 Darcy–Weisbach 公式。"
    if re.search(r"(?:不按|不要按|禁止使用|不要使用|不使用|别用|勿用)\s*Darcy", text, re.IGNORECASE):
        return None, "本轮未明确授权使用 Darcy–Weisbach 公式，请澄清。"
    if re.search(r"Fanning|Hazen|Williams|舍维列夫|\bDN\s*\d", text, re.IGNORECASE):
        return None, "请确认 Darcy 摩阻系数与实际内径；不能混用 Fanning、其他公式或由 DN 推定内径。"
    values: dict[str, Decimal] = {}
    for key, pattern in _PATTERNS.items():
        matches = list(pattern.finditer(text))
        if not matches and key == "gravity_mps2":
            # An explicitly written but unrecognised gravity must never become a default.
            if re.search(r"重力加速度|\bg\b", text, re.IGNORECASE):
                return None, "请明确重力加速度的数值及 m/s² 单位。"
            values[key] = Decimal("9.81")
            continue
        if len(matches) != 1:
            return None, f"请明确唯一的 {key} 及约定单位（管长m、实际内径mm、流速m/s、Darcy摩阻系数无量纲）。"
        value = Decimal(matches[0].group(1))
        if not value.is_finite() or value <= 0:
            return None, f"{key} 必须为有限正数。"
        values[key] = value
    remainder = text
    for pattern in _PATTERNS.values():
        remainder = pattern.sub("", remainder)
    # Remove only the supported nonnumeric provenance phrases before looking
    # for unresolved declarations. A valid occurrence must not hide a second
    # occurrence with an unknown unit, approximate value or unsupported type.
    for phrase in ("摩阻系数是本次测试给定值", "摩阻系数由用户给定", "不推导摩阻系数"):
        remainder = remainder.replace(phrase, "")
    if re.search(r"管长|长度|内径|管径|流速|摩阻系数|摩擦系数|重力加速度|\b(?:[Ldvfg]|length_m|diameter_mm|velocity_mps|friction_factor|gravity_mps2)\b", remainder, re.IGNORECASE):
        return None, "存在额外或无法识别的参数声明，请明确唯一数值及约定单位后再计算。"
    return values, ""


def darcy_args_match(query: str, args: Mapping[str, Any]) -> tuple[bool, str]:
    values, error = parse_darcy_inputs(query)
    if values is None:
        return False, error
    for key, value in values.items():
        number = args.get(key, 9.81 if key == "gravity_mps2" else None)
        if type(number) not in (int, float) or Decimal(str(number)) != value:
            return False, f"{key} 与用户本轮明确输入不一致；请澄清，不能补参或换算未知单位。"
    return True, ""


def guard_darcy_request(args: Any) -> None:
    query = _CURRENT_QUERY.get()
    if query is None:
        raise ValueError("缺少本轮用户明确输入，不能执行 Darcy–Weisbach 计算。")
    matches, error = darcy_args_match(query, args.model_dump())
    if not matches:
        raise ValueError(error)


def pure_darcy_request(query: str) -> bool:
    values, _ = parse_darcy_inputs(query)
    if values is None:
        return False
    text = _FORMULA.sub("", unicodedata.normalize("NFKC", query))
    for pattern in _PATTERNS.values():
        text = pattern.sub("", text)
    position = 0
    while position < len(text):
        if text[position].isspace() or text[position] in "，,。、：:；;？?！!（）()":
            position += 1
            continue
        phrase = next((p for p in _PURE_PHRASES if text.startswith(p, position)), None)
        if phrase is None:
            return False
        position += len(phrase)
    return True


def darcy_default_values(query: str | None = None) -> dict[str, float]:
    """Source of defaults is trusted human text, never the model's field set."""
    current = query if query is not None else _CURRENT_QUERY.get()
    if current is None or parse_darcy_inputs(current)[0] is None:
        raise ValueError("缺少有效的本轮用户参数，无法确定默认值来源。")
    text = unicodedata.normalize("NFKC", current)
    return {} if _PATTERNS["gravity_mps2"].search(text) else {"gravity_mps2": 9.81}
