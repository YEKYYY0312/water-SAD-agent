"""给排水符号库

从标准图例 DWG 提取的管件、阀门、设备符号定义。
用于识别 INSERT 块引用的语义（"这是一个 DN100 的闸阀"）。

数据来源：《最全的给排水图例》图例汇总.dwg
提取时间：2026-09-15
"""

from dataclasses import dataclass


@dataclass
class SymbolInfo:
    """符号信息"""
    block_name: str  # 块名（如 M_H2RAH）
    label: str       # 中文名称（如"闸阀"）
    category: str    # 类别（阀门/管道附件/消防设施/排水设施/仪表设备）
    entity_count: int  # 几何实体数
    has_size_attrs: bool  # 是否有 XSIZE/YSIZE 属性


# 符号库数据（从图例汇总提取）
_SYMBOL_DEFINITIONS: dict[str, SymbolInfo] = {
    # 阀门类
    'M_H2RAH': SymbolInfo('M_H2RAH', '闸阀', '阀门', 7, True),
    'M_H0XAZ': SymbolInfo('M_H0XAZ', '截止阀', '阀门', 5, True),
    'M_H12VSN': SymbolInfo('M_H12VSN', '止回阀', '阀门', 8, True),
    'M_H4SNO': SymbolInfo('M_H4SNO', '止回阀', '阀门', 7, True),
    'M_H11VPK': SymbolInfo('M_H11VPK', '消声止回阀', '阀门', 8, True),
    'M_H10MWA': SymbolInfo('M_H10MWA', '蝶阀', '阀门', 8, True),
    'M_H9OMI': SymbolInfo('M_H9OMI', '球阀', '阀门', 10, True),
    'M_H30QJQ': SymbolInfo('M_H30QJQ', '浮球阀', '阀门', 5, True),
    'M_H1SCH': SymbolInfo('M_H1SCH', '泄压持压阀', '阀门', 6, True),
    'M_H19MFQ': SymbolInfo('M_H19MFQ', '湿式报警阀', '阀门', 9, True),
    'M_H21PPO': SymbolInfo('M_H21PPO', '自动排气阀', '阀门', 3, True),

    # 管道附件
    'M_L0TDU': SymbolInfo('M_L0TDU', '立管检查口', '管道附件', 4, True),
    'QSK_': SymbolInfo('QSK_', '清扫口', '管道附件', 5, False),
    'X_QSK': SymbolInfo('X_QSK', '清扫口', '管道附件', 2, False),
    'GDU_': SymbolInfo('GDU_', '管堵', '管道附件', 2, False),
    'TQM_': SymbolInfo('TQM_', '通气帽', '管道附件', 2, False),
    'YSM_': SymbolInfo('YSM_', '通气帽', '管道附件', 2, False),

    # 消防设施
    '_XHS1': SymbolInfo('_XHS1', '室内消火栓', '消防设施', 6, False),
    '_XHS2': SymbolInfo('_XHS2', '室内消火栓', '消防设施', 88, False),
    '_XHS11': SymbolInfo('_XHS11', '室内双阀双出口消火栓', '消防设施', 7, False),
    '_XHS12': SymbolInfo('_XHS12', '室内双阀双出口消火栓', '消防设施', 50, False),
    'X_PT1': SymbolInfo('X_PT1', '闭式自动喷洒头', '消防设施', 4, False),

    # 排水设施
    'YDL_': SymbolInfo('YDL_', '地漏', '排水设施', 6, False),
    'X_YDL': SymbolInfo('X_YDL', '地漏', '排水设施', 3, False),
    'M_L1TTK': SymbolInfo('M_L1TTK', '雨水斗', '排水设施', 5, True),

    # 仪表设备
    'M_L7BRT': SymbolInfo('M_L7BRT', '温度计', '仪表设备', 8, True),
    'M_H35GGJ': SymbolInfo('M_H35GGJ', '水龙头', '仪表设备', 8, True),
    'M_H36ZCL': SymbolInfo('M_H36ZCL', '皮带龙头', '仪表设备', 9, True),
}


def lookup_symbol(block_name: str) -> SymbolInfo | None:
    """根据块名查找符号信息

    Args:
        block_name: INSERT 实体的块名（如 "M_H2RAH"）

    Returns:
        符号信息，如果不在库中则返回 None

    Example:
        >>> info = lookup_symbol("M_H2RAH")
        >>> print(info.label, info.category)
        闸阀 阀门
    """
    return _SYMBOL_DEFINITIONS.get(block_name)


def get_symbols_by_category(category: str) -> list[SymbolInfo]:
    """获取指定类别的所有符号

    Args:
        category: 类别名称（阀门/管道附件/消防设施/排水设施/仪表设备）

    Returns:
        符号列表
    """
    return [s for s in _SYMBOL_DEFINITIONS.values() if s.category == category]


def get_all_categories() -> list[str]:
    """获取所有类别名称"""
    return sorted({s.category for s in _SYMBOL_DEFINITIONS.values()})


def fuzzy_lookup(text: str) -> list[tuple[SymbolInfo, float]]:
    """模糊查找符号（用于从文字标注反推可能的符号类型）

    Args:
        text: 文字内容（如"DN100闸阀"）

    Returns:
        (符号信息, 相似度) 列表，按相似度降序

    Example:
        >>> matches = fuzzy_lookup("DN100闸阀")
        >>> matches[0][0].label
        '闸阀'
    """
    text = text.strip()
    matches = []

    for symbol in _SYMBOL_DEFINITIONS.values():
        label = symbol.label

        # 精确匹配
        if label in text or text in label:
            score = 1.0
        # 部分匹配（如"消声止回阀"匹配"止回阀"）
        elif any(word in text for word in label.split() if len(word) >= 2):
            score = 0.8
        # 类别关键字匹配
        elif symbol.category in text:
            score = 0.3
        else:
            continue

        matches.append((symbol, score))

    return sorted(matches, key=lambda x: -x[1])


def get_statistics() -> dict[str, int]:
    """获取符号库统计信息"""
    stats = {'total': len(_SYMBOL_DEFINITIONS)}
    for category in get_all_categories():
        stats[category] = len(get_symbols_by_category(category))
    return stats
