"""把几何规划翻译成 DXF（P6-S1 Step 4）。**只做翻译，不做任何几何或数值推导。**

规划（`layout_engine` / `system_schematic` / `detail_drawings`）给出的每个坐标、每段文字
原样落成图元：管段 → LWPOLYLINE，管径标注 → TEXT（BOTTOM_CENTER），附件 → INSERT，
说明 → MTEXT。渲染层不重新排版、不补任何数字。

格式：R2010（AC1024），``$INSUNITS = 4``（毫米）。文字样式 ``WS-FS``：主字体
``simfang.ttf``（仿宋），大字体 ``gbcbig.shx``。CAD 找不到字体只会替换显示、不会报错，
所以字体文件名与扩展字体族名（``仿宋``）都写上；大字体字段只对 SHX 主字体生效，
主字体为 TTF 时由 CAD 的替换字体机制兜底——**CAD 里的实际显示效果未实测**。

输出**必须**能被 ``ezdxf.readfile`` 与本仓库 ``read_drawing`` 直接打开（不走 recover），
由 `generation_service` 在交付前逐张读回核对。

代码页一律 ``$DWGCODEPAGE = ANSI_936``（GBK）。R2010 的 DXF 正文本身是 UTF-8，这个头
只影响老版本读写方；但实验性 DWG 输出必须用它：LibreDWG 的 ``dxf2dwg`` 把 R2010 的
UTF-8 字节**原样**塞进 R2000 DWG、不做转码（实测回读后中文全部乱码），所以 DWG 的
源文件另渲染一份 R2000 + GBK 编码的 DXF。
"""

from __future__ import annotations

import io
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from water_agent.drawing.blocks import BLOCK_NAMES, define_blocks
from water_agent.drawing.generation_models import (
    LAYER_AXIS,
    LAYER_DIM,
    LAYER_METER,
    LAYER_OUTLINE,
    LAYER_PIPE_BRANCH,
    LAYER_PIPE_MAIN,
    LAYER_TEXT,
    LAYER_VALVE,
    PIPE_LAYERS,
    SheetPlan,
)
from water_agent.drawing.layout_engine import WIDTH_FACTOR
from water_agent.drawing.models import DrawingEntityKind, DrawingExtraction

DXF_VERSION: Literal["R2010"] = "R2010"
DXF_VERSION_CODE = "AC1024"
#: 实验性 DWG 的源文件版本（LibreDWG 写 DWG 只稳定支持 R2000）。
DWG_SOURCE_VERSION: Literal["R2000"] = "R2000"
#: Python 编码名；ezdxf 据此写 ``$DWGCODEPAGE = ANSI_936``（``cp936`` 会被映射成 ANSI_1252，已实测）。
DXF_ENCODING = "gbk"
INSUNITS_MM = 4
TEXT_STYLE = "WS-FS"
FONT_FILE = "simfang.ttf"
BIGFONT_FILE = "gbcbig.shx"
FONT_FAMILY = "仿宋"
#: 线型比例 = 出图比例分母 × 0.3：点划线在图纸上约 9.5 mm 一段。
LTSCALE_FACTOR = 0.3

#: (ACI 颜色, 线宽 1/100 mm, 线型)。颜色只为屏幕区分，出图一般按线宽打印。
LAYER_STYLE: dict[str, tuple[int, int, str]] = {
    LAYER_PIPE_MAIN: (3, 50, "Continuous"),
    LAYER_PIPE_BRANCH: (3, 35, "Continuous"),
    LAYER_VALVE: (6, 25, "Continuous"),
    LAYER_METER: (6, 25, "Continuous"),
    LAYER_TEXT: (7, 18, "Continuous"),
    LAYER_DIM: (7, 18, "Continuous"),
    LAYER_AXIS: (1, 18, "CENTER"),
    LAYER_OUTLINE: (8, 25, "Continuous"),
}

#: acadiso.lin 的毫米图案（图纸毫米）：[总长, 实线, 空, ...]。
LINETYPES: dict[str, tuple[str, list[float]]] = {
    "CENTER": ("Center ____ _ ____ _ ____", [50.8, 31.75, -6.35, 6.35, -6.35]),
    "DASHED": ("Dashed __ __ __ __ __", [19.05, 12.7, -6.35]),
}


