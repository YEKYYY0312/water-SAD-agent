"""符号库增强：为 INSERT 图元添加语义信息。

P3 图纸审查链路的增强模块 —— 在解析器输出的结构化图元基础上，
为 INSERT 实体添加符号库查询结果（阀门/消火栓/地漏等的语义标签）。

**设计原则**：
1. **解耦**：不侵入 dxf_reader（它只负责提取 block_name），符号库是增强层
2. **可选**：符号库查询失败不影响解析流程，只是缺少语义标签
3. **零拷贝**：返回增强后的 DrawingEntity，而非修改原对象（frozen=True）
"""
from __future__ import annotations

from water_agent.drawing.models import DrawingEntity, DrawingEntityKind
from water_agent.drawing.symbol_library import SymbolInfo, lookup_symbol


def enrich_insert_entities(
    entities: tuple[DrawingEntity, ...],
) -> tuple[tuple[DrawingEntity, SymbolInfo | None], ...]:
    """为所有 INSERT 图元添加符号库信息。

    Args:
        entities: dxf_reader 解析出的图元列表

    Returns:
        (图元, 符号信息) 对的元组。非 INSERT 或查询失败时符号信息为 None。

    示例::

        extraction = read_drawing("plan1", root=DRAWING_ROOT)
        enriched = enrich_insert_entities(extraction.entities)

        for entity, symbol in enriched:
            if symbol:
                print(f"{entity.handle}: {symbol.label} ({symbol.category})")
    """
    result: list[tuple[DrawingEntity, SymbolInfo | None]] = []

    for entity in entities:
        if entity.kind != DrawingEntityKind.INSERT or not entity.block_name:
            result.append((entity, None))
            continue

        symbol = lookup_symbol(entity.block_name)
        result.append((entity, symbol))

    return tuple(result)


def get_symbols_near_point(
    enriched: tuple[tuple[DrawingEntity, SymbolInfo | None], ...],
    x: float,
    y: float,
    radius: float = 100.0,
) -> list[tuple[DrawingEntity, SymbolInfo]]:
    """获取指定点附近的所有符号（用于"标注-符号"关联检测）。

    Args:
        enriched: enrich_insert_entities 的输出
        x, y: 查询点坐标（通常是标注文本的位置）
        radius: 搜索半径

    Returns:
        (图元, 符号信息) 列表，按距离升序排序
    """
    candidates: list[tuple[float, DrawingEntity, SymbolInfo]] = []

    for entity, symbol in enriched:
        if symbol is None or not entity.points:
            continue

        # INSERT 的 points[0] 是插入点
        px, py = entity.points[0]
        dist = ((px - x) ** 2 + (py - y) ** 2) ** 0.5

        if dist <= radius:
            candidates.append((dist, entity, symbol))

    candidates.sort(key=lambda t: t[0])
    return [(e, s) for _, e, s in candidates]


def find_valves_without_size_annotation(
    enriched: tuple[tuple[DrawingEntity, SymbolInfo | None], ...],
    text_entities: tuple[DrawingEntity, ...],
    max_distance: float = 150.0,
) -> list[tuple[DrawingEntity, SymbolInfo]]:
    """查找缺少管径标注的阀门符号（典型的审查规则）。

    Args:
        enriched: enrich_insert_entities 的输出
        text_entities: 所有文本图元（TEXT/MTEXT）
        max_distance: 判定"有标注"的最大距离

    Returns:
        缺少标注的 (阀门图元, 符号信息) 列表
    """
    import re

    # 匹配管径标注的正则（DN50、De110、φ200 等）
    size_pattern = re.compile(r"(DN|De|[dφΦ])\s*\d+", re.IGNORECASE)

    missing: list[tuple[DrawingEntity, SymbolInfo]] = []

    for entity, symbol in enriched:
        # 只检查需要标注尺寸的阀门
        if symbol is None or symbol.category != "阀门" or not symbol.has_size_attrs:
            continue
        if not entity.points:
            continue

        x, y = entity.points[0]

        # 查找附近是否有管径标注
        has_annotation = False
        for text_entity in text_entities:
            if not text_entity.text or not text_entity.points:
                continue
            if not size_pattern.search(text_entity.text):
                continue

            tx, ty = text_entity.points[0]
            dist = ((tx - x) ** 2 + (ty - y) ** 2) ** 0.5

            if dist <= max_distance:
                has_annotation = True
                break

        if not has_annotation:
            missing.append((entity, symbol))

    return missing
