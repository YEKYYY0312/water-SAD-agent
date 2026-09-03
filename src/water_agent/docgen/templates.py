"""文档模板引擎（P2）：计算书 / 设计说明的结构化确定性生成。

设计约束（三条铁律对齐）：
- **数值程序化**：引擎只渲染入参给出的数值（浮点原值 + 单位），不做任何数值运算，
  不推断、不补数；入参数值的出处由调用方（计算子图/检索）保证；
- **引用可追溯**：正文只出现入参 ``citations`` 提供的 source_id；没有任何输入时
  明确写出"未声明依据"，禁止模板自行补任何条款号；
- **结构合规可评测**：每种文档类型有固定必需章节（``required_sections``），渲染结果
  保留结构化章节（heading + lines）与数值映射（``calc_values``），供机制层评测与
  后续 LLM 润色的结构化底座使用。

产物形态：``RenderedDoc`` = 标题 + 章节列表 + 结构化数值/引用，``text`` 为 Markdown 全文
（草稿预览/正式文档共用；草稿标题带稿次，终稿不带）。
"""
from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

SUPPORTED_DOC_TYPES: tuple[str, ...] = ("calculation_book", "design_note")

DOC_TYPE_LABELS: dict[str, str] = {
    "calculation_book": "计算书",
    "design_note": "设计说明",
}

# 必需章节（key = 无编号的规范短名；heading 形如 "1 工程概况"）
REQUIRED_SECTIONS: dict[str, list[str]] = {
    "calculation_book": ["工程概况", "设计依据", "计算参数与结果", "结论"],
    "design_note": ["工程概况", "设计依据", "设计参数与计算结果", "结论"],
}

# 结果/参数单位（展示用；数值仍以浮点原值进 calc_values，单位不进数值）
UNIT_MAP: dict[str, str] = {
    "velocity_mps": "m/s",
    "flow_lps": "L/s",
    "diameter_mm": "mm",
    "length_m": "m",
    "head_loss_m": "m",
    "pump_head_m": "m",
    "slope": "",
    "static_head_m": "m",
    "friction_head_m": "m",
    "local_head_m": "m",
    "residual_head_m": "m",
    "manning_n": "",
    "hazen_williams_c": "",
    "friction_factor": "",
    "fill_ratio": "",
}

# 备注字段的展示名（非数值键；缺省回退为键本身）
NOTE_LABELS: dict[str, str] = {
    "missing_inputs": "缺少参数",
    "errors": "计算错误",
    "assumptions": "假设 / 越界提示",
    "candidates": "候选方案",
    "formula": "公式口径",
    "spec_record": "口径记录",
}

EMPTY_CALC_NOTE = "（无计算结果：本次未执行程序化计算或计算未产出数值）"
EMPTY_CITATION_NOTE = "（未声明规范依据：不得在文档中自行补充未经检索的条款）"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _number_of(value: Any) -> float:
    return float(value)


def _split_numeric(calc_result: dict[str, Any] | None) -> tuple[dict[str, float], list[tuple[str, str]]]:
    """数值键与备注键分流：数值进 calc_values，其余作为备注行。"""
    values: dict[str, float] = {}
    notes: list[tuple[str, str]] = []
    for key, value in (calc_result or {}).items():
        if _is_number(value):
            values[key] = _number_of(value)
        else:
            text = str(value).strip()
            if text:
                notes.append((key, text))
    return values, notes


def _fmt_number(value: float) -> str:
    """数值展示：≤6 位有效数字，去除尾零（工程常用刻度）。"""
    return f"{value:g}"


def _heading(no: int, name: str) -> str:
    return f"{no} {name}"


@dataclass(slots=True)
class DocSection:
    """一个章节：编号标题 + 行内容（每行一条 bullet/文字，渲染时逐行输出）。"""

    key: str      # 无编号规范短名（评测/结构校验用）
    heading: str  # 完整标题（含编号）
    lines: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RenderedDoc:
    """模板渲染产物：结构化章节 + 数值/引用映射 + Markdown 全文。"""

    doc_type: str
    title: str
    sections: list[DocSection]
    calc_values: dict[str, float] = field(default_factory=dict)
    citation_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # 缺参/错误/假设等备注（未参与数值）

    @property
    def text(self) -> str:
        blocks = [self.title, ""]
        for section in self.sections:
            blocks.append(f"## {section.heading}")
            blocks.append("")
            if section.lines:
                blocks.extend(section.lines)
            blocks.append("")
        return "\n".join(blocks).rstrip() + "\n"

    def section_keys(self) -> list[str]:
        return [section.key for section in self.sections]


def _citation_id(item: Any) -> str:
    """引用入参归一：CitationClaim / {source_id} / 字符串 → source_id。"""
    if hasattr(item, "source_id"):
        return str(item.source_id).strip()
    if isinstance(item, dict):
        return str(item.get("source_id") or item.get("id") or "").strip()
    return str(item).strip()


