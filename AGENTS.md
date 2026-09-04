# AGENTS.md

给排水领域 AI Agent 的构建约定。任何会话开工前先读本文件与 `docs/给排水agent改造任务书.md`。

## 项目是什么
面向给排水设计院的 Agent，覆盖规范问答、水力计算、计算书/设计说明生成、图纸审查、材料统计。Step 0–10 的工程机制已交付；P2 的模板引擎、材料聚合与导出、HITL、自检、专项评测已落地。2026-09-04 的受限 docgen 真实 LLM 润色样本为 4/4，但真实 RAG 质量、正式规范引用、检索 gold 与 P3 DWG 自动抽取仍未收口。P2 权威口径见 `docs/design/11-docgen-materials.md`，最新质量证据见 `docs/evals/p2-quality-2026-09-04.md`。

## 三条铁律（不得违反）
1. 计算类永远程序化校验，LLM 不参与数值运算。
2. 引用可追溯，禁止编造条款/规范号。
3. 评测先行，改动不得让关键指标下降。

## 验证命令（改动完成前必须执行）
```bash
bash scripts/ci_gate.sh  # pytest + ruff + mypy + 四套机制层 harness + baseline 对比
_venv/bin/python -m water_agent.harness.runner data/eval/docgen_quality.jsonl
_venv/bin/python -m water_agent.harness.runner data/eval/material_quality.jsonl
_venv/bin/python scripts/evaluate_retrieval_matrix.py  # 真实检索矩阵，需百炼 Key/权限/网络
```
无 Key 属前置条件缺失时输出结构化 SKIP；已有 Key 但网络、权限或组件失败必须记为 blocked/非零退出，禁止包装成 SKIP 或通过。

## 架构现状
- `graphs/rag.py`：legacy 单次 RAG 图（router→retrieve→generate→citation_check→finalize），保留不删；citation_check 已接 Step 5 确定性校验器 `validate_citations`（state 层 `citations` 统一为 `list[CitationClaim]`）。
- 已完成：工具注册表（`ToolSpec`/`ToolRegistry`，执行唯一入口 `ToolRegistry.invoke`）、会话记忆（checkpointer + thread_id）、agentic RAG 外层循环（`graphs/agent_rag.py` = agent_loop→citation_validator→finalize）、DeepSeek function calling 与流式。无效引用有界回流；有检索/有回答但零引用时强制回流一次，再次零引用结构化失败。`--mode agentic` 默认真实模型，`--mock-generator` 走离线脚本模型。
- 动态提示装配（Step 8）：`prompting/assembler.py`（分段系统提示 + 段级预算 + create_react_agent callable 签名）、`prompting/runtime_context.py`（RuntimeContextProjection 变化才注入，省 token）、`ingestion/version_registry.py` + `data/versions.json`（只录有来源事实：非 unknown 条目缺 source/verified_at 加载时降级 unknown；未录入显式返回 status=unknown；四本入库规范现行状态尚未人工核实，当前全部 unknown）；校验器 `validate_citations(version_registry=)` 已接进 legacy/agentic 两条图，版本状态写回 `CitationClaim.spec_version/version_status`，未知只标记进 `warnings` 不进 `errors`（不是模型能重试纠正的事）；`--mode agentic` 走 `prompt=PromptAssembler(tool_registry=registry)`。
- Human-in-the-Loop（Step 9 + P2）：`graphs/docgen.py` = gather→draft→self_check→approval(interrupt)→finalize；自检失败有界重试，拒绝后带反馈重拟，总稿数上限 `MAX_DRAFT_ROUNDS=2`。审批只接受严格 bool，`failed` 不进入 finalize，`approval_log` 由 checkpointer 持久化，SqliteSaver 跨进程 resume 已测。
- 双轨评测（Step 10）：`harness/runner.py --variant real` 默认走 **agentic 图**（模型经 cite_clauses 声明引用、校验器真判；`--real-graph legacy` 引用系统代填、citation_ok 恒真，仅供改造前后对比）；退出码 0 通过/全 SKIP、1 用例失败、2 加载问题、3 有 Key 但真实组件建不起来（`RealComponentsError`，绝不包成 SKIP）；观测 `llm_call` 事件已接进 legacy generate 节点与 agentic `_BudgetChatModel`（token/cost/延迟/摘要，trace 落盘）；PR 门禁 `scripts/ci_gate.sh` + `compare_baseline.py`（THRESHOLDS 单一来源）。
- CLI 收口：`--interactive` 真正作答（`--mode agentic` 用同一 `AgenticSession`/thread 跨轮，`--resume` 恢复；其他模式每行 `run_once`）；`--flow docgen` = gather（计算/RAG）→ draft → self_check → 人审 y/n+原因 → finalize，EOF 挂起 exit 0、`--resume` 恢复，审批记录写 trace；`--doc-type auto|calculation_book|design_note`（P2，默认 auto：计算意图→计算书，其余→设计说明）；检索装配统一 `_build_retriever/_build_reranker`，agentic 检索工具经 `RerankingRetriever` 套 rerank。`--mode legacy|agentic` 双轨共存。
- P2 文档链：`docgen/templates.py` 负责计算书/设计说明的结构化渲染，数值只来自输入 `calc_values`，引用只来自输入 `source_id`；`graphs/docgen.py` 草稿/终稿同源。`docgen/quality.py` 增加受限 prose 润色与确定性自检（结构、逐行数字、引用白名单、非有限值），真实调用失败或结构破坏均返回 blocked，不静默回退为通过。
- P2 材料统计：`materials/core.py` 管段表（段号/系统/管径/长度/材质）→ 按 `(材质, 规格)` 聚合（数量=长度和，round 3 位单一来源），每行带 `segments`+`detail` 溯源；`materials/export.py` 导出 `.xlsx`（材料清单 + 统计说明两个 sheet，openpyxl>=3.1,<4 已入 pyproject 依赖）与 CSV；`tools/material_stats.py`（ToolSpec，注册进 CLI agentic 会话与 runner real-agentic registry）。
- P2 评测：`docgen_p0` 11/11、`material_p0` 10/10，均进入 `scripts/ci_gate.sh` 与 `data/eval/baseline.json`；默认 baseline 四套为 rag_p0/calc_p0/docgen_p0/material_p0。专项质量集为 `docgen_quality` 4 条（离线 4/4；2026-09-04 真实 LLM 4/4）和 `material_quality` 7/7；专项质量集尚未纳入默认 baseline。
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
- `data/eval/baseline.json`：2026-09-04 更新的 PR 机制层基线，包含 rag_p0 11/12、calc_p0 28/28、docgen_p0 11/11、material_p0 10/10。
- `docs/design/10-sdd-harness.md`：SDD+Harness 工程规范（规格模板/错误状态语义/完成定义）。

