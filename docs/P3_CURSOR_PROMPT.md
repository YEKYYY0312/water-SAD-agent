# P3 任务：INSERT 块内容展开 - Cursor 执行指令

你是一名有经验的 Python 工程师，现在需要实现 DXF INSERT 块的展开功能。

## 任务概述

**目标**：让 `src/water_agent/drawing/dxf_reader.py` 能够展开 DXF INSERT 块内部的内容。

**背景**：
- 当前代码只识别 INSERT 实体本身，不展开块内部
- PUMP-03.dxf 有 3,285 个 INSERT，占 57.3% 的图元，审查链路完全看不到块内内容
- 需要用 ezdxf 的 `virtual_entities()` 方法展开块

## 执行步骤

### Step 1: 理解现有代码结构

**任务**：
1. 打开 `src/water_agent/drawing/dxf_reader.py`
2. 找到 `read_dxf_file()` 函数
3. 找到处理 INSERT 实体的代码位置
4. 理解当前 `_extract_insert()` 的实现

**输出**：告诉我当前代码在哪一行处理 INSERT，以及现在如何处理的。

---

### Step 2: 查看 PUMP-03 当前解析结果

**任务**：运行以下代码，查看展开前的状态

```bash
_venv/bin/python -c "
from water_agent.drawing.dxf_reader import read_dxf_file
import json

result = read_dxf_file('data/drawings/real_samples/PUMP-03.dxf')
print(f'图元总数: {len(result[\"entities\"])}')
print(f'INSERT 数量: {sum(1 for e in result[\"entities\"] if e[\"type\"] == \"INSERT\")}')
print(f'TEXT 数量: {sum(1 for e in result[\"entities\"] if e[\"type\"] == \"TEXT\")}')
print(f'LINE 数量: {sum(1 for e in result[\"entities\"] if e[\"type\"] == \"LINE\")}')
"
```

**输出**：告诉我展开前有多少图元。

---

### Step 3: 实验 ezdxf 的 virtual_entities

**任务**：创建实验脚本，验证 ezdxf 的 `virtual_entities()` 工作方式

```bash
cat > test_virtual_entities.py << 'EOF'
"""实验 ezdxf 的 virtual_entities() 方法"""
import ezdxf

doc = ezdxf.readfile("data/drawings/real_samples/PUMP-03.dxf")
msp = doc.modelspace()

insert_count = 0
virtual_count = 0
virtual_types = {}

for entity in msp:
    if entity.dxftype() == 'INSERT':
        insert_count += 1
        if insert_count <= 3:  # 只看前 3 个块
            print(f"\n块 {insert_count}: {entity.dxf.name}")
            for virtual_entity in entity.virtual_entities():
                virtual_count += 1
                vtype = virtual_entity.dxftype()
                virtual_types[vtype] = virtual_types.get(vtype, 0) + 1
                if virtual_count <= 10:  # 只打印前 10 个
                    print(f"  - {vtype} @ layer={virtual_entity.dxf.layer}")

print(f"\n总计:")
print(f"  INSERT 数量: {insert_count}")
print(f"  展开后图元类型分布: {virtual_types}")
EOF

_venv/bin/python test_virtual_entities.py
```

**输出**：告诉我展开后都有哪些类型的图元。

---

### Step 4: 修改 dxf_reader.py 实现块展开

**任务**：修改 `src/water_agent/drawing/dxf_reader.py`

**修改要点**：

1. **在主循环中处理 INSERT**：
   ```python
   for entity in msp:
       if entity.dxftype() == 'INSERT':
           # 展开块内容
           try:
               for virtual_entity in entity.virtual_entities():
                   # 递归处理展开后的图元
                   extracted = _extract_entity(virtual_entity, source_block=entity.dxf.name)
                   if extracted:
                       entities.append(extracted)
           except Exception as e:
               # 展开失败不中断，记录错误
               errors.append(f"INSERT {entity.dxf.handle} 展开失败: {str(e)}")
       else:
           # 原有逻辑
           extracted = _extract_entity(entity, source_block=None)
           if extracted:
               entities.append(extracted)
   ```

