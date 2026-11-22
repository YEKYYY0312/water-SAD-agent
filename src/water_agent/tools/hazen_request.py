"""Finite SI declarations for Hazen; application metadata is separate from inputs."""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from typing import Any

_CURRENT_QUERY: ContextVar[str | None] = ContextVar('hazen_current_query', default=None)
_FORMULA = re.compile(r'Hazen\s*[-–—−]\s*Williams', re.IGNORECASE)
# Aliases are identifiers, while unit syntax is explicitly case sensitive.
_FIELDS = {
    'length_m': (r'管长|长度|\blength_m\b|\bL\b', r'm|米'),
    'diameter_mm': (r'实际内径|管内径|内径|\bdiameter_mm\b|\bd\b', r'mm|毫米'),
    'flow_lps': (r'流量|\bflow_lps\b|\bQ\b', r'L/s|l/s|升/秒'),
    'hazen_williams_c': (r'(?:Hazen\s*[-–—−]\s*Williams\s*)?系数\s*C?|\bhazen_williams_c\b|\bC\b', r''),
}
_DECLARATIONS = {
    key: re.compile(rf'(?:{alias})\s*(?:[=:：]|为)?\s*([0-9]+(?:\.[0-9]+)?)\s*'
                    rf'(?-i:{unit})(?![A-Za-z0-9/.^²³])', re.IGNORECASE)
    for key, (alias, unit) in _FIELDS.items()
}
_TEMPERATURE = re.compile(r'(?:本次)?水温\s*(-?[0-9]+(?:\.[0-9]+)?)\s*°C(?![A-Za-z0-9/])')
_INTENT = re.compile(r'(?:计算|求)[^，,。；;\n]{0,30}沿程水头损失')
_NEGATED = re.compile(r'(?:不要|不许|禁止|勿|别|无需|不必|不|停止|取消)[^，,。；;\n]{0,16}(?:计算|求|Hazen)', re.IGNORECASE)
_UNRESOLVED = re.compile('|'.join(alias for alias, _ in _FIELDS.values()) +
    r'|水温|\b(?:DN|f|n|v|friction_factor|manning_n|velocity_mps)\b|[0-9=]', re.IGNORECASE)
_PHRASES = tuple(sorted((
    '仅按', '按', '的SI公式计算', 'SI公式计算', '公式计算', '计算', '求', '的', '沿程水头损失',
    '满管有压给水圆管', '满管有压给水', '满管有压', '满管', '有压', '给水', '圆管',
    '无量纲', '由用户给定', '用户给定', '本次不核验或推导系数', '不核验或推导系数',
    '请调用程序化工具', '请调用程序化计算工具', '请调用计算工具', '请',
    '列明输入、公式、结果及经验近似的适用前提', '列明输入、公式和结果',
    '不检索条文', '不选型', '不做规范符合性判断',
), key=len, reverse=True))


@contextmanager
def hazen_request_scope(query: str | None) -> Iterator[None]:
    token = _CURRENT_QUERY.set(query)
    try:
        yield
    finally:
        _CURRENT_QUERY.reset(token)


def _parse(query: str) -> tuple[dict[str, Decimal] | None, str]:
    if len(query) > 16000:
        return None, ''
    text = unicodedata.normalize('NFKC', query)
    if not _FORMULA.search(text) or not _INTENT.search(text) or _NEGATED.search(text):
        return None, ''
    # Another formula/coefficient cannot be used as an implicit conversion.
    if re.search(r'Darcy|Fanning|曼宁|Manning|舍维列夫', text, re.IGNORECASE):
        return None, ''
    temps = list(_TEMPERATURE.finditer(text))
    if len(temps) > 1:
        return None, ''
    remainder = _TEMPERATURE.sub('', text)
    values: dict[str, Decimal] = {}
    for key, pattern in _DECLARATIONS.items():
        matches = list(pattern.finditer(remainder))
        if len(matches) != 1:
            return None, ''
        value = Decimal(matches[0].group(1))
        if not value.is_finite() or value <= 0:
            return None, ''
        values[key] = value
        remainder = pattern.sub('', remainder)
    # Known provenance is not a second coefficient declaration.
    remainder = remainder.replace('本次不核验或推导系数', '').replace('不核验或推导系数', '')
    remainder = _FORMULA.sub('', remainder)
    if _UNRESOLVED.search(remainder) or not _allowed_remainder(remainder, mixed=True):
        return None, ''
    return values, remainder


def hazen_args_match(query: str, args: Mapping[str, Any]) -> bool:
    values, _ = _parse(query)
    return values is not None and set(args) == set(values) and all(
        type(args.get(key)) in (int, float) and Decimal(str(args[key])) == value
        for key, value in values.items()
    )


def guard_hazen_request(args: Any) -> None:
    query = _CURRENT_QUERY.get()
    if query is None or not hazen_args_match(query, args.model_dump()):
        raise ValueError('请明确本轮Hazen–Williams计算意图及唯一管长m、实际内径mm、流量L/s、用户给定无量纲C；'
                         '缺参、单位不明、重复或参数不一致时先澄清，不猜C或由DN推内径。')


def _allowed_remainder(text: str, *, mixed: bool = False) -> bool:
    # The execution guard also consumes a finite vocabulary: an unknown unit or
    # a negated physical condition must not hide in otherwise free-form prose.
    phrases = _PHRASES + (("并查询规范", "并选择管材", "并计算流速", "并判断是否符合规范") if mixed else ())
    position = 0
    while position < len(text):
        if text[position].isspace() or text[position] in '，,。、：:；;？?！!（）()':
            position += 1
            continue
        phrase = next((p for p in phrases if text.startswith(p, position)), None)
        if phrase is None:
            return False
        position += len(phrase)
    return True


def pure_hazen_request(query: str) -> bool:
    values, text = _parse(query)
    return values is not None and _allowed_remainder(text)


def application_note(query: str | None = None) -> str:
    text = unicodedata.normalize('NFKC', query if query is not None else (_CURRENT_QUERY.get() or ''))
    temperatures = list(_TEMPERATURE.finditer(text))
    if len(temperatures) == 1:
        temperature = Decimal(temperatures[0].group(1))
        temperature_note = f'用户声明水温{temperature}°C；' + ('处于口径表温度范围。' if 10 <= temperature <= 20 else '超出口径表温度范围，工程适用性未确认。')
    else:
        temperature_note = '水温未确认，工程适用性未确认。'
    condition = '用户声明满管有压给水。' if '满管有压给水' in text else '满管有压给水条件未确认。'
    return ('经验近似：适用前提为满管有压给水，口径表水温10–20°C；'
            '依据本项目docs/design/09-hydraulics-formulas.md §2.2，非规范符合性判定。'
            + condition + temperature_note + 'C由用户给定，工程来源未核验，不按管材猜取或自动推导。')


def current_hazen_query() -> str | None:
    return _CURRENT_QUERY.get()
