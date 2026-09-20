#!/usr/bin/env python3
"""诊断：PUMP-03 中的块名分布"""
import sys
sys.path.insert(0, 'src')

import ezdxf
from collections import Counter

doc = ezdxf.readfile('data/drawings/real_samples/PUMP-03.dxf')
msp = doc.modelspace()

inserts = [e for e in msp if e.dxftype() == 'INSERT']
block_names = [e.dxf.name for e in inserts]

print(f'INSERT 总数: {len(inserts)}\n')

# 统计块名分布
counter = Counter(block_names)
print('块名分布（按使用次数排序）:\n')
for name, count in counter.most_common(20):
    print(f'  {name}: {count} 次')
