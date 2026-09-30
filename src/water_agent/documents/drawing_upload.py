"""DXF 上传 → 确定性图纸审查（P5-S50）。

上传一张 DXF 时，这里把它交给 P3 的审查子图（读取 → 映射 → 规则 → 报告，全程
不调模型），把**审查报告**作为这份上传的"抽取文本"交还入库流程。之后的分块、召回、
显式附件读取都复用 S48/S49，一行不改——对模型而言，一张图纸就是一份带条款口径的
审查报告，而不是几千个原始图元（规格 D1/D2）。

路径纪律（规格 D4）
-------------------
审查只在**一个临时目录**里跑，目录里只有一个文件 ``drawing.dxf``：
- 用户文件名（可能是中文、以 ``_`` 开头）过不了 ``drawing_id`` 正则，固定名绕开它；
- root 就是这个临时目录，``resolve_drawing_path`` 的 ``relative_to`` 与
  ``ToolPolicy.read_dir_allowed`` 两道防线原样生效，物理上只能读到这一个文件；
- 临时目录用完即删；原图字节由入库流程照 S48 保留在上传卷上。

报告正文纪律（规格 D7/D8）
---------------------------
报告全文来自 ``render_report_text``——``Finding.rendered_finding()`` 是缺陷表述的唯一
出口，``not_checked`` 是独立小节，``pipes_reviewed == 0`` 时不出现合规措辞。这里**只加
一个说明头**，不改写报告本身。图元里的文字不进报告，只有规则引擎的结论进；命中注入
规则的图元只计数、如实披露。
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from water_agent.documents.errors import (
    CODE_UPLOAD_EXTRACTION_FAILED,
    CODE_UPLOAD_MAGIC_MISMATCH,
    UploadError,
)

DXF_SUFFIX = ".dxf"
#: 审查时读取的图元上限。CLI 默认的 500 在真实图上全部截断（P-02 实测 791 只审
#: 500），报告会系统性少报管段。20,000 覆盖 18 张真实样图中的 17 张，最慢 1.04 s。
REVIEW_MAX_ENTITIES = 20_000
#: 审查时用的登记号 = 临时目录里的固定文件名（不含扩展名）。
_DRAWING_ID = "drawing"

_BINARY_DXF_SENTINEL = b"AutoCAD Binary DXF\r\n\x1a\x00"
#: DWG 文件头：AC1012（R13）… AC1032（R2018+）。
_DWG_PREFIX = b"AC10"


@dataclass(frozen=True, slots=True)
class DrawingReviewText:
    """审查产物：进入库流程的文本 + 供卡片展示的计数。"""

    text: str
    pipes_reviewed: int
    findings: int
    not_checked: int
    suspicious_entities: int
    truncated: bool
    #: 审查结论原文（`ReviewResult.summary()`）与未检查项原文：P6-S1 出图复审原样转交前端，
    #: 不必从报告文本里再解析一遍（报告格式仍只有 `render_report_text` 一处定义）。
    summary: str = ""
    not_checked_items: tuple[str, ...] = ()


def check_dxf_magic(data: bytes) -> None:
    """DXF 没有固定魔数：ASCII 版要求首个组码是 ``0`` 且值为 ``SECTION``。

    只看扩展名会让任意文本冒充图纸；DWG 改名为 ``.dxf`` 单独给出可操作的提示
    （DWG 直读属于 S51）。
    """

    if data.startswith(_BINARY_DXF_SENTINEL):
        return
    if data.startswith(_DWG_PREFIX):
        raise UploadError(
            CODE_UPLOAD_MAGIC_MISMATCH,
            "这是 DWG 文件，但扩展名写成了 .dxf。请把扩展名改回 .dwg 后上传",
        )
    lines = data[:1024].lstrip(b"\xef\xbb\xbf").decode("ascii", errors="replace").splitlines()
    # 组码/值成对出现。开头允许任意条 999 注释（LibreDWG、许多 CAD 导出器都会写一行
    # "999 / LibreDWG 0.14" 之类的注释，属于合法 DXF——S51 实测）。
    pairs = [(lines[i].strip(), lines[i + 1].strip()) for i in range(0, len(lines) - 1, 2)]
    while pairs and pairs[0][0] == "999":
        pairs.pop(0)
    if not pairs or pairs[0][0] != "0" or pairs[0][1].upper() != "SECTION":
        raise UploadError(
            CODE_UPLOAD_MAGIC_MISMATCH,
            "文件内容不是 DXF（文件头应为组码 0 / SECTION）",
        )


def _header(counts: DrawingReviewText) -> str:
    """报告前言。首句是固定格式的计数（前端卡片从 200 字预览里读它，必须在最前）。"""

    susp = (
        f"其中 {counts.suspicious_entities} 个图元文字命中可疑指令规则，已排除在数值推导之外。"
        if counts.suspicious_entities
        else ""
    )
    trunc = (
        f"图元超过 {REVIEW_MAX_ENTITIES}，只审查了前 {REVIEW_MAX_ENTITIES} 个。" if counts.truncated else ""
    )
    return (
        f"【DXF 程序化审查报告】已审查管段 {counts.pipes_reviewed} 条，待核问题 {counts.findings} 条，"
        f"未检查 {counts.not_checked} 项。"
        "以下由规则引擎确定性生成（不调模型），是审查结论而非图纸原文；转述时必须保留\"未检查项\"，"
        f"不得把未检查写成合规。{susp}{trunc}"
    )


#: 图面信息摘要的上限。整张总图的标注文字 ~170 条、去重后 ~130 条，4000 字足够；
#: 再大就挤占审查报告与问题本身的预算。
SUMMARY_MAX_CHARACTERS = 4000
_COORD_RE = re.compile(r"^\s*[AXBY]\s*[=＝]\s*-?\d+(?:\.\d+)?\s*$", re.IGNORECASE)
_ELEV_RE = re.compile(r"^\s*[±+\-]?\d{1,4}\.\d{2,3}\s*(?:m)?\s*$")
_SPEC_RE = re.compile(
    r"(?:DN|De|Φ|φ|%%c)\s*\d+"          # 公称直径 / 直径符号（%%c 是 AutoCAD 的 Φ 转义）
    r"|\bd\s*\d+(?:\.\d+)?\s*[x×X*]\s*\d"  # 外径×壁厚：D219×7、d630X9
    r"|\b(?:Q|H|N|Pg|PN|T)\s*[=＝]\s*\d"  # 设备参数：Q=1250、N=75Kw、Pg=10
    r"|\d+\s*(?:mm|MPa|kPa|m³|m3|L/s|m/h|‰|%)",
    re.IGNORECASE,
)
_CODE_RE = re.compile(r"^[A-Z]{1,4}[-_]?\d{1,3}(?:[-_]\d{1,3})*$")
#: 图签/会签栏的固定字样：对审图没有信息量，还会挤掉真正的标注。只按整条精确匹配
#: （去空白后），不做模糊判断——宁可漏滤，不误删工程标注。
_TITLE_BLOCK = frozenset(
    {
        "会", "签", "审核", "审定", "校对", "设计", "制图", "图号", "张数", "张号", "修改内容", "修改人", "日期", "版次",
        "设计号", "图名", "设计阶段", "比例", "项目", "业主", "项目负责人", "子项名称", "子", "号", "项", "备注", "AUDIT",
        "APPR.", "CHKD.", "PROJ.M.", "DESN.", "DRN.", "DRN.No.", "T.PAGE", "PAGE", "PROJECT",
        "INVESTOR", "DESN.No.", "TITLE", "STAGE", "DATE", "SCALE", "项目:", "业主:", "图名:",
    }
)


def _is_title_block(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    return compact in _TITLE_BLOCK or compact.startswith("[") or bool(re.search(r"[A-Za-z]{4,}.*,", text))


def _drawing_summary(extraction: object) -> str:
    """图面信息摘要：标注文字按类型归组 + 图层/图元/图块计数。

    这是**图纸原文数据**（已过内容护栏、已脱敏），与上面的审查结论分开成节，
    让模型能回答"图上标了哪些构筑物、标高、坐标"，而不是只能说"报告里没有"。
    不做任何推导：不把标高相减算坡度、不把坐标连成管线——那是规则引擎的事，
    缺标高结构化解析时规则如实报"未检查"。命中注入规则的标注不进摘要，只计数。
    """

    from collections import Counter

    entities = getattr(extraction, "entities", ())
    texts: list[str] = []
    for entity in entities:
        if entity.kind.value in ("text", "mtext") and entity.text.strip() and not entity.suspicious:
            texts.append(" ".join(entity.text.split()))
    unique = [text for text in dict.fromkeys(texts) if not _is_title_block(text)]
    groups: dict[str, list[str]] = {"编号": [], "坐标": [], "标高": [], "规格/参数": [], "其他文字": []}
    for text in unique:
        if _COORD_RE.match(text):
            groups["坐标"].append(text)
        elif _ELEV_RE.match(text):
            groups["标高"].append(text)
        elif _CODE_RE.match(text):
            groups["编号"].append(text)
        elif _SPEC_RE.search(text):
            groups["规格/参数"].append(text)
        else:
            groups["其他文字"].append(text)
    kinds = Counter(entity.kind.value for entity in entities)
    blocks = Counter(entity.block_name for entity in entities if entity.block_name)
    layers = [layer for layer in getattr(extraction, "layers", ()) if not layer.startswith("$TD_AUDIT")]
    lines = [
        (
            "【图面信息摘要】以下是图纸上的标注文字与图元统计（图纸原文数据，不是审查结论）。"
            "只列出了文字标注；管线走向、连接关系、尺寸线几何未转写，不得据此推断图面没有的内容。"
        ),
        "图元：" + "，".join(f"{kind} {count}" for kind, count in kinds.most_common()),
    ]
    if layers:
        lines.append(f"图层（{len(layers)} 个）：" + "、".join(layers[:40]))
    if blocks:
        lines.append("图块：" + "、".join(f"{name}×{count}" for name, count in blocks.most_common(20)))
    for name, items in groups.items():
        if items:
            lines.append(f"{name}（{len(items)} 条）：" + "；".join(items))
    rendered = "\n".join(lines)
    if len(rendered) > SUMMARY_MAX_CHARACTERS:
        rendered = rendered[:SUMMARY_MAX_CHARACTERS].rsplit("；", 1)[0] + "；…（摘要超长已截断）"
    return rendered


def review_uploaded_dxf(
    data: bytes, *, display_name: str = "", source_note: str = ""
) -> DrawingReviewText:
    """在只含这一张图的临时目录里跑完整审查，返回报告文本。

    Raises:
        UploadError: 文件头不是 DXF，或图纸无法解析（``UPLOAD_EXTRACTION_FAILED``，
            detail 带原 ``DRAWING_*`` 错误码）。
    """

    from water_agent.drawing.dxf_reader import read_drawing
    from water_agent.drawing.mapping import map_pipes
    from water_agent.drawing.rules import review_drawing
    from water_agent.graphs.drawing_review import _build_report
    from water_agent.runtime.governance.pipeline import ToolPolicy

    check_dxf_magic(data)
    with tempfile.TemporaryDirectory(prefix="wa-dxf-") as tmp:
        root = Path(tmp).resolve()
        (root / f"{_DRAWING_ID}{DXF_SUFFIX}").write_bytes(data)
        policy = ToolPolicy(allowed_read_dirs=frozenset({root}))
        extraction = read_drawing(
            _DRAWING_ID, root=root, policy=policy, max_entities=REVIEW_MAX_ENTITIES
        )
        if not extraction.ok:
            codes = ",".join(extraction.error_codes) or "DRAWING_UNREADABLE"
            raise UploadError(
                CODE_UPLOAD_EXTRACTION_FAILED,
                f"DXF 无法解析（{codes}），请确认文件完整、可在 CAD 中正常打开",
            )
        review = review_drawing(map_pipes(extraction))
        # 与审查子图的 report 节点同一个纯函数：报告格式只有一处定义（P3 Step 22 纪律）。
        report = _build_report(
            {
                "drawing_id": _DRAWING_ID,
                "extraction": extraction,
                "mapping": None,
                "review": review,
                "report": None,
                "status": "",
                "step_count": 0,
            },
            root=root,
        )
        # 报告头的登记号是临时目录里的固定名；换成用户看得懂的文件名（只动这一行）。
        report_text = report.report_text.replace(
            f"== 图纸审查报告：{_DRAWING_ID} ==",
            f"== 图纸审查报告：{display_name or '上传图纸'} ==",
            1,
        )
        summary = _drawing_summary(extraction)
    counts = DrawingReviewText(
        text="",
        pipes_reviewed=report.pipes_reviewed,
        findings=len(report.findings),
        not_checked=len(report.not_checked),
        suspicious_entities=sum(1 for e in extraction.entities if e.suspicious),
        truncated=extraction.truncated,
    )
    return DrawingReviewText(
        text=f"{_header(counts)}{source_note}\n\n{report_text}\n\n{summary}",
        pipes_reviewed=counts.pipes_reviewed,
        findings=counts.findings,
        not_checked=counts.not_checked,
        suspicious_entities=counts.suspicious_entities,
        truncated=counts.truncated,
        summary=report.summary,
        not_checked_items=tuple(report.not_checked),
    )


__all__ = [
    "DXF_SUFFIX",
    "REVIEW_MAX_ENTITIES",
    "DrawingReviewText",
    "check_dxf_magic",
    "review_uploaded_dxf",
]
