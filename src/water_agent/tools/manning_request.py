"""Finite Manning declarations; applicability is metadata, never a fifth input."""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from itertools import pairwise
from typing import Any

_CURRENT_QUERY: ContextVar[str | None] = ContextVar('manning_current_query', default=None)
_FORMULA = re.compile(r'曼宁|(?<![A-Za-z])Manning(?![A-Za-z])', re.IGNORECASE)
_FIELDS = {
    'flow_lps': (r'流量|\bflow_lps\b|\bQ\b', r'L/s|l/s|升/秒'),
    'diameter_mm': (r'实际内径|管内径|内径|\bdiameter_mm\b|\bd\b', r'mm|毫米'),
    'fill_ratio': (r'充满度h/d|充满度|h/d|\bfill_ratio\b', r''),
    'manning_n': (r'曼宁粗糙系数n|曼宁粗糙系数|\bmanning_n\b|\bn\b', r''),
}
_DECLARATIONS = {
    key: re.compile(rf'(?:{alias})\s*(?:[=:：]|为)?\s*([0-9]+(?:\.[0-9]+)?)\s*'
                    rf'(?:{unit})(?![A-Za-z0-9/.^²³])')
    for key, (alias, unit) in _FIELDS.items()
}
_INTENT = re.compile(r'(?:计算|求)[^，,。；;\n]{0,40}坡度')
_NEGATED = re.compile(r'(?:不要|不许|禁止|勿|别|无需|不必|不|停止|取消)[^，,。；;\n]{0,16}(?:计算|求|曼宁|Manning)', re.IGNORECASE)
_PHRASES = tuple(sorted((
    '仅按','按','公式计算','计算','求','的','参考坡度','坡度',
    '非满管','圆管','重力','均匀流','工况未确认',
    '请调用程序化工具','请调用程序化计算工具','请调用计算工具','请',
    '列明输入、几何关系、公式、结果及适用前提','列明输入、公式和结果',
    '由用户按SI口径给定','本次不核验其工程来源',
    '不推定DN','不选型','不检索条文','不做最小设计坡度或规范符合性判断',
    '不做规范符合性判断','内径不是DN','实际内径不等于DN',
    'h/d为充满度','充满度是h/d',
),key=len,reverse=True))
_MIXED = ('并查询规范','并选择管材','并计算流速','并判断是否符合规范')


@contextmanager
def manning_request_scope(query: str | None) -> Iterator[None]:
    token = _CURRENT_QUERY.set(query)
    try:
        yield
    finally:
        _CURRENT_QUERY.reset(token)


def current_manning_query() -> str | None:
    return _CURRENT_QUERY.get()


def _allowed_remainder(text: str, *, mixed: bool = False) -> bool:
    phrases = _PHRASES + (_MIXED if mixed else ())
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


def _parse(query: str) -> tuple[dict[str, Decimal] | None, str]:
    if len(query) > 16000:
        return None, ''
    text = unicodedata.normalize('NFKC', query)
    if not _FORMULA.search(text) or not _INTENT.search(text) or _NEGATED.search(text):
        return None, ''
    values: dict[str, Decimal] = {}
    spans: list[tuple[int, int]] = []
    for key, pattern in _DECLARATIONS.items():
        matches = list(pattern.finditer(text))
        if len(matches) != 1:
            return None, ''
        value = Decimal(matches[0].group(1))
        if not value.is_finite() or value <= 0:
            return None, ''
        if key == 'fill_ratio' and not 0 < value < 1:
            return None, ''
        values[key] = value
        spans.append(matches[0].span())
    # Every declaration must already exist contiguously in the SAME original
    # normalized text. Never form a new declaration by deleting another field
    # or metadata, even when whitespace is allowed between label/value/unit.
    ordered = sorted(spans)
    if any(left[1] > right[0] for left, right in pairwise(ordered)):
        return None, ''
    characters = list(text)
    for start, end in spans:
        characters[start:end] = ' ' * (end-start)
    remainder = ''.join(characters)
    for pattern in (_FORMULA,):
        remainder = pattern.sub(lambda match: ' ' * len(match.group(0)), remainder)
    # Strict finite token consumption rejects all unknown/duplicate declarations,
    # unsupported units, incompatible flow conditions or undeclared inputs.
    if not _allowed_remainder(remainder, mixed=True):
        return None, ''
    return values, remainder


def manning_args_match(query: str, args: Mapping[str, Any]) -> bool:
    values, _ = _parse(query)
    return values is not None and set(args) == set(values) and all(
        type(args.get(key)) in (int, float) and Decimal(str(args[key])) == value
        for key, value in values.items()
    )


def guard_manning_request(args: Any) -> None:
    query = _CURRENT_QUERY.get()
    if query is None or not manning_args_match(query, args.model_dump()):
        raise ValueError('请明确本轮曼宁非满管圆管坡度计算及唯一流量L/s、实际内径mm、充满度0<h/d<1、SI曼宁n；缺参/重复/未知单位/不适用工况先澄清。')


def pure_manning_request(query: str) -> bool:
    values, remainder = _parse(query)
    return values is not None and _allowed_remainder(remainder)


def application_note(query: str) -> str:
    text = unicodedata.normalize('NFKC', query)
    conditions = ''.join(f'用户声明{p}。' if p in text else f'{p}条件未确认。'
                         for p in ('非满管', '圆管', '重力', '均匀流'))
    return (conditions + 'n由用户按SI口径提供，单位s/m^(1/3)，工程来源未核验。'
            '实际适用性未独立核验；仅均匀重力流下可对应底坡；不适用于非均匀流、回水、满管有压流或非圆断面。'
            '未检索条文，未做最小设计坡度或规范符合性校核。')
