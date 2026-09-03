# Step 10 生产清单对照（2026-09-03）

> 目标：逐项核对 Step 10 上线/收口前的生产准备情况。此文档只记录**当前真实状态**，不把 blocked 项写成完成。

| 项目 | applicable | 状态 | 验证命令 / 证据 | 备注 / 风险 |
|---|---|---|---|---|
| 离线机制层 CI 门禁 | yes | pass | `bash scripts/ci_gate.sh` | pytest + ruff + mypy + rag_p0 + calc_p0 + baseline compare 全过。 |
| 真实质量层评测（rag_p1 --variant real --judge） | yes | blocked | `_venv/bin/python -m water_agent.harness.runner data/eval/rag_p1.jsonl --variant real --judge` → `OpenAIConnectionError: Connection error.` | 当前 shell 代理网络不可用，真实 LLM / judge 无法完成。 |
| 真实检索矩阵重跑 | yes | blocked | 需 Key + 可达网络后运行 `scripts/evaluate_retrieval_matrix.py` | 旧矩阵基于泄漏 gold，不可作为现状基线。 |
| zero-citation 强制闭环 | yes | pass | `tests/test_agent_rag.py`、`scripts/run_agentic_smoke.py` | 有检索、有回答、无 cite_clauses 时回流一次；再次空引用结构化失败。 |
| forbidden_articles 不得改写检索 | yes | pass | `tests/test_harness.py::TestNoGoldLeakInRealAgenticRun` | runner 现在保留真实召回顺序，scorer 只在回答后判分。 |
| 非正式语料策略 | yes | pass | `tests/test_assembler.py`、`tests/test_agent_rag.py` | 非正式条目仅作检索线索，必须显式披露其非正式性质。 |
| 三条铁律单一来源 | yes | pass | `tests/test_assembler.py::test_prompt_rules_single_source_no_drift` | `ROLE_AND_PRINCIPLES` / `DEFAULT_AGENTIC_PROMPT` 已复用同一静态定义。 |
| 质量层独立判定（quality_pass） | yes | pass | `tests/test_ci_gate.py::TestRealQualityJudgement` | 阻断维度：faithfulness / citation_coverage / no_fabrication。 |
| rag_p1 独立质量基线 | yes | blocked | 当前仅有失败的 real 运行报告 `data/eval/reports/rag_p1_20260903T133322Z.json` | 需要网络可用后重新跑出可用报告，再用独立 baseline 文件版本化。 |
| Step 10 收口证据文档 | yes | pass | `docs/evals/step10-prompt-tuning-2026-09-03.md`、本文件 | 已记录前后报告、完整 prompt diff、阻塞项与数据质量问题。 |
| 生产清单风险登记 | yes | pass | 本文件 | 未完成项全部保留为 blocked，不写成完成。 |

## 当前结论

- 可离线确认的机制层与文档收口已完成。
- 真实质量层仍 blocked，不能伪造为 pass。
- rag-p1 的 `GB50015-2019 4.7.1` 仍是数据质量阻塞案例：现有语料是“要点综述 / 非正式规范条文”，不能当正式规范原文引用。
