# 给排水 Agent 项目 Review 报告

**Review 时间**: 2026-09-18  
**Review 范围**: 代码库全量检查  
**门禁状态**: ✅ 通过（退出码 0）

---

## 一、总体评价

### ✅ 核心优势

1. **工程质量高**：门禁通过，2143 条测试全部通过，mypy/ruff 零错误
2. **评测覆盖完备**：8 套 harness 评测集（rag/calc/docgen/material/governance/drawing/multi_agent/memory p0）全部通过
3. **真实样本已采集**：18 份真实设计院 DXF（P×7 / S×5 / D×3 / PUMP×3），覆盖平面/系统/大样/泵房四类
4. **重大进展**：
   - RAG 真实质量从 62.07% 提升至 84.48%（+22.41pp）
   - 图纸召回率 100%（P-03 defect 2/2）
   - 规范版本已核实解锁缓存
   - MIF 转义解码完成（1,320 条 → 残留 0）

### 🔴 关键发现：文档与代码严重不一致

**问题根源**：存在大量未追踪文件（68+ 个），包括：
- 新增核心模块（mif.py / symbol_library.py / symbol_enrichment.py / desensitize.py / cite_traceability）
- 真实评测数据（drawing_p4/p5/p5.1 的 gold / recall results）
- 真实样本图纸（18 份 DXF）
- 阶段性完成报告（P2–P5.1 COMPLETION_REPORT）
- 大量调试脚本

**严重程度**：高 —— AGENTS.md 声称"P3 真实图纸未验证 blocked"，但实际已完成 P2–P5.1 全套工作。

---

## 二、未追踪文件分析

### 2.1 已完成但未入库的核心模块（4 个）

| 模块 | 测试 | 质量 | 用途 | 建议 |
|------|------|------|------|------|
| `src/water_agent/drawing/mif.py` | 19 passed | mypy/ruff ✅ | MIF 转义解码（37% 文本） | **必须入库** |
| `src/water_agent/drawing/symbol_library.py` | 15 passed | mypy/ruff ✅ | 符号库（阀门/设备识别） | **必须入库** |
| `src/water_agent/drawing/symbol_enrichment.py` | 10 passed | mypy/ruff ✅ | INSERT 块语义增强 | **必须入库** |
| `src/water_agent/drawing/desensitize.py` | 18 passed | mypy/ruff ✅ | 图纸脱敏（PII/项目名） | **必须入库** |

**证据**：
- 4 个模块已被 `dxf_reader.py` 引用（生产依赖）
- 68 条测试全部通过
- P3 COMPLETION_REPORT 显示 PUMP-03 成功展开 INSERT（5,729 → 5,922 实体）

### 2.2 真实评测数据（blocked 已解除）

| 评测集 | 样本 | 指标 | 状态 | 建议 |
|--------|------|------|------|------|
| `drawing_p4_defects_gold_v2.jsonl` | 4 张植入缺陷图 | 2 条 gold | 有效 | **必须入库** |
| `drawing_p5.1_recall_results.json` | P-03 defect | 召回率 100% (2/2) | ✅ 通过 | **必须入库** |
| `drawing_p5.1_fp_results.json` | 18 张阴性集 | 误报率 11.1% | ⚠️ 略高 | **必须入库** |

**AGENTS.md 当前声称**：
> P3 真实图纸未验证 blocked：全仓 0 个真实样例图纸

**实际状态**：18 份真实样本 + 召回率验收已完成。

### 2.3 真实样本图纸（18 份 DXF，13.2 MB）

**不入库理由合理**：`.gitignore` 已排除，MANIFEST.md 记录了元数据。

**问题**：MANIFEST.md 也未入库 → 无法证明样本集的存在性与质量。

### 2.4 文档类（11 份完成报告 + 多份任务规划）

```
docs/P2_COMPLETION_REPORT.md         (9 KB, 2026-09-15)
docs/P3_COMPLETION_REPORT.md         (8.5 KB, 2026-09-16)
docs/P4_COMPLETION_REPORT.md         (5.7 KB, 2026-09-16)
docs/P5_COMPLETION_REPORT.md         (3.8 KB, 2026-09-16)
docs/P5.1_COMPLETION_REPORT.md       (5.4 KB, 2026-09-16)
REVIEW_SUMMARY.md                    (顶层, 9.8 KB)
```

**建议**：核心完成报告必须入库，作为阶段交付证据。

### 2.5 调试脚本（14 个 debug_*.py）

**不入库合理**：一次性调试代码。

**建议**：清理或移到 `scripts/debug/`。

---

## 三、文档一致性问题

### 3.1 AGENTS.md §"尚未完成" 与实际完成状态

