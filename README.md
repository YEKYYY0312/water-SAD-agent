# 给排水 Agent

面向给排水设计院的 AI Agent，覆盖 **规范问答、水力计算、计算书/设计说明生成、图纸审查、材料统计** 共五类能力。
当前已完成 **P1 MVP（规范问答 + 水力计算 + 核心生产化能力）**，正在推进 P2 文档生成。

## 三条铁律

1. **计算类永远走程序化校验** —— 数值由真实水力计算工具算，LLM 只做参数理解和结果解释
2. **引用可追溯** —— 每个回答必须指回「规范号 + 条款号」，禁止编造条号
3. **评测先行** —— 每个阶段连同评测集一起交付，改动不得让关键指标下降

## 环境

- Python 3.12+（`requires-python >=3.11,<3.13`）
- `langgraph>=1.2.0` / `triviumdb>=0.8.2` / `pydantic>=2.7`
- 安装：`uv sync` 或 `pip install -e .[dev]`

## 快速验证

```bash
# 1) 完整测试 + 静态检查
bash scripts/ci_gate.sh

# 2) 机制层 Harness 评测（离线、确定性，PR 门禁）
_venv/bin/python -m water_agent.harness.runner data/eval/rag_p0.jsonl          # 11/12
_venv/bin/python -m water_agent.harness.runner data/eval/calc_p0.jsonl          # 28/28

# 3) 质量层 Harness 评测（需 DEEPSEEK_API_KEY + DASHSCOPE_API_KEY，无 Key 则 SKIP）
_venv/bin/python -m water_agent.harness.runner data/eval/rag_p1.jsonl --variant real

# 4) CLI 无 Key 离线自测（--mock-generator）
_venv/bin/python -m water_agent.cli --mock-generator "给水管道的设计秒流量如何确定？"

# 5) CLI 真实模型链路（需 DEEPSEEK_API_KEY + DASHSCOPE_API_KEY）
_venv/bin/python -m water_agent.cli "给水管道的设计秒流量如何确定？"
_venv/bin/python -m water_agent.cli --mode agentic "给水管道的设计秒流量如何确定？" --stream

# 6) 模型冒烟测试
bash scripts/smoke_agentic.sh
```

## 目录结构

```
src/water_agent/
  domain/         # models / state / citation / fixtures
  retrieval/      # base / in_memory / trivium (TriviumDB search_hybrid 适配器) / loader / rerank
  generation/     # base / mock / llm (DeepSeek) / chat_model (ChatOpenAI) / cost
  graphs/         # rag / calc / agent_rag (agentic 循环) / docgen (HITL) / chat
  prompting/      # assembler / runtime_context
  ingestion/      # chunker / pipeline / version_registry / quality / gold_set
  harness/        # loader / scorer / runner / doubles / retrieval_metrics / llm_judge
  observability/  # json_trace / otel (guarded OTel GenAI)
  cli.py          # 生产问答 CLI (--mode legacy|agentic, --variant, --mock-generator, --stream)
  embedding.py    # 阿里云百炼 Embedding Provider
  session.py      # 多轮会话记忆 (checkpointer + sqlite)
  tools/          # hydraulics (水力计算工具族) / calc_velocity 等工具规格
data/
  eval/           # 评测集 & 报告 & 基线
    rag_p0.jsonl          # P0 RAG 评测集（12 条有效 + 1 条非法）
    calc_p0.jsonl         # P0 计算评测集（28 条）
    rag_p1.jsonl          # P1 真实规范问答评测集（58 条，变体 real）
    baseline.json         # 门禁基线（compare_baseline.py 消费）
    reports/              # Harness 评测报告（JSON，被 .gitignore 忽略）
    traces/               # 过程 Trace（JSON，被 .gitignore 忽略）
    judge_cache/          # LLM-as-judge 结果缓存
  curated/clauses/  # 人工审校后的规范条款语料
  indexes/          # TriviumDB 向量索引（build_index.py 构建）
  fixtures/         # 测试用 fixture 条款
  knowledge/        # 原始知识库语料（40MB，221 个 md）
  versions.json     # 规范版本注册表
scripts/
  ci_gate.sh                  # PR 门禁（pytest + ruff + mypy + harness + 基线对比）
  compare_baseline.py          # 基线对比工具（阈值：通过率下降 ≤2pt，引用正确率下降 ≤3pt）
  build_index.py               # 构建 TriviumDB 索引
  smoke_agentic.sh             # 真实模型冒烟（3 问）
  run_real_qa.sh               # 真实问答验证
  verify.sh                    # 统一验证（旧版，由 ci_gate.sh 取代）
tests/              # 所有测试（pytest）
```

