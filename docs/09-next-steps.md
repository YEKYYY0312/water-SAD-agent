# 工程现状盘点与 P1 收尾计划

> 更新时间：2026-08-31（晚）。基于 git 提交历史（HEAD=e2fca9d，共 10 次提交）、`scripts/verify.sh` 新鲜输出与代码实况校准，不是转述 README。

> **2026-09-02 增量更新（Step 5–7 已完成）**：引用声明工具+确定性校验器（`tools/cite.py`、`domain/citation.py`，commit 2300dc3）→ agentic RAG 外层循环（`graphs/agent_rag.py` + `--mode agentic`，commit e7d5843）→ 真实模型接入（`generation/chat_model.py` build_chat_model + `--api-key-env/--stream` + `scripts/smoke_agentic.sh`，commit c4d4a0e）。门禁最新输出：pytest 432 全绿 / ruff 0 / rag_p0 11/12 / calc_p0 28/28 / mypy 31（=基线存量）。`--mode agentic` 无 DEEPSEEK_API_KEY 时输出结构化错误并指引 `--mock-generator`（不静默降级）；有 Key 时走真实 DeepSeek function calling，`--stream` 流式打印 token。

> **2026-09-03 增量更新（Step 8–9 已完成 + 真实链路闭环）**：
> - **Step 8 动态提示装配**：`prompting/assembler.py`（分段系统提示 + 段级预算 + create_react_agent callable 签名）、`prompting/runtime_context.py`（RuntimeContextProjection 变化才注入）、`ingestion/version_registry.py` + `data/versions.json`（只录有来源事实，未知显式 unknown；四本入库规范现行状态待人工核实，当前全部 unknown）；`validate_citations(version_registry=)` 已接进 legacy/agentic 两条图，版本状态写回 `CitationClaim.spec_version/version_status`（未知只标记不进 errors）；`--mode agentic` 走 `prompt=PromptAssembler(tool_registry=registry)`。
> - **Step 9 Human-in-the-Loop**：`graphs/docgen.py`（gather→draft→approval(interrupt)→finalize，拒绝→带反馈回 draft 重拟，草稿总稿数上限 2（初稿+1 次重拟），最后一稿被拒收口 failed；approval_log 记录审批；SqliteSaver 落盘恢复已测）+ `tests/test_docgen_hitl.py` 6 用例。审批语义与代码一致：`approval` 字段 None=待审批 / True=已批 / False=已拒。CLI 交互入口（`--flow docgen`）尚未接线，待补。
> - **R2 真实模型链路闭环**：DEEPSEEK_API_KEY 配置后 `--mode agentic` 真实回答 4 条引用全部 ∈ 检索结果、0 重试 0 错误（见对话记录）。`scripts/smoke_agentic.sh` 有 Key 时连跑 3 问。
> - 门禁最新输出：pytest 432 全绿 / ruff 0 / rag_p0 11/12 / calc_p0 28/28 / mypy 31（=基线存量）。

> **2026-09-03 增量更新（Step 10 已完成 + P1 收口）**：
> - **Step 10 双轨评测**：`harness/runner.py` 支持 `--variant mock|trivium|real`（机制层 InMemory+Mock 离线确定性；质量层真实 TriviumDB+真实 LLM+可选 rerank/LLM-as-judge，无 Key 结构化 SKIP 且退出码 0、有 Key 调用出错退出码 ≠ 0）；`EvalResult` 增加 `variant/skipped/token_usage(cost)/retrieval_metrics/judge_scores`，`EvalReport` 增加 `citation_ok_rate/cost/tokens/latency 分位/retrieval_metrics 聚合`。
> - **检索指标接入**：`harness/retrieval_metrics.py` 新增 `case_retrieval_metrics/aggregate_retrieval_metrics`（recall@1/3/5 + mrr），scorer 对 rag 用例计算并写入 `EvalResult.retrieval_metrics`，引用判分保持确定性。
> - **LLM-as-judge**：`harness/llm_judge.py`（5 准则锚定刻度：忠实度/引用覆盖/完整性/清晰度/不编造；judge 模型可配置；逐条 1-5 分+理由；结果缓存 `data/eval/judge_cache/` 防重复扣费；`FakeJudge` 供离线测试）。
> - **评测集扩充**：`data/eval/rag_p1.jsonl`（58 条真实规范问答 gold，source_id 与 `data/curated/clauses/` 对齐、含 `spec_version`，变体 real，无 Key 时全部 SKIP）。
> - **基线 + CI 门禁**：`data/eval/baseline.json` + `scripts/compare_baseline.py`（单一显式阈值集 `THRESHOLDS`：pass_rate 下降 ≤2pt、citation_ok 下降 ≤3pt、无新增核心失败 = PASS_TO_PASS）+ `scripts/ci_gate.sh`（PR 门禁只跑离线机制层：pytest→ruff→mypy→rag_p0→calc_p0≥95%→基线对比，任一失败 exit 1）。
> - **观测**：`observability/json_trace.py` 增加 `record_llm_call` 事件（prompt/response 摘要、token、cost、latency）；新增 `observability/otel.py`（OTel GenAI 语义约定，guarded import，SDK 未装全 no-op）。
> - **门禁最新输出**：pytest 432 全绿 / ruff 0 / mypy **0**（Step 10 清零）/ rag_p0 11/12 / calc_p0 28/28 / `bash scripts/ci_gate.sh` 全通过（含基线对比）。

