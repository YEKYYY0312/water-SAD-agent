# P0 最终验收与基线固化

## 1. 检查项

| 项 | 结果 | 说明 |
|---|---|---|
| 项目目录与模块依赖 | ✅ | `pyproject.toml` / `src/water_agent` 结构完整；`langgraph / triviumdb / pydantic` 可导入 |
| RAG / Trace / Harness 接口一致 | ✅ | `build_rag_graph(retriever, generator, trace)` / `JsonTraceRecorder` / `HarnessRunner` 接口稳定 |
| 循环无上限 | ✅ 无 | `MAX_TOTAL_STEPS=10 / MAX_CITATION_RETRIES=2 / MAX_RETRIEVAL_RETRIES=2`，`route_after_citation` 双上限守卫 |
| 异常吞没 | ✅ 无 | 仅 `TriviumRetriever` 回落路径捕获异常并记录 `db_error`；其余 `finish()` / Harness 非法 JSON 均显式抛 `OSError / EvalLoadError` |
| 引用伪造 | ✅ 无 | `citation_check` 校验 `citations ⊆ context`，`MockGenerator` 仅取 `context[:3]`，`scorer` 二次校验引用来自当次 `context` |
| Trace 覆盖 | ✅ 无 | `finish()` 原子写 `<trace_id>.json.tmp → <trace_id>.json`，Harness 每次用例独立 `trace_id/ run_id` |
| 硬编码 data-cleaning | ✅ 无 | 全局仅使用 `data/fixtures/rag_fixture.jsonl` 与 `data/eval/*` |
| P0 范围外实现 | ✅ 无 | 未新增水力计算 / 真实数据导入 / LLM / DWG-PDF / MCP Server |

## 2. TriviumDB 验证

- `triviumdb==0.8.2` 已安装，`TriviumDB.search_hybrid(query_vector, query_text, top_k, hybrid_alpha)` 可用。
- 适配器 `src/water_agent/retrieval/trivium.py:TriviumRetriever` 显式调用 `search_hybrid`，失败回落到 `InMemoryRetriever`，`mock_embed(dim=1536)` 仅用于 P0 自检。

## 3. 无真实数据时的行为

- 无 `db_path` 或路径不存在时，CLI 与检索均回落到 `data/fixtures/rag_fixture.jsonl` 的 10 条 fixture，并在输出中标注 `retriever_fallback: true`，不误报为已接入真实库。

## 4. 验收命令

```bash
_venv/bin/pytest -q
_venv/bin/python -m water_agent.harness.runner data/eval/rag_p0.jsonl
_venv/bin/python -m water_agent.cli "给水管道设计问题"
```

## 5. P0 通过判定（9 项）

需同时满足：`pytest` 通过 / Harness 可读 JSONL 并输出报告 / 失败用例非零退出 / CLI 可完成 RAG / Trace 合法且字段完整 / Trace 含检索-生成-引用检查-结束事件 / TriviumDB 适配器存在 / 无真实数据不误报 / 无 P0 范围外实现。
