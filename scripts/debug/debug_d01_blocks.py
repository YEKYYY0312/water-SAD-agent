#!/usr/bin/env python3
"""诊断脚本：查看 D-01 中的 INSERT 块名"""
import sys
sys.path.insert(0, 'src')

import ezdxf

doc = ezdxf.readfile('data/drawings/real_samples/D-01.dxf')
msp = doc.modelspace()

inserts = [e for e in msp if e.dxftype() == 'INSERT']
print(f'INSERT 图元数量: {len(inserts)}\n')

if inserts:
    print('所有 INSERT 块名:')
    for e in inserts[:20]:  # 最多显示 20 个
        print(f'  块名: {e.dxf.name}')
        print(f'  位置: ({e.dxf.insert.x:.1f}, {e.dxf.insert.y:.1f})')
        print(f'  图层: {e.dxf.layer}')
        print()
