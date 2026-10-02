# SDD + Harness Engineering —— 规格驱动开发与工程框架（V1.0）

> 本文档把「规格驱动开发（SDD）+ Harness Engineering」方法论落地到本项目，与 `00-overview-and-decisions.md`（ADR）、`01-architecture.md`、`03-harness-engineering.md`、`09-hydraulics-formulas.md`、`docs/给排水agent改造任务书.md` 对齐。
>
> **一句话定位**：SDD 负责把「正确」定义清楚（可验证规格）；Harness 负责让 AI 在受控环境里持续、可靠地做到这个正确（工作台 + 护栏 + 证据）。

## 1. 核心原则

| 原则 | 在本项目的含义 |
|---|---|
| 规格可验证，不靠"读懂" | 每条要求都能映射到测试/运行时检查，而非一段漂亮说明 |
| 正确路径更容易走 | 统一验证入口 `scripts/verify.sh`、评测集、fixture、口径表齐全 |
| 错误状态无法伪装成功 | `missing_inputs`/`assumptions`/`SKIP`/`invalid_input` 结构化返回，不伪造真实服务结果 |
| 每次完成留下证据 | 测试输出、harness 报告、trace、变更清单进版本控制 |

## 2. 规格模板（本项目每项能力的可验证规格）

每个功能/能力在 `specs/<capability>/` 下放一份 `spec.md`（本任务书 Step 即规格的一种载体）。模板：

```markdown
# Feature: <能力名>

## Goal / 非目标
- Goal：<一句话用户目标>
- 非目标：<明确不做的事，防止范围蔓延>

## Input（可验证的输入契约）
- <参数名>: <类型>，范围 <约束>，默认 <默认值或"无">
- 缺失：<缺参行为——本项目一律返回结构化 missing_inputs，禁止猜测>

## Output（可验证的输出契约）
- 成功：<结构化的成功形态>
- 非法输入：<错误码/状态>
- 后端不可用：<明确错误码，禁止伪装成功>

## Constraints（实现边界与禁区）
- <不允许发生的行为，例如：不得静默切换公式、不得把 BM25 回退宣称为语义检索成功>

## Acceptance Tests（映射为自动化测试）
1. <可执行断言 1>
2. <可执行断言 2>
```

### 2.1 完整示例：水力计算（已落地）

`specs/calc-engine/spec.md` 的对照物（已实现，作为模板的"已完成的样例"）：

| 规格项 | 落地产物 |
|---|---|
| Goal | 给排水设计参数的程序化计算，LLM 不参与数值运算 |
| Input 契约 | 每个公式的显式输入（口径表 §1–§5） |
| Output 契约 | `CalcResult{ok, values, missing_inputs, errors, assumptions}` |
| Constraints | 公式由输入显式指定（`HydraulicsFormula` 枚举）；缺参返回 `missing_inputs`；越界返回 `assumptions` |
| Acceptance Tests | `tests/test_hydraulics.py`（37 用例）+ `data/eval/calc_p0.jsonl`（22 条） |
| 运行时检查 | `_venv/bin/python -m water_agent.harness.runner data/eval/calc_p0.jsonl` → 100% |
| 验收证据 | `data/eval/reports/calc_p0_*.json` |

**规格 → 测试的映射纪律**：每条 Acceptance Test 必须对应一个可运行的断言（pytest 或 harness 用例），禁止"规格里写了、测试里找不到"。

## 3. Harness 六层在本项目的落地

| 层级 | 作用 | 本项目的载体 |
|---|---|---|
| 指令层 | 规定代理如何工作 | `AGENTS.md`（三条铁律、验证命令、改动范围纪律） |
| 上下文层 | 提供正确的项目事实 | `docs/design/*`（架构、ADR、口径表）、本任务书 |
| 工具层 | 给代理可靠操作能力 | `_venv/bin/pytest`、`ruff`、`mypy`、`scripts/verify.sh`、harness runner |
| 质量门禁 | 阻止"看似完成" | `scripts/verify.sh`（pytest + ruff + rag_p0 + calc_p0） |
| 证据层 | 让完成可审计 | `data/eval/reports/*.json`、`data/eval/traces/`、`baseline_preflight.json`、git 历史 |
| 记忆层 | 固化长期约束 | `docs/09-next-steps.md`、`data/eval/baseline_preflight.json`、ADR |