def _format_material_row(index: int, row: dict[str, Any]) -> str:
    """材料行文本（单一格式化点）：材料统计章节与参数章节内嵌行共用同一形态。

    ``index`` 为行号（1 起）；行文本 = "{n}. {材料} {规格}：{数量} {单位}（来源：{明细}）"。
    materials/material 行字段语义对齐：material/spec/quantity/unit/detail。
    """
    material = row.get("material", "")
    spec = row.get("spec", "")
    quantity = row.get("quantity", "")
    unit = row.get("unit", "")
    detail = row.get("detail", "")
    return f"{index}. {material} {spec}：{quantity} {unit}（来源：{detail}）".strip()


def render_docgen_doc(
    *,
    query: str,
    doc_type: str = "calculation_book",
    calc_result: dict[str, Any] | None = None,
    citations: Sequence[Any] | None = None,
    feedback: str = "",
    draft_no: int | None = None,
    material_rows: Sequence[dict[str, Any]] | None = None,
) -> RenderedDoc:
    """按文档类型渲染结构化文档。

    - ``draft_no``：给出时标题为"XXX草稿（第 N 稿）"，否则为正式标题；
    - ``feedback``：非空时追加"用户反馈（已吸收）"章节（HITL 回流痕迹，可追溯）；
    - ``material_rows``：可选材料统计行（引擎不计算材料，仅展示调用方聚合结果）。
    """
    if doc_type not in SUPPORTED_DOC_TYPES:
        raise ValueError(f"未知文档类型 {doc_type!r}；支持：{SUPPORTED_DOC_TYPES}")
    label = DOC_TYPE_LABELS[doc_type]
    title = f"# {label}草稿（第 {draft_no} 稿）" if draft_no else f"# {label}"

    calc_values, note_items = _split_numeric(calc_result)
    citation_ids: list[str] = []
    for item in citations or []:
        cid = _citation_id(item)
        if cid and cid not in citation_ids:
            citation_ids.append(cid)

    sections: list[DocSection] = []

    # 1 工程概况
    overview = DocSection(key="工程概况", heading=_heading(1, "工程概况"))
    overview.lines.append(f"- 项目 / 查询：{query.strip() or '（未提供）'}")
    overview.lines.append(f"- 文档类型：{label}")
    sections.append(overview)

    # 2 设计依据
    basis = DocSection(key="设计依据", heading=_heading(2, "设计依据"))
    if citation_ids:
        basis.lines += [f"- {cid}" for cid in citation_ids]
    else:
        basis.lines.append(f"- {EMPTY_CITATION_NOTE}")
    sections.append(basis)

    # 3 计算参数与结果（计算书）/ 设计参数与计算结果（设计说明）
    params_heading_name = "计算参数与结果" if doc_type == "calculation_book" else "设计参数与计算结果"
    params = DocSection(key=params_heading_name, heading=_heading(3, params_heading_name))
    if calc_values:
        for key, value in calc_values.items():
            unit = UNIT_MAP.get(key, "")
            suffix = f" {unit}" if unit else ""
            params.lines.append(f"- {key} = {_fmt_number(value)}{suffix}")
    else:
        params.lines.append(f"- {EMPTY_CALC_NOTE}")
    for key, text in note_items:
        label = NOTE_LABELS.get(key, key)
        params.lines.append(f"- {label}：{text}")
    if material_rows:
        params.lines.append("- 材料统计：")
        for index, row in enumerate(material_rows, start=1):
            params.lines.append(f"  {_format_material_row(index, row)}")
    sections.append(params)

    # 4 结论
    conclusion = DocSection(key="结论", heading=_heading(4, "结论"))
    if not calc_values and not calc_result:
        conclusion.lines.append("- 本次未执行程序化计算，文档仅记录设计依据与项目信息。")
    elif not calc_values and calc_result:
        conclusion.lines.append("- 计算未产出数值结果：见上节备注（缺少参数 / 输入非法时不会猜数）。")
    else:
        conclusion.lines.append("- 上表数值均来自程序化计算（LLM 不参与数值运算），供人工复核后确认。")
    sections.append(conclusion)

    # 5 材料统计（可选章节，展示不入数值映射）
    if material_rows:
        materials = DocSection(key="材料统计", heading=_heading(5, "材料统计"))
        for index, row in enumerate(material_rows, start=1):
            materials.lines.append(f"- {_format_material_row(index, row)}")
        sections.append(materials)

    # 反馈（HITL 回流痕迹：拒绝后重拟时随草稿/终稿保留，可追溯）
    if feedback.strip():
        feedback_section = DocSection(key="用户反馈", heading=_heading(5, "用户反馈（已吸收）"))
        feedback_section.lines.append(f"- {feedback.strip()}")
        sections.append(feedback_section)

    notes = [
        f"{NOTE_LABELS.get(key, key)}：{text}"
        for key, text in note_items
    ]
    return RenderedDoc(
        doc_type=doc_type,
        title=title,
        sections=sections,
        calc_values=calc_values,
        citation_ids=citation_ids,
        notes=notes,
    )


