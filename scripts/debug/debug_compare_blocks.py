#!/usr/bin/env python3
"""对比符号库与所有真实图纸的块名交集"""
import sys
sys.path.insert(0, 'src')

import ezdxf
from pathlib import Path
from collections import Counter

# 1. 读取符号库中的块名
symbol_doc = ezdxf.readfile('/Users/yaerken/Downloads/图例汇总.dxf')
symbol_blocks = set(b.name for b in symbol_doc.blocks if not b.name.startswith('*'))
print(f'符号库块定义: {len(symbol_blocks)} 个\n')

# 2. 读取所有真实图纸中使用的块名
samples_dir = Path('data/drawings/real_samples')
all_used_blocks = Counter()

for dxf_file in sorted(samples_dir.glob('*.dxf')):
    doc = ezdxf.readfile(dxf_file)
    msp = doc.modelspace()
    inserts = [e for e in msp if e.dxftype() == 'INSERT']
    block_names = [e.dxf.name for e in inserts]
    all_used_blocks.update(block_names)

print(f'真实图纸中使用的不同块名: {len(all_used_blocks)} 个')
print(f'真实图纸中 INSERT 总数: {sum(all_used_blocks.values())}\n')

# 3. 找交集
intersection = symbol_blocks & set(all_used_blocks.keys())
print(f'【交集】符号库与真实图纸共同的块名: {len(intersection)} 个\n')

if intersection:
    print('共同块名:')
    for name in sorted(intersection):
        print(f'  {name} (真实图纸中使用 {all_used_blocks[name]} 次)')
else:
    print('无交集 —— 符号库与真实图纸使用完全不同的块命名体系')
    print()
    print('符号库块名示例（前 10 个）:')
    for name in sorted(symbol_blocks)[:10]:
        print(f'  {name}')
    print()
    print('真实图纸块名示例（前 10 个，按使用频率）:')
    for name, count in all_used_blocks.most_common(10):
        print(f'  {name}: {count} 次')
