# 分阶段路线图（Roadmap）

> 原则：**MVP 优先，DWG 与企业 MCP 后置**。每个阶段**连同其评测集一起交付**。
> 路线图同时记录规划与当前验收状态；只把有版本化证据的项目标为完成，外部依赖或质量阈值未收口的项目保留为 blocked。
> 每阶段列出**可度量验收标准**，作为阶段通过的门槛。

---

## P0 —— 项目骨架 + Harness 基础 + 最小 RAG 环
**目标**：跑通「提问→检索→回答→评测」最小闭环，确立 Harness 与 Loop 骨架。

- [ ] 项目结构 + 依赖(config) + Python(3.11/3.12) 环境
- [ ] harness 跑分器骨架（数据集加载 / 指标 / 报告 / CI 门禁返回值）
- [ ] 最小 RAG 环：`提问 → TriviumDB search_hybrid → mock 生成器 → 回答`
- [ ] TriviumDB 环境与 API 烟囱验证；真实条款目录未提供前使用项目内最小 fixture
- [ ] 本地 JSON trace；LangSmith / Langfuse 作为可选适配器
- [ ] 最小规范问答金标准（10 条）

**验收标准**
- [ ] harness 能对 jsonl 评测集跑分并输出通过率，失败用例使 CI 返回非零
- [ ] 一次 RAG 调用产生完整本地 trace，含检索命中、引用、重试和耗时

---

## P1 —— 规范问答 + 水力计算（核心 MVP）
**目标**：Agent 会查规范、会算、可追溯。

- [ ] 用户提供真实条款目录后，规范条款 → **结构化父子级切块**（见 08-ingestion）
- [ ] 子块（条/款/项/表）向量+文本 → TriviumDB（`search_hybrid`）建索引；父块（章/节）仅作回填
- [ ] 父块持久化为 `<db_path>.parents.json`，新进程可自动回填
- [ ] 递归切块兜底：超长条款/表格/无结构文本/标题型无条号文本
- [ ] RAG 子图完整环（混合检索 / 生成 / 引用自检 / retry 回流）
- [ ] 计算子图完整环（参数解析 / 程序化数值校验 / 候选方案回环）；公式由输入显式指定
- [ ] 水力计算工具集（流速 / 沿程水头损失 / 管径选择 / 泵扬程 / 排水坡度）
- [ ] 自研 MCP：水力计算 server + 规范库检索 server
- [ ] 规范问答金标准（≥50 条）+ 计算案例集（≥20 条，每个用例显式绑定公式口径）

**验收标准**
- [ ] 计算用例程序化判分通过率 ≥ 95%（相对误差 ≤1%）；每个用例的公式和参数口径均记录
- [ ] 规范问答引用**可追溯**（回答能指回规范号+条款号，无编造条款；检索命中子块后 parent_id 回填父块上下文）
- [ ] 真实 DB 检索失败不得静默回退 fixture；错误必须记录到 Trace/CLI
- [ ] 二次入库幂等，连续 ingest 同一 source 不产生重复检索结果
- [ ] 端到端：输入一段真实规范问题，返回带条款引用的回答

---

## P2 —— 计算书/设计说明生成 + 材料统计（机制层 2026-09-03 完成；质量专项 2026-09-04 部分完成）
**目标**：从「散点计算」到「成文成果」。实现与口径见 `docs/design/11-docgen-materials.md`。

- [x] 文档生成子图（基于计算结果 + 引用，模板化输出）——`water_agent/docgen/` 模板引擎
  （计算书/设计说明必需章节 + 数值/引用纪律）+ `graphs/docgen.py` 结构化渲染接线
- [x] Excel 材料统计工具——`water_agent/materials/` 聚合引擎（材质×规格，来源可追溯）
  + `.xlsx`/CSV 导出（openpyxl）+ `material_stats` 工具
- [x] Human-in-the-loop（生成正式文档前确认）——Step 9 审批图复用（interrupt + resume，
  拒绝回流 ≤2 稿；正式文档仅批准后产出）

**机制层验收**
- [x] 计算书/设计说明基于计算结果 + 引用生成，结构合规：`data/eval/docgen_p0.jsonl` 11/11
  （必需章节齐全 / 数值与原值一致 / 引用不编造，机制层判分）
- [x] 材料清单数值与计算一致、可追溯来源：`data/eval/material_p0.jsonl` 10/10
  （行集合精确一致 / 数量双阈值 / 全段溯源覆盖）
- [x] 关键文档生成前有人工确认节点（checkpoint 可暂停/恢复）：docgen 审批图 + `--resume`

**质量层与鲁棒性验收**
- [x] 受限 prose 润色 + 确定性自检管线：`data/eval/docgen_quality.jsonl` 离线 4/4；
  2026-09-04 真实 DeepSeek 运行 4/4（仅证明 4 条样本，见 `docs/evals/p2-quality-2026-09-04.md`）
- [x] 材料鲁棒性套件：`data/eval/material_quality.jsonl` 7/7；xlsx round-trip 由
  `tests/test_materials_quality.py` 覆盖