def normalize_heading(heading: str) -> str:
    """去编号归一：'3 计算参数与结果' / '3.1 工程概况' → '工程概况'。"""
    text = heading.strip().lstrip("#").strip()
    parts = text.split(" ", 1)
    if len(parts) == 2 and parts[0].replace(".", "", 1).isdigit():
        return parts[1].strip()
    # 兼容 "3、工程概况" / "3.工程概况"
    return re.sub(r"^\s*\d+(?:[.、]\d+)*[.、]?\s*", "", text)


def check_structure(rendered: RenderedDoc) -> tuple[bool, list[str]]:
    """结构合规校验：必需章节是否齐全（按无编号短名匹配）。"""
    present = {normalize_heading(section.heading) for section in rendered.sections}
    required = set(REQUIRED_SECTIONS.get(rendered.doc_type, []))
    missing = sorted(required - present)
    return (not missing, missing)


def _numeric_integrity_problems(
    rendered: RenderedDoc,
    *,
    calc_result: dict[str, Any] | None = None,
) -> list[str]:
    """数值完整性：calc_values 必须全部有限；输入非空时不能既无数值也无备注。

    - 有限性：NaN/inf 永不进入 calc_values（渲染层已挡，此处双保险）；
    - 可见性：calc_result 有输入但渲染后既无数值也无备注 = 输入被静默丢弃
      （如某键值是空串/None），按结构化问题报出而非静默吞掉。
    """
    problems: list[str] = []
    for key, value in rendered.calc_values.items():
        if not math.isfinite(value):
            problems.append(f"数值 {key}={value!r} 非有限（NaN/inf），不得进入数值渲染")
    if calc_result and not rendered.calc_values and not rendered.notes:
        problems.append("calc_result 有输入但渲染后既无数值也无备注：输入被静默丢弃")
    return problems


def check_docgen_self_check(
    rendered: RenderedDoc,
    *,
    calc_result: dict[str, Any] | None = None,
) -> list[str]:
    """草稿自检（draft → format_template → self_check → approval 的 self_check）。

    组合三项确定性检查，返回问题清单（空 = 通过）：
    1. 结构合规：canonical 必需章节齐全（check_structure）；
    2. 引用策略：正文不得出现输入之外的条款（validate_citation_policy，防编造）；
    3. 数值完整性：calc_values 全有限；输入非空时必须可见为数值或备注。
    """
    problems: list[str] = []
    ok, missing = check_structure(rendered)
    if not ok:
        problems.append(f"缺少必需章节：{'、'.join(missing)}")
    problems.extend(validate_citation_policy(rendered))
    problems.extend(_numeric_integrity_problems(rendered, calc_result=calc_result))
    return problems


# 规范/引用编号的规范号形态（首 token）：GB*/FIX*/CJ*/JGJ*/CECS*/DBJ*
_SPEC_TOKEN_RE = re.compile(r"\b((?:GB|FIX|CJ|JGJ|CECS|DBJ)[A-Z0-9\-]{0,30})\b")
# 条款号形态（紧跟规范号之后的数字 token）：1.1.1 / 3.7.6 / 7.1.2.3
_ARTICLE_TOKEN_RE = re.compile(r"\b(\d+(?:\.\d+){1,4})\b")


def _iter_declared_ids(text: str) -> list[str]:
    """从正文提取"规范号 + 条款号"形态的引用串（与 CitationClaim.source_id 同构）。

    只收带条款号的完整引用（GB50015-2019 3.7.6）；正文里单独出现的规范名（如建议语
    里的"GB 50014"）不算声明引用，不做扫描目标。
    """
    ids: list[str] = []
    for spec_match in _SPEC_TOKEN_RE.finditer(text):
        spec = spec_match.group(1)
        tail = text[spec_match.end():]
        article_match = _ARTICLE_TOKEN_RE.match(tail.lstrip())
        if not article_match:
            continue
        candidate = f"{spec} {article_match.group(1)}"
        if candidate not in ids:
            ids.append(candidate)
    return ids


def validate_citation_policy(rendered: RenderedDoc) -> list[str]:
    """引用策略校验：正文不得出现入参之外的 source_id（模板禁止编造条款）。

    返回问题清单（空 = 合规）。正文中任何"规范号 条款号"形态的完整引用都必须在入参
    清单内。用户反馈章节是人工原文的回显（非模板输出），不参与扫描。
    """
    problems: list[str] = []
    allowed = set(rendered.citation_ids)
    for section in rendered.sections:
        if section.key == "用户反馈":
            continue
        for line in section.lines:
            for declared in _iter_declared_ids(line):
                if declared not in allowed:
                    problems.append(f"章节「{section.key}」出现未经声明的引用：{declared}")
    return problems


def available_doc_types() -> list[str]:
    return list(SUPPORTED_DOC_TYPES)
