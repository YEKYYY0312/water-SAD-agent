# P2 任务完成报告

**任务名称**: 真实图纸样本采集和分析  
**完成日期**: 2026-09-15  
**执行者**: Cursor (自动化) + Claude (验证)  

---

## ✅ 验收结果：通过

所有验收标准已满足，P2 任务**完成并通过验收**。

---

## 📊 交付物清单

### 1. 真实图纸样本（18 份 DXF）

已采集并脱敏处理的真实污水处理厂图纸，存放于 `data/drawings/real_samples/`：

| 类型 | 数量 | 样本 ID |
|------|------|---------|
| 平面图 (P) | 7 | P-01 ~ P-07 |
| 系统图 (S) | 5 | S-01 ~ S-05 |
| 大样图 (D) | 3 | D-01 ~ D-03 |
| 泵房图 (PUMP) | 3 | PUMP-01 ~ PUMP-03 |
| **合计** | **18** | - |

### 2. 样本清单文档

- **MANIFEST.md**: 详细记录每张图纸的元信息（类型、图层数、实体数、DXF版本、单位等）
- **位置**: `data/drawings/real_samples/MANIFEST.md`
- **入库状态**: ✅ 已入 git（仅非识别性元信息）

### 3. 验证报告

- **可读性验证**: `data/eval/reports/real_samples_readability.json`
- **PII 扫描**: `data/eval/reports/real_samples_pii_scan.json`

### 4. 自动化脚本

| 脚本 | 功能 | 位置 |
|------|------|------|
| `convert_dwg_batch.sh` | DWG → DXF 批量转换 | `scripts/` |
| `desensitize.py` | DXF 脱敏处理 | `src/water_agent/drawing/` |
| `archive_drawing_samples.py` | 样本归档和重命名 | `scripts/` |
| `verify_drawing_samples.py` | 可读性验证 | `scripts/` |
| `scan_drawing_pii.py` | PII 泄漏扫描 | `scripts/` |

---

## 🎯 验收标准对照

根据 P2 任务书的验收标准：

| # | 验收标准 | 要求 | 实际 | 状态 |
|---|----------|------|------|------|
| 1 | 样本数量 | 10-20 张 | **18 张** | ✅ |
| 2 | 全部可解析 | 100% | **18/18** (100%) | ✅ |
| 3 | 覆盖多种图纸类型 | ≥2 类 | **4 类**（平面/系统/大样/泵房） | ✅ |
| 4 | 识别 INSERT 实体 | ≥5 张 | **16/18** 张（合计 4,037 个） | ✅ |
| 5 | 识别 ARC 实体 | ≥5 张 | **15/18** 张（合计 926 个） | ✅ |
| 6 | 生成 MANIFEST | 有 | ✅ `MANIFEST.md` | ✅ |
| 7 | 无敏感信息泄漏 | 0 处 | **18/18 干净**（`undecodable_total = 0`） | ✅ |
| - | 单份文件大小 | ≤2 MB | 最大 **1.76 MB** (P-05) | ✅ |
| - | 合计文件大小 | ≤30 MB | **13.2 MB** | ✅ |

**注意事项约束**（任务书额外要求）：

| # | 约束 | 状态 |
|---|------|------|
| 1 | 原始 DWG 不删除 | ✅ 保留在 `~/文稿/363污水处理厂cad图/` |
| 2 | 样本不入 git | ✅ `.gitignore` 覆盖 `data/drawings/real_samples/*` |

---

## 📈 技术统计

### 图纸规模

- **总实体数**: 36,605
- **总图层数**: 342（中位数 10 层/图）
- **总文本数**: 3,967（TEXT + MTEXT）
- **DXF 版本**: 全部 **AC1032** (AutoCAD 2018)

### 实体类型分布

| 类型 | 数量 | 占比 |
|------|------|------|
| LINE | 20,437 | 55.8% |
| INSERT | 4,037 | 11.0% |
| LWPOLYLINE | 3,901 | 10.7% |
| TEXT | 3,436 | 9.4% |
| CIRCLE | 1,880 | 5.1% |
| ARC | 926 | 2.5% |
| 其他 | 1,988 | 5.5% |

### 单位声明情况

- **已声明单位**: 4/18 (22.2%)
  - P-01: INSUNITS=1 (英寸)
  - P-03, P-04, P-05: INSUNITS=4 (毫米)
- **未声明单位**: 14/18 (77.8%)
  - 影响：长度类结论需人工核对，不能直接采信数值

### PII 扫描结果

- **扫描文本值**: 26,147 个（组码 1 & 3）
- **用户 PII 泄漏**: **0 处** ✅
- **厂商样板**: 46 处（CAD 软件自身写入，已判定为非用户 PII）
  - `expresstools@autodesk.com` 等邮箱：28 处
  - `www.tangent.com.cn` 等 URL：18 处
- **编码损坏**: **0 处** ✅

---

## 🔍 已知限制（不影响验收）

根据 MANIFEST.md §4，以下是样本集的**已知边界**（不是缺陷）：

### 1. 单位多数未声明

仅 4/18 声明了 `$INSUNITS`，其余 14 张的单位只能从图面文字推断。  
**影响**: 长度类结论在这 14 张上需人工核对，不能直接采信数值。

### 2. INSERT 块内容不展开

`dxf_reader` 识别 INSERT 为实体类型（样本内共 4,037 个），但不做 `virtual_entities` / `explode`。  
**影响**: 块参照内的几何与文字对审查链**不可见**。  
**最严重案例**: PUMP-03 有 3,285 个 INSERT / 共 5,729 个图元，**超过一半的图纸内容在块里**。

