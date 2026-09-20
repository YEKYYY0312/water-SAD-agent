#!/usr/bin/env python3
"""在 mapping 过程中追踪 handle=2167 的系统类型设置过程。"""

from pathlib import Path
from water_agent.drawing.dxf_reader import read_drawing, _layer_system
from water_agent.drawing.mapping import infer_system_from_annotation, _nearby_annotations, ANNOTATION_MATCH_RADIUS
from water_agent.runtime.governance.pipeline import ToolPolicy

# 读取 P-02 图纸
samples_dir = Path('data/drawings/real_samples')
policy = ToolPolicy(allowed_read_dirs=[samples_dir])
extraction = read_drawing('P-02', root=samples_dir, policy=policy, max_entities=5000)

# 找到目标管段实体
from water_agent.drawing.models import DrawingEntityKind
pipes_raw = [e for e in extraction.entities
             if e.kind in {DrawingEntityKind.LINE, DrawingEntityKind.POLYLINE}
             and len(e.points) >= 2]
texts = [e for e in extraction.entities
         if e.kind in {DrawingEntityKind.TEXT, DrawingEntityKind.MTEXT}
         and e.points]
trusted_texts = [t for t in texts if not t.suspicious]

target_pipe = None
for entity in pipes_raw:
    if entity.handle == '2167':
        target_pipe = entity
        break

if target_pipe:
    print('=' * 60)
    print(f'追踪 handle={target_pipe.handle} 的系统类型识别')
    print('=' * 60)

    print(f'\n1. 图层识别')
    print(f'   layer={target_pipe.layer}')
    system_from_layer = _layer_system(target_pipe.layer)
    print(f'   _layer_system() → {system_from_layer}')

    print(f'\n2. 附近标注')
    nearby = _nearby_annotations(target_pipe, trusted_texts)
    print(f'   找到 {len(nearby)} 个附近标注')
    for i, ann in enumerate(nearby, 1):
        print(f'   {i}. handle={ann.handle}, text={ann.text!r}')
        inferred = infer_system_from_annotation(ann.text or '')
        print(f'      infer_system_from_annotation() → {inferred}')

    print(f'\n3. 最终结果')
    system = system_from_layer
    if system is None and nearby:
        for annotation in nearby:
            inferred = infer_system_from_annotation(annotation.text or '')
            if inferred is not None:
                system = inferred
                print(f'   从标注 {annotation.handle} 推断为 {system}')
                break

    print(f'   system = {system}')
else:
    print('未找到 handle=2167 的管段')