class RenderError(RuntimeError):
    """渲染失败（ezdxf 缺失或规划数据无法翻译）。对外一律 ``GEN_RENDER_FAILED``。"""


@dataclass(frozen=True, slots=True)
class RenderedSheet:
    sheet_id: str
    data: bytes
    #: 规划管段 id → DXF handle：读回核对时按 handle 找回每条管段。
    pipe_handles: dict[str, str] = field(default_factory=dict)
    #: 模型空间顶层图元数（不含块展开）。
    entity_count: int = 0


def _setup(doc: Any, scale: int) -> None:
    from ezdxf import units

    doc.units = units.MM
    doc.header["$MEASUREMENT"] = 1
    doc.header["$LUNITS"] = 2
    doc.header["$LTSCALE"] = round(scale * LTSCALE_FACTOR, 3)
    for name, (description, pattern) in LINETYPES.items():
        if name not in doc.linetypes:
            doc.linetypes.add(name, pattern=pattern, description=description)
    style = doc.styles.add(TEXT_STYLE, font=FONT_FILE)
    style.dxf.bigfont = BIGFONT_FILE
    style.dxf.width = WIDTH_FACTOR
    style.set_extended_font_data(family=FONT_FAMILY)
    doc.header["$TEXTSTYLE"] = TEXT_STYLE
    for layer, (color, lineweight, linetype) in LAYER_STYLE.items():
        doc.layers.add(layer, color=color, lineweight=lineweight, linetype=linetype)
    define_blocks(doc)


DxfVersion = Literal["R2010", "R2000"]


def render_sheet(plan: SheetPlan, *, dxf_version: DxfVersion = "R2010") -> RenderedSheet:
    """一张规划 → 一份 DXF 字节（默认 R2010；R2000 只用作实验性 DWG 的源文件）。"""

    try:
        import ezdxf
        from ezdxf.enums import MTextEntityAlignment, TextEntityAlignment
    except ImportError as exc:  # pragma: no cover - ezdxf 是运行时依赖
        raise RenderError("缺少 ezdxf，无法写 DXF") from exc

    doc = ezdxf.new(dxf_version)
    doc.encoding = DXF_ENCODING
    _setup(doc, plan.scale)
    msp = doc.modelspace()
    handles: dict[str, str] = {}

    for run in plan.pipes:
        entity = msp.add_lwpolyline(list(run.points), dxfattribs={"layer": run.layer})
        handles[run.run_id] = str(entity.dxf.handle)

    for label in plan.labels:
        text = msp.add_text(
            label.text,
            height=label.height,
            rotation=label.rotation,
            dxfattribs={"layer": LAYER_TEXT, "style": TEXT_STYLE, "width": WIDTH_FACTOR},
        )
        text.set_placement(label.anchor, align=TextEntityAlignment.BOTTOM_CENTER)

    for symbol in plan.symbols:
        msp.add_blockref(
            BLOCK_NAMES[symbol.symbol],
            symbol.point,
            dxfattribs={
                "layer": symbol.layer,
                "xscale": symbol.scale,
                "yscale": symbol.scale,
                "rotation": symbol.rotation,
            },
        )

    for item in plan.texts:
        text = msp.add_text(
            item.text,
            height=item.height,
            rotation=item.rotation,
            dxfattribs={"layer": item.layer, "style": TEXT_STYLE, "width": WIDTH_FACTOR},
        )
        text.set_placement(item.point, align=TextEntityAlignment[item.align])

    for line in plan.lines:
        attribs: dict[str, object] = {"layer": line.layer}
        if line.linetype:
            attribs["linetype"] = line.linetype
        if len(line.points) == 2 and not line.closed:
            msp.add_line(line.points[0], line.points[1], dxfattribs=attribs)
        else:
            msp.add_lwpolyline(list(line.points), close=line.closed, dxfattribs=attribs)

    for circle in plan.circles:
        msp.add_circle(circle.center, circle.radius, dxfattribs={"layer": circle.layer})

    for note in plan.notes:
        if dxf_version == DWG_SOURCE_VERSION:
            # R2000 的 MTEXT 正文被 ezdxf 按 250 个字符分段写出，GBK 下每段超过 255 字节，
            # LibreDWG 会把段尾截坏（实测中文乱码）。DWG 的源文件改为逐行单行文字。
            spacing = note.height * 5.0 / 3.0
            for index, content in enumerate(note.lines):
                text = msp.add_text(
                    content,
                    height=note.height,
                    dxfattribs={"layer": note.layer, "style": TEXT_STYLE, "width": WIDTH_FACTOR},
                )
                text.set_placement(
                    (note.point[0], note.point[1] - note.height - index * spacing),
                    align=TextEntityAlignment.LEFT,
                )
            continue
        mtext = msp.add_mtext(
            note.text, dxfattribs={"layer": note.layer, "style": TEXT_STYLE, "char_height": note.height}
        )
        mtext.set_location(note.point, attachment_point=MTextEntityAlignment.TOP_LEFT)

    stream = io.StringIO()
    doc.write(stream)
    return RenderedSheet(
        sheet_id=plan.sheet_id,
        data=stream.getvalue().encode(doc.output_encoding),
        pipe_handles=handles,
        entity_count=len(msp),
    )