## 双轨评测

| 轨道 | 变体 | 检索器 | 生成器 | 依赖 | 用途 |
|------|------|--------|--------|------|------|
| 机制层 | `mock` | InMemoryRetriever (fixture) | MockGenerator | 无 | PR 门禁，离线确定性 |
| 质量层 | `real` | TriviumDB + 真实 Embedding | DeepSeek/ChatOpenAI | DASHSCOPE + DEEPSEEK Key | 质量评估，人工/定时触发 |

**SKIP 语义**：质量层无 Key 时输出结构化 `SKIP`（退出码 0，不参与基线比较）。
有 Key 但调用出错退出码 ≠ 0。

## CI 门禁

`scripts/ci_gate.sh`（PR 门禁，只跑离线机制层）：

```bash
pytest → ruff → mypy → rag_p0 机制层 → calc_p0 ≥95% → 基线对比
```

阈值集（`scripts/compare_baseline.py` 中 `THRESHOLDS`）：

| 指标 | 阈值（百分点） | 说明 |
|------|---------------|------|
| `pass_rate_decline_pt` | 2.0 | 通过率相对基线下降 ≤2pt |
| `citation_ok_decline_pt` | 3.0 | 引用正确率相对基线下降 ≤3pt |
| `new_core_failures` | 0 | 无新增核心失败（PASS_TO_PASS 门禁） |

## 关键能力

- **规范问答 RAG**：TriviumDB 混合检索 + 父子块回填 + LocalReranker/BailianReranker + 引用校验 + 重试回流
- **Agentic RAG**：`--mode agentic` 由 `create_react_agent` 驱动的工具调用型多轮循环，引用声明工具 + 确定性校验
- **水力计算**：流速 / 沿程损失 / 管径 / 泵扬程 / 排水坡度等，全部程序化
- **Human-in-the-Loop**：`--flow docgen` 文档生成审批节点（interrupt + Command resume，拒绝回流 ≤2 轮）
- **动态提示装配**：系统提示分段 + 运行时上下文投影 + 规范版本注册表
- **LLM-as-judge**：5 准则锚定刻度评分（忠实度/引用覆盖/完整性/清晰度/不编造），结果缓存防重复扣费
- **观测**：本地 JSON Trace + 可选 OTel GenAI 约定（guarded import）

## 容量与基线

| 评测集 | 条数 | 通过率 | 说明 |
|--------|------|--------|------|
| rag_p0 | 12 | 11/12 (91.7%) | 含 1 条设计的 bad_citation 陷阱 |
| calc_p0 | 28 | 28/28 (100%) | 容差 rel=1e-4，4 条带越界断言 |
| rag_p1 | 58 | — | 真实规范问答 gold，质量层专用（需 Key） |

输出：`pytest 375 全绿` / `ruff 0` / `mypy 0`（Step 10 清零）。

## 已知约束

- GB50015-2019 语料仅 9 条摘要条目（非正式规范条文），正式问答建议以标准成果为准
- 百炼 rerank（gte-rerank）暂不可达，默认 LocalReranker
- 无 DEEPSEEK_API_KEY 时离线闭环（`--mock-generator` / `--variant mock`）完全可用
- 语料扩容：`knowledge/` 下 221 个 md（40MB）尚未入库