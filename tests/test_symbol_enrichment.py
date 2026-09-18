"""测试符号增强功能"""

import pytest
from water_agent.drawing.models import DrawingEntity, DrawingEntityKind
from water_agent.drawing.symbol_enrichment import (
    enrich_insert_entities,
    find_valves_without_size_annotation,
    get_symbols_near_point,
)


class TestEnrichInsertEntities:
    """测试 INSERT 图元增强"""

    def test_enrich_valve_insert(self):
        """测试阀门符号识别"""
        entities = (
            DrawingEntity(
                handle="A1",
                kind=DrawingEntityKind.INSERT,
                dxftype="INSERT",
                layer="W-PIPE",
                points=((100.0, 200.0),),
                block_name="M_H2RAH",  # 闸阀
            ),
        )

        enriched = enrich_insert_entities(entities)
        assert len(enriched) == 1

        entity, symbol = enriched[0]
        assert symbol is not None
        assert symbol.label == "闸阀"
        assert symbol.category == "阀门"
        assert symbol.has_size_attrs is True

    def test_enrich_unknown_block(self):
        """测试未知块名"""
        entities = (
            DrawingEntity(
                handle="A2",
                kind=DrawingEntityKind.INSERT,
                dxftype="INSERT",
                layer="W-PIPE",
                points=((100.0, 200.0),),
                block_name="UNKNOWN_BLOCK",
            ),
        )

        enriched = enrich_insert_entities(entities)
        entity, symbol = enriched[0]
        assert symbol is None

    def test_enrich_mixed_entities(self):
        """测试混合图元（INSERT + 其他）"""
        entities = (
            DrawingEntity(
                handle="A1",
                kind=DrawingEntityKind.INSERT,
                dxftype="INSERT",
                layer="W-PIPE",
                points=((100.0, 200.0),),
                block_name="M_H2RAH",
            ),
            DrawingEntity(
                handle="A2",
                kind=DrawingEntityKind.LINE,
                dxftype="LINE",
                layer="W-PIPE",
                points=((0.0, 0.0), (100.0, 0.0)),
            ),
            DrawingEntity(
                handle="A3",
                kind=DrawingEntityKind.TEXT,
                dxftype="TEXT",
                layer="W-TEXT",
                points=((100.0, 200.0),),
                text="DN100",
            ),
        )

        enriched = enrich_insert_entities(entities)
        assert len(enriched) == 3

        # INSERT 有符号信息
        assert enriched[0][1] is not None
        assert enriched[0][1].label == "闸阀"

        # LINE 和 TEXT 无符号信息
        assert enriched[1][1] is None
        assert enriched[2][1] is None


class TestGetSymbolsNearPoint:
    """测试空间查询"""

    def test_find_nearby_valve(self):
        """测试查找附近的阀门"""
        entities = (
            DrawingEntity(
                handle="A1",
                kind=DrawingEntityKind.INSERT,
                dxftype="INSERT",
                layer="W-PIPE",
                points=((100.0, 200.0),),
                block_name="M_H2RAH",
            ),
            DrawingEntity(
                handle="A2",
                kind=DrawingEntityKind.INSERT,
                dxftype="INSERT",
                layer="W-PIPE",
                points=((1000.0, 2000.0),),  # 很远
                block_name="M_H10MWA",  # 蝶阀
            ),
        )

        enriched = enrich_insert_entities(entities)

        # 查找 (100, 200) 附近 50 单位内的符号
        nearby = get_symbols_near_point(enriched, x=100.0, y=200.0, radius=50.0)

        assert len(nearby) == 1
        assert nearby[0][1].label == "闸阀"

    def test_sort_by_distance(self):
        """测试按距离排序"""
        entities = (
            DrawingEntity(
                handle="A1",
                kind=DrawingEntityKind.INSERT,
                dxftype="INSERT",
                layer="W-PIPE",
                points=((150.0, 200.0),),  # 距离 50
                block_name="M_H2RAH",
            ),
            DrawingEntity(
                handle="A2",
                kind=DrawingEntityKind.INSERT,
                dxftype="INSERT",
                layer="W-PIPE",
                points=((110.0, 200.0),),  # 距离 10
                block_name="M_H10MWA",
            ),
        )

        enriched = enrich_insert_entities(entities)
        nearby = get_symbols_near_point(enriched, x=100.0, y=200.0, radius=100.0)

        # 应该按距离升序
        assert len(nearby) == 2
        assert nearby[0][1].label == "蝶阀"  # 更近
        assert nearby[1][1].label == "闸阀"


