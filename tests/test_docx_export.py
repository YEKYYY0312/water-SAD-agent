"""P5-S48 Step 3: Word (.docx) rendering of calculation books and design notes.

The .docx path must not become a second document engine.  These tests assert the
three docgen disciplines survive the new output format:

- required sections present (checked against ``REQUIRED_SECTIONS``, not literals);
- every number comes from the programmatic ``CalcResult`` -- nothing computed,
  nothing guessed, and conflicting values are refused rather than overwritten;
- no clause id outside the declared citations appears in the body; a
  self-check failure writes **no** file.
"""

from __future__ import annotations

import re
from pathlib import Path

import docx
import pytest

from water_agent.docgen.docx_export import (
    DocxExportError,
    merge_calc_results,
    render_calculation_book_docx,
    render_design_note_docx,
)
from water_agent.docgen.templates import EMPTY_CITATION_NOTE, REQUIRED_SECTIONS, normalize_heading
from water_agent.domain.citation import CitationClaim
from water_agent.tools.hydraulics import CalcResult


def _all_text(path: Path) -> str:
    document = docx.Document(str(path))
    parts = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.extend(cell.text for cell in row.cells)
    return "\n".join(parts)


def _headings(path: Path) -> list[str]:
    document = docx.Document(str(path))
    return [p.text for p in document.paragraphs if p.style.name == "Heading 1"]


def _calc_book(tmp_path: Path, **overrides) -> Path:
    kwargs = {
        "title": "某住宅小区给水干管计算书",
        "input_params": {"flow_lps": 12.5, "diameter_mm": 100},
        "calc_results": [CalcResult(ok=True, values={"velocity_mps": 1.5915})],
        "citations": [CitationClaim(source_id="GB50015-2019 3.7.6", version_status="current")],
        "output_path": tmp_path / "calc.docx",
    }
    kwargs.update(overrides)
    return render_calculation_book_docx(**kwargs)


def test_calculation_book_is_a_valid_docx_with_required_sections(tmp_path: Path) -> None:
    path = _calc_book(tmp_path)
    assert path.read_bytes()[:4] == b"PK\x03\x04"
    headings = {normalize_heading(text) for text in _headings(path)}
    missing = set(REQUIRED_SECTIONS["calculation_book"]) - headings
    assert not missing, f"缺少必需章节：{missing}"
    assert "引用" in headings


def test_calculation_book_numbers_come_only_from_calc_results(tmp_path: Path) -> None:
    path = _calc_book(tmp_path)
    text = _all_text(path)
    assert "1.5915" in text  # 程序化结果原值
    assert "12.5" in text and "100" in text  # 输入参数原值
    # 模板不做运算：输入里没有的派生量不得出现（例如由 Q/A 推出的面积）
    assert "area" not in text.lower()
    assert "m/s" in text


def test_conflicting_values_across_calc_results_are_refused(tmp_path: Path) -> None:
    target = tmp_path / "conflict.docx"
    with pytest.raises(DocxExportError, match="velocity_mps"):
        _calc_book(
            tmp_path,
            calc_results=[
                CalcResult(ok=True, values={"velocity_mps": 1.2}),
                CalcResult(ok=True, values={"velocity_mps": 1.9}),
            ],
            output_path=target,
        )
    assert not target.exists(), "自相矛盾的数值不得落成文件"


def test_identical_values_across_calc_results_merge(tmp_path: Path) -> None:
    merged = merge_calc_results(
        [
            CalcResult(ok=True, values={"velocity_mps": 1.2}, assumptions=["管内满流"]),
            CalcResult(ok=True, values={"velocity_mps": 1.2}, assumptions=["管内满流"]),
        ]
    )
    assert merged == {"velocity_mps": 1.2, "assumptions": "管内满流"}


def test_missing_inputs_are_rendered_not_guessed(tmp_path: Path) -> None:
    path = _calc_book(
        tmp_path,
        input_params={},
        calc_results=[CalcResult(ok=False, missing_inputs=["diameter_mm"])],
    )
    text = _all_text(path)
    assert "diameter_mm" in text
    assert "缺少参数" in text
    assert "velocity_mps" not in text


