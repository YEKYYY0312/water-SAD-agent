"""测试符号库"""

import pytest
from water_agent.drawing.symbol_library import (
    lookup_symbol,
    get_symbols_by_category,
    get_all_categories,
    fuzzy_lookup,
    get_statistics,
)


class TestSymbolLookup:
    """测试符号查询"""

    def test_lookup_valve(self):
        """查询阀门符号"""
        info = lookup_symbol('M_H2RAH')
        assert info is not None
        assert info.label == '闸阀'
        assert info.category == '阀门'
        assert info.has_size_attrs is True

    def test_lookup_hydrant(self):
        """查询消火栓符号"""
        info = lookup_symbol('_XHS1')
        assert info is not None
        assert info.label == '室内消火栓'
        assert info.category == '消防设施'

    def test_lookup_nonexistent(self):
        """查询不存在的符号"""
        info = lookup_symbol('UNKNOWN_BLOCK')
        assert info is None


class TestCategoryFiltering:
    """测试分类过滤"""

    def test_get_all_categories(self):
        """获取所有类别"""
        categories = get_all_categories()
        assert '阀门' in categories
        assert '消防设施' in categories
        assert '排水设施' in categories
        assert len(categories) == 5

    def test_get_valves(self):
        """获取所有阀门"""
        valves = get_symbols_by_category('阀门')
        assert len(valves) >= 10
        assert all(s.category == '阀门' for s in valves)

        # 检查常见阀门是否存在
        labels = [v.label for v in valves]
        assert '闸阀' in labels
        assert '止回阀' in labels
        assert '蝶阀' in labels
        assert '球阀' in labels

    def test_get_fire_equipment(self):
        """获取消防设施"""
        fire = get_symbols_by_category('消防设施')
        assert len(fire) >= 4
        labels = [s.label for s in fire]
        assert any('消火栓' in l for l in labels)
        assert any('喷洒' in l for l in labels)


class TestFuzzyLookup:
    """测试模糊查询"""

    def test_exact_match(self):
        """精确匹配"""
        matches = fuzzy_lookup('闸阀')
        assert len(matches) > 0
        assert matches[0][0].label == '闸阀'
        assert matches[0][1] == 1.0

    def test_with_diameter(self):
        """带管径的匹配"""
        matches = fuzzy_lookup('DN100闸阀')
        assert len(matches) > 0
        assert matches[0][0].label == '闸阀'
        assert matches[0][1] == 1.0

    def test_partial_match(self):
        """部分匹配"""
        matches = fuzzy_lookup('止回阀')
        assert len(matches) >= 2  # 普通止回阀 + 消声止回阀

        # 精确匹配应该排在前面
        top = matches[0]
        assert '止回阀' in top[0].label

    def test_category_match(self):
        """类别关键字匹配"""
        matches = fuzzy_lookup('阀门')
        assert len(matches) > 0
        assert all(m[0].category == '阀门' for m in matches)

    def test_no_match(self):
        """无匹配结果"""
        matches = fuzzy_lookup('电梯')
        assert len(matches) == 0


class TestStatistics:
    """测试统计功能"""

    def test_get_statistics(self):
        """获取统计信息"""
        stats = get_statistics()

        assert stats['total'] >= 28
        assert stats['阀门'] >= 10
        assert stats['消防设施'] >= 4
        assert stats['排水设施'] >= 3

        # 总数应该等于各类别之和
        category_sum = sum(v for k, v in stats.items() if k != 'total')
        assert category_sum == stats['total']


class TestIntegrationScenarios:
    """集成场景测试"""

    def test_insert_recognition_pipeline(self):
        """模拟 INSERT 识别流程"""
        # 场景：图纸上有一个 INSERT 实体，块名为 M_H2RAH
        block_name = 'M_H2RAH'

        # 1. 查找符号库
        info = lookup_symbol(block_name)
        assert info is not None

        # 2. 获取语义
        assert info.label == '闸阀'
        assert info.category == '阀门'

        # 3. 判断是否需要标注管径
        assert info.has_size_attrs is True

    def test_annotation_to_symbol_matching(self):
        """模拟标注文字反推符号类型"""
        # 场景：图纸上标注"DN150蝶阀"，但 INSERT 块名未知
        annotation = 'DN150蝶阀'

        # 模糊查找
        matches = fuzzy_lookup(annotation)

        # 应该找到蝶阀
        assert len(matches) > 0
        assert matches[0][0].label == '蝶阀'
        assert matches[0][0].block_name == 'M_H10MWA'

    def test_category_based_detection(self):
        """基于类别的缺陷检测"""
        # 场景：检查所有阀门符号附近是否有管径标注
        valves = get_symbols_by_category('阀门')

        # 过滤出需要标注尺寸的
        valves_need_size = [v for v in valves if v.has_size_attrs]

        assert len(valves_need_size) >= 8
        assert all(v.category == '阀门' for v in valves_need_size)
