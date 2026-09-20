#!/usr/bin/env python3
"""测试标注文本的系统类型推断。"""

from water_agent.drawing.mapping import infer_system_from_annotation

test_texts = [
    '网孔钢管复合塑料管段',
    '1 DN110',
    'DN200污水管',
    '给水立管 DN100',
    'DN150',
]

print('标注文本 → 系统类型推断测试')
print('=' * 60)
for text in test_texts:
    system = infer_system_from_annotation(text)
    print(f'{text:30s} → {system}')
