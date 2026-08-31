# AGENTS.md

给排水领域 AI Agent 的构建约定。任何会话开工前先读本文件与 `docs/给排水agent改造任务书.md`。

## 项目是什么
面向给排水设计院的 Agent，覆盖五类能力：规范问答、水力计算、计算书生成、图纸审查、材料统计。Step 0–4 已完成（工程基线 / 水力计算内核 / 计算子图+意图 / 多轮会话记忆 / 工具抽象层），下一步 Step 5（引用声明工具与确定性校验器）。git 已 init，共 10 次提交，HEAD=e2fca9d。

## 三条铁律（不得违反）
1. 计算类永远程序化校验，LLM 不参与数值运算。
2. 引用可追溯，禁止编造条款/规范号。
3. 评测先行，改动不得让关键指标下降。

## 验证命令（改动完成前必须执行）
```bash
bash scripts/verify.sh   # 统一门禁：pytest + ruff + rag_p0(11/12) + calc_p0(28/28)
_venv/bin/mypy src/water_agent   # 仅记录告警，Step 10 清零
_venv/bin/python scripts/evaluate_retrieval_matrix.py  # 真实检索矩阵（需网络+百炼 Key）
```
真实模型/真实检索无 Key 或网络不可达时：输出结构化 SKIP，禁止伪造结果。

## 架构现状
- `graphs/rag.py`：legacy 单次 RAG 图（router→retrieve→generate→citation_check→finalize），保留不删。
- 改造目标：工具注册表 + agentic 外层循环 + 会话记忆，用 `--mode legacy|agentic` 双轨共存。
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
- `data/eval/baseline_preflight.json`：基线记录（含 Step -1 与 Step 0–4 post_state），改动前先对照。
- `docs/design/10-sdd-harness.md`：SDD+Harness 工程规范（规格模板/错误状态语义/完成定义）。

## 术语
- 机制层评测：mock/in-memory，离线确定性。质量层评测：真实模型+检索，需 Key，可 SKIP。
- observation：工具结果/错误回填给模型，失败文本必须可见。

## 尚未完成（已知阻塞）
- 检索 gold 存在泄漏（retrieval_gold.jsonl 的 70 条 query 由源条款自动派生，query≈条文原文，必然命中源条款），需构造独立可人工验证的 gold（不同措辞/引用式/跨条款 + 负例）后重跑真实检索矩阵。
- DEEPSEEK_API_KEY 未配置：真实 LLM 问答链路无运行证据，按 SKIP 语义处理。