- [x] PR baseline 已纳入 `docgen_p0` 与 `material_p0`；`data/eval/baseline.json` 默认比较
  rag_p0/calc_p0/docgen_p0/material_p0 四套机制层 suite
- [ ] **blocked**：正式规范语料与版本状态未核实，docgen 的真实检索/正式条文引用质量尚未通过验收
- [ ] **blocked**：共享 RAG 质量层 `rag_p1 --variant real --judge` 最新为机制 56/58、
  独立质量 36/58（62.07%），未达到收口条件
- [ ] **blocked**：材料管段从 DWG 自动抽取依赖 P3，当前只接收结构化管段表

---

## P2.5 —— 生产治理与安全基座（进行中：Step 11–12 已交付，13–15 未实施；P3 前置）

**目标**：为已有 RAG、计算、docgen、材料和远程 provider 调用建立可测试、不可绕过的
缓存、限流、成本、可靠性与安全边界。权威规格：`docs/design/12-p2.5-production-governance.md`。

- [x] 统一 `ProviderGateway`：限流、配额、预算、timeout、有界重试、429 退避、熔断与 usage 对账（**Step 12 已完成，6/6 出口接入**）
- [ ] 仅对已验证引用的 temperature=0 RAG 回答启用精确缓存（TTL、失效、跨主体隔离）；不做语义缓存
- [ ] `GuardrailPipeline`：来源标签、注入防御、PII/secret、工具 capability/allowlist、输出检查和脱敏 trace
- [ ] `governance_p0` 评测、红队合成用例、失败检测器、告警事件和 CI/baseline 门禁
- [x] 为 P4 预留 `Principal`/`Capability` 契约；P2.5 不将本地 CLI 主体误称为外部认证（**Step 11 已完成**）

**验收标准**
- [x] 所有远程 provider 调用已接入统一入口；绕过入口的回归测试失败（**Step 12：静态扫描断言「代码出站模块 == 清单」**）
- [ ] 所有工具调用已接入 `GuardrailPipeline`（Step 14）
- [ ] 缓存命中不绕过引用/安全检查，版本/策略/权限域变化必定失效（Step 13）
- [x] 限流、预算、timeout、重试、熔断均有确定性正反例（**Step 12：`tests/test_provider_gateway.py` 63 条**）
- [ ] PII/注入/工具策略的确定性正反例（Step 14）
- [ ] `governance_p0` 进入 `scripts/ci_gate.sh`，真实组件失败与无 Key 的 SKIP 语义明确区分（Step 15）

**Step 11–12 交付摘要**（权威细节见 `p2.5-step-11-decision.md` / `p2.5-step-12-decision.md`）
- `runtime/governance/`：契约（不依赖 langchain/langgraph）+ gateway + 配置 + 限流/预算存储 + 接入清单
- 出站模块由 6 个收敛为 4 个；`ToolSpec.max_retries` 死字段删除，`timeout_seconds` 接入协作式 deadline
- **成本硬上限默认不启用**（价格未与账单核对，不得用估算当账单）；`BudgetPolicy` 默认 `WARN`
- 内存限流/预算 `supports_multi_instance=False`（不谎称支持多实例）

---

## P3 —— 图纸读取与审查（DWG，最难）
**目标**：读图 + 合规审查。

- [ ] DWG 解析管线（ODA / libredwg → 图元）
- [ ] 给排水对象语义映射（管线/立管/洁具/水表…）
- [ ] 规则引擎：规范条文 → 可执行检查规则
- [ ] 图纸审查子图 + 缺陷标注

**验收标准**
- [ ] 构造含缺陷图纸集，缺陷**召回率 ≥80%**、误报率可控
- [ ] 能定位缺陷对象（图层/图元/坐标）

---

## P4 —— MCP 扩展（CAD/BIM + 企业库）
**目标**：打通专业软件与企业数据。

- [ ] 接入专业 CAD/BIM（天正/Revit/中望）MCP
- [ ] 接入企业知识库/数据库 MCP
- [ ] 网关权限控制 + 危险操作确认

**验收标准**
- [ ] 至少接通 1 个 CAD/BIM 工具与 1 个企业数据源
- [ ] 写/危险操作需人工确认，权限可配置

---

## P5 —— React 前端
**目标**：完整工程师使用体验（透明 + 可追溯）。

- [ ] 聊天流式 + 文件上传
- [ ] 工具调用可视化 + 规范引用卡片 + 计算卡片
- [ ] 图纸预览（依赖 P3）
- [ ] 一键反馈回流 harness

**验收标准**
- [ ] 用户能实时看到 Agent 调用/检索/引用（透明化）
- [ ] 差评反馈能回流为候选评测集（数据飞轮）

---

## 里程碑与迭代节奏
- 每个阶段结束 → 跑一次 harness 全量回归 → 写阶段小结。
- 每阶段 git tag 记录基线；**CI 门禁：改动不得使「P1 计算判分」与「规范引用」等关键指标显著下降**。
- 关键技术风险点（由大到小）：DWG 解析、规范条款/表格解析、公式口径锁定、模型 API 成本。
