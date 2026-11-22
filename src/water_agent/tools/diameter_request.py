"""Bounded, explicit Q/v declarations from the current trusted human message."""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from typing import Any

_CURRENT_QUERY: ContextVar[str | None] = ContextVar("diameter_current_query", default=None)
DIAMETER_INTENT_PATTERNS = (
    r"(?:计算|求)[^，,。；;\n]{0,20}(?:实际内径|管内径|管径|内径)",
    r"(?:内径|管径)(?:是多少|多少)",
)
_NEGATED_CALCULATION = re.compile(r"(?:不要|不许|禁止|勿|别|无需|不必|不|停止|取消)[^，,。；;\n]{0,12}(?:计算|求|推导)")
# One declarative grammar: aliases share the same number/unit boundary rules.
_FIELDS = {
    "flow_lps": (r"流量|\bflow_lps\b|\bQ\b", r"L/s|l/s|升/秒"),
    "velocity_mps": (r"目标流速|设计流速|流速|\bvelocity_mps\b|\bv\b", r"m/s|米/秒"),
}
_DECLARATIONS = {
    key: re.compile(
        rf"(?:{aliases})\s*(?:[=:：]|为)?\s*([0-9]+(?:\.[0-9]+)?)\s*"
        rf"(?-i:{units})(?![A-Za-z0-9/.^²³])", re.IGNORECASE,
    )
    for key, (aliases, units) in _FIELDS.items()
}
_UNRESOLVED = re.compile(
    "|".join(aliases for aliases, _ in _FIELDS.values())
    + r"|(?:内径|管径)\s*(?:[=:：为]|[0-9])|\bDN\s*[0-9]|[0-9=]", re.IGNORECASE,
)
_PHRASES = tuple(sorted((
    "仅计算", "计算", "求", "请", "满管圆管", "满管", "圆管", "的", "实际内径",
    "计算内径", "管内径", "内径", "管径", "是多少", "请调用程序化工具", "请调用程序化计算工具",
    "请调用计算工具", "列明输入、公式和结果", "列出输入、公式和结果", "不圆整DN",
    "不自动圆整DN", "不检索条文", "不检索", "不选型", "不做选型或规范符合性判断",
    "不做规范符合性判断", "不做规范判定",
), key=len, reverse=True))


@contextmanager
def diameter_request_scope(query: str | None) -> Iterator[None]:
    token = _CURRENT_QUERY.set(query)
    try:
        yield
    finally:
        _CURRENT_QUERY.reset(token)


def _parse(query: str) -> tuple[dict[str, Decimal] | None, str]:
    if len(query) > 16000:
        return None, ""
    remainder = unicodedata.normalize("NFKC", query)
    if (_NEGATED_CALCULATION.search(remainder)
            or not any(re.search(pattern, remainder) for pattern in DIAMETER_INTENT_PATTERNS)):
        return None, ""
    values: dict[str, Decimal] = {}
    for key, pattern in _DECLARATIONS.items():
        matches = list(pattern.finditer(remainder))
        if len(matches) != 1:
            return None, ""
        value = Decimal(matches[0].group(1))
        if not value.is_finite() or value <= 0:
            return None, ""
        values[key] = value
        remainder = pattern.sub("", remainder)
    # A second alias, unsupported value/unit, or additional numeric declaration
    # cannot be hidden behind the first valid declaration. No default inference.
    if _UNRESOLVED.search(remainder):
        return None, ""
    return values, remainder


def diameter_args_match(query: str, args: Mapping[str, Any]) -> bool:
    values, _ = _parse(query)
    return values is not None and set(args) == set(values) and all(
        type(args.get(key)) in (int, float) and Decimal(str(args[key])) == value
        for key, value in values.items()
    )


def guard_diameter_request(args: Any) -> None:
    query = _CURRENT_QUERY.get()
    if query is None or not diameter_args_match(query, args.model_dump()):
        raise ValueError(
            "请明确本轮唯一流量(L/s)与目标流速(m/s)及有限正数；"
            "缺参、重复声明、未知单位或参数不一致时不能计算，不猜流速。"
        )


def pure_diameter_request(query: str) -> bool:
    values, remainder = _parse(query)
    if values is None or not any(word in query for word in ("内径", "管径")):
        return False
    position = 0
    while position < len(remainder):
        if remainder[position].isspace() or remainder[position] in "，,。、：:；;？?！!（）()":
            position += 1
            continue
        phrase = next((p for p in _PHRASES if remainder.startswith(p, position)), None)
        if phrase is None:
            return False
        position += len(phrase)
    return True