## 一、这个项目在做什么

面向给排水设计院的 AI Agent，最终要覆盖五件事：**规范问答、水力计算、计算书/设计说明生成、图纸审查、材料统计**。

三条不可妥协的工程原则（写在 `docs/CURSOR_BUILD_BRIEF.md`）：

1. **计算类永远走程序化校验** —— 数值由真实水力计算工具算，LLM 只做参数理解和结果解释，绝不让 LLM 口算。
2. **引用可追溯** —— 每个回答必须指回「规范号 + 条款号」，禁止编造条号。
3. **评测先行** —— 每个阶段连同评测集一起交付，改动不得让关键指标下降。

技术栈：LangGraph 编排 + TriviumDB（向量×文本混合检索 `search_hybrid`）+ 自建 harness 评测 + 本地 JSON trace。

## 二、当前进度（P0 完成，P1 100%，Step 0–10 全部完成）

任务书 Step 0–10 已全部完成，P1 收口。下一步进入 P2 文档生成（计算书/设计说明 + HITL，`graphs/docgen.py` 已就位，CLI `--flow docgen` 待接线）。

| Step | 内容 | 状态 | 验收证据 |
|---|---|---|---|
| -1 / 0 | 现状基线 + 工程基建（git / ruff / uv.lock / AGENTS.md） | ✅ | `baseline_preflight.json` step0_post_state |
| 1 | 水力计算内核 + 程序化评测（7 公式，口径表先于代码） | ✅ | calc_p0 28/28；test_hydraulics |
| 2 | 计算确定性子图（parse→compute→validate→finish）+ 意图路由 | ✅ | test_calc_graph 21 用例；calc_p0 28/28 |
| 3 | 多轮会话记忆（checkpointer + thread_id） | ✅ | test_chat_memory 9 用例；SqliteSaver 可用 |
| 4 | 工具抽象层（ToolSpec + ToolRegistry + 首批工具） | ✅ | test_tools 16 用例 |
| 5 | 引用声明工具与确定性校验器 | ✅ | test_citation_check；validate_citations 纯函数 |
| 6 | Agentic RAG 外层循环 + ToolNode（agent_loop→citation_validator→finalize，回流 ≤2 次，轮数预算） | ✅ | test_agent_rag 12 用例（Step 6 交付 9 + 评审 1 + Step 7 增 2）；run_agentic_smoke.py |
| 7 | 真实模型接入（DeepSeek function calling + 流式） | ✅ | test_chat_model 4 用例；smoke_agentic.sh（无 Key SKIP）；--mode agentic 无 Key 结构化报错 |
| 8 | 系统提示装配 + 运行时上下文投影 + 规范版本注册表 | ✅ | test_assembler / test_runtime_context / test_version_registry 36 用例；run_agentic_smoke.py 上下文段仅注入一次 |
| 9 | Human-in-the-Loop：interrupt 审批节点 | ✅ | test_docgen_hitl 9 用例；拒绝回流重拟（总稿数上限 2）+ 最后一稿被拒收口 failed + approval_log + SqliteSaver 落盘恢复+ `cli.py --flow docgen` 人审闭环（y/n+原因、EOF 挂起、--resume 恢复、审批记录进 trace，test_cli_flows） |
| 10 | 双轨评测 + 观测 + CI 门禁 + 文档收口 | ✅（两项未做见下） | 评审修复：real 变体默认 agentic 图（引用由模型声明）、真实组件失败 exit 3（≠SKIP）、llm_call 观测真正接线、`--interactive` 真正作答；**未做**：prompt 打磨轮询、生产清单对照；pytest 432 全绿 / ruff 0 / mypy 0（清零）/ ci_gate.sh 通过 |