## 术语
- 机制层评测：mock/in-memory，离线确定性。质量层评测：P2 docgen real 只调用 DeepSeek 润色；RAG real 使用 DeepSeek + 百炼 embedding，并可选 judge。缺 Key 可结构化 SKIP。
- observation：工具结果/错误回填给模型，失败文本必须可见。

## 尚未完成（已知阻塞）
- **真实 RAG 质量层 blocked**：2026-09-04 的 `rag_p1 --variant real --judge` 为机制判分 56/58、独立质量判定 36/58（62.07%），退出码 1；失败与低分项必须继续保留，不能写成通过。2026-09-03 的连接错误是历史阻塞证据，已被本次可运行但未达标的结果更新。
- **真实检索矩阵 blocked**：2026-09-04 已重跑；none/local 四组虽为 Recall@1/5/10=1.0、MRR=1.0，但 70 条 query 与条文原文高度同构，属于泄漏 gold，不能建基线；两组百炼 rerank 为 HTTP 403 `AccessDenied`。
- **正式规范引用 blocked**：docgen 真实润色样本使用显式 `FIX` 引用，只证明受限润色与守卫，不证明真实规范检索/正式条文引用质量。`data/versions.json` 的 GB50015-2019、GB50013-2018、GB50014-2021、GB50318-2017 均为 unknown；公告号、生效日期与来源待人工核实，核实后同步更新 `tests/test_version_registry.py::test_status_from_prod_json`。
- **P3 依赖 blocked**：材料统计目前接收结构化管段表；从 DWG 自动抽取管段仍依赖 P3，P3 尚未启动。
- `DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY` 非持久配置；无 Key 时结构化 SKIP，有 Key但网络或权限失败时记 blocked。
- `src/water_agent/ingestion/semantic_audit.py` 仍有编码事故遗留的 `[??]` 占位字符；需人工确认原字符集后替换并重跑相关质检报告。