def render(plan: SheetPlan) -> bytes:
    """执行 prompt 约定的入口：规划 → DXF 字节。"""

    return render_sheet(plan).data


# ---------------------------------------------------------------------------
# 读回核对：生成图必须能被本仓库自己的读取器与映射层正确读懂
# ---------------------------------------------------------------------------

_READBACK_ID = "sheet"
_PIPE_KINDS = frozenset({DrawingEntityKind.LINE, DrawingEntityKind.POLYLINE})


def read_back(data: bytes) -> DrawingExtraction:
    """在只含这一张图的临时目录里用 ``read_drawing`` 读回（与 S50 上传审查同一路径纪律）。"""

    from water_agent.documents.drawing_upload import REVIEW_MAX_ENTITIES
    from water_agent.drawing.dxf_reader import read_drawing
    from water_agent.runtime.governance.pipeline import ToolPolicy

    with tempfile.TemporaryDirectory(prefix="wa-gen-") as tmp:
        root = Path(tmp).resolve()
        (root / f"{_READBACK_ID}.dxf").write_bytes(data)
        policy = ToolPolicy(allowed_read_dirs=frozenset({root}))
        return read_drawing(_READBACK_ID, root=root, policy=policy, max_entities=REVIEW_MAX_ENTITIES)


def _pipe_entities(extraction: DrawingExtraction) -> list:
    return [e for e in extraction.entities if e.layer in PIPE_LAYERS and e.kind in _PIPE_KINDS]


@dataclass(frozen=True, slots=True)
class ReadbackReport:
    """一张图的读回结果。``verified`` 为真才允许交付。"""

    ok: bool
    errors: tuple[str, ...]
    truncated: bool
    entity_count: int
    layers: tuple[str, ...]
    #: ``WS-PIPE-*`` 图层上读回的管段（LINE / LWPOLYLINE）数。
    pipe_segments: int
    planned_segments: int
    #: 规划管段里，按 handle 找回且 ``map_pipes`` 读出的 ``dn_mm`` 与规划一致的条数。
    dn_matched: int
    dn_mismatches: tuple[str, ...]
    suspicious_entities: int

    @property
    def verified(self) -> bool:
        return (
            self.ok
            and not self.truncated
            and self.pipe_segments == self.planned_segments == self.dn_matched
            and self.suspicious_entities == 0
        )


def verify_readback(rendered: RenderedSheet, plan: SheetPlan) -> ReadbackReport:
    from water_agent.drawing.mapping import map_pipes

    extraction = read_back(rendered.data)
    mapped = {pipe.handle: pipe for pipe in map_pipes(extraction).pipes}
    mismatches: list[str] = []
    for run in plan.pipes:
        pipe = mapped.get(rendered.pipe_handles.get(run.run_id, ""))
        if pipe is None or pipe.dn_mm != run.dn.mm:
            mismatches.append(run.run_id)
    return ReadbackReport(
        ok=extraction.ok,
        errors=extraction.errors,
        truncated=extraction.truncated,
        entity_count=len(extraction.entities),
        layers=extraction.layers,
        pipe_segments=len(_pipe_entities(extraction)),
        planned_segments=len(plan.pipes),
        dn_matched=len(plan.pipes) - len(mismatches),
        dn_mismatches=tuple(mismatches),
        suspicious_entities=sum(1 for e in extraction.entities if e.suspicious),
    )


