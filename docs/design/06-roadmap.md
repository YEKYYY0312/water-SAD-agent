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
- [ ] **blocked**：材料管段从图纸自动抽取依赖 P3。**Step 21 已交付语义映射，
  `DXF → MappedPipe → materials.PipeRun → aggregate_materials` 链路实测闭环**；
  Step 22 已交付审查子图编排与 smoke 评测集（`drawing_p0`，合成样本）。
  但仅覆盖 LINE/POLYLINE 管线，且真实设计院图纸未验证（标注关联半径需按真实
  图纸重新标定），故**尚不能声称"能从真实图纸出材料清单/审查报告"**。
  见 `docs/design/p3-step-22-decision.md` 残余风险 1–3 与 blocked 待办

---

## P2.5 —— 生产治理与安全基座（2026-09-06 完成：Step 11–15 已交付；P3 前置）

**目标**：为已有 RAG、计算、docgen、材料和远程 provider 调用建立可测试、不可绕过的
缓存、限流、成本、可靠性与安全边界。权威规格：`docs/design/12-p2.5-production-governance.md`。

- [x] 统一 `ProviderGateway`：限流、配额、预算、timeout、有界重试、429 退避、熔断与 usage 对账（**Step 12 已完成，6/6 出口接入**）
- [x] 仅对已验证引用的 temperature=0 RAG 回答启用精确缓存（TTL、失效、跨主体隔离）；不做语义缓存（**Step 13 机制已完成；但当前语料 4 本规范版本全 unknown，按规格 §5 写入资格恒为 false → 命中率结构性为 0，且未接进 RAG 主路径**）
- [x] `GuardrailPipeline`：来源标签、注入防御、PII/secret、工具 capability/allowlist、输出检查和脱敏 trace（**Step 14 已完成，4/4 工具接入；规则集是确定性正则，非分类器，必然存在未覆盖的改写变体**）
- [x] `governance_p0` 评测（32 条离线场景）、红队合成用例（33 条）、失败检测器（8 个）、告警事件和 CI/baseline 门禁（**Step 15 已完成，进入 `scripts/ci_gate.sh` 与 `data/eval/baseline.json`**）
- [x] 为 P4 预留 `Principal`/`Capability` 契约；P2.5 不将本地 CLI 主体误称为外部认证（**Step 11 已完成**）

**验收标准**
- [x] 所有远程 provider 调用已接入统一入口；绕过入口的回归测试失败（**Step 12：静态扫描断言「代码出站模块 == 清单」**）
- [x] 所有工具调用已接入 `GuardrailPipeline`（**Step 14：`ToolRegistry.invoke` 在 schema 校验前调用 `validate_tool_call`；裸构造 `ToolRegistry()` 回落进程级默认单例，无绕过后门；阻断场景用计数替身断言 execute 从未执行**）
- [x] 缓存命中不绕过引用/安全检查，版本/策略/权限域变化必定失效（**Step 13：10 个失效因子各有独立 miss 测试；未验证引用与版本未知的结果永不入缓存**）
- [ ] 缓存已接进 RAG 主路径并有真实命中证据（**阻塞于 `data/versions.json` 人工核实**）
- [x] 限流、预算、timeout、重试、熔断均有确定性正反例（**Step 12：`tests/test_provider_gateway.py` 63 条**）
- [x] PII/注入/工具策略的确定性正反例（**Step 14：`test_guardrails.py` 77 + `test_injection_defense.py` 43 + `test_tool_policy.py` 52 + `test_redteam_dataset.py` 111**）
- [x] `governance_p0` 进入 `scripts/ci_gate.sh`，真实组件失败与无 Key 的 SKIP 语义明确区分（**Step 15：32 条场景全离线，检测器+观测有 76 条测试，进入 CI gate 与 baseline**）

