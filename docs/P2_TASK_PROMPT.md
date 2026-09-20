# P2 任务：真实图纸样本采集和分析

## 🎯 任务目标

从真实污水处理厂 CAD 图纸中采集 10-20 张脱敏 DXF 样本，用于：
1. 建立真实评测集（drawing_p1）
2. 人工标注缺陷 ground truth
3. 验证并补充 INSERT/ARC 实体支持
4. 完成 P2 里程碑验收

## 📂 资源位置

- **DWG 源文件**：`~/文稿/363污水处理厂cad图/`（数百份真实图纸）
- **ODA File Converter**：已安装（用于 DWG → DXF 转换）
- **目标目录**：`data/drawings/real_samples/`

## 🔧 执行步骤

### Step 1: 扫描并筛选合适的图纸样本

```bash
# 扫描所有 DWG 文件
find ~/文稿/363污水处理厂cad图 -name "*.dwg" -type f > /tmp/dwg_list.txt

# 统计数量和大小分布
wc -l /tmp/dwg_list.txt
du -h ~/文稿/363污水处理厂cad图 | sort -h | tail -20
```

**筛选标准：**
- 文件大小 50KB - 2MB（太小可能是空图，太大解析慢）
- 文件名包含关键词：`平面图`、`系统图`、`给水`、`排水`、`污水`、`泵房`
- 优先选择不同类型的图纸（平面、系统、大样、详图）

请帮我从列表中挑选 **15-20 张**代表性图纸，输出为：
```
/path/to/图纸1.dwg  类型:平面图
/path/to/图纸2.dwg  类型:系统图
...
```

---

### Step 2: 批量转换 DWG → DXF

使用 ODA File Converter 将选定图纸转换为 DXF R2018 格式：

```bash
# 创建转换脚本
# ODA File Converter 命令行用法：
# ODAFileConverter <input_dir> <output_dir> <output_version> <output_format> <recursive> <audit>

# 参数说明：
# - output_version: ACAD2018 (推荐，ezdxf 支持最好)
# - output_format: DXF
# - recursive: 0 (不递归)
# - audit: 1 (审计修复)
```

**请帮我生成批量转换脚本**，要求：
1. 逐个转换选定的 DWG 文件
2. 输出到临时目录 `/tmp/dxf_converted/`
3. 转换为 DXF ACAD2018 格式
4. 记录转换日志（成功/失败/错误信息）

---

### Step 3: 脱敏处理

转换后的 DXF 文件可能包含敏感信息（设计单位、项目名称、人名等），需要脱敏：

```python
# 在 src/water_agent/drawing/ 下创建 desensitize.py

from pathlib import Path
import ezdxf
import hashlib
import re

def desensitize_dxf(input_path: Path, output_path: Path) -> dict:
    """
    脱敏 DXF 文件，移除/替换敏感信息。
    
    返回: {
        'ok': bool,
        'changes': [修改记录],
        'error': str | None
    }
    """
    # TODO: 实现以下脱敏规则
    # 1. 文本实体 (TEXT/MTEXT)：
    #    - 移除手机号 (11位数字)
    #    - 移除邮箱
    #    - 替换设计单位名 → "XX设计院"
    #    - 替换人名特征（审核人、设计人）→ "张工"、"李工"
    # 
    # 2. 图纸元数据：
    #    - $TDCREATE (创建时间) → 保留年份，日期设为 01-01
    #    - $TDUPDATE (修改时间) → 同上
    #    - $AUTHOR → "Anonymized"
    #    - $VERSIONGUID → 重新生成
    # 
    # 3. 保留技术内容：
    #    - 管径标注 (DN100, Φ200)
    #    - 高程标注 (-3.500)
    #    - 图层名称
    #    - 线型、颜色
    #    - 几何图元（坐标、半径等）
    
    pass
```

**请帮我实现完整的脱敏函数**，并提供测试用例。

---

### Step 4: 重命名并复制到项目目录

将脱敏后的 DXF 文件按规范命名并复制到 `data/drawings/real_samples/`：

**命名规范：**
- 平面图：`P-01.dxf`, `P-02.dxf`, ...
- 系统图：`S-01.dxf`, `S-02.dxf`, ...
- 大样图：`D-01.dxf`, `D-02.dxf`, ...
- 泵房图：`PUMP-01.dxf`, `PUMP-02.dxf`, ...

**请帮我生成重命名和复制脚本**。

---

### Step 5: 验证 DXF 可读性

使用项目的 `dxf_reader` 验证所有图纸都能成功解析：

