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
            "这是 DWG 文件（扩展名写成了 .dxf）。网页端暂不支持 DWG，请在 CAD 中另存为 DXF 后上传",
        )
    head = data[:256].lstrip(b"\xef\xbb\xbf").decode("ascii", errors="replace").split()
    if len(head) < 2 or head[0] != "0" or head[1].upper() != "SECTION":
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


def review_uploaded_dxf(data: bytes, *, display_name: str = "") -> DrawingReviewText:
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
    counts = DrawingReviewText(
        text="",
        pipes_reviewed=report.pipes_reviewed,
        findings=len(report.findings),
        not_checked=len(report.not_checked),
        suspicious_entities=sum(1 for e in extraction.entities if e.suspicious),
        truncated=extraction.truncated,
    )
    return DrawingReviewText(
        text=f"{_header(counts)}\n\n{report_text}",
        pipes_reviewed=counts.pipes_reviewed,
        findings=counts.findings,
        not_checked=counts.not_checked,
        suspicious_entities=counts.suspicious_entities,
        truncated=counts.truncated,
    )


__all__ = [
    "DXF_SUFFIX",
    "REVIEW_MAX_ENTITIES",
    "DrawingReviewText",
    "check_dxf_magic",
    "review_uploaded_dxf",
]