| 声称状态 | 实际状态 | 证据 |
|----------|----------|------|
| "P3 真实图纸未验证 blocked" | ✅ 已完成 | 18 份样本 + MANIFEST.md + P2 COMPLETION_REPORT |
| "真实设计院 DXF 含 INSERT 块参照 ... 采集真实样本 + 重标定 ... + 补 INSERT/ARC 实体覆盖（blocked 待办）" | ✅ 已实现 | `dxf_reader.py:1-5` 显式支持 INSERT 展开，P3 COMPLETION_REPORT 实测 PUMP-03 展开 193 个块内实体 |
| "drawing_p0 是合成 smoke 集 ... 不构成缺陷召回率验收证据" | ⚠️ 部分完成 | drawing_p5.1 召回率 100%，但仅 2 条 gold（样本量不足） |
| "最小管径规则只能标 engineering_practice" | ⚠️ 可能已变更 | P5.1 COMPLETION_REPORT 未提及此限制 |

### 3.2 规格文档未更新

`docs/design/13-p3-drawing-review.md` 的 §8.3 仍声称：
> drawing_p0 与真实样本的关系 ... 真实样本集（drawing_p1）的采集与标定是 blocked 待办

**实际**：drawing_p4/p5/p5.1 已建立，有 gold 标注与召回率验证。

---

## 四、具体问题清单

### 🔴 P0 - 阻塞问题（必须处理）

#### P0-1: 核心模块未入库
**严重程度**: 阻塞  
**影响**: 生产代码依赖未追踪文件，无法复现构建

**模块清单**：
- `src/water_agent/drawing/mif.py` （被 dxf_reader.py 引用）
- `src/water_agent/drawing/symbol_library.py` （被 symbol_enrichment.py 引用）
- `src/water_agent/drawing/symbol_enrichment.py` （被 dxf_reader.py 引用）
- `src/water_agent/drawing/desensitize.py` （生产工具）
- `tests/test_mif.py` (19 测试)
- `tests/test_symbol_library.py` (15 测试)
- `tests/test_symbol_enrichment.py` (10 测试)
- `tests/test_desensitize.py` (18 测试)
- `tests/test_cite_traceability.py` (6 测试)

**修复**：
```bash
git add src/water_agent/drawing/{mif,symbol_library,symbol_enrichment,desensitize}.py
git add tests/test_{mif,symbol_library,symbol_enrichment,desensitize,cite_traceability}.py
```

#### P0-2: 真实评测数据未入库
**严重程度**: 阻塞  
**影响**: 无法验证召回率声称，无法复现评测结果

**数据清单**：
- `data/eval/drawing_p4_defects_gold_v2.jsonl` (2 条 gold)
- `data/eval/drawing_p5.1_recall_results.json` (召回率证据)
- `data/eval/drawing_p5.1_fp_results.json` (误报率证据)

**修复**：
```bash
git add data/eval/drawing_p4_defects_gold_v2.jsonl
git add data/eval/drawing_p5.1_*_results.json
```

#### P0-3: MANIFEST.md 未入库
**严重程度**: 阻塞  
**影响**: 样本集质量无文档证明

**修复**：
```bash
git add data/drawings/real_samples/MANIFEST.md
```

#### P0-4: AGENTS.md 与实际状态严重不一致
**严重程度**: 阻塞  
**影响**: 误导后续开发者，声称未完成的功能已完成

**需更新章节**：
- §"尚未完成" 的 "P3 真实图纸未验证 blocked"
- §"架构现状" 的 P3 图纸链描述
- §"改动范围纪律" 的 Step 编号

**建议文本**：
```markdown
## 尚未完成（已知阻塞）

- **P3 图纸召回率验收样本量不足**：drawing_p5.1 召回率 100%（2/2），
  但 gold 样本仅 2 条，不足以构成生产级验收证据。建议补充至 ≥10 条 gold。
- **P3 误报率略高于目标**：18 张阴性集误报率 11.1%（目标 ≤10%），
  主要来自 P-02 的系统类型推断。可接受但需监控。
- **P3 规则引擎条文支撑不足 blocked**：（保持原文）
```

### 🟡 P1 - 重要问题（应该处理）

#### P1-1: 阶段完成报告未入库
**影响**: 无法追溯各阶段交付证据

**建议入库**：
- `docs/P2_COMPLETION_REPORT.md`
- `docs/P3_COMPLETION_REPORT.md`
- `docs/P5.1_COMPLETION_REPORT.md`
- `REVIEW_SUMMARY.md`

P4/P5 COMPLETION_REPORT 偏简略，可选。

#### P1-2: 规格文档未同步更新
**影响**: 规格与实现不一致

**需更新**：
- `docs/design/13-p3-drawing-review.md` §8.3：删除"真实样本 blocked"
- `docs/design/13-p3-drawing-review.md` §9：补充 P2–P5.1 的完成状态

#### P1-3: 评测脚本未入库
**影响**: 无法复现 drawing_p4/p5 评测

**建议入库**：
- `scripts/eval_drawing_p5.1_validation.py`
- `scripts/implant_defects_v2.py`

### 🟢 P2 - 改进建议（可选）

#### P2-1: 清理调试脚本
14 个 `debug_*.py` 占据顶层目录。

**建议**：
```bash
mkdir -p scripts/debug
mv debug_*.py scripts/debug/
echo "scripts/debug/" >> .gitignore
```

#### P2-2: 整合零散文档
`docs/P2.5_*.md` 等规划文档共 11 份，可考虑合并或移到 `docs/planning/`。

