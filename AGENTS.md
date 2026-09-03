# AGENTS.md

给排水领域 AI Agent 的构建约定。任何会话开工前先读本文件与 `docs/给排水agent改造任务书.md`。

## 项目是什么
面向给排水设计院的 Agent，覆盖五类能力：规范问答、水力计算、计算书生成、图纸审查、材料统计。Step 0–10 已完成（工程基线 / 水力计算内核 / 计算子图+意图 / 多轮会话记忆 / 工具抽象层 / 引用声明工具+确定性校验器 / agentic RAG 外层循环 / 真实模型接入：DeepSeek function calling + 流式 / 系统提示装配+运行时上下文投影+规范版本注册表 / Human-in-the-Loop 审批图 / 双轨评测+CI 门禁+观测），任务书全部步骤已交付；P2 机制层（文档模板引擎 + 材料统计 + Excel 导出）已完成（2026-09-03，口径与验收见 `docs/design/11-docgen-materials.md`），后续为质量层真实运行与 P3。git 已 init，共 41 次提交，HEAD=56f3ce0。

## 三条铁律（不得违反）
1. 计算类永远程序化校验，LLM 不参与数值运算。
2. 引用可追溯，禁止编造条款/规范号。
3. 评测先行，改动不得让关键指标下降。

## 验证命令（改动完成前必须执行）
```bash
bash scripts/verify.sh   # 统一门禁：pytest + ruff + rag_p0(11/12) + calc_p0(28/28)
_venv/bin/mypy src/water_agent   # 必须清零（Step 10 已达成，ci_gate 强制）
_venv/bin/python scripts/evaluate_retrieval_matrix.py  # 真实检索矩阵（需网络+百炼 Key）
```
真实模型/真实检索无 Key 或网络不可达时：输出结构化 SKIP，禁止伪造结果。

## 架构现状
- `graphs/rag.py`：legacy 单次 RAG 图（router→retrieve→generate→citation_check→finalize），保留不删；citation_check 已接 Step 5 确定性校验器 `validate_citations`（state 层 `citations` 统一为 `list[CitationClaim]`）。
- 已完成：工具注册表（Step 4：`ToolSpec`/`ToolRegistry`，执行唯一入口 `ToolRegistry.invoke`）、会话记忆（Step 3：checkpointer + thread_id）、agentic RAG 外层循环（Step 6：`graphs/agent_rag.py` = agent_loop(create_react_agent 子图)→citation_validator→finalize；引用校验失败以带错误文本 ToolMessage 回流 ≤2 次；agent_loop 轮数预算默认 10）、真实模型接入（Step 7：`generation/chat_model.py` 的 `build_chat_model`（DeepSeek OpenAI 兼容，无 Key 抛错）；CLI `--mode agentic` 默认走真实模型、`--mock-generator` 走脚本化占位模型（离线）、`--stream` 流式打印 token；无 Key/无真实检索装配时输出结构化错误并指引，绝不静默降级）。
- 动态提示装配（Step 8）：`prompting/assembler.py`（分段系统提示 + 段级预算 + create_react_agent callable 签名）、`prompting/runtime_context.py`（RuntimeContextProjection 变化才注入，省 token）、`ingestion/version_registry.py` + `data/versions.json`（只录有来源事实：非 unknown 条目缺 source/verified_at 加载时降级 unknown；未录入显式返回 status=unknown；四本入库规范现行状态尚未人工核实，当前全部 unknown）；校验器 `validate_citations(version_registry=)` 已接进 legacy/agentic 两条图，版本状态写回 `CitationClaim.spec_version/version_status`，未知只标记进 `warnings` 不进 `errors`（不是模型能重试纠正的事）；`--mode agentic` 走 `prompt=PromptAssembler(tool_registry=registry)`。
- Human-in-the-Loop（Step 9）：`graphs/docgen.py` = gather→draft→approval(interrupt)→finalize；拒绝→带反馈回流 draft 重拟，草稿总稿数上限 MAX_DRAFT_ROUNDS=2（初稿 + 1 次重拟），最后一稿仍被拒收口 failed；approval 字段 None=待审批（重拟时复位）/ True=已批 / False=已拒；每次 resume 追加 `approval_log`（checkpointer 持久化）；DocGenState 预留 citations/calc_result；无 checkpointer 拒绝构建；SqliteSaver 落盘→重开连接→resume 已测。
- 双轨评测（Step 10）：`harness/runner.py --variant real` 默认走 **agentic 图**（模型经 cite_clauses 声明引用、校验器真判；`--real-graph legacy` 引用系统代填、citation_ok 恒真，仅供改造前后对比）；退出码 0 通过/全 SKIP、1 用例失败、2 加载问题、3 有 Key 但真实组件建不起来（`RealComponentsError`，绝不包成 SKIP）；观测 `llm_call` 事件已接进 legacy generate 节点与 agentic `_BudgetChatModel`（token/cost/延迟/摘要，trace 落盘）；PR 门禁 `scripts/ci_gate.sh` + `compare_baseline.py`（THRESHOLDS 单一来源）。
- CLI 收口：`--interactive` 真正作答（`--mode agentic` 用同一 `AgenticSession`/thread 跨轮，`--resume` 恢复；其他模式每行 `run_once`）；`--flow docgen` = gather（计算/RAG）→ draft → 人审 y/n+原因 → finalize，EOF 挂起 exit 0、`--resume` 恢复，审批记录写 trace；`--doc-type auto|calculation_book|design_note`（P2，默认 auto：计算意图→计算书，其余→设计说明）；检索装配统一 `_build_retriever/_build_reranker`，agentic 检索工具经 `RerankingRetriever` 套 rerank。`--mode legacy|agentic` 双轨共存。
- P2 文档模板引擎：`docgen/templates.py`（计算书/设计说明 = 必需章节 工程概况/设计依据/计算参数(与结果)/结论 + 可选材料统计；数值只渲染入参浮点原值+单位并保留 `calc_values` 映射，引用只渲染入参 source_id，`validate_citation_policy` 防编造、`check_structure` 结构校验）；`graphs/docgen.py` 状态加 `doc_type`，draft 以引擎渲染带"（第 N 稿）"标题，finalize 用**同一数据源**做正式渲染（终稿无"草稿/稿次"，用户反馈章节保留可追溯）——批准内容与终稿内容一致。
- P2 材料统计：`materials/core.py` 管段表（段号/系统/管径/长度/材质）→ 按 `(材质, 规格)` 聚合（数量=长度和，round 3 位单一来源），每行带 `segments`+`detail` 溯源；`materials/export.py` 导出 `.xlsx`（材料清单 + 统计说明两个 sheet，openpyxl>=3.1,<4 已入 pyproject 依赖）与 CSV；`tools/material_stats.py`（ToolSpec，注册进 CLI agentic 会话与 runner real-agentic registry）。
- P2 评测：`data/eval/docgen_p0.jsonl`（10 条：结构合规+数值双阈值+引用必须/禁止）与 `data/eval/material_p0.jsonl`（10 条：行集合一致+数量双阈值+全段溯源覆盖，含 1 条 invalid_input）机制层均 10/10；`DocgenCase`/`MaterialCase` 已进 loader/scorer/runner（离线确定性）；`scripts/ci_gate.sh` 已加两套 ≥95% 门禁。
- 计算永远走程序化工具，缺参→`missing_inputs`，越界→`assumptions`。

