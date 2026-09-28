"""计算书 / 设计说明的 Word（.docx）渲染（P5-S48 Step 3）。

**本模块是 ``RenderedDoc`` 的渲染器，不是第二个文档引擎。** 结构、数值与引用一律先经
``docgen/templates.py::render_docgen_doc()`` 产出，再交给这里排版。这样三条纪律仍只有
一个来源、不会在 Markdown 与 Word 两条路径之间漂移：

- 必需章节齐全（``REQUIRED_SECTIONS``）；
- 数值只来自调用方给出的程序化计算结果（``calc_values``），这里不运算、不补数；
- 正文不得出现入参之外的「规范号 条款号」（``validate_citation_policy``）。

写盘前跑 ``check_docgen_self_check``，**不通过就不写文件**（抛 :class:`DocxExportError`）：
一份自检失败的计算书以 .docx 形式流出去，比没有文件更糟。

多个 ``CalcResult`` 的同名数值不一致时**报错而不是后者覆盖前者**——静默覆盖就是
"LLM 不参与数值"之外另一种无声改数。

已知边界：``CalcResult`` 不携带推导过程，所以"计算过程"一节只列程序化结果与公式口径
备注，**不生成推导步骤**（模板不编造计算过程）。样式只做到"能打开、内容正确"
（spec R2），美观迭代留到后续。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from water_agent.docgen.templates import (
    UNIT_MAP,
    DocSection,
    RenderedDoc,
    _fmt_number,
    _is_number,
    check_docgen_self_check,
    normalize_heading,
    render_docgen_doc,
)
from water_agent.domain.citation import CitationClaim
from water_agent.materials.export import _validate_export_path
from water_agent.tools.hydraulics import CalcResult

BODY_FONT = "Calibri"
#: Calibri 没有中文字形；不显式设 eastAsia，Word 会按主题字体随机回落，同一份文档在
#: 不同机器上字形不同。
EAST_ASIA_FONT = "宋体"
BODY_SIZE_PT = 11
HEADING_SIZE_PT = 14
LINE_SPACING = 1.5
MARGIN_CM = 2.54

_DOCX_INSTALL_HINT = "导出 .docx 需要 python-docx：安装 water-agent 依赖后重试"


class DocxExportError(ValueError):
    """文档未通过渲染前自检，或输入自相矛盾。``problems`` 为可行动的问题清单。"""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        super().__init__("；".join(self.problems) or "docx 导出失败")


def _citation_ids(citations: Sequence[CitationClaim | dict[str, Any] | str]) -> list[str]:
    seen: list[str] = []
    for item in citations:
        if isinstance(item, CitationClaim):
            cid = item.source_id
        elif isinstance(item, dict):
            cid = str(item.get("source_id") or "")
        else:
            cid = str(item)
        cid = cid.strip()
        if cid and cid not in seen:
            seen.append(cid)
    return seen


def _version_status(citations: Sequence[Any]) -> dict[str, str]:
    status: dict[str, str] = {}
    for item in citations:
        if isinstance(item, CitationClaim) and item.version_status:
            status[item.source_id] = item.version_status
    return status


def merge_calc_results(calc_results: Sequence[CalcResult]) -> dict[str, Any]:
    """把多个 ``CalcResult`` 合成 ``render_docgen_doc`` 的 ``calc_result`` 入参。

    数值键冲突且值不同 → :class:`DocxExportError`。备注类（缺参/错误/假设）合并去重。
    """

    values: dict[str, float] = {}
    conflicts: list[str] = []
    missing: list[str] = []
    errors: list[str] = []
    assumptions: list[str] = []
    for result in calc_results:
        for key, value in result.values.items():
            if key in values and values[key] != value:
                conflicts.append(f"数值 {key} 在多个计算结果中不一致（{values[key]!r} ≠ {value!r}）")
                continue
            values[key] = value
        missing.extend(item for item in result.missing_inputs if item not in missing)
        errors.extend(item for item in result.errors if item not in errors)
        assumptions.extend(item for item in result.assumptions if item not in assumptions)
    if conflicts:
        raise DocxExportError(conflicts)
    merged: dict[str, Any] = dict(values)
    if missing:
        merged["missing_inputs"] = "、".join(missing)
    if errors:
        merged["errors"] = "；".join(errors)
    if assumptions:
        merged["assumptions"] = "；".join(assumptions)
    return merged


def _renumber(sections: list[DocSection]) -> list[DocSection]:
    """插入章节后按顺序重排编号（"1 工程概况"…），键不变。"""

    renumbered: list[DocSection] = []
    for index, section in enumerate(sections, start=1):
        name = normalize_heading(section.heading)
        renumbered.append(DocSection(key=section.key, heading=f"{index} {name}", lines=section.lines))
    return renumbered


def _set_run_font(run: Any, *, size_pt: int, bold: bool = False) -> None:
    from docx.oxml.ns import qn
    from docx.shared import Pt

    run.font.name = BODY_FONT
    run.font.size = Pt(size_pt)
    run.font.bold = bold
    r_pr = run._element.get_or_add_rPr()
    r_fonts = r_pr.find(qn("w:rFonts"))
    if r_fonts is None:
        r_fonts = r_pr.makeelement(qn("w:rFonts"), {})
        r_pr.append(r_fonts)
    r_fonts.set(qn("w:eastAsia"), EAST_ASIA_FONT)


def _configure_styles(document: Any) -> None:
    from docx.enum.text import WD_LINE_SPACING
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor

    for section in document.sections:
        section.page_width = Cm(21.0)
        section.page_height = Cm(29.7)
        section.top_margin = section.bottom_margin = Cm(MARGIN_CM)
        section.left_margin = section.right_margin = Cm(MARGIN_CM)

    for style_name, size, bold in (
        ("Normal", BODY_SIZE_PT, False),
        ("Title", HEADING_SIZE_PT, True),
        ("Heading 1", HEADING_SIZE_PT, True),
    ):
        style = document.styles[style_name]
        style.font.name = BODY_FONT
        style.font.size = Pt(size)
        style.font.bold = bold
        style.font.color.rgb = RGBColor(0, 0, 0)
        r_pr = style.element.get_or_add_rPr()
        r_fonts = r_pr.find(qn("w:rFonts"))
        if r_fonts is None:
            r_fonts = r_pr.makeelement(qn("w:rFonts"), {})
            r_pr.append(r_fonts)
        r_fonts.set(qn("w:eastAsia"), EAST_ASIA_FONT)
        # 主题字体属性优先级高于 w:ascii，不删掉的话 Heading 仍走主题字体。
        for attribute in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme"):
            r_fonts.attrib.pop(qn(attribute), None)
        paragraph_format = style.paragraph_format
        paragraph_format.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
        paragraph_format.line_spacing = LINE_SPACING


def _add_page_field(paragraph: Any) -> None:
    """插入 PAGE 域（Word 打开时计算页码）。"""

    from docx.oxml.ns import qn

    run = paragraph.add_run()
    for tag, attrs, text in (
        ("w:fldChar", {qn("w:fldCharType"): "begin"}, None),
        ("w:instrText", {qn("xml:space"): "preserve"}, " PAGE "),
        ("w:fldChar", {qn("w:fldCharType"): "end"}, None),
    ):
        element = run._element.makeelement(qn(tag), attrs)
        if text is not None:
            element.text = text
        run._element.append(element)
    _set_run_font(run, size_pt=9)


def _configure_header(document: Any, project_name: str) -> None:
    """页眉：项目名（左）+ 页码（右，右对齐制表位）。"""

    from docx.enum.text import WD_TAB_ALIGNMENT
    from docx.shared import Cm

    for section in document.sections:
        header = section.header
        paragraph = header.paragraphs[0] if header.paragraphs else header.add_paragraph()
        usable = section.page_width - section.left_margin - section.right_margin
        paragraph.paragraph_format.tab_stops.add_tab_stop(usable or Cm(15.92), WD_TAB_ALIGNMENT.RIGHT)
        run = paragraph.add_run(project_name or "给排水 Agent")
        _set_run_font(run, size_pt=9)
        tab = paragraph.add_run("\t第 ")
        _set_run_font(tab, size_pt=9)
        _add_page_field(paragraph)
        tail = paragraph.add_run(" 页")
        _set_run_font(tail, size_pt=9)


def _add_heading(document: Any, text: str, *, style: str = "Heading 1") -> None:
    paragraph = document.add_paragraph(style=style)
    run = paragraph.add_run(text)
    _set_run_font(run, size_pt=HEADING_SIZE_PT, bold=True)


def _add_body(document: Any, text: str) -> None:
    paragraph = document.add_paragraph(style="Normal")
    run = paragraph.add_run(text)
    _set_run_font(run, size_pt=BODY_SIZE_PT)


def _add_table(document: Any, header: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    table = document.add_table(rows=1 + len(rows), cols=len(header))
    table.style = "Table Grid"
    for col, text in enumerate(header):
        cell = table.cell(0, col)
        cell.text = ""
        _set_run_font(cell.paragraphs[0].add_run(text), size_pt=BODY_SIZE_PT, bold=True)
    for row_index, row in enumerate(rows, start=1):
        for col, text in enumerate(row):
            cell = table.cell(row_index, col)
            cell.text = ""
            _set_run_font(cell.paragraphs[0].add_run(text), size_pt=BODY_SIZE_PT)


def _value_text(value: Any) -> str:
    return _fmt_number(float(value)) if _is_number(value) else str(value)


def _write_docx(
    rendered: RenderedDoc,
    *,
    title: str,
    output_path: Path,
    citations: Sequence[Any],
    input_params: dict[str, Any] | None,
    values_table: bool,
) -> Path:
    try:
        import docx  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover -- 依赖缺失兜底
        raise ImportError(_DOCX_INSTALL_HINT) from exc

    destination = _validate_export_path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    document = docx.Document()
    _configure_styles(document)
    _configure_header(document, title)
    _add_heading(document, title or rendered.title.lstrip("# "), style="Title")

    params_key = "计算参数与结果" if rendered.doc_type == "calculation_book" else "设计参数与计算结果"
    for section in rendered.sections:
        _add_heading(document, section.heading)
        if section.key == params_key:
            if input_params is not None:
                _add_body(document, "输入参数：")
                if input_params:
                    _add_table(
                        document,
                        ["参数", "数值", "单位"],
                        [
                            [key, _value_text(value), UNIT_MAP.get(key, "")]
                            for key, value in input_params.items()
                        ],
                    )
                else:
                    _add_body(document, "（本次运行未记录计算输入参数）")
            if values_table and rendered.calc_values:
                _add_body(document, "程序化计算结果：")
                _add_table(
                    document,
                    ["参数", "数值", "单位"],
                    [
                        [key, _fmt_number(value), UNIT_MAP.get(key, "")]
                        for key, value in rendered.calc_values.items()
                    ],
                )
                # 数值已在表中；其余行（备注、缺参说明）照原文输出，不重复数值行。
                lines = [line for line in section.lines if not any(
                    line.startswith(f"- {key} = ") for key in rendered.calc_values
                )]
            else:
                lines = section.lines
        else:
            lines = section.lines
        for line in lines:
            _add_body(document, line)

    ids = rendered.citation_ids
    _add_heading(document, "引用")
    if ids:
        status = _version_status(citations)
        for index, cid in enumerate(ids, start=1):
            suffix = f"（版本状态：{status[cid]}）" if cid in status else ""
            _add_body(document, f"[{index}] {cid}{suffix}")
    else:
        _add_body(document, "（未声明规范依据）")

    document.save(str(destination))
    return destination


def render_calculation_book_docx(
    title: str,
    input_params: dict[str, Any],
    calc_results: list[CalcResult],
    citations: list[CitationClaim],
    output_path: Path,
) -> Path:
    """计算书：标题 → 工程概况 → 设计依据 → 输入参数表 + 程序化结果表 → 结论 → 引用。

    Raises:
        DocxExportError: 数值冲突或自检未通过（此时不写文件）。
        ValueError: ``output_path`` 非绝对路径或含 ``..``。
    """

    calc_result = merge_calc_results(calc_results)
    rendered = render_docgen_doc(
        query=title,
        doc_type="calculation_book",
        calc_result=calc_result or None,
        citations=_citation_ids(citations),
    )
    problems = check_docgen_self_check(rendered, calc_result=calc_result or None)
    if problems:
        raise DocxExportError(problems)
    return _write_docx(
        rendered,
        title=title,
        output_path=output_path,
        citations=citations,
        input_params=input_params,
        values_table=True,
    )


def render_design_note_docx(
    title: str,
    sections: list[dict[str, str]],
    citations: list[CitationClaim],
    output_path: Path,
) -> Path:
    """设计说明：标准四章节 + 调用方给出的正文章节（插在"设计依据"之后）→ 引用。

    调用方章节同样过 ``validate_citation_policy``：正文里出现入参之外的条款号即拒绝导出。
    """

    rendered = render_docgen_doc(
        query=title,
        doc_type="design_note",
        citations=_citation_ids(citations),
    )
    extra: list[DocSection] = []
    for index, item in enumerate(sections, start=1):
        heading = str(item.get("heading") or "").strip() or f"正文 {index}"
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        lines = [line for line in content.splitlines() if line.strip()]
        extra.append(DocSection(key=f"正文:{heading}", heading=f"0 {heading}", lines=lines))
    # 插在"设计依据"之后、"设计参数与计算结果"之前：读者先看依据再看正文。
    basis_index = next(
        (i for i, section in enumerate(rendered.sections) if section.key == "设计依据"), 1
    )
    merged = rendered.sections[: basis_index + 1] + extra + rendered.sections[basis_index + 1 :]
    rendered.sections = _renumber(merged)

    # check_docgen_self_check 已含 validate_citation_policy，调用方章节因此同样受
    # "正文不得出现入参之外的条款号"约束。
    problems = check_docgen_self_check(rendered)
    if problems:
        raise DocxExportError(problems)
    return _write_docx(
        rendered,
        title=title,
        output_path=output_path,
        citations=citations,
        input_params=None,
        values_table=False,
    )


__all__ = [
    "DocxExportError",
    "merge_calc_results",
    "render_calculation_book_docx",
    "render_design_note_docx",
]