def test_reference_list_equals_declared_citations(tmp_path: Path) -> None:
    path = _calc_book(
        tmp_path,
        citations=[
            CitationClaim(source_id="GB50015-2019 3.7.6", version_status="current"),
            CitationClaim(source_id="GB50013-2018 7.1.2"),
            CitationClaim(source_id="GB50015-2019 3.7.6"),  # 重复声明只列一次
        ],
    )
    text = _all_text(path)
    refs = re.findall(r"^\[(\d+)\] (\S+ [\d.]+)", text, flags=re.MULTILINE)
    assert refs == [("1", "GB50015-2019 3.7.6"), ("2", "GB50013-2018 7.1.2")]
    assert "版本状态：current" in text


def test_empty_citations_render_the_no_basis_note(tmp_path: Path) -> None:
    text = _all_text(_calc_book(tmp_path, citations=[]))
    assert EMPTY_CITATION_NOTE in text
    assert not re.search(r"GB\d{5}-\d{4} \d", text), "无引用输入时不得出现任何条款号"


def test_design_note_places_caller_sections_and_keeps_required_ones(tmp_path: Path) -> None:
    path = render_design_note_docx(
        title="某厂区排水设计说明",
        sections=[
            {"heading": "排水体制", "content": "本工程采用雨污分流制。\n污水经化粪池后排入市政管网。"},
            {"heading": "空章节", "content": "   "},
        ],
        citations=[CitationClaim(source_id="GB50014-2021 4.1.1")],
        output_path=tmp_path / "note.docx",
    )
    headings = _headings(path)
    names = [normalize_heading(text) for text in headings]
    assert set(REQUIRED_SECTIONS["design_note"]) <= set(names)
    assert names.index("排水体制") == names.index("设计依据") + 1
    assert "空章节" not in names, "空内容章节不渲染"
    # 重排后编号连续
    numbers = [int(text.split(" ", 1)[0]) for text in headings if text[0].isdigit()]
    assert numbers == list(range(1, len(numbers) + 1))
    assert "雨污分流" in _all_text(path)


def test_design_note_with_an_undeclared_clause_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "fabricated.docx"
    with pytest.raises(DocxExportError, match="GB50014-2021 5.2.10"):
        render_design_note_docx(
            title="设计说明",
            sections=[{"heading": "管径", "content": "最小管径按 GB50014-2021 5.2.10 取值。"}],
            citations=[CitationClaim(source_id="GB50014-2021 4.1.1")],
            output_path=target,
        )
    assert not target.exists()


@pytest.mark.parametrize("bad", ["relative/out.docx", "/tmp/../etc/out.docx"])
def test_unsafe_output_paths_are_refused(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        _calc_book(tmp_path, output_path=Path(bad))


def test_page_layout_fonts_and_header(tmp_path: Path) -> None:
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt

    document = docx.Document(str(_calc_book(tmp_path)))
    section = document.sections[0]
    assert abs(section.page_width - Cm(21.0)) < Cm(0.01)
    assert abs(section.page_height - Cm(29.7)) < Cm(0.01)
    for margin in (section.top_margin, section.bottom_margin, section.left_margin, section.right_margin):
        assert abs(margin - Cm(2.54)) < Cm(0.01)

    normal = document.styles["Normal"]
    assert normal.font.name == "Calibri"
    assert normal.font.size == Pt(11)
    assert normal.paragraph_format.line_spacing == 1.5
    heading = document.styles["Heading 1"]
    assert heading.font.size == Pt(14) and heading.font.bold is True
    east_asia = normal.element.rPr.find(qn("w:rFonts")).get(qn("w:eastAsia"))
    assert east_asia, "中文字体必须显式设置，否则不同机器字形不同"

    header_xml = section.header._element.xml
    assert "某住宅小区给水干管计算书" in header_xml
    assert "PAGE" in header_xml, "页眉右侧必须是页码域"