2. **添加 `_extract_entity()` 统一分派函数**：
   ```python
   def _extract_entity(entity, source_block: str | None = None) -> dict | None:
       """统一提取图元，支持 source_block 标记"""
       entity_type = entity.dxftype()
       
       if entity_type == 'TEXT':
           return _extract_text(entity, source_block)
       elif entity_type == 'MTEXT':
           return _extract_mtext(entity, source_block)
       elif entity_type == 'LINE':
           return _extract_line(entity, source_block)
       elif entity_type == 'CIRCLE':
           return _extract_circle(entity, source_block)
       elif entity_type == 'ARC':
           return _extract_arc(entity, source_block)
       elif entity_type == 'POLYLINE' or entity_type == 'LWPOLYLINE':
           return _extract_polyline(entity, source_block)
       elif entity_type == 'INSERT':
           # 嵌套块：递归展开
           results = []
           for virtual_entity in entity.virtual_entities():
               extracted = _extract_entity(virtual_entity, source_block=entity.dxf.name)
               if extracted:
                   results.append(extracted)
           return results if results else None
       else:
           return None
   ```

3. **更新各个 `_extract_*()` 函数**，添加 `source_block` 参数：
   ```python
   def _extract_text(entity, source_block: str | None = None) -> dict:
       return {
           "type": "TEXT",
           "handle": entity.dxf.handle,
           "layer": entity.dxf.layer,
           "text": _entity_text(entity),
           "position": (float(entity.dxf.insert.x), float(entity.dxf.insert.y)),
           "source_block": source_block,  # 新增字段
       }
   ```

4. **更新 `DrawingEntity` 类型定义**（如果有 TypedDict）：
   ```python
   class DrawingEntity(TypedDict):
       type: str
       handle: str
       layer: str
       source_block: str | None  # 新增字段
       # ... 其他字段
   ```

**注意**：
- 保持向后兼容：`source_block=None` 表示不是来自块
- 块可能嵌套：INSERT 的 `virtual_entities()` 可能又产生 INSERT
- 展开失败不应中断整个解析：用 try-except 包裹

---

### Step 5: 更新测试

**任务**：在 `tests/test_dxf_reader.py` 中添加块展开测试

```python
def test_insert_block_expansion():
    """测试 INSERT 块展开"""
    # 用 ezdxf 创建简单测试图纸
    doc = ezdxf.new()
    msp = doc.modelspace()
    
    # 定义块
    block = doc.blocks.new('TEST_BLOCK')
    block.add_circle((0, 0), radius=10)
    block.add_text('TEST', dxfattribs={'insert': (0, 0)})
    
    # 插入块
    msp.add_blockref('TEST_BLOCK', (100, 200))
    
    # 保存临时文件
    import tempfile
    with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as f:
        doc.saveas(f.name)
        
        # 解析
        result = read_dxf_file(f.name)
        
        # 验证
        assert result['ok']
        
        # 应该能看到展开后的圆和文字
        circles = [e for e in result['entities'] if e['type'] == 'CIRCLE']
        texts = [e for e in result['entities'] if e['type'] == 'TEXT']
        
        assert len(circles) == 1, "应该展开出 1 个圆"
        assert len(texts) == 1, "应该展开出 1 个文字"
        assert circles[0]['source_block'] == 'TEST_BLOCK'
        assert texts[0]['source_block'] == 'TEST_BLOCK'
```

---

### Step 6: 验证 PUMP-03 展开效果

**任务**：运行修改后的代码，验证 PUMP-03 的展开效果

```bash
_venv/bin/python -c "
from water_agent.drawing.dxf_reader import read_dxf_file

result = read_dxf_file('data/drawings/real_samples/PUMP-03.dxf')
print(f'展开后图元总数: {len(result[\"entities\"])}')
print(f'来自块的图元数: {sum(1 for e in result[\"entities\"] if e.get(\"source_block\"))}')

# 统计类型分布
from collections import Counter
type_counts = Counter(e['type'] for e in result['entities'])
print(f'\\n类型分布:')
for etype, count in sorted(type_counts.items(), key=lambda x: -x[1]):
    print(f'  {etype}: {count}')
"
```