**Step 11–15 交付摘要**（权威细节见 `p2.5-step-1{1,2,3,4,5}-decision.md`）
- `runtime/governance/`：契约（不依赖 langchain/langgraph）+ gateway + 配置 + 限流/预算存储 + 精确缓存 + 分层护栏 + 治理场景 + 失败检测器 + 聚合观测 + 接入清单
- 出站模块由 6 个收敛为 4 个；`ToolSpec.max_retries` 死字段删除，`timeout_seconds` 接入协作式 deadline
- **成本硬上限默认不启用**（价格未与账单核对，不得用估算当账单）；`BudgetPolicy` 默认 `WARN`
- 内存限流/预算/缓存均 `supports_multi_instance=False`（不谎称支持多实例）
- **不做语义缓存**（`cache.py` 有测试断言不含 embedding 相关代码）；缓存键 10 个失效因子
- ⚠️ **缓存解锁条件**：人工核实 `data/versions.json` 的 4 本规范状态。当前 fail closed，
  有 3 条测试断言此事实；核实后相关测试会失败并提示更新
- `GuardrailPipeline` 五个检查面动作不同：用户输入/工具参数/记忆写入命中即 BLOCK；
  检索内容命中是 REDACT+标记（丢弃整条会让投毒语料把正常检索废掉）；输出面与既有
  docgen 校验组合。规则集版本 `p2.5-step14-2026-09-06`；**不得声称能拦住任意注入**。
- 治理评测（Step 15）：32 条离线场景覆盖限流/预算/可靠性/护栏/trace/检测器/缓存十个面；
  8 个失败模式检测器（不调 LLM、不只在崩溃时打标）；聚合指标与本地结构化告警
  （`delivery="local_event_only"`，不假装已发 PagerDuty/Slack）；工具执行事件与护栏事件
  通过 `invocation_id` 配对供 `guardrail_bypass` 检测。检测器版本 `p2.5-step15-2026-09-06`。
- ⚠️ **Step 15 残余缺口**：检测器尚未接进 CLI/runner 生产调用站点（trace 参数未传递）；
  告警路由未接外部投递通道；`check_memory_write` 未接 checkpointer；`materials/export.py`
  未接护栏路径校验

---

## P3 —— 图纸读取与审查（DXF-first，最难）
**目标**：读图 + 合规审查。

> **权威规格**：`docs/design/13-p3-drawing-review.md`。2026-09-07 启动（Step 17–20 地基）。
> **解析基线是 DXF，不是 DWG** —— 见下方 blocked 说明。

- [x] **Step 17** P3 权威规格 + 决策记录 + 文档收口（含修正威胁模型信任边界图）
- [x] **Step 18** 治理契约扩展：`READ_DRAWING` 只读能力 + `allowed_read_dirs` 读写白名单分离
- [x] **Step 19** DXF 解析管线（ezdxf → 图元）+ 统一中间格式 + `DRAWING_ROOT` 四态契约
      + 合成图纸生成器
- [x] **Step 20** `read_drawing` 只读工具 + 四道护栏与 CLI/harness 接线
- [x] **Step 21** 给排水对象语义映射（管线 → `materials.PipeRun`；立管/洁具/水表未覆盖，见决策记录残余风险 2）
- [x] **Step 21** 规则引擎：3 条规则 + 阈值来源分级（其中坡度/流速恒报未检查，见决策记录 D8）
- [x] **Step 22** 图纸审查子图（`02-loop-engineering.md:67-71` 固定管线）+ `drawing_p0` 合成 smoke 评测集 + 门禁接入
      （**合成样本集，`real_samples=false`，只守护链路回归，不构成召回率验收**；见决策记录 D1）

**验收标准**
- [ ] 构造含缺陷图纸集，缺陷**召回率 ≥80%**、误报率可控
      （度量口径见规格 §7；**不能复用** `harness/retrieval_metrics.py`）
      **—— blocked**：无真实设计院样本；`drawing_p0` 为合成 smoke 集，门禁只守护回归。
      开启条件：真实样本采集 + `ANNOTATION_MATCH_RADIUS` 重标定 + INSERT/ARC 实体覆盖
      （Step 22 决策记录残余风险 1–3，已列 blocked 待办）
- [ ] 能定位缺陷对象（图层/图元/坐标）—— 由 `DrawingEntity.handle` + `points` 承担

**已知阻塞（不得淡化）**
- [ ] **blocked：DWG 直读**。本机无 ODA File Converter / libredwg
      （`odafc.is_installed()` 实测 `False`），且转换需 `Capability.EXECUTE_SHELL` ——
      该能力在 `Principal` 构造期即被拒绝，**故不可能做成模型可调用的工具**。
      `.dwg` 恒返回结构化 `DRAWING_UNSUPPORTED_FORMAT`，只能由人工预先转 DXF。