### 3. PII 扫描的覆盖边界

复扫口径是**组码流里的文本值**（26,147 个），覆盖 ENTITIES / BLOCKS / CLASSES / HEADER 全部节的组码 1 与 3。  
**不覆盖**: 
- 二进制形态的 XDATA
- 缩略图（已整段删除）
- **非文本的可识别信息**（例如图形本身绘制的院徽、图签里作为图元画的字）

**风险控制**: 后者需要人眼过一遍（已在脱敏流程中处理）。

### 4. 厂商样板未删

CLASSES 节有 46 处 CAD 软件自身写入的代理类注册记录。  
**判定**: 非用户 PII 且改写有破坏代理实体解析的风险，故保留。  
**透明性**: 在复扫报告里单列供人工复核，**不做静默忽略**。

### 5. 真实缺陷召回率仍未验收 ⚠️

本样本集尚未接入 `drawing_eval`，真实缺陷标注（gold）未建立。  
**结论边界**: 本 MANIFEST 只证明"样本可读、覆盖多类型、无 PII 泄漏"，**不构成审查质量的证据**。

---

## 🛠️ 技术亮点

### 1. 完整的自动化流水线

从 DWG 扫描 → 转换 → 脱敏 → 归档 → 验证 → PII 扫描，全流程可编程化重现：

```bash
# 1. DWG → DXF
bash scripts/convert_dwg_batch.sh --map <mapping.json> --out /tmp/dxf_converted

# 2. 脱敏
_venv/bin/python -m water_agent.drawing.desensitize \
    --in /tmp/dxf_converted --out /tmp/dxf_desens \
    --config data/drawings/desensitize_terms.json

# 3. 归档
_venv/bin/python scripts/archive_drawing_samples.py \
    --src /tmp/dxf_desens --map /tmp/p2_samples/classification.json \
    --out data/drawings/real_samples --provenance /tmp/p2_samples/provenance.json

# 4. 可读性验证
_venv/bin/python scripts/verify_drawing_samples.py \
    --root data/drawings/real_samples \
    --report data/eval/reports/real_samples_readability.json

# 5. PII 扫描
_venv/bin/python scripts/scan_drawing_pii.py data/drawings/real_samples \
    --report data/eval/reports/real_samples_pii_scan.json
```

### 2. 强制验证机制

- **PII 扫描默认阻断**: `undecodable` 非零时退出码 1（「无法判定 ≠ 通过」）
- **编码损坏检测**: 自动识别 CP1252 重编码损坏（如原 P-01 案例）
- **阴性对照**: 脱敏前必须扫出命中，否则「脱敏后 0 命中」不构成证据

### 3. 可追溯但隐私安全的设计

- **样本本体不入库**: `.gitignore` 排除 `data/drawings/real_samples/*.dxf`
- **溯源映射隔离**: `provenance.json` 写在仓库外，绝不允许入库
- **MANIFEST 可审阅**: 只含非识别性元信息（类型、图层数、实体数、版本、单位），可入库作为凭证

---

## 🚀 后续工作（P2.5 及以后）

P2 任务**完成**，但 P2.5 及后续阶段仍需：

### P2.5: 人工标注缺陷 ground truth

1. 对 18 份样本逐张人工审查，标注真实缺陷
2. 建立 `drawing_p1` 真实评测集
3. 编写标注规范和案例库

### P3: 补充 INSERT/ARC 实体支持

1. 实现 INSERT 块展开（`virtual_entities` / `explode`）
2. 补充 ARC 圆弧的几何计算
3. 回归测试确保现有 8/8 基线不下降

### P4: 真实召回率验收

1. 在 `drawing_p1` 上运行审查链路
2. 计算缺陷召回率（目标 ≥80%）
3. 根因分析漏检案例并优化

---

## 📝 复盘与教训

### 做得好的地方

1. ✅ **自动化优先**: 整个流程可重现，单份样本替换只需重跑 3 步
2. ✅ **验证先行**: 每个环节都有检查点（可读性、PII、编码损坏）
3. ✅ **失败透明**: PII 扫描的厂商样板单列供复核，不做静默忽略
4. ✅ **文档完备**: MANIFEST 详尽记录元信息和已知限制

### 遇到的坑

1. **P-01 编码损坏**: 原样本有 58/111 条文本是 CP1252 重编码损坏，导致 PII 扫描无法判定。
   - **处置**: 替换为干净样本，并固化检测逻辑到 `scan_drawing_pii.py`
   - **教训**: 编码损坏不可逆，检测必须常驻并阻断
   
2. **单位声明缺失**: 14/18 张图纸未声明 `$INSUNITS`，影响长度类结论可信度。
   - **处置**: 在 MANIFEST 中明确标注为「已知限制」
   - **教训**: 真实图纸的元数据质量参差不齐，需人工复核

3. **INSERT 块内容不可见**: PUMP-03 超过一半的内容在块里，对审查链不可见。
   - **处置**: 列为 P3 待办（块展开）
   - **教训**: 真实图纸大量使用块引用，必须支持才能覆盖完整内容

---

## ✅ 结论

**P2 任务已完成并通过验收**。交付物齐全，所有验收标准满足，自动化流程完备，文档详实。

下一步：启动 P2.5（人工标注缺陷 gold）和 P3（INSERT/ARC 支持）。

---

**报告生成时间**: 2026-09-15  
**版本**: v1.0  
**审核状态**: ✅ 已验证（Claude Opus 5）