**预期结果**：
- 展开后图元总数应该接近 5,729（当前约 2,444）
- 来自块的图元数应该 > 3,000

---

### Step 7: 运行回归测试

**任务**：确保修改不破坏现有功能

```bash
# 运行所有 dxf_reader 测试
_venv/bin/python -m pytest tests/test_dxf_reader.py -v

# 验证其他 17 张图纸
_venv/bin/python -c "
from pathlib import Path
from water_agent.drawing.dxf_reader import read_dxf_file

drawings_dir = Path('data/drawings/real_samples')
for dxf_file in sorted(drawings_dir.glob('*.dxf')):
    result = read_dxf_file(str(dxf_file))
    status = '✓' if result['ok'] else '✗'
    print(f'{status} {dxf_file.name}: {len(result[\"entities\"])} 图元')
"
```

**预期结果**：
- 所有测试通过
- 所有 18 张图纸都能正常解析（ok=True）

---

### Step 8: 更新文档

**任务**：
1. 更新 `src/water_agent/drawing/dxf_reader.py` 的 docstring
2. 在 `data/drawings/real_samples/MANIFEST.md` 中移除"INSERT 块内容不展开"的已知限制
3. 更新 P3 相关文档

---

## 验收标准

完成后应满足：

- [x] PUMP-03 的图元总数从 2,444 增加到接近 5,729
- [x] 块内的 TEXT/LINE/CIRCLE 都能被提取
- [x] 每个来自块的图元都有 `source_block` 字段
- [x] 块嵌套正确处理（块里引用块）
- [x] 新增测试 `test_insert_block_expansion` 通过
- [x] 所有回归测试通过
- [x] 其他 17 张图纸解析正常
- [x] 性能可接受（PUMP-03 < 5 秒）

---

## 常见问题

### Q1: virtual_entities() 返回什么？

**A**: 返回一个**生成器**，产出展开后的图元（LINE/CIRCLE/TEXT 等）。坐标已应用块的变换。

### Q2: 如何处理块嵌套？

**A**: `virtual_entities()` 自动递归展开。你只需要在 `_extract_entity()` 中再次处理 INSERT 类型即可。

### Q3: 如何处理匿名块（`*U`/`*D`）？

**A**: 与普通块相同，`entity.dxf.name` 会返回 `*U123` 这样的名称，直接用作 `source_block` 即可。

### Q4: 展开失败怎么办？

**A**: 用 try-except 包裹，记录到 `errors` 列表，不中断整个解析。

### Q5: 性能会变慢吗？

**A**: `virtual_entities()` 是生成器，按需计算，内存友好。PUMP-03 从 2,444 → 5,729 图元，解析时间预计增加 1-2 秒，在可接受范围内。

---

## 执行记录

本任务已按现有 P3 接口完成：实际入口为 `read_drawing()`，而不是旧提示中的
`read_dxf_file()`；解析器没有独立的 `_extract_*()` 分派函数，故实现直接复用现有
`DrawingEntity` 构造流程并增加递归展开。

- `INSERT` 保留为原始实体，同时展开 `virtual_entities()` 的可见内容。
- 展开图元携带 `source_block`；嵌套块使用 `OUTER/INNER` 形式的来源链。
- 展开失败写入 `DrawingExtraction.errors`，不阻断其他图元。
- 增加普通块与嵌套块测试。
- `tests/test_dxf_reader.py` 全部通过。
- 真实样本全部 `ok=True`；PUMP-03 为 5,922 个实体，其中 193 个来自块，解析耗时约 0.4 秒。

由于 `PUMP-03.dxf` 的当前基线已经包含 5,729 个模型空间实体，且其中大量 INSERT
为空块或 CAD 代理块，提示中“2,444 → 5,729”的预期不适用于这份工作区样本。
