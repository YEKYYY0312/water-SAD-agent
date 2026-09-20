#!/usr/bin/env python3
"""诊断 P-02 图纸的全局系统类型推断。"""

from pathlib import Path
from water_agent.drawing.dxf_reader import read_drawing
from water_agent.drawing.mapping import map_pipes
from water_agent.runtime.governance.pipeline import ToolPolicy

# 读取 P-02 图纸（使用 real_samples 目录）
samples_dir = Path('data/drawings/real_samples')
policy = ToolPolicy(allowed_read_dirs=[samples_dir])
extraction = read_drawing('P-02', root=samples_dir, policy=policy, max_entities=5000)
result = map_pipes(extraction)

# 统计系统类型分布
system_counts = {}
total_pipes = len(result.pipes)

for pipe in result.pipes:
    system = pipe.system or 'None'
    system_counts[system] = system_counts.get(system, 0) + 1

print('=' * 60)
print('P-02 图纸系统类型分布')
print('=' * 60)
print(f'总管段数: {total_pipes}')
print()
for system, count in sorted(system_counts.items(), key=lambda x: -x[1]):
    ratio = count / total_pipes * 100
    print(f'{system:20s}: {count:4d} ({ratio:5.1f}%)')
print()

# 查看全局推断信息
print('=' * 60)
print('全局推断 Assumptions')
print('=' * 60)
for i, assumption in enumerate(result.assumptions, 1):
    print(f'{i}. {assumption}')
print()

# 统计小管径管段（DN < 300）的图层分布
print('=' * 60)
print('小管径管段（DN < 300）的图层分布')
print('=' * 60)
small_pipes = [p for p in result.pipes if p.dn_mm is not None and p.dn_mm < 300]
layer_counts = {}
for pipe in small_pipes:
    layer_counts[pipe.layer] = layer_counts.get(pipe.layer, 0) + 1

print(f'小管径管段总数: {len(small_pipes)}')
print()
for layer, count in sorted(layer_counts.items(), key=lambda x: -x[1]):
    print(f'{layer:30s}: {count:3d}')
print()

# 查看前 5 个小管径管段的详细信息
print('=' * 60)
print('前 5 个小管径管段详情')
print('=' * 60)
for i, pipe in enumerate(small_pipes[:5], 1):
    print(f'{i}. handle={pipe.handle}, layer={pipe.layer}')
    print(f'   system={pipe.system}, dn_mm={pipe.dn_mm}')
    print(f'   evidence_handles={pipe.evidence_handles}')
    print()
