#!/usr/bin/env python3
"""测试 _layer_system 对特定图层的识别。"""

from water_agent.drawing.dxf_reader import _layer_system

test_layers = [
    '$TD_AUDIT_GENERATED_(1613)',
    '0',
    'W-DRAN',  # 已知的排水图层
    'W-WATR',  # 已知的给水图层
]

print('图层名 → 系统类型识别测试')
print('=' * 60)
for layer in test_layers:
    system = _layer_system(layer)
    print(f'{layer:40s} → {system}')
