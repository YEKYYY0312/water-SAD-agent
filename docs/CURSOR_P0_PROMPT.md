# Cursor P0 第一步 Prompt

请在当前项目根目录执行本任务。你现在只负责 P0 的环境诊断、技术验证和工程骨架设计，不得直接实现 P1 及之后的业务能力。

## 目标

验证并搭建以下最小闭环的基础：

```text
问题输入
  -> TriviumDB 检索适配器
  -> Mock Generator
  -> 结构化引用回答
  -> Harness 确定性评测
  -> 本地 JSON Trace
```

真实规范数据目录尚未提供。数据目录由用户后续提供，在此之前只能使用项目内的最小 fixture。

## 开始前必须读取

1. `docs/design/00-overview-and-decisions.md`
2. `docs/design/01-architecture.md`
3. `docs/design/02-loop-engineering.md`
4. `docs/design/03-harness-engineering.md`
5. `docs/design/04-file-reading-and-mcp.md`
6. `docs/design/06-roadmap.md`
7. `docs/CURSOR_BUILD_BRIEF.md`

读取后先输出一份简短的“理解与风险报告”，不得跳过。

## 第一步：环境与依赖诊断

依次检查并报告：

1. 当前工作目录和项目结构。
2. Python 版本。项目只允许 Python 3.11 或 3.12。
3. 当前目录是否为 Git 仓库。
4. `../data-cleaning` 是否存在。
5. 是否存在 `DATA_ROOT` 环境变量或等效配置。
6. TriviumDB 是否可以安装和导入。
7. TriviumDB 是否存在并可调用 `search_hybrid`。
8. LangGraph、LangChain、FastAPI、pytest 是否可用。
9. 当前机器是否适合 TriviumDB 的本地运行和持久化。

如果 TriviumDB 安装失败或 API 无法验证：

- 不得偷偷改用 Chroma、Qdrant 或其他数据库；
- 保留 TriviumDB 作为正式技术选型；
- P0 可以使用 `InMemoryRetriever` 作为测试替身；
- 在报告中明确失败原因、影响和后续处理方式。

## P0 范围

必须实现：

1. Python 项目骨架和依赖配置。
2. 结构化领域模型：
   - `RetrievedClause`
   - `Citation`
   - `GeneratedAnswer`
   - `TraceEvent`
   - `EvalCase`
   - `EvalResult`
3. `Retriever` 接口。
4. TriviumDB Retriever 适配器接口和最小烟囱测试。
5. 本地 `InMemoryRetriever` 测试替身。
6. `MockGenerator`。
7. 最小 LangGraph RAG 图：
   - `router`
   - `retrieve`
   - `generate`
   - `citation_check`
   - `finalize`
8. 本地 JSON Trace。
9. JSONL Harness：
   - 数据加载；
   - 引用存在性判分；
   - 状态判分；
   - 汇总报告；
   - 失败返回非零退出码。
10. 至少 10 条项目内 mock RAG 评测用例。
11. 单元测试和最小集成测试。

## P0 明确禁止

本阶段不得实现：

- 水力计算公式和水力计算工具；
- DWG、PDF、Word、Excel 解析；
- 真实云端 LLM 调用；
- 完整 Supervisor；
- 计算、图纸、文档、材料子图；
- 真实 MCP server；
- React 前端；
- 自动调参；
- 自动判断规范是否现行有效；
- 硬编码用户尚未提供的 `data-cleaning` 路径。

## 建议目录

```text
src/water_agent/
  domain/
  graphs/
  retrieval/
  generation/
  observability/
  harness/
  cli.py
tests/
data/eval/
data/fixtures/
config/
scripts/
```

## 状态和循环要求

状态至少包含：

```text
query
intent
context
answer
citations
errors
run_id
trace_id
retry_count
step_count
status
missing_inputs
assumptions
```

所有重试必须有上限。至少设置：

- 最大总步数；
- 最大引用校验重试次数；
- 检索异常和生成异常的结构化错误；
- 无结果时的明确失败状态。

引用检查必须验证：

```text
引用的 source_id/article 是否真实存在于本次检索结果
```

不得仅因为模型输出了一个规范号就认为引用有效。

## Harness 用例格式

P0 使用如下结构：

```json
{
  "id": "rag-p0-0001",
  "type": "rag",
  "query": "给水管道的设计问题",
  "gold_articles_any": ["fixture.spec 1.1"],
  "gold_articles_all": [],
  "forbidden_articles": [],
  "expected_status": "succeeded",
  "eval": "citation_exists"
}
```

至少覆盖：

- 正常命中并生成；
- 多条款命中；
- 无检索结果；
- 生成了不存在的引用；
- 评测数据格式错误；
- 达到最大重试次数；
- Trace 字段完整性。

## Git 规则

当前目录可能不是 Git 仓库：

- 先检查；
- 如果不是，报告结果；
- 可以在用户确认后执行 `git init`；
- 不得自动执行 commit 或 tag；
- 不得删除或覆盖用户已有文件。

## 完成标准

完成后必须实际运行并报告：

```bash
pytest
python -m water_agent.harness.runner data/eval/rag_p0.jsonl
python -m water_agent.cli "给水管道设计问题"
```

如果包路径需要调整，提供等价的可执行命令。

最终报告必须包含：

1. 修改和新增的文件；
2. TriviumDB 验证结果；
3. 测试命令及结果；
4. Harness 报告摘要；
5. P0 验收是否通过；
6. 未解决风险；
7. 进入 P1 前需要用户提供的 `DATA_ROOT` 路径和数据校验结果。

一次只实现一个小增量。每完成一个增量，先运行相关测试，再继续下一项。不要为了“完整”创建未实现功能的空壳代码。
