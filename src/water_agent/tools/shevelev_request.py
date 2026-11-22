"""Finite Shevelev declarations; applicability is metadata, never a fourth input."""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from itertools import pairwise
from typing import Any

_CURRENT_QUERY: ContextVar[str | None] = ContextVar('shevelev_current_query', default=None)
_FORMULA = re.compile(r'舍维列夫|(?<![A-Za-z])Shevelev(?![A-Za-z])', re.IGNORECASE)
_FIELDS = {
    'length_m': (r'管长|长度|\blength_m\b|\bL\b', r'm|米'),
    'diameter_mm': (r'实际内径|管内径|内径|\bdiameter_mm\b|\bd\b', r'mm|毫米'),
    'velocity_mps': (r'流速|\bvelocity_mps\b|\bv\b', r'm/s|米/秒'),
}
_DECLARATIONS = {
    key: re.compile(rf'(?:{alias})\s*(?:[=:：]|为)?\s*([0-9]+(?:\.[0-9]+)?)\s*'
                    rf'(?:{unit})(?![A-Za-z0-9/.^²³])')
    for key, (alias, unit) in _FIELDS.items()
}
_TEMPERATURE = re.compile(r'(?:本次)?水温\s*(-?[0-9]+(?:\.[0-9]+)?)\s*°C(?![A-Za-z0-9/])')
_MATERIAL = re.compile(r'铸铁管|钢管|管材未知')
_INTENT = re.compile(r'(?:计算|求)[^，,。；;\n]{0,30}沿程水头损失')
_NEGATED = re.compile(r'(?:不要|不许|禁止|勿|别|无需|不必|不|停止|取消)[^，,。；;\n]{0,16}(?:计算|求|舍维列夫|Shevelev)', re.IGNORECASE)
_PHRASES = tuple(sorted((
    '仅按', '按', '公式计算', '计算', '求', '的', '沿程水头损失',
    '满管', '有压', '给水', '圆管', '工况未确认',
    '请调用程序化工具', '请调用程序化计算工具', '请调用计算工具', '请',
    '列明输入、采用的分段公式、结果及经验近似的适用前提',
    '列明输入、公式、结果及经验近似的适用前提', '列明输入、公式和结果',
    '经验范围来源本次未核验', '不检索条文', '不选型', '不做规范符合性判断',
    # These are semantic disclosures, not another d/DN declaration.
    '实际内径不等于DN', '内径不是DN', '不由DN推实际内径',
), key=len, reverse=True))
_MIXED = ('并查询规范', '并选择管材', '并计算流速', '并判断是否符合规范')


@contextmanager
def shevelev_request_scope(query: str | None) -> Iterator[None]:
    token = _CURRENT_QUERY.set(query)
    try:
        yield
    finally:
        _CURRENT_QUERY.reset(token)


def current_shevelev_query() -> str | None:
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
    temps = list(_TEMPERATURE.finditer(text))
    materials = list(_MATERIAL.finditer(text))
    if len(temps) > 1 or len(materials) > 1:
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
    for pattern in (_TEMPERATURE, _MATERIAL, _FORMULA):
        remainder = pattern.sub(lambda match: ' ' * len(match.group(0)), remainder)
    # Strict finite token consumption rejects all unknown/duplicate declarations,
    # unsupported units, non-full/non-pressure flow and incompatible materials.
    if not _allowed_remainder(remainder, mixed=True):
        return None, ''
    return values, remainder


def shevelev_args_match(query: str, args: Mapping[str, Any]) -> bool:
    values, _ = _parse(query)
    return values is not None and set(args) == set(values) and all(
        type(args.get(key)) in (int, float) and Decimal(str(args[key])) == value
        for key, value in values.items()
    )


def guard_shevelev_request(args: Any) -> None:
    query = _CURRENT_QUERY.get()
    if query is None or not shevelev_args_match(query, args.model_dump()):
        raise ValueError('请明确本轮舍维列夫计算意图及唯一管长m、实际内径mm、流速m/s；'
                         '缺参、重复、单位不明、参数不一致或明确不适用工况时先澄清，不由DN或流量推算。')


def pure_shevelev_request(query: str) -> bool:
    values, remainder = _parse(query)
    return values is not None and _allowed_remainder(remainder)


def application_note(query: str) -> str:
    text = unicodedata.normalize('NFKC', query)
    temps = list(_TEMPERATURE.finditer(text)); materials = list(_MATERIAL.finditer(text))
    temperature = f'用户声明水温{temps[0].group(1)}°C。' if len(temps) == 1 else '水温未确认。'
    material = (f'用户声明管材{materials[0].group(0)}。'
                if len(materials) == 1 and materials[0].group(0) != '管材未知' else '管材未确认。')
    conditions = ''.join(f'用户声明{p}。' if p in text else f'{p}条件未确认。'
                         for p in ('满管', '有压', '给水'))
    return ('经验近似：项目docs/design/09-hydraulics-formulas.md §2.3冻结算法，'
            '前提为给水钢管/铸铁管、满管有压、水温10°C附近；非已核实工程规范。'
            '10°C附近未定义精确许可区间，不据温度数值宣布适用。'
            + material + conditions + temperature
            + '适用性未核验；实际DN及来源未提供，DN50–DN1000经验范围未核验。'
            '内核按实际内径数值与50/1000比较的警示原样保留，不等于DN范围校核；不从内径反推DN。')