统一门禁新鲜输出（`bash scripts/ci_gate.sh`，2026-09-03）：**pytest 432 全绿 + ruff 0 error + mypy 0 error + rag_p0 11/12 + calc_p0 28/28 + 基线对比通过**。

## 三、已完成的资产（真金白银，别丢）

### 数据管线

| 环节 | 产物 | 状态 |
|---|---|---|
| 原始语料 | `knowledge/` 40MB，221 个 md + `clauses/` 7 本规范 1992 条 | 就位 |
| 语义质检 | `data/quality/reports/*_semantic_audit.json` | 已跑（乱码已修复，仅余 OCR 正则占位待人工确认） |
| 精选语料 | `data/curated/clauses/` 1595 条入选 / 397 条隔离 | 已建 |
| 父子级切块 | 3313 个子块 + `*.parents.json` | 已入库 |
| 向量索引 | `qwen3.7__512__curated.db` + `qwen3.7__256__curated.db` | 已建两套维度 |

入库策略：保留 GB50013-2018、GB50014-2021、GB50015-2019、GB50318-2017 四本；GB18918-2002 与 GB5749-2022（限值表为主、条款文本有限）、GBT50104-2010 整本排除。

### 代码能力

- `embedding.py` —— 阿里云百炼 qwen3.7 embedding，支持 256/512/768/1024 维，显式禁用本地兜底
- `retrieval/rerank.py` —— IdentityReranker / LocalReranker / BailianReranker（gte-rerank，需开通权限）三套
- `generation/llm.py` —— DeepSeek 生成器，引用只取自检索上下文
- `graphs/rag.py` —— legacy RAG 图（router → retrieve → generate → citation_check → finalize，双上限重试守卫），保留不删
- `graphs/calc.py` —— 计算确定性子图 + 规则式话术解析 + `detect_intent` 意图路由（LLM 解析 Step 7 接入）
- `graphs/chat.py` + `session.py` —— 多轮会话记忆（add_messages 累积 / thread_id 隔离 / SqliteSaver 持久化）
- `tools/` —— hydraulics 7 公式工具族（公式显式指定、缺参→missing_inputs、越界→assumptions）+ `base.py`/`registry.py` 工具抽象层 + 首批工具（calc_velocity、retrieve_clauses）
- `harness/` —— 评测加载 / 判分 / 报告 / CI 退出码 / 检索指标 / LLM-as-judge（5 准则锚定刻度，可配置 judge 模型，结果缓存）
- `graphs/rag.py` —— 完整 legacy RAG 图（router → retrieve → generate → citation_check → finalize，双上限重试守卫）
- `graphs/calc.py` —— 计算确定性子图 + 规则式话术解析 + `detect_intent` 意图路由（LLM 解析 Step 7 接入）
- `graphs/chat.py` + `session.py` —— 多轮会话记忆（add_messages 累积 / thread_id 隔离 / SqliteSaver 持久化）
- `graphs/agent_rag.py` —— agentic 循环（create_react_agent + ToolNode + 引用校验回流，`--mode agentic`）
- `graphs/docgen.py` —— Human-in-the-Loop 文档生成审批图（interrupt + Command resume，拒绝回流 ≤2 轮）
- `prompting/` —— 分段系统提示装配（PromptAssembler + RuntimeContextProjection）
- `ingestion/` —— 语料入库管线（chunker / pipeline / version_registry / quality / gold_set）
- `observability/` —— JSON Trace + OTel GenAI 观测（guarded import）
- `tools/` —— hydraulics 7 公式工具族（公式显式指定、缺参→missing_inputs、越界→assumptions）+ `base.py`/`registry.py` 工具抽象层 + 首批工具（calc_velocity、retrieve_clauses）
- 测试 **420 个全部通过**（`bash scripts/ci_gate.sh` 退出码 0）

