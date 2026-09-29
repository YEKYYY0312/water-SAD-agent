"""Word 公式（OMML）→ 线性文本（P5-S50 修复）。

python-docx 的 ``paragraph.text`` 只拼 ``w:t``，**Word 公式（``m:oMath``）整段被跳过**。
计算书的数值几乎都写在公式里：实测 137400 计算说明书有 114 个 OMML 公式，抽取文本
里格栅间隙数、水头损失、池容一律"只剩式中说明、没有公式也没有结果"，模型据此如实
答"关键数值只有公式没有结果"——根因在抽取，不在模型。

这里做确定性的结构转写（分式 → ``(a)/(b)``、上下标 → ``x_i``/``x^2``、根式 →
``√(x)``、函数 → ``sin(x)``），**不求值、不改写数值**：公式里的数字原样保留，计算
仍由程序化工具做（铁律 1）。MathType 的 OLE 对象（``w:object``）是图片 + 二进制，
没有可读文本，只计数、如实报告"读不到"，不猜。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

#: 上下标里超过一个字符就加括号，避免 ``Q_max`` 与 ``Q_m ax`` 的歧义。
_ATOMIC_MAX = 1


def _wrap(text: str) -> str:
    text = text.strip()
    if len(text) <= _ATOMIC_MAX or (text.startswith("(") and text.endswith(")")):
        return text
    return f"({text})"


def _children(node: Any, tag: str) -> Any | None:
    return next((child for child in node if child.tag == M + tag), None)


def _render(node: Any | None) -> str:
    if node is None:
        return ""
    return "".join(_render_one(child) for child in node)


def _render_one(node: Any) -> str:
    tag = node.tag[len(M):] if node.tag.startswith(M) else ""
    if not tag:
        return ""
    if tag == "r":
        return "".join(t.text or "" for t in node.iter(M + "t"))
    if tag == "f":
        return f"{_wrap(_render(_children(node, 'num')))}/{_wrap(_render(_children(node, 'den')))}"
    if tag == "sSub":
        return f"{_render(_children(node, 'e'))}_{_wrap(_render(_children(node, 'sub')))}"
    if tag == "sSup":
        return f"{_render(_children(node, 'e'))}^{_wrap(_render(_children(node, 'sup')))}"
    if tag == "sSubSup":
        base = _render(_children(node, "e"))
        return f"{base}_{_wrap(_render(_children(node, 'sub')))}^{_wrap(_render(_children(node, 'sup')))}"
    if tag == "rad":
        degree = _render(_children(node, "deg")).strip()
        body = _render(_children(node, "e"))
        return f"{degree}√({body})" if degree else f"√({body})"
    if tag == "d":
        props = _children(node, "dPr")
        beg = end = None
        if props is not None:
            b = _children(props, "begChr")
            e = _children(props, "endChr")
            beg = b.get(M + "val") if b is not None else None
            end = e.get(M + "val") if e is not None else None
        inner = ", ".join(_render(child) for child in node if child.tag == M + "e")
        return f"{'(' if beg is None else beg}{inner}{')' if end is None else end}"
    if tag == "func":
        return f"{_render(_children(node, 'fName'))}({_render(_children(node, 'e'))})"
    if tag == "nary":
        props = _children(node, "naryPr")
        chr_node = _children(props, "chr") if props is not None else None
        symbol = chr_node.get(M + "val") if chr_node is not None else "∫"
        sub = _render(_children(node, "sub"))
        sup = _render(_children(node, "sup"))
        limits = (f"_{_wrap(sub)}" if sub else "") + (f"^{_wrap(sup)}" if sup else "")
        return f"{symbol}{limits} {_render(_children(node, 'e'))}"
    if tag in {"ctrlPr", "rPr", "sSubPr", "sSupPr", "fPr", "radPr", "dPr", "funcPr", "naryPr"}:
        return ""
    # e / num / den / sub / sup / deg / fName / oMath / box / acc / bar ... ：递归取内容
    return _render(node)


def omml_to_text(omath: Any) -> str:
    """一个 ``m:oMath`` / ``m:oMathPara`` 元素 → 线性文本。"""

    return " ".join(part for part in (_render(m) for m in omath.iter(M + "oMath")) if part).strip()


def paragraph_text(paragraph_element: Any) -> str:
    """按文档顺序拼出段落文字：普通文本 + 公式（``【公式】…``）。

    与 python-docx 的 ``paragraph.text`` 相同地拼 ``w:t``，外加把段内的 ``m:oMath``
    按出现位置转写进去。公式加 ``【公式】`` 前缀，让模型知道这是转写出的公式而不是
    原文句子（排版信息已丢，只保留结构）。
    """

    parts: list[str] = []
    for child in paragraph_element:
        if child.tag in (M + "oMath", M + "oMathPara"):
            formula = omml_to_text(child)
            if formula:
                parts.append(f"【公式】{formula}")
        else:
            parts.append("".join(t.text or "" for t in child.iter(W + "t")))
    return "".join(parts)


def count_ole_objects(elements: Iterable[Any]) -> int:
    """MathType 等 OLE 公式对象数（只有预览图片 + 二进制，没有可读文本）。"""

    return sum(1 for element in elements for _ in element.iter(W + "object"))


__all__ = ["count_ole_objects", "omml_to_text", "paragraph_text"]