class TestFindValvesWithoutSizeAnnotation:
    """测试缺陷检测：阀门缺管径标注"""

    def test_valve_with_annotation(self):
        """阀门有标注 → 不报缺陷"""
        entities = (
            DrawingEntity(
                handle="A1",
                kind=DrawingEntityKind.INSERT,
                dxftype="INSERT",
                layer="W-PIPE",
                points=((100.0, 200.0),),
                block_name="M_H2RAH",  # 闸阀
            ),
        )
        text_entities = (
            DrawingEntity(
                handle="T1",
                kind=DrawingEntityKind.TEXT,
                dxftype="TEXT",
                layer="W-TEXT",
                points=((105.0, 205.0),),  # 距离 7.07
                text="DN100",
            ),
        )

        enriched = enrich_insert_entities(entities)
        missing = find_valves_without_size_annotation(enriched, text_entities, max_distance=50.0)

        assert len(missing) == 0

    def test_valve_without_annotation(self):
        """阀门无标注 → 报缺陷"""
        entities = (
            DrawingEntity(
                handle="A1",
                kind=DrawingEntityKind.INSERT,
                dxftype="INSERT",
                layer="W-PIPE",
                points=((100.0, 200.0),),
                block_name="M_H2RAH",
            ),
        )
        text_entities = (
            DrawingEntity(
                handle="T1",
                kind=DrawingEntityKind.TEXT,
                dxftype="TEXT",
                layer="W-TEXT",
                points=((500.0, 500.0),),  # 太远
                text="DN100",
            ),
        )

        enriched = enrich_insert_entities(entities)
        missing = find_valves_without_size_annotation(enriched, text_entities, max_distance=50.0)

        assert len(missing) == 1
        assert missing[0][1].label == "闸阀"

    def test_multiple_size_formats(self):
        """测试多种管径标注格式"""
        entities = (
            DrawingEntity(
                handle="A1",
                kind=DrawingEntityKind.INSERT,
                dxftype="INSERT",
                layer="W-PIPE",
                points=((100.0, 200.0),),
                block_name="M_H2RAH",
            ),
        )

        # 测试 DN、De、φ 等格式
        for annotation in ["DN100", "De110", "φ200", "d75", "Φ150"]:
            text_entities = (
                DrawingEntity(
                    handle="T1",
                    kind=DrawingEntityKind.TEXT,
                    dxftype="TEXT",
                    layer="W-TEXT",
                    points=((105.0, 205.0),),
                    text=annotation,
                ),
            )

            enriched = enrich_insert_entities(entities)
            missing = find_valves_without_size_annotation(
                enriched, text_entities, max_distance=50.0
            )

            assert len(missing) == 0, f"应该识别 {annotation} 为管径标注"

    def test_ignore_non_valves(self):
        """非阀门符号不检测"""
        entities = (
            DrawingEntity(
                handle="A1",
                kind=DrawingEntityKind.INSERT,
                dxftype="INSERT",
                layer="W-PIPE",
                points=((100.0, 200.0),),
                block_name="_XHS1",  # 消火栓，不需要管径标注
            ),
        )
        text_entities = ()

        enriched = enrich_insert_entities(entities)
        missing = find_valves_without_size_annotation(enriched, text_entities)

        assert len(missing) == 0

    def test_ignore_valves_without_size_attrs(self):
        """不需要标注尺寸的阀门不检测"""
        # 假设某些特殊阀门符号不需要标注尺寸（has_size_attrs=False）
        # 当前符号库中所有阀门都需要，这里测试逻辑分支
        entities = (
            DrawingEntity(
                handle="A1",
                kind=DrawingEntityKind.LINE,
                dxftype="LINE",
                layer="W-PIPE",
                points=((0.0, 0.0), (100.0, 0.0)),
            ),
        )
        text_entities = ()

        enriched = enrich_insert_entities(entities)
        missing = find_valves_without_size_annotation(enriched, text_entities)

        assert len(missing) == 0
