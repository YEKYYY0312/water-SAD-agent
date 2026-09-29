"""Word 公式（OMML）抽取：python-docx 的 paragraph.text 会整段跳过 m:oMath。

实测 137400 计算说明书 114 个公式全部丢失，模型据此答"关键数值只有公式没有结果"。
"""

from __future__ import annotations

import io

from lxml import etree

from water_agent.documents.omml import M, W, omml_to_text, paragraph_text

_NS = f'xmlns:m="{M[1:-1]}" xmlns:w="{W[1:-1]}"'


def _el(xml: str):
    return etree.fromstring(f"<root {_NS}>{xml}</root>")[0]


def _r(text: str) -> str:
    return f"<m:r><m:t>{text}</m:t></m:r>"


def test_fraction_subscript_root_and_function_are_linearised() -> None:
    omath = _el(
        "<m:oMath>" + _r("n=")
        + "<m:f><m:num><m:sSub><m:e>" + _r("Q") + "</m:e><m:sub>" + _r("max") + "</m:sub></m:sSub>"
        + "<m:rad><m:radPr><m:degHide m:val='1'/></m:radPr><m:deg/><m:e><m:func><m:fName>" + _r("sin")
        + "</m:fName><m:e>" + _r("α") + "</m:e></m:func></m:e></m:rad></m:num>"
        + "<m:den>" + _r("bhv") + "</m:den></m:f>" + _r("=54.9") + "</m:oMath>"
    )
    assert omml_to_text(omath) == "n=(Q_(max)√(sin(α)))/(bhv)=54.9"


def test_numbers_are_kept_verbatim_never_evaluated() -> None:
    omath = _el("<m:oMath>" + _r("B=0.01×(60−1)+0.05×60+0.2=3.34m") + "</m:oMath>")
    assert omml_to_text(omath) == "B=0.01×(60−1)+0.05×60+0.2=3.34m"


def test_superscript_and_delimiters() -> None:
    omath = _el(
        "<m:oMath><m:sSup><m:e><m:d><m:e>" + _r("s/b") + "</m:e></m:d></m:e><m:sup>"
        + _r("4/3") + "</m:sup></m:sSup></m:oMath>"
    )
    assert omml_to_text(omath) == "(s/b)^(4/3)"


def test_paragraph_keeps_formula_in_reading_order() -> None:
    paragraph = _el(
        "<w:p><w:r><w:t>栅前渠道超高 </w:t></w:r>"
        "<m:oMath><m:sSub><m:e>" + _r("h") + "</m:e><m:sub>" + _r("2") + "</m:sub></m:sSub>" + _r("=0.3m") + "</m:oMath>"
        "<w:r><w:t>，栅后槽总高度：</w:t></w:r></w:p>"
    )
    assert paragraph_text(paragraph) == "栅前渠道超高 【公式】h_2=0.3m，栅后槽总高度："


def test_docx_extraction_includes_formulas_and_reports_ole_objects() -> None:
    import docx
    from docx.oxml import parse_xml

    from water_agent.documents.extractors import extract_text

    document = docx.Document()
    paragraph = document.add_paragraph("格栅间隙数：")
    paragraph._p.append(parse_xml(f"<m:oMath {_NS}>" + _r("n=54.9") + "</m:oMath>"))
    document.add_paragraph()._p.append(parse_xml(f"<w:r {_NS}><w:object/></w:r>"))
    buffer = io.BytesIO()
    document.save(buffer)
    extracted = extract_text(buffer.getvalue(), ".docx")
    assert "格栅间隙数：【公式】n=54.9" in extracted.text
    assert "另含 1 个 OLE 嵌入对象" in extracted.text
    assert extracted.engine == "python-docx+omml"