### 评测集

- `retrieval_gold.jsonl` / `retrieval_gold_50.jsonl` —— 检索金标准（人工审过；**存在 gold 泄漏，见 R1，暂不能作为基线**）
- `rag_p0.jsonl` —— 13 条（12 有效 + 1 条设计非法用于异常覆盖），通过率 11/12
- `calc_p0.jsonl` —— 28 条（gold 独立复算提精度、容差 rel=1e-4、4 条带 expected_assumptions 越界断言），28/28
- `rag_p1.jsonl` —— 58 条真实规范问答 gold（source_id 与 `data/curated/clauses/` 对齐、含 `spec_version`），质量层专用
- `baseline.json` —— Step 10 基线（`scripts/compare_baseline.py --init` 生成，含 `passed_case_ids` 供 PTP 门禁）

### 工程基线

- git 已 init，25 次提交，一步一 commit 与任务书纪律一致；`.gitignore` 覆盖可重建产物（索引/报告/trace/根目录杂物）
- ruff 0 error；mypy **0 告警（Step 10 已清零）**；uv.lock 已锁定
- PR 门禁：`bash scripts/ci_gate.sh`（pytest → ruff → mypy → rag_p0 → calc_p0 ≥95% → 基线对比，任一失败 exit 1）

## 四、P1 剩余项（Step 10 已完成后的状态）

### R1（部分完成）构造独立 gold 并重跑检索矩阵

现有 70 条 gold 的 query 由源条款自动派生，query≈条文原文，必然命中源条款——既有矩阵报告（Recall@1/5/10 与 MRR 全 1.0）已自判无效。**检索质量至今没有可信数字**。
Step 10 交付了 `data/eval/rag_p1.jsonl`（58 条人工措辞的自然问题 + 真实 source_id），但**纯检索矩阵的可信基线**（`evaluate_retrieval_matrix.py` 重跑 + 负例）仍未落地，留待 P2 前处理。百炼 rerank 需先开通 gte-rerank 权限（当前 403），不可达时按 SKIP 语义记录。

### R2（需 DEEPSEEK_API_KEY）真实 LLM 问答链路证据

Step 7 已完成接线：legacy 路径（`LLMGenerator`）与 agentic 路径（`build_chat_model` → DeepSeek function calling，`--mode agentic`）均已具备，`--stream` 可流式打印 token；无 Key 时全程离线可跑（`--mock-generator`/fake），有 Key 时跑 `scripts/smoke_agentic.sh` 连跑 3 问。**但 `DEEPSEEK_API_KEY` 仍未配置（`DASHSCOPE_API_KEY` 已配置），质量层评测（`--variant real` + `--judge`）的真实运行记录仍待补。** Key 配好后：跑 `rag_p1 --variant real --judge`，人工核验引用是否指回正确条款、judge 分数是否合理。

### R3 自研 MCP server（路线图 P1 清单项）

把已就位的能力（规范条款检索、水力计算工具族）包装为 MCP server 供外部 Agent 调用。这是路线图 P1 的最后一个清单项，建议在 R1 可信基线落地后启动，避免在无度量状态下对外暴露能力。

## 五、后续展望（P1 收口后再动）

- **语料扩容**：`knowledge/` 下 221 个 md（40MB，标准图集 + 教材）完全未入库；GB50015-2019.jsonl 仅 4.7KB 需单独补语料。这些是「怎么设计 / 怎么选型」类问题的答案来源，纯规范条款只回答「规定是什么」。
- **P2 文档生成**：`graphs/docgen.py`（Human-in-the-Loop 审批图）已就位，下一步接 CLI `--flow docgen` 交互入口 + 计算书模板渲染。
- **真实质量层评测轮询**：`rag_p1 --variant real --judge` + `compare_baseline.py` 做成定时/人工触发，作为真实模型链路的质量哨兵。
- 不建议现在碰 P3（DWG 图纸审查）——P3 被设计文档标为「最难」。