#### P2-3: drawing_p1/p2/p3 命名跳跃
评测集命名从 drawing_p0 直接跳到 drawing_p4，中间缺 p1/p2/p3。

**建议**：要么补全编号，要么在文档说明"p1–p3 为废弃编号"。

---

## 五、质量观察

### ✅ 优秀实践

1. **测试先行**：新模块全部有测试（68 条），覆盖边界 case
2. **类型安全**：mypy 通过，docstring 详实
3. **错误处理**：MIF 解码失败降级为原文，不阻断解析
4. **威胁模型**：desensitize 模块考虑了 PII/凭据/MIF 转义三种形态
5. **可追溯性**：MANIFEST.md 详细记录样本来源、筛选标准、替换历史

### ⚠️ 需注意

1. **INSERT 展开的性能**：PUMP-03 展开 3,285 个 INSERT 耗时 0.4s，单张图可接受；
   但若批量处理 100+ 张图，总耗时可能成为瓶颈。建议监控。

2. **误报率目标**：P5.1 误报率 11.1% 略高于目标 10%，且集中在 P-02 一张图。
   需确认这是该图纸的特殊性还是规则的系统性问题。

3. **gold 样本量**：drawing_p5.1 只有 2 条 gold，样本量不足。
   生产级验收建议 ≥10 条，覆盖多种缺陷类型。

4. **条文覆盖**：AGENTS.md 已明确"缺正式条文者不得声称按规范校验"，
   P5.1 COMPLETION_REPORT 未提及此限制 —— 需确认规则的分级标注是否正确。

---

## 六、建议操作

### 立即执行（解除阻塞）

```bash
cd /Users/yaerken/Desktop/给排水agent

# 1. 入库核心模块
git add src/water_agent/drawing/{mif,symbol_library,symbol_enrichment,desensitize}.py
git add tests/test_{mif,symbol_library,symbol_enrichment,desensitize,cite_traceability}.py

# 2. 入库评测数据
git add data/eval/drawing_p4_defects_gold_v2.jsonl
git add data/eval/drawing_p5.1_recall_results.json
git add data/eval/drawing_p5.1_fp_results.json
git add data/eval/drawing_p5_fp_results.json
git add data/eval/drawing_p5_fp_annotations.json

# 3. 入库样本 MANIFEST
git add data/drawings/real_samples/MANIFEST.md

# 4. 入库核心完成报告
git add docs/P2_COMPLETION_REPORT.md
git add docs/P3_COMPLETION_REPORT.md
git add docs/P5.1_COMPLETION_REPORT.md
git add REVIEW_SUMMARY.md

# 5. 入库评测脚本
git add scripts/implant_defects_v2.py
git add scripts/eval_drawing_p5.1_validation.py
git add scripts/scan_drawing_pii.py
git add scripts/verify_drawing_samples.py
git add scripts/auto_annotate_drawings.py

# 6. 更新 AGENTS.md（手工编辑后 git add）
```

### 验证完整性

```bash
# 确认门禁仍通过
bash scripts/ci_gate.sh

# 确认新测试被执行
_venv/bin/pytest tests/test_mif.py tests/test_symbol_library.py \
  tests/test_symbol_enrichment.py tests/test_desensitize.py \
  tests/test_cite_traceability.py -v

# 确认 mypy
_venv/bin/mypy src/water_agent/drawing/{mif,symbol_library,symbol_enrichment,desensitize}.py
```

### 提交

```bash
git commit -m "feat(drawing): 入库 P2–P5.1 真实图纸链路（MIF/符号库/脱敏/召回率验收）

- 新增 4 个核心模块：mif / symbol_library / symbol_enrichment / desensitize
- 新增 68 条测试（全部通过）
- 真实样本 18 份（MANIFEST.md 记录元数据，样本不入库）
- 召回率验收：drawing_p5.1 = 100% (2/2)，误报率 11.1% (18 张阴性集)
- MIF 转义解码：1,320 条 → 残留 0
- INSERT 块展开：PUMP-03 = 5,729 → 5,922 实体

决策记录见 docs/P2/P3/P5.1_COMPLETION_REPORT.md

Co-authored-by: Claude Code <claude-code@anthropic.com>"
```

---

## 七、后续待办

1. **补充 drawing gold 至 ≥10 条**：当前 2 条不足以构成生产级验收
2. **分析 P-02 误报根因**：确认是图纸特殊性还是规则问题
3. **整合零散规划文档**：P2.5_*.md 等可移到 docs/planning/
4. **清理调试脚本**：移到 scripts/debug/
5. **补全评测集编号说明**：drawing_p1/p2/p3 缺失的原因

---

## 附录：门禁通过证据

```
==> CI 门禁通过：pytest + ruff + mypy + rag_p0(11/12) + calc_p0(28/28) 
    + docgen_p0(11/11) + material_p0(10/10) + governance_p0(32/32) 
    + drawing_p0(合成 smoke 回归) + multi_agent_p0 + memory_p0(§13.2 十二项硬指标) 
    + 基线对比
```

测试统计：2143 passed / 24 skipped / 0 failed

---

**生成时间**: 2026-09-18  
**Review 人**: Claude (Opus 5)
