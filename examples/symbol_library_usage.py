"""符号库使用示例

展示如何在图纸审查流程中使用符号库增强功能。
"""

from pathlib import Path

from water_agent.drawing.dxf_reader import read_drawing
from water_agent.drawing.models import DrawingEntityKind
from water_agent.drawing.symbol_enrichment import (
    enrich_insert_entities,
    find_valves_without_size_annotation,
    get_symbols_near_point,
)
from water_agent.drawing.symbol_library import get_symbols_by_category


def example_basic_enrichment():
    """示例 1：基础符号识别"""
    print("=== 示例 1：基础符号识别 ===\n")

    # 假设有一张图纸
    from water_agent.drawing.drawing_root import resolve_drawing_root

    result = resolve_drawing_root()
    if result.status != "OK":
        print(f"图纸目录不可用: {result.message}")
        return

    # 使用第一张图纸
    if not result.drawings:
        print("没有可用图纸")
        return

    drawing_path = result.drawings[0]
    drawing_id = drawing_path.stem  # 文件名（不含扩展名）

    extraction = read_drawing(drawing_id, root=Path(result.drawing_root))

    if not extraction.ok:
        print(f"读取失败: {extraction.errors}")
        return

    # 增强 INSERT 图元
    enriched = enrich_insert_entities(extraction.entities)

    # 打印识别出的符号
    print(f"图纸共有 {len(extraction.entities)} 个图元\n")

    symbol_count = 0
    for entity, symbol in enriched:
        if symbol:
            symbol_count += 1
            print(f"  - {entity.handle}: {symbol.label} ({symbol.category})")
            print(f"    块名: {entity.block_name}")
            print(f"    位置: {entity.points[0] if entity.points else 'N/A'}")
            if symbol.has_size_attrs:
                print(f"    ⚠️  需要标注管径")
            print()

    print(f"识别出 {symbol_count} 个符号\n")


def example_defect_detection():
    """示例 2：缺陷检测（阀门缺管径标注）"""
    print("=== 示例 2：缺陷检测 - 阀门缺管径标注 ===\n")

    from water_agent.drawing.drawing_root import resolve_drawing_root

    result = resolve_drawing_root()
    if result.status != "OK" or not result.drawings:
        print(f"图纸目录不可用: {result.message}")
        return

    drawing_path = result.drawings[0]
    drawing_id = drawing_path.stem
    extraction = read_drawing(drawing_id, root=result.drawing_root)

    if not extraction.ok:
        print(f"读取失败: {extraction.errors}")
        return

    # 增强图元
    enriched = enrich_insert_entities(extraction.entities)

    # 提取所有文本图元
    text_entities = tuple(
        e for e in extraction.entities if e.kind in (DrawingEntityKind.TEXT, DrawingEntityKind.MTEXT)
    )

    # 查找缺少标注的阀门
    missing = find_valves_without_size_annotation(
        enriched,
        text_entities,
        max_distance=150.0,  # 搜索半径 150 单位
    )

    if not missing:
        print("✅ 所有阀门都有管径标注\n")
    else:
        print(f"❌ 发现 {len(missing)} 个阀门缺少管径标注:\n")
        for entity, symbol in missing:
            print(f"  - {symbol.label} (块名: {entity.block_name})")
            print(f"    位置: {entity.points[0] if entity.points else 'N/A'}")
            print(f"    图层: {entity.layer}")
            print()


def example_annotation_symbol_matching():
    """示例 3：标注-符号匹配"""
    print("=== 示例 3：标注-符号匹配 ===\n")

    from water_agent.drawing.drawing_root import resolve_drawing_root

    result = resolve_drawing_root()
    if result.status != "OK" or not result.drawings:
        print(f"图纸目录不可用: {result.message}")
        return

    drawing_path = result.drawings[0]
    drawing_id = drawing_path.stem
    extraction = read_drawing(drawing_id, root=result.drawing_root)

    if not extraction.ok:
        print(f"读取失败: {extraction.errors}")
        return

    enriched = enrich_insert_entities(extraction.entities)

    # 找一个标注了管径的文本
    import re

    size_pattern = re.compile(r"(DN|De|[dφΦ])\s*\d+", re.IGNORECASE)

    for entity in extraction.entities:
        if entity.kind not in (DrawingEntityKind.TEXT, DrawingEntityKind.MTEXT):
            continue
        if not entity.text or not size_pattern.search(entity.text):
            continue
        if not entity.points:
            continue

        x, y = entity.points[0]
        print(f"标注: {entity.text} @ ({x:.1f}, {y:.1f})")

        # 查找附近的符号
        nearby = get_symbols_near_point(enriched, x, y, radius=150.0)

        if nearby:
            print(f"  附近有 {len(nearby)} 个符号:")
            for e, s in nearby:
                dist = ((e.points[0][0] - x) ** 2 + (e.points[0][1] - y) ** 2) ** 0.5
                print(f"    - {s.label} (距离: {dist:.1f})")
        else:
            print("  ⚠️  附近没有符号（可能是孤立标注）")
        print()

        break  # 只演示第一个


def example_category_statistics():
    """示例 4：符号分类统计"""
    print("=== 示例 4：符号分类统计 ===\n")

    from water_agent.drawing.drawing_root import resolve_drawing_root

    result = resolve_drawing_root()
    if result.status != "OK" or not result.drawings:
        print(f"图纸目录不可用: {result.message}")
        return

    drawing_path = result.drawings[0]
    drawing_id = drawing_path.stem
    extraction = read_drawing(drawing_id, root=result.drawing_root)

    if not extraction.ok:
        print(f"读取失败: {extraction.errors}")
        return

    enriched = enrich_insert_entities(extraction.entities)

    # 按类别统计
    from collections import Counter

    category_count = Counter(symbol.category for _, symbol in enriched if symbol)

    print("图纸中的符号分布:\n")
    for category, count in category_count.most_common():
        print(f"  {category}: {count} 个")
    print()


def example_symbol_library_info():
    """示例 5：符号库概览"""
    print("=== 示例 5：符号库概览 ===\n")

    from water_agent.drawing.symbol_library import get_all_categories, get_statistics

    stats = get_statistics()
    print(f"符号库总计: {stats['total']} 个符号\n")

    categories = get_all_categories()
    for category in sorted(categories):
        symbols = get_symbols_by_category(category)
        print(f"{category} ({stats[category]} 个):")
        for symbol in symbols[:5]:  # 只显示前 5 个
            attrs = "需要管径标注" if symbol.has_size_attrs else ""
            print(f"  - {symbol.label} ({symbol.block_name}) {attrs}")
        if len(symbols) > 5:
            print(f"  ... 还有 {len(symbols) - 5} 个")
        print()


if __name__ == "__main__":
    example_symbol_library_info()
    print("\n" + "=" * 60 + "\n")
    example_basic_enrichment()
    print("\n" + "=" * 60 + "\n")
    example_defect_detection()
    print("\n" + "=" * 60 + "\n")
    example_annotation_symbol_matching()
    print("\n" + "=" * 60 + "\n")
    example_category_statistics()
