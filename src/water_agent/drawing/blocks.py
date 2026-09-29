"""出图用的标准图块（P6-S1 Step 4）：闸阀、截止阀、止回阀、水表、水泵、立管编号圈、标高、流向。

块按**图纸毫米**定义、以符号中心为基点，插入时按出图比例缩放，出图后符号大小一致。
块名一律 ``WS_`` 前缀，避免与设计院 CAD 模板里已有的块同名冲突。

图例风格参照 GB/T 50106 的给水图例绘制（闸阀为对顶三角、截止阀加实心圆点、止回阀
一侧涂实），但 **GB/T 50106 不在本仓语料库**，符号未逐一核对标准原图；每张图都附图例
说明符号含义，不依赖读者记忆标准图例。

块内图元显式放在 ``WS-VALVE`` / ``WS-METER`` 等图层上，**不用 0 层**：审查器的
``read_drawing`` 用 ``virtual_entities()`` 展开块，0 层图元展开后仍是 0 层（已实测），
系统归属会变成"未知"，给复审平白添一批"系统归属未知"的未检查项。
"""

from __future__ import annotations

from typing import Any

from water_agent.drawing.generation_models import (
    LAYER_DIM,
    LAYER_METER,
    LAYER_TEXT,
    LAYER_VALVE,
    SymbolKind,
)

BLOCK_NAMES: dict[SymbolKind, str] = {
    SymbolKind.GATE_VALVE: "WS_GATE_VALVE",
    SymbolKind.GLOBE_VALVE: "WS_GLOBE_VALVE",
    SymbolKind.CHECK_VALVE: "WS_CHECK_VALVE",
    SymbolKind.WATER_METER: "WS_WATER_METER",
    SymbolKind.PUMP: "WS_PUMP",
    SymbolKind.RISER_TAG: "WS_RISER_TAG",
    SymbolKind.ELEVATION_MARK: "WS_ELEV_MARK",
    SymbolKind.FLOW_ARROW: "WS_FLOW_ARROW",
}

#: 各块沿管线方向（块内 x 轴）的长度，图纸毫米。`layout_engine.SYMBOL_LENGTH_MM` 与之一致（有测试核对）。
BLOCK_LENGTH_MM: dict[SymbolKind, float] = {
    SymbolKind.GATE_VALVE: 4.0,
    SymbolKind.GLOBE_VALVE: 4.0,
    SymbolKind.CHECK_VALVE: 4.0,
    SymbolKind.WATER_METER: 2.8,
    SymbolKind.PUMP: 5.0,
}

_BOWTIE = [(-2.0, 1.0), (-2.0, -1.0), (2.0, 1.0), (2.0, -1.0)]


def _solid(block: Any, points: list[tuple[float, float]], layer: str) -> None:
    """实心三角：SOLID 的四个顶点里第 4 点重复第 3 点。"""

    a, b, c = points
    block.add_solid([a, b, c, c], dxfattribs={"layer": layer})


def _filled_disc(block: Any, center: tuple[float, float], radius: float, layer: str) -> None:
    block.add_circle(center, radius, dxfattribs={"layer": layer})
    hatch = block.add_hatch(dxfattribs={"layer": layer})
    hatch.paths.add_edge_path().add_arc(center, radius, 0, 360)


def define_blocks(doc: Any) -> None:
    """在文档里定义全部 ``WS_*`` 图块（已存在则跳过，幂等）。"""

    def new(kind: SymbolKind) -> Any | None:
        name = BLOCK_NAMES[kind]
        return None if name in doc.blocks else doc.blocks.new(name)

    if (block := new(SymbolKind.GATE_VALVE)) is not None:
        block.add_lwpolyline(_BOWTIE, close=True, dxfattribs={"layer": LAYER_VALVE})
    if (block := new(SymbolKind.GLOBE_VALVE)) is not None:
        block.add_lwpolyline(_BOWTIE, close=True, dxfattribs={"layer": LAYER_VALVE})
        _filled_disc(block, (0.0, 0.0), 0.5, LAYER_VALVE)
    if (block := new(SymbolKind.CHECK_VALVE)) is not None:
        block.add_lwpolyline(_BOWTIE, close=True, dxfattribs={"layer": LAYER_VALVE})
        _solid(block, [(0.0, 0.0), (2.0, 1.0), (2.0, -1.0)], LAYER_VALVE)
    if (block := new(SymbolKind.WATER_METER)) is not None:
        block.add_circle((0.0, 0.0), 1.4, dxfattribs={"layer": LAYER_METER})
        _solid(block, [(-0.6, 0.7), (-0.6, -0.7), (0.8, 0.0)], LAYER_METER)
    if (block := new(SymbolKind.PUMP)) is not None:
        block.add_circle((0.0, 0.0), 2.5, dxfattribs={"layer": LAYER_VALVE})
        _solid(block, [(-1.2, 1.4), (-1.2, -1.4), (1.8, 0.0)], LAYER_VALVE)
    if (block := new(SymbolKind.RISER_TAG)) is not None:
        block.add_circle((0.0, 0.0), 4.0, dxfattribs={"layer": LAYER_TEXT})
    if (block := new(SymbolKind.ELEVATION_MARK)) is not None:
        # 标高符号：顶点朝下落在标高线上，上方横线承托标高数字（数字是模型空间文字，不在块里）。
        block.add_lwpolyline([(0.0, 0.0), (-2.5, 2.5), (2.5, 2.5)], close=True, dxfattribs={"layer": LAYER_DIM})
        block.add_line((2.5, 2.5), (14.0, 2.5), dxfattribs={"layer": LAYER_DIM})
    if (block := new(SymbolKind.FLOW_ARROW)) is not None:
        _solid(block, [(1.5, 0.0), (-1.5, 0.75), (-1.5, -0.75)], LAYER_TEXT)


__all__ = ["BLOCK_LENGTH_MM", "BLOCK_NAMES", "define_blocks"]
