"""Finite pump head declarations from contiguous original text spans."""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from itertools import pairwise
from typing import Any

_CURRENT_QUERY: ContextVar[str | None] = ContextVar('pump_head_current_query', default=None)
_FORMULA = re.compile(r'水泵扬程|泵扬程|(?<![A-Za-z])pump[ _]head(?![A-Za-z])', re.IGNORECASE)
_FIELDS = {
 'static_head_m': r'静扬程|几何扬程|(?<![A-Za-z_])H_static(?![A-Za-z_])|\bstatic_head_m\b',
 'friction_head_m': r'沿程损失扬程|沿程水头损失|沿程损失|(?<![A-Za-z_])H_friction(?![A-Za-z_])|\bfriction_head_m\b',
 'local_head_m': r'局部损失扬程|局部水头损失|局部损失|(?<![A-Za-z_])H_local(?![A-Za-z_])|\blocal_head_m\b',
 'residual_head_m': r'末端所需余压扬程|末端余压扬程|剩余水头|出水自由水头|(?<![A-Za-z_])H_residual(?![A-Za-z_])|\bresidual_head_m\b',
}
_DECLARATIONS = {
 key: re.compile(rf'(?:{alias})(?:\s*\((?:{alias})\))?\s*(?:[=:：]|为)?\s*([0-9]+(?:\.[0-9]+)?)\s*(?:m|米)(?![A-Za-z0-9/.^²³])')
 for key, alias in _FIELDS.items()
}
_INTENT = re.compile(r'计算|求')
_NEGATED = re.compile(r'(?:不要|不许|禁止|勿|别|无需|不必|不|停止|取消)\s*(?:计算|求)')
_PHRASES = tuple(sorted((
 '仅按','按','四分量相加','计算','求','泵扬程','水泵扬程','的','参考合计',
 '请调用程序化工具','请调用程序化计算工具',
 '列明四项输入、公式、完整精度结果和适用前提','四项由用户明确给定',
 '本次不核验其工程来源','不另算损失','不选泵','不检索条文','不做规范符合性判断',
), key=len, reverse=True))
_MIXED = ('并查询规范','并选择水泵','并另算沿程损失','并计算流速')


@contextmanager
def pump_head_request_scope(query: str | None) -> Iterator[None]:
    token = _CURRENT_QUERY.set(query)
    try:
        yield
    finally:
        _CURRENT_QUERY.reset(token)


def current_pump_head_query() -> str | None:
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
        if not value.is_finite() or value < 0:
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


def pump_head_args_match(query: str, args: Mapping[str, Any]) -> bool:
    values, _ = _parse(query)
    return values is not None and set(args) == set(values) and all(
        type(args.get(key)) in (int, float) and Decimal(str(args[key])) == value
        for key, value in values.items()
    )


def guard_pump_head_request(args: Any) -> None:
    query = _CURRENT_QUERY.get()
    if query is None or not pump_head_args_match(query, args.model_dump()):
        raise ValueError('请明确本轮泵扬程计算与静扬程、沿程损失扬程、局部损失扬程、末端余压扬程四项非负数及单位m；缺参/重复/未知单位先澄清。')


def pure_pump_head_request(query: str) -> bool:
    values, remainder = _parse(query)
    return values is not None and _allowed_remainder(remainder)


def application_note(query: str) -> str:
    return ('四项由用户明确给定，输入工程来源未核验，工程适用性未独立核验。'
            '仅为给定四分量的数学合计，包含全0情况；不据此判断水泵可用或无需设置水泵。'
            '未检索条文，未做规范符合性校核；不另算损失、不加裕量、不选泵。')
