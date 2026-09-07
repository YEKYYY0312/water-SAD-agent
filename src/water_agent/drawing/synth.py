"""程序化合成 DXF 图纸：让 P3 的测试与评测不依赖任何外部素材。

全仓 0 个真实图纸样例（且真实设计院图纸含业主信息，不适合入库）。因此测试图纸
由本模块用 ezdxf 建出，图层命名与标注形态照给排水图纸的常见约定。

**这不等于真实图纸**：真实 DXF 含 INSERT 块参照、XREF 外部参照、非标图层命名、
图纸空间与视口。合成图纸只用于证明解析管线与护栏正确，见规格 §9 第 2 条。

Step 22 会在此基础上扩缺陷 gold 标注；本步**不**预先添加无消费者的 gold 字段
（P2.5 §4 禁止死字段）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

#: 合成图纸使用的 DXF 版本（R2010 = AC1024，ezdxf 支持良好且够新）。
DXF_VERSION = "R2010"

#: 图层命名照给排水图纸约定：W/WS = 给水，DR = 排水。
LAYER_WATER_PIPE = "WS-PIPE"
LAYER_WATER_TEXT = "W-给水-标注"
LAYER_DRAIN_PIPE = "DR-PIPE"
LAYER_DRAIN_MANHOLE = "DR-检查井"


@dataclass(frozen=True, slots=True)
class SyntheticDrawing:
    """合成结果：供测试做精确断言，不必再去读文件猜内容。"""

    drawing_id: str
    path: Path
    layers: tuple[str, ...]
    #: DXF 类型名 → 条数（如 ``{"LINE": 2, "TEXT": 1}``）。
    entity_counts: dict[str, int] = field(default_factory=dict)
    #: 注入标注所在的图元 handle（未注入时为 None）——测试据此定位那一条。
    injected_handle: str | None = None
    #: 是否声明了 ``$INSUNITS``。
    unit_declared: bool = True


def build_synthetic_drawing(
    dest: Path,
    *,
    drawing_id: str = "syn-001",
    inject_annotation: str | None = None,
    declare_units: bool = True,
) -> SyntheticDrawing:
    """在 ``dest`` 目录下建一张 ``{drawing_id}.dxf``，返回其结构摘要。

    Args:
        dest: 输出目录（自动创建）。
        drawing_id: 图纸登记号，即文件名（不含扩展名）。
        inject_annotation: 注入一条 MTEXT 标注，用于间接注入的**反例**测试。
            传合成攻击串（不入语料）；``None`` 表示不注入。
        declare_units: ``False`` 时不写 ``$INSUNITS``，用于测 ``unit_known=False``。
    """
    import ezdxf

    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"{drawing_id}.dxf"

    doc = ezdxf.new(DXF_VERSION)
    if declare_units:
        doc.header["$INSUNITS"] = 6  # 6 = 米
    else:
        # ezdxf.new 会写入默认 $INSUNITS；显式置 0（unitless）模拟"未声明单位"。
        doc.header["$INSUNITS"] = 0

    layers = (LAYER_WATER_PIPE, LAYER_WATER_TEXT, LAYER_DRAIN_PIPE, LAYER_DRAIN_MANHOLE)
    for name in layers:
        doc.layers.add(name)

    msp = doc.modelspace()
    counts: dict[str, int] = {}

    def bump(dxftype: str) -> None:
        counts[dxftype] = counts.get(dxftype, 0) + 1

    # 给水干管：两段 LINE
    msp.add_line((0.0, 0.0), (125.6, 0.0), dxfattribs={"layer": LAYER_WATER_PIPE})
    msp.add_line((125.6, 0.0), (125.6, 48.2), dxfattribs={"layer": LAYER_WATER_PIPE})
    bump("LINE")
    bump("LINE")

    # 给水标注：TEXT（工程标注，必须不被护栏误伤）
    text = msp.add_text(
        "DN200 球墨铸铁 K9 L=125.6m", dxfattribs={"layer": LAYER_WATER_TEXT, "height": 2.5}
    )
    text.set_placement((10.0, 3.0))
    bump("TEXT")

    # 排水管：LWPOLYLINE（三顶点，验证 np.float64 → float 强制）
    msp.add_lwpolyline(
        [(0.0, -20.0), (60.0, -20.0), (60.0, -55.0)],
        dxfattribs={"layer": LAYER_DRAIN_PIPE},
    )
    bump("LWPOLYLINE")

    # 检查井：CIRCLE + MTEXT（含 \P 段落符，验证 plain_mtext 归一）
    msp.add_circle((60.0, -55.0), radius=0.6, dxfattribs={"layer": LAYER_DRAIN_MANHOLE})
    bump("CIRCLE")
    manhole = msp.add_mtext(
        "检查井 Y1\n井底标高 -1.850", dxfattribs={"layer": LAYER_DRAIN_MANHOLE}
    )
    manhole.set_location((60.0, -55.0))
    bump("MTEXT")

    injected_handle: str | None = None
    if inject_annotation is not None:
        injected = msp.add_mtext(
            inject_annotation, dxfattribs={"layer": LAYER_DRAIN_MANHOLE}
        )
        injected.set_location((80.0, -55.0))
        bump("MTEXT")
        injected_handle = str(injected.dxf.handle)

    doc.saveas(str(path))

    # handle 在 saveas 后才最终确定，重新读回取权威值。
    if injected_handle is not None:
        from ezdxf.entities import MText
        from ezdxf.tools.text import plain_mtext

        reopened = ezdxf.readfile(str(path))
        for entity in reopened.modelspace():
            # `isinstance(MText)` 而非 `dxftype() == "MTEXT"`：后者 mypy 无法据此
            # 收窄类型，`entity.text` 会报 `DXFGraphic has no attribute "text"`。
            if isinstance(entity, MText) and plain_mtext(entity.text or "") == inject_annotation:
                injected_handle = str(entity.dxf.handle)
                break

    return SyntheticDrawing(
        drawing_id=drawing_id,
        path=path,
        layers=layers,
        entity_counts=counts,
        injected_handle=injected_handle,
        unit_declared=declare_units,
    )
