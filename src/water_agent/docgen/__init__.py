"""文档生成模板引擎（P2）：计算书 / 设计说明的结构化确定性渲染。

供 docgen 图（graphs/docgen.py）、机制层评测与后续 LLM 润色共用；
数值与引用均由调用方输入，模板不运算、不编造。
"""

from water_agent.docgen.templates import (
    DOC_TYPE_LABELS,
    EMPTY_CALC_NOTE,
    EMPTY_CITATION_NOTE,
    NOTE_LABELS,
    REQUIRED_SECTIONS,
    SUPPORTED_DOC_TYPES,
    UNIT_MAP,
    DocSection,
    RenderedDoc,
    check_docgen_self_check,
    check_structure,
    normalize_heading,
    render_docgen_doc,
    validate_citation_policy,
)

__all__ = [
    "DOC_TYPE_LABELS",
    "EMPTY_CALC_NOTE",
    "EMPTY_CITATION_NOTE",
    "NOTE_LABELS",
    "REQUIRED_SECTIONS",
    "SUPPORTED_DOC_TYPES",
    "UNIT_MAP",
    "DocSection",
    "RenderedDoc",
    "check_docgen_self_check",
    "check_structure",
    "normalize_heading",
    "render_docgen_doc",
    "validate_citation_policy",
]
