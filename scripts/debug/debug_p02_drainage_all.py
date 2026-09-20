#!/usr/bin/env python3
"""查看 P-02 所有被识别为 drainage 的管段。"""

from pathlib import Path
from water_agent.drawing.dxf_reader import read_drawing
from water_agent.drawing.mapping import map_pipes
from water_agent.runtime.governance.pipeline import ToolPolicy

# 读取 P-02 图纸
samples_dir = Path('data/drawings/real_samples')
policy = ToolPolicy(allowed_read_dirs=[samples_dir])
extraction = read_drawing('P-02', root=samples_dir, policy=policy, max_entities=5000)
result = map_pipes(extraction)

# 找到所有 drainage 管段
drainage_pipes = [p for p in result.pipes if p.system == 'drainage']

print('=' * 60)
print(f'P-02 所有 drainage 管段（共 {len(drainage_pipes)} 个）')
print('=' * 60)

for i, pipe in enumerate(drainage_pipes, 1):
    print(f'\n{i}. handle={pipe.handle}, layer={pipe.layer}')
    print(f'   dn_mm={pipe.dn_mm}, length_m={pipe.length_m}')
    print(f'   evidence_handles={pipe.evidence_handles}')

    # 查找关联标注的文本
    if pipe.evidence_handles:
        texts = []
        for entity in extraction.entities:
            if entity.handle in pipe.evidence_handles:
                texts.append(entity.text or '')
        print(f'   标注文本: {texts}')
