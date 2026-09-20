# P3 任务简介：INSERT 块内容展开

## 🎯 任务目标

让图纸审查链路能够"看到"DXF INSERT 块内部的内容（几何实体和文字标注）。

**当前问题**：
- `dxf_reader` 只识别 INSERT 实体本身，不展开块内部
- PUMP-03 有 3,285 个 INSERT / 共 5,729 个图元 = **57.3% 的内容在块里看不到**
- 16/18 张图纸都含 INSERT 块（合计 4,037 个）

**目标效果**：
- 展开后审查链路可见内容：43% → 100%
- 块内的管道、标注、设备都能被审查规则检查到

---

## 📚 背景知识

### 什么是 INSERT 块？

DXF/DWG 的 **块（BLOCK）** 类似编程中的"函数定义"：
- 块定义（BLOCK）：一组图元的集合，存在 BLOCKS 节
- 块参照（INSERT）：对块定义的引用，可以重复使用，带变换（位移/旋转/缩放）

**类比**：
```python
# 块定义 = 函数定义
def pump_symbol():
    draw_circle(0, 0, 10)
    draw_text(0, 0, "P")

# 块参照 = 函数调用（带参数）
pump_symbol(x=100, y=200, scale=1.5, rotation=45°)
pump_symbol(x=300, y=400, scale=2.0, rotation=0°)
```

**真实例子**：
- 泵房图中的水泵、阀门、法兰符号都定义成块
- 一个水泵符号可能被引用 10 次（10 个 INSERT）
- 每个 INSERT 只存位置和变换参数，不存图元本体

### 为什么需要展开？

块参照（INSERT）只是一个"指针"，包含：
- 块名称（如 `PUMP-001`）
- 插入点坐标（如 `(100, 200)`）
- 变换参数（缩放、旋转）

审查链路需要的是**展开后的实际图元**：
- 块内的圆（CIRCLE）：水泵外轮廓
- 块内的文字（TEXT）：设备编号、管径标注
- 块内的线（LINE）：连接管道

**不展开的后果**：
- 块内管径标注 `DN100` 看不到 → 无法检查管径是否合规
- 块内设备位置看不到 → 无法检查间距是否合规

---

## 🔧 技术方案

### 使用 ezdxf 的 `virtual_entities()` 方法

ezdxf 提供了现成的块展开 API：

```python
import ezdxf

doc = ezdxf.readfile("PUMP-03.dxf")
msp = doc.modelspace()

for entity in msp:
    if entity.dxftype() == 'INSERT':
        # 展开块参照
        for virtual_entity in entity.virtual_entities():
            # virtual_entity 是展开后的实体
            # 坐标已应用块的变换（位移/旋转/缩放）
            process_entity(virtual_entity)
```

**核心要点**：
1. `virtual_entities()` 返回**生成器**，需要遍历
2. 展开后的实体坐标是**最终坐标**（已应用变换）
3. 块嵌套（块里引用块）会**递归展开**
4. 每个 INSERT 独立展开，互不影响

### 修改位置

**主文件**：`src/water_agent/drawing/dxf_reader.py`

当前逻辑（简化）：
```python
for entity in msp:
    if entity.dxftype() == 'TEXT':
        entities.append(_extract_text(entity))
    elif entity.dxftype() == 'LINE':
        entities.append(_extract_line(entity))
    elif entity.dxftype() == 'INSERT':
        entities.append(_extract_insert(entity))  # 只记录 INSERT 本身
```

需要改成：
```python
for entity in msp:
    if entity.dxftype() == 'INSERT':
        # 展开块，递归处理块内实体
        for virtual_entity in entity.virtual_entities():
            # 递归调用，处理块内的 TEXT/LINE/CIRCLE 等
            process_virtual_entity(virtual_entity)
    elif entity.dxftype() == 'TEXT':
        entities.append(_extract_text(entity))
    # ... 其他类型
```

**注意事项**：
1. 块可能包含任何类型的实体（TEXT/LINE/CIRCLE/ARC/POLYLINE/INSERT）
2. 块可能嵌套（块里引用块），`virtual_entities()` 会递归处理
3. 需要保留 `source_block` 信息（记录这个实体来自哪个块）
4. 匿名块（`*U`/`*D` 前缀）需要特殊处理

---

## ✅ 验收标准

### 功能验收

- [ ] PUMP-03 的 3,285 个 INSERT 块全部展开
- [ ] 块内几何实体（LINE/CIRCLE/ARC/POLYLINE）可见
- [ ] 块内文本实体（TEXT/MTEXT）可见
- [ ] 块嵌套正确处理（块里引用块）
- [ ] 块变换正确应用（旋转/缩放/位移后坐标正确）
- [ ] 匿名块（`*U`/`*D`）正确处理

### 性能验收

- [ ] PUMP-03（5,729 图元）解析时间 < 5 秒
- [ ] 其他图纸解析时间无明显增加（< 2 秒）

### 测试验收

- [ ] 单元测试：测试简单块展开（1 个 INSERT，块内 2-3 个图元）
- [ ] 单元测试：测试块嵌套展开（块 A 引用块 B）
- [ ] 单元测试：测试块变换（旋转 45°，缩放 2.0）
- [ ] 集成测试：用 PUMP-03 真实图纸验证

### 回归验证

- [ ] 其他 17 张图纸的解析结果无变化
- [ ] `test_dxf_reader.py` 全部通过
- [ ] 不引入新的 INSERT 相关 assumptions/errors

---

## 📊 预期效果

### 展开前（当前）