- [ ] **blocked：真实图纸未验证**。全仓 0 个样例图纸，测试图纸由 ezdxf 程序化合成。
      真实设计院 DXF 含 INSERT 块参照、XREF 外部参照、非标图层命名与图纸空间视口，
      本轮只证明解析管线与护栏正确，**不声称**能处理真实图纸。
- [ ] **blocked：规则引擎条文支撑不足**。`GB50014-2021 5.2.10`（最小管径与最小设计坡度）、
      `5.2.13` **均不在语料库**；`GB50015-2019` 全库仅 9 条且 `4.7.1` 自述非正式条文。
      Step 21 的规则必须按规格 §6 分级标注，缺正式条文者**不得声称"已按规范校验"**。

---

## P4 —— MCP 扩展（CAD/BIM + 企业库）
**目标**：打通专业软件与企业数据。

> **权威规格**：`docs/design/14-p4-mcp-gateway.md`。2026-09-08 启动（Step 23 地基）。

- [x] **Step 23** 客户端网关地基：纯标准库 stdio JSON-RPC 客户端（无 `mcp` SDK——
      `pypi.org` 实测不可达，无法安装）+ 版本化配置（`allow` 白名单 / `env_file`+`env`
      显式注入 / `human_confirmation` 确认位）+ inputSchema 确定性子集 → pydantic
      模型 + 描述投毒安检 + 治理绑定桥（`MCP_INVOKE` 能力默认无主体持有，
      `inventory.register_mcp_tool_binding` 单一真源） + fake server 离线测试
      （`tests/test_mcp_gateway.py` 17 条 + `tests/test_mcp_behaviors.py` 16 条）
- [x] **Step 24** 自研 MCP server 端：水力计算 + 规范库检索包装（`mcp/servers/`）
      + stdio JSON-RPC 直连验证（`MCPStdioClient` → `hydraulics_server`/`specs_server`，
      `isError=true` 业务失败与 `GUARDRAIL_BLOCKED` 护栏全链路验证）
- [ ] **Step 25** CLI/harness 接线 + 危险操作确认门（interrupt/HITL）+ 端到端验证
- [ ] 接入专业 CAD/BIM（天正/Revit/中望）MCP
- [ ] 接入企业知识库/数据库 MCP
- [ ] 网关权限控制 + 危险操作确认（Step 23 已交付**结构性拒绝**面：权限走
      `MCP_INVOKE` + 白名单 + 主体授予两侧收敛；完整 HITL 确认门在 Step 25）

**验收标准**
- [ ] 至少接通 1 个 CAD/BIM 工具与 1 个企业数据源
      **—— blocked**：本机无天正/Revit/中望与企业库端点；Step 23 的 fake server 是
      **机制验证**（与 P3 合成图纸同纪律），**不构成验收证据**。
- [ ] 写/危险操作需人工确认，权限可配置
      —— 权限可配置面已交付（`MCP_INVOKE` + `allow` 白名单 + `Principal` 授予，
      有测试锁死）；写/危险操作的人工确认门（interrupt → 人工批准 → resume）在
      Step 25 接线，接线前 `human_confirmation=true` 的 server 一律不暴露
      （`MCP_CONFIRMATION_UNAVAILABLE`，不假装已完成确认）

**已知阻塞（不得淡化）**
- [ ] **blocked：真实 server 未验证**。官方 `mcp` SDK 无法安装、无网络、无真实
      第三方 server；Step 23 只与协议正确的本地 fake server 验证过往返
      （协议骨架 + 全部治理语义），握手细节未与真实厂商实现对照。
- [ ] **blocked：HTTP/SSE transport 未做**。远程 MCP 端点需 `NETWORK_FETCH` 类能力，
      属 Step 26+。
- [ ] **blocked：OAuth/外部认证未做**。`Principal.authenticated()` 仍无调用方；
      "配置了 env/env_file" 不等于"已完成企业认证"。

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
- 关键技术风险点（由大到小）：DWG 解析（**2026-09-07 已收敛口径**：P3 改为 DXF-first，
  DWG 直读 blocked，见 P3 一节）、规范条款/表格解析、公式口径锁定、模型 API 成本。