```python
from pathlib import Path
from water_agent.drawing.dxf_reader import read_drawing

samples_dir = Path("data/drawings/real_samples")

for dxf_file in sorted(samples_dir.glob("*.dxf")):
    drawing_id = dxf_file.stem
    result = read_drawing(
        drawing_id,
        root=samples_dir,
        policy=None,  # 临时绕过 ToolPolicy
        pipeline=None
    )
    
    print(f"\n{'='*60}")
    print(f"图纸: {drawing_id}")
    print(f"成功: {result.ok}")
    print(f"DXF 版本: {result.dxf_version}")
    print(f"单位已知: {result.unit_known} (INSUNITS={result.insunits})")
    print(f"图层数: {len(result.layers)}")
    print(f"实体数: {len(result.entities)}")
    
    if result.ok:
        # 统计实体类型分布
        kind_counts = {}
        for entity in result.entities:
            kind_counts[entity.kind] = kind_counts.get(entity.kind, 0) + 1
        print(f"实体类型: {dict(sorted(kind_counts.items()))}")
        
        # 检查是否有 INSERT/ARC（P2 需要补充支持的类型）
        if kind_counts.get('insert', 0) > 0:
            print(f"⚠️  包含 {kind_counts['insert']} 个 INSERT 实体（块引用）")
        if kind_counts.get('arc', 0) > 0:
            print(f"⚠️  包含 {kind_counts['arc']} 个 ARC 实体（圆弧）")
    
    if result.errors:
        print(f"❌ 错误: {result.errors}")
    if result.assumptions:
        print(f"⚠️  假设: {result.assumptions}")
```

**请运行验证脚本并生成报告**：
- 成功解析的图纸数量
- 解析失败的图纸及原因
- 各图纸的实体类型统计
- INSERT/ARC 实体的出现频率

---

### Step 6: 生成图纸样本清单

创建 `data/drawings/real_samples/MANIFEST.md`，记录每张图纸的元信息：

```markdown
# 真实图纸样本清单

采集日期：2026-09-15
来源：污水处理厂设计图纸（已脱敏）
总计：XX 张

## 图纸列表

| 登记号 | 原始文件名 | 类型 | 图层数 | 实体数 | DXF版本 | 单位 | 备注 |
|--------|-----------|------|--------|--------|---------|------|------|
| P-01   | XXX平面图.dwg | 平面图 | 15 | 324 | R2018 | mm | 包含 INSERT |
| S-01   | XXX系统图.dwg | 系统图 | 8  | 156 | R2018 | mm | 包含 ARC |
| ...    | ...       | ...  | ...| ... | ...     | ...  | ... |

## 实体类型统计

- LINE: XXX 个
- POLYLINE: XXX 个
- CIRCLE: XXX 个
- ARC: XXX 个
- TEXT: XXX 个
- MTEXT: XXX 个
- INSERT: XXX 个
- OTHER: XXX 个

## 已知问题

1. XX 张图纸缺失单位声明（INSUNITS=0 或缺失）
2. XX 张图纸包含 INSERT 实体，需补充块引用支持
3. XX 张图纸包含 ARC 实体，需补充圆弧支持
```

**请帮我生成完整的清单文件**。

---

### Step 7: 更新项目文档

更新 `REVIEW_FINDINGS.md` 中的 P2 进度：

```markdown
### P2: 真实图纸样本 ✅ 已完成

**状态**: DONE (2026-09-15)

**交付物**:
- ✅ 采集 XX 张脱敏 DXF 样本（污水处理厂）
- ✅ 验证所有样本可被 `dxf_reader` 成功解析
- ✅ 生成图纸样本清单 `MANIFEST.md`
- ✅ 识别需补充支持的实体类型（INSERT: XX 个，ARC: XX 个）

**下一步**:
- P2.5: 人工标注缺陷 ground truth
- P3: 补充 INSERT/ARC 实体支持
```

---

## ✅ 验收标准

完成后应满足：
1. `data/drawings/real_samples/` 包含 10-20 张脱敏 DXF 文件
2. 所有图纸能被 `dxf_reader` 成功解析（`ok=True`）
3. 图纸覆盖多种类型（平面、系统、大样等）
4. 识别出至少 5 张图纸包含 INSERT 或 ARC 实体
5. 生成完整的 `MANIFEST.md` 清单
6. 无敏感信息泄漏（人名、单位、联系方式）

---

## 📝 注意事项

1. **脱敏优先**：转换后立即脱敏，不要将原始文件提交到 git
2. **gitignore 配置**：确保 `data/drawings/real_samples/*.dxf` 在 `.gitignore` 中（或用 LFS）
3. **文件大小控制**：单个 DXF 文件不超过 2MB，总计不超过 30MB
4. **保留原始 DWG**：不要删除 `~/文稿/363污水处理厂cad图/` 中的原始文件

---

## 🤖 Cursor 执行指令

**请按照上述 Step 1-7 依次执行，每完成一步输出结果并等待确认后再继续下一步。**

开始执行 Step 1: 扫描并筛选合适的图纸样本。
