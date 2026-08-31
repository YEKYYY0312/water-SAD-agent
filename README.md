# 给排水 Agent

P0 最小闭环：规范问答 RAG（检索 → 生成 → 引用校验）+ 本地 JSON Trace + JSONL Harness 评测。

## 环境

- Python 3.12.14（`requires-python >=3.11,<3.13`）
- `langgraph>=1.2.0` / `triviumdb>=0.8.2` / `pydantic>=2.7`
- 安装：`pip install -e .[dev]`

## 快速验证（P0 验收）

```bash
# 1) 完整测试
_venv/bin/pytest -q

# 2) Harness 评测（读取 JSONL → 执行 RAG → 引用存在性判分 → 输出报告，非零退出码）
_venv/bin/python -m water_agent.harness.runner data/eval/rag_p0.jsonl

# 3) CLI 单次 RAG 调用（无真实库时自动回落到 fixture，不伪造引用）
_venv/bin/python -m water_agent.cli "给水管道的设计秒流量如何确定"
_venv/bin/python -m water_agent.cli --json "给水管道的设计秒流量如何确定" | python -m json.tool
```

## 目录与模块

```
src/water_agent/
  domain/        # models / state / fixtures
  retrieval/     # base / in_memory / trivium (TriviumDB search_hybrid 适配器) / loader
  generation/    # base / mock
  graphs/        # rag.py  (router → retrieve → generate → citation_check → finalize)
  observability/ # json_trace.py
  harness/       # loader / scorer / runner / doubles
  cli.py         # P0 CLI 入口
data/
  fixtures/rag_fixture.jsonl   # P0 10 条 fixture，供无真实库时使用
  eval/rag_p0.jsonl            # P0 评测集（12 条有效 + 1 条非法记录用于异常覆盖）
  eval/reports/ / eval/traces/ # Harness 报告与过程 Trace（被 .gitignore 忽略）
```

## 本地 Trace

```bash
python -m json.tool data/traces/<trace_id>.json
# 或 Harness 过程 trace
python -m json.tool data/eval/traces/<trace_id>.json
```

Trace 事件包含：`run_start` / `node_enter` / `node_exit` / `retrieve_complete` / `generate_complete` / `citation_check_complete` / `retry` / `error` / `run_end`，每条事件含 `run_id / trace_id / retry_count / status / retrieved_context / citations`，且 `finish()` 原子落盘为 `<trace_id>.json`，不覆盖。

## Harness

- 输入：`data/eval/rag_p0.jsonl`（每行一 `EvalCase`）
- 判分（确定性，无 LLM Judge）：`actual_status` 是否符合预期；引用是否来自当次 `context`；`gold_articles_any / gold_articles_all / forbidden_articles` 是否满足。
- 输出：`data/eval/reports/<stem>_<UTC>.json` 含 `total / passed / failed / pass_rate / cases / created_at / load_issues`
- 退出码：`0` 全通过；`1` 有用例失败；`2` JSONL 非法或加载错误（缺字段 / 非法 JSON）。

## TriviumDB 适配器

保留 `src/water_agent/retrieval/trivium.py:TriviumRetriever`，显式调用 `TriviumDB.search_hybrid(query_vector, query_text, top_k, hybrid_alpha)`。

- `mock_embed()` 仅用于 P0 自检；真实嵌入在 P1 替换 `embedding_fn` 即可。
- 当 `--db-path` 不存在或打不开时自动回落到 `InMemoryRetriever(fallback_clauses)`，避免无真实数据时误报“已完成真实数据接入”。
- 验证：`_venv/bin/python -c "import triviumdb; db=triviumdb.TriviumDB('/tmp/_p0_probe'); print(db.search_hybrid)"`

## CLI

```bash
python -m water_agent.cli "给水管道设计问题"                    # 人类可读
python -m water_agent.cli --json "给水管道设计问题"              # JSON（含 answer/citations/status/trace_id/trace_path）
python -m water_agent.cli --db-path data/trivium.db "问题"       # 有真实库时走 search_hybrid
```

## 已知约束

- 未实现：真实规范数据导入 / 水力计算 / 云端 LLM / DWG、PDF、Word、Excel / 文档生成 / React / 真实 MCP Server。
- Git：本目录尚未 `git init`（`cursor/` 子目录为独立仓库），按要求不自动创建 commit/tag。
- 无硬编码 `data-cleaning` 路径；P0 仅依赖 `data/fixtures/rag_fixture.jsonl`。

## P0 通过标准

见 `docs/P0_ACCEPTANCE.md`。