```python
# PUMP-03.dxf 解析结果
{
  "entity_count": 2444,  # 只看到模型空间的图元
  "entities": [
    {"kind": "INSERT", "block_name": "PUMP-001", ...},  # 块本身
    {"kind": "INSERT", "block_name": "VALVE-003", ...},
    # ... 块内的内容看不到
  ]
}
```

**可见率**：2444 / 5729 = **42.7%**

### 展开后（目标）

```python
# PUMP-03.dxf 解析结果
{
  "entity_count": 5729,  # 包含块内展开的图元
  "entities": [
    # INSERT PUMP-001 展开后的内容
    {"kind": "CIRCLE", "layer": "设备", "source_block": "PUMP-001", ...},
    {"kind": "TEXT", "text": "DN100", "source_block": "PUMP-001", ...},
    # INSERT VALVE-003 展开后的内容
    {"kind": "CIRCLE", "layer": "阀门", "source_block": "VALVE-003", ...},
    {"kind": "LINE", ..., "source_block": "VALVE-003", ...},
    # ... 全部展开
  ]
}
```

**可见率**：5729 / 5729 = **100%**

---

## ⚠️ 注意事项

### 1. 坐标变换

展开后的坐标是**最终坐标**，不是块定义中的原始坐标。

**例子**：
```
块定义中：圆心 (0, 0)，半径 10
INSERT 参数：插入点 (100, 200)，缩放 1.5，旋转 0°
展开后：圆心 (100, 200)，半径 15  # ezdxf 自动算好了
```

### 2. 块嵌套

块可以引用块（类似函数调用函数）。

**例子**：
```
块 A = {圆 + 文字}
块 B = {块 A + 块 A + 线}  # 块 B 引用块 A 两次
INSERT 块 B
```

`virtual_entities()` 会递归展开，最终得到：圆 + 文字 + 圆 + 文字 + 线

### 3. 匿名块

有些块没有显式名称，用 `*U` 或 `*D` 前缀标识（如 `*U123`）。

这些是 AutoCAD 自动生成的：
- `*U` = 用户创建的匿名块
- `*D` = 尺寸标注等系统块

处理方式相同，只是名称特殊。

### 4. 性能考虑

- 展开会增加实体数量（3-5 倍很正常）
- `virtual_entities()` 是生成器，按需计算，内存友好
- 如果图纸特别大（>10,000 图元），考虑分批处理

---

## 📝 实现检查清单

### Step 1: 理解现有代码

- [ ] 阅读 `src/water_agent/drawing/dxf_reader.py`
- [ ] 找到处理 INSERT 的代码位置
- [ ] 理解 `_extract_insert()` 函数的实现

### Step 2: 实现块展开

- [ ] 修改主循环，遇到 INSERT 时调用 `virtual_entities()`
- [ ] 递归处理展开后的实体（可能是 TEXT/LINE/CIRCLE/INSERT）
- [ ] 为展开的实体添加 `source_block` 字段
- [ ] 处理匿名块（`*U`/`*D` 前缀）

### Step 3: 更新数据模型

- [ ] 在 `DrawingEntity` 中添加 `source_block: str | None` 字段
- [ ] 更新相关 TypedDict/dataclass 定义

### Step 4: 编写测试

- [ ] 用 `ezdxf.new()` 合成简单测试图纸（1 个块，3 个图元）
- [ ] 测试块嵌套场景
- [ ] 测试块变换场景（旋转/缩放）
- [ ] 用 PUMP-03 真实图纸做集成测试

### Step 5: 回归验证

- [ ] 运行 `pytest tests/test_dxf_reader.py`
- [ ] 验证其他 17 张图纸的解析结果
- [ ] 检查性能（PUMP-03 < 5 秒）

### Step 6: 文档更新

- [ ] 更新 `dxf_reader.py` 的 docstring
- [ ] 在 `MANIFEST.md` 中更新"已知限制"（移除 INSERT 不展开）
- [ ] 更新测试文档

---

## 🚀 快速启动

### 1. 查看 PUMP-03 当前状态

```bash
_venv/bin/python -c "
from water_agent.drawing.dxf_reader import read_dxf_file

result = read_dxf_file('data/drawings/real_samples/PUMP-03.dxf')
print(f\"图元总数: {len(result['entities'])}\")
print(f\"INSERT 数量: {sum(1 for e in result['entities'] if e['type'] == 'INSERT')}\")
"
```

### 2. 试用 ezdxf 的 virtual_entities

```python
import ezdxf

doc = ezdxf.readfile("data/drawings/real_samples/PUMP-03.dxf")
msp = doc.modelspace()

insert_count = 0
virtual_count = 0

for entity in msp:
    if entity.dxftype() == 'INSERT':
        insert_count += 1
        for virtual_entity in entity.virtual_entities():
            virtual_count += 1
            print(f"  展开: {virtual_entity.dxftype()} @ {virtual_entity.dxf.layer}")

print(f"\nINSERT 总数: {insert_count}")
print(f"展开后图元数: {virtual_count}")
```

### 3. 参考文档

- ezdxf 文档：https://ezdxf.readthedocs.io/en/stable/tutorials/blocks.html
- `virtual_entities()` API：https://ezdxf.readthedocs.io/en/stable/dxfentities/insert.html#ezdxf.entities.Insert.virtual_entities
- P3 规格：`docs/design/13-p3-drawing-review.md`

---

## 📈 成功标准

完成 P3 后，项目应具备：

1. **完整可见性**：PUMP-03 的 5,729 个图元全部可见（100%）
2. **审查能力提升**：块内管径、设备、标注都能被规则检查
3. **测试覆盖**：块展开、嵌套、变换都有测试
4. **性能可接受**：单张图纸 < 5 秒
5. **无回归**：其他 17 张图纸解析正常

完成后，P4 的召回率验收才有意义（当前链路能看到完整内容）。