# ---------------------------------------------------------------------------
# 实验性 DWG 输出（LibreDWG dxf2dwg → dwg2dxf 回读核对）
# ---------------------------------------------------------------------------

DwgStatus = Literal["ok", "discarded", "unavailable", "failed"]


@dataclass(frozen=True, slots=True)
class DwgExport:
    status: DwgStatus
    reason: str
    data: bytes | None = None
    converter_warnings: int = 0
    readback_pipe_segments: int | None = None


def _plan_text_lines(plan: SheetPlan) -> Counter[str]:
    """规划里全部文字，按行计（说明块拆成行）。"""

    lines = [label.text for label in plan.labels] + [text.text for text in plan.texts]
    lines += [line for note in plan.notes for line in note.lines]
    return Counter(lines)


def export_dwg(plan: SheetPlan) -> DwgExport:
    """规划 → R2000/GBK DXF → DWG，再用 dwg2dxf 读回，核对管段数、管径与文字；不一致就丢弃 DWG。"""

    from water_agent.documents.dwg_convert import (
        DwgWriteError,
        convert_dwg,
        convert_dxf_to_dwg,
        dwg_roundtrip_available,
    )
    from water_agent.documents.errors import UploadError
    from water_agent.drawing.mapping import map_pipes

    if not dwg_roundtrip_available():
        return DwgExport("unavailable", "服务器未安装 LibreDWG（dxf2dwg / dwg2dxf），只交付 DXF")
    source = render_sheet(plan, dxf_version=DWG_SOURCE_VERSION)
    try:
        written = convert_dxf_to_dwg(source.data)
    except DwgWriteError as exc:
        return DwgExport("failed", f"{exc}；只交付 DXF")
    try:
        back = convert_dwg(written.dwg)
    except UploadError as exc:
        return DwgExport("discarded", f"LibreDWG 回读失败（{exc.message}），DWG 已丢弃，只交付 DXF")
    extraction = read_back(back.dxf)
    warnings = written.warnings + back.warnings
    if not extraction.ok:
        return DwgExport(
            "discarded", "回读的 DXF 无法解析，DWG 已丢弃，只交付 DXF", converter_warnings=warnings
        )
    pipes = _pipe_entities(extraction)
    problems: list[str] = []
    if len(pipes) != len(plan.pipes):
        problems.append(f"管段数 {len(pipes)} ≠ DXF 版 {len(plan.pipes)}")
    read_dn = Counter(p.dn_mm for p in map_pipes(extraction).pipes if p.layer in PIPE_LAYERS)
    if read_dn != Counter(run.dn.mm for run in plan.pipes):
        problems.append("管径读回与 DXF 版不一致")
    read_texts: Counter[str] = Counter()
    for entity in extraction.entities:
        if entity.source_block is None and entity.kind in (DrawingEntityKind.TEXT, DrawingEntityKind.MTEXT):
            read_texts.update(entity.text.split("\n"))
    if read_texts != _plan_text_lines(plan):
        problems.append("文字读回与 DXF 版不一致（中文可能在代码页转换中损坏）")
    if problems:
        return DwgExport(
            "discarded",
            "DWG 回读核对未通过（" + "；".join(problems) + "），DWG 已丢弃，只交付 DXF",
            converter_warnings=warnings,
            readback_pipe_segments=len(pipes),
        )
    return DwgExport(
        "ok",
        "LibreDWG 写出（实验性，R2000/AC1015），经 dwg2dxf 回读核对管段数、管径与文字一致；未在 AutoCAD/天正中验证",
        data=written.dwg,
        converter_warnings=warnings,
        readback_pipe_segments=len(pipes),
    )


__all__ = [
    "BIGFONT_FILE",
    "DWG_SOURCE_VERSION",
    "DXF_ENCODING",
    "DXF_VERSION",
    "DXF_VERSION_CODE",
    "FONT_FAMILY",
    "FONT_FILE",
    "INSUNITS_MM",
    "LAYER_STYLE",
    "LINETYPES",
    "LTSCALE_FACTOR",
    "TEXT_STYLE",
    "DwgExport",
    "ReadbackReport",
    "RenderError",
    "RenderedSheet",
    "export_dwg",
    "read_back",
    "render",
    "render_sheet",
    "verify_readback",
]