## 改动范围纪律
- 本任务书共 Step -1…10，每步一个逻辑面，一步一 commit。
- 未完成功能用特性开关护住，不删旧路径。
- 每步验收 = 该步"最小闭环验证"命令全部跑出预期且 `pytest` 不回退。

## 数据资产策略
- 入库：源码、设计文档、评测集、`data/curated/clauses/` + `manifest.json`。
- 不入库（可重建）：`data/indexes/*.db/.vec/.wal/.lock`、`data/quality/reports/`、`data/eval/{reports,traces}/`。
- 重建索引：`scripts/build_index.py`（源自 `data/curated/`）。

## 工程基线入口
- `data/eval/baseline_preflight.json`：基线记录（含 Step -1 与 Step 0–10 post_state），改动前先对照。
- `docs/design/10-sdd-harness.md`：SDD+Harness 工程规范（规格模板/错误状态语义/完成定义）。

## 术语
- 机制层评测：mock/in-memory，离线确定性。质量层评测：真实模型+检索，需 Key，可 SKIP。
- observation：工具结果/错误回填给模型，失败文本必须可见。

## 尚未完成（已知阻塞）
- **P2 质量层后置**（机制层已交付，见 docs/design/11-docgen-materials.md §6）：正式文档 prose 的真实 LLM 润色（基于 `RenderedDoc` 结构化底座，数值仍程序化注入）；正式规范语料到位后 docgen 引用走真实检索 + 版本注册表校验；材料统计自动抽取依赖 P3 DWG 后置。
- **真实检索矩阵需重跑**：2026-09-03 发现 triviumdb 文本索引不落盘、此前所有真实检索实际是纯向量（hybrid_alpha 无效）；`TriviumRetriever` 已改为打开库时重建文本索引（commit cdd6f46），`data/eval/reports/retrieval_matrix_*` 旧数据作废，需有 Key 时重跑 `scripts/evaluate_retrieval_matrix.py`。
- agentic 路径模型一次 cite_clauses 都不调也会 succeeded（引用可追溯未强制）：待加"有检索、有回答、无引用 → 回流一次'请声明引用'"。
- Step 10 收口已补齐：`docs/evals/step10-prompt-tuning-2026-09-03.md` 与 `docs/reviews/step10-production-checklist-2026-09-03.md` 已写入版本化证据；离线机制层 `bash scripts/ci_gate.sh` 已通过，`quality_pass` 作为独立质量判定已落到测试与 runner 聚合。
- 真实质量层仍 blocked：`_venv/bin/python -m water_agent.harness.runner data/eval/rag_p1.jsonl --variant real --judge` 在当前 shell 通过代理返回 `OpenAIConnectionError: Connection error.`，无法生成可用的真实质量层收口证据（不能伪造通过）。
- 真实检索矩阵仍需在可达网络+Key 下重跑：`retrieval_gold.jsonl` 的 70 条 query 仍是泄漏 gold（query≈条文原文），`rag_p1` 里的 `GB50015-2019 4.7.1` 仍属数据质量阻塞案例（非正式语料只能作检索线索）。
- `DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY` 非持久配置：本机有 Key 但网络不可达时按阻塞记录，不把失败包装成 SKIP；无 Key 时仍应走结构化 SKIP。
- `data/versions.json` 四条规范（GB50015-2019/GB50013-2018/GB50014-2021/GB50318-2017）现行状态待人工核实（公告号/生效日期）；核实后改 `status: current` 并填 `source`，同步改 `tests/test_version_registry.py::test_status_from_prod_json` 断言。核实前所有引用的 `version_status` 都是 unknown（只标记）。
- `ingestion/semantic_audit.py` 质检报告中存在编码事故遗留的 `[??]` 占位字符（代码内 TODO 需人工确认原字符集后替换），相关质检报告需在人工确认后重跑。