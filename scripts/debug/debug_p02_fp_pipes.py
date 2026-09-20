#!/usr/bin/env python3
"""诊断 P-02 的 2 个误报管段：为什么 DN110 会被判定为 drainage？"""

from pathlib import Path
from water_agent.drawing.dxf_reader import read_drawing
from water_agent.drawing.mapping import map_pipes
from water_agent.runtime.governance.pipeline import ToolPolicy

# 读取 P-02 图纸
samples_dir = Path('data/drawings/real_samples')
policy = ToolPolicy(allowed_read_dirs=[samples_dir])
extraction = read_drawing('P-02', root=samples_dir, policy=policy, max_entities=5000)
result = map_pipes(extraction)

# 找到那 2 个误报管段
target_handles = {'2167', '236B'}
fp_pipes = [p for p in result.pipes if p.handle in target_handles]

print('=' * 60)
print('P-02 误报管段诊断')
print('=' * 60)

for pipe in fp_pipes:
    print(f'\nhandle={pipe.handle}')
    print(f'  layer: {pipe.layer}')
    print(f'  system: {pipe.system}')
    print(f'  dn_mm: {pipe.dn_mm}')
    print(f'  length_m: {pipe.length_m}')
    print(f'  evidence_handles: {pipe.evidence_handles}')
    print(f'  ready_for_materials: {pipe.ready_for_materials}')

# 查看这些管段附近的标注文本
print('\n' + '=' * 60)
print('关联标注文本')
print('=' * 60)

evidence_handles = set()
for pipe in fp_pipes:
    evidence_handles.update(pipe.evidence_handles)

for entity in extraction.entities:
    if entity.handle in evidence_handles:
        print(f'\nhandle={entity.handle}, kind={entity.kind.value}')
        print(f'  text: {entity.text!r}')
        print(f'  layer: {entity.layer}')

# 统计 P-02 中所有 DN < 200 且 system=drainage 的管段
print('\n' + '=' * 60)
print('所有 DN < 200 且 system=drainage 的管段')
print('=' * 60)

small_drainage = [p for p in result.pipes
                  if p.system == 'drainage' and p.dn_mm is not None and p.dn_mm < 200]

print(f'总数: {len(small_drainage)}')
for pipe in small_drainage[:10]:  # 只显示前 10 个
    print(f'  handle={pipe.handle}, layer={pipe.layer}, dn_mm={pipe.dn_mm}')
