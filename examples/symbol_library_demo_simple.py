"""符号库演示（简化版，绕过 ToolPolicy）

直接调用底层 ezdxf 解析，演示符号库的增强能力。
"""

from pathlib import Path

from water_agent.drawing.symbol_enrichment import (
    enrich_insert_entities,
    find_valves_without_size_annotation,
)
from water_agent.drawing.symbol_library import get_symbols_by_category

# 绕过 ToolPolicy，直接解析
import ezdxf
from water_agent.drawing.models import DrawingEntity, DrawingEntityKind


def parse_dxf_direct(path: Path) -> list[DrawingEntity]:
    """直接解析 DXF，不走 read_drawing 的策略检查"""
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()

    entities = []
    for e in msp:
        if e.dxftype() == 'INSERT':
            # INSERT 实体（块引用）
            insert_pt = e.dxf.insert
            entities.append(DrawingEntity(
                handle=e.dxf.handle,
                kind=DrawingEntityKind.INSERT,
                dxftype='INSERT',
                layer=e.dxf.layer,
                points=((float(insert_pt.x), float(insert_pt.y)),),
                block_name=e.dxf.name,
            ))
        elif e.dxftype() == 'TEXT':
            # 文本标注
            insert_pt = e.dxf.insert
            entities.append(DrawingEntity(
                handle=e.dxf.handle,
                kind=DrawingEntityKind.TEXT,
                dxftype='TEXT',
                layer=e.dxf.layer,
                points=((float(insert_pt.x), float(insert_pt.y)),),
                text=e.dxf.text,
            ))
        elif e.dxftype() == 'MTEXT':
            # 多行文本
            insert_pt = e.dxf.insert
            entities.append(DrawingEntity(
                handle=e.dxf.handle,
                kind=DrawingEntityKind.MTEXT,
                dxftype='MTEXT',
                layer=e.dxf.layer,
                points=((float(insert_pt.x), float(insert_pt.y)),),
                text=e.text,
            ))

    return entities


def demo_symbol_enrichment():
    """演示符号增强"""
    print("=== 符号库演示 ===\n")

    # 1. 符号库概览
    print("【1】符号库概览\n")
    for category in ["阀门", "消防设施", "排水设施"]:
        symbols = get_symbols_by_category(category)
        print(f"{category}: {len(symbols)} 个符号")
        for sym in symbols[:3]:
            print(f"  - {sym.label} ({sym.block_name})")
        if len(symbols) > 3:
            print(f"  ... 还有 {len(symbols) - 3} 个")
        print()

    # 2. 读取真实图纸（选择 PUMP-03，它有最多的 INSERT 块）
    samples_dir = Path("data/drawings/real_samples")
    target_file = samples_dir / "PUMP-03.dxf"

    if not target_file.exists():
        print(f"未找到 {target_file}")
        return

    print(f"【2】分析图纸: {target_file.name}\n")
    entities = parse_dxf_direct(target_file)
    print(f"图元总数: {len(entities)}")

    inserts = [e for e in entities if e.kind == DrawingEntityKind.INSERT]
    print(f"INSERT 块: {len(inserts)}")

    # 3. 符号增强
    print(f"\n【3】符号识别\n")
    enriched = enrich_insert_entities(entities)

    recognized = [(e, sym) for e, sym in enriched if sym is not None]
    print(f"识别出的符号: {len(recognized)}/{len(inserts)}")

    if recognized:
        print("\n前 10 个识别出的符号:")
        for e, sym in recognized[:10]:
            x, y = e.points[0] if e.points else (0, 0)
            print(f"  - {sym.label} at ({x:.1f}, {y:.1f})")
            print(f"    类别: {sym.category}, 块名: {e.block_name}")

    # 4. 缺陷检测
    print(f"\n【4】缺陷检测：阀门缺管径标注\n")

    # 提取所有文本实体
    text_entities = tuple(e for e in entities if e.kind in (DrawingEntityKind.TEXT, DrawingEntityKind.MTEXT))

    defects = find_valves_without_size_annotation(enriched, text_entities, max_distance=50.0)

    if defects:
        print(f"发现 {len(defects)} 个可疑阀门（附近无管径标注）:\n")
        for valve_entity, valve_symbol in defects[:5]:
            x, y = valve_entity.points[0] if valve_entity.points else (0, 0)
            print(f"  - {valve_symbol.label} at ({x:.1f}, {y:.1f})")
            print(f"    块名: {valve_entity.block_name}, 图层: {valve_entity.layer}")
        if len(defects) > 5:
            print(f"  ... 还有 {len(defects) - 5} 个")
    else:
        print("未发现缺陷（所有阀门附近都有标注）")


if __name__ == "__main__":
    demo_symbol_enrichment()