## 4. 错误状态语义（防伪装的关键）

本项目必须区分以下状态，**禁止混为一谈**：

| 状态 | 含义 | 输出要求 |
|---|---|---|
| `succeeded` | 真实计算/检索成功 | 正常结果 |
| `missing_inputs` | 缺参数，要求澄清 | 结构化列出缺失参数，**禁止让模型猜测** |
| `invalid_input` | 输入无物理意义（负值、越界充满度等） | 结构化错误说明 |
| `assumptions` | 计算完成但越界 | 随结果返回并在回答中明示 |
| `SKIP` | 真实变体无 Key/网络不可达 | 结构化 SKIP，**不算失败也不算通过**，不参与基线比较 |
| `real_failed` | 有 Key 但调用出错 | 与 SKIP 不同的错误码，必须区分（Step 10 落实） |

检索侧还须显式区分（Step 10 落实）：
- `vectorstore_missing`：系统尚未建立向量索引
- `semantic_search_available=false`：语义检索不可用
- `bm25_fallback`：允许关键词降级，但**不得宣称为语义检索成功**

## 5. 目录结构（规格与证据进版本控制）

```
docs/design/          # 架构、ADR、口径表、本文件（上下文层）
docs/09-next-steps.md # 已知问题与下一步（记忆层）
specs/                # 可验证规格（按能力分目录；本任务书 Step 也是规格载体）
data/eval/*.jsonl     # 金标准评测集（规格的验收测试）
data/eval/reports/    # harness 报告（证据层，gitignore 排除）
tests/                # 单元/集成测试（规格断言的落点）
scripts/verify.sh     # 一键验证（质量门禁）
AGENTS.md             # 指令层
```

## 6. 统一验证入口

所有改动完成前必须通过 `bash scripts/verify.sh`（等价于本任务书 Step 10 的 `ci_gate.sh` 离线机制层）：

```bash
bash scripts/verify.sh
```

预期：pytest 全绿、ruff 0 error、rag_p0 12/12、calc_p0 ≥95%。任一项不达标即失败，任务不得标为完成。

## 7. 标准工作循环（SDD 循环）

```
读取规格/任务书 → 检查现有实现与测试 → 形成最小实施计划
→ 先写/补失败测试 → 实现最小改动 → 执行 scripts/verify.sh
→ 修复失败 → 生成交付证据（报告/trace） → 报告真实状态
```

规则：
1. 只改动当前规格明确要求的文件；与规格冲突时停止并说明，不自行扩大设计。
2. 新增行为必须有测试；修复缺陷必须先补回归测试。
3. 无法连接外部服务时，报告阻塞原因 + 可验证的本地范围（SKIP 语义）。
4. 最终报告必须列出：变更、验证命令、实际结果、未验证项、遗留风险。

## 8. 完成定义（DoD）

- [ ] `scripts/verify.sh` 通过
- [ ] 本次改动对应的 harness 报告/trace 已生成（证据）
- [ ] 基线记录 `baseline_preflight.json` 已更新
- [ ] 报告说明：改了哪些文件、跑了哪些验证、真实通过、哪些外部依赖未验证、已知风险

## 9. 常见误区对照（本项目立场）

| 误区 | 本项目做法 |
|---|---|
| 规格=很长的 PRD | 规格必须可测试：每条 Acceptance 有对应断言 |
| Harness=大 Prompt | Harness 是仓库结构 + verify.sh + 测试 + 规则（AGENTS.md） |
| 只靠单元测试 | 单元 + harness 端到端（rag_p0/calc_p0）+ 真实冒烟（有 Key 时） |
| "测试绿了"=交付 | 外部 API/网络/Key 未验证时如实 SKIP 并标注 |
| 小改动无规格 | 小任务也用一页规格 + 一两个测试兜底 |

---

## 附：与任务书的关系

- 本任务书（`docs/给排水agent改造任务书.md`）Step 0–10 是项目级规格的**实施路径**；本文件是其 SDD+Harness 方法论抽象。
- Step 10 将落地 `scripts/ci_gate.sh` / `scripts/compare_baseline.py` / `data/eval/baseline.json`；`verify.sh` 是其先行版（离线机制层）。
