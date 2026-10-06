# P7-S4 决策记录：按意图最小暴露与工具分组（Step C）

**日期**：2026-10-06
**状态**：已完成
**规格来源**：`docs/design/16-p7-plugin-mcp.md` §2 F4/F5、§3.6、§7 P7-S4；执行 Prompt `docs/execution-prompts/P7-S4-intent-tool-exposure.md`（其 §0 的决定以 Prompt 为准）
**基线**：main `7162eee`（P7-S4 执行 Prompt 合入后）

---

## 0. 偏离与修订

### 0.1 执行 Prompt §0 的决定（照做）

| 决定 | 落点 |
|---|---|
| 每轮在 `agent_loop` 入口决定一次，存进 state，回流复用 | `graphs/agent_rag.py::make_agent_loop`、`AgenticRAGState.tool_exposure`、`PER_TURN_RESET` |
| 确定性关键词 / 正则，不调模型 | `plugins/exposure.py`（AST 断言不 import 模型 / HTTP / random / time） |
| `calc_velocity` 与检索、引用同属 core，每轮必开 | `plugins/builtin.py`（`HydraulicsPlugin` 声明 `CORE`） |
| 未开放的工具被调用时照常执行，trace 标记 | `unexposed_call` 事件（选了"单独一条事件"，没改 `tools/**` 与 `ToolRegistry`） |
| 按意图格式的指引：工具名 + 描述首句 + "本轮未开放"行；回退格式逐字不变 | `plugins/exposure.py::guide_line / render_guide`、`PromptAssembler._build_tool_guide` |
| MCP 每台 server 一组 ambient，不加 per-server 关键词 | `McpContribution.exposure_groups()` |
| 全量 `< 4500` 断言与 F4 的 4497 原样保留 | 受保护断言未动（范围守卫核对） |
| 回滚开关 `WATER_AGENT_TOOL_EXPOSURE=intent|full` | `plugins/exposure.py::resolve_exposure_strategy`，`assemble_tools` 解析一次 |

### 0.2 实测后追加的偏离

| 执行 Prompt 原文 | 本步做法 | 原因 |
|---|---|---|
| 超预算时"按入选顺序的逆序整组丢弃" | **按优先级逐组入选，放不下的整组跳过** | 效果同样是低优先级先让位；但逆序丢弃会在一个大组放不下时，把排在它后面、本来放得下的小组一起丢掉。逐组入选不会 |
| Step 3"不在本 commit 写基线，留到 Step 5" | Step 3 的 commit 里就为 `lifecycle/app/app_kwargs` 一处变化 `--write --reason` | Prompt §4 开头要求"除 Step 5 外每个 commit 快照一致"，与 Step 3 的写法自相矛盾；照前者做，每个 commit 都是绿的。这处变化不属于 surface，不递进版本号 |
| 新增 §4 不变量 P7-I08（两处构图都传 `exposure=`） | 写成独立测试 `test_production_app_receives_exposure_policy`，挂验收 ID P7-S4-A08 | `tests/test_p7_traceability.py` 把不变量个数写死为 7，该文件在本步 forbid 里（停止条件）。判定逻辑不变；若要把它登记成 P7-I08，另开小 PR 改那个计数 |
| `tool_exposure_eval` 的指标由 harness 内部聚合 | 抽成 `metrics_from_case_dicts`，harness 与比较器共用 | 比较器要从用例明细重算指标以拒绝手改报告，两处必须是同一个函数 |
| 预算按 `registry.list_tools()` 口径 | 按**实际发出**的口径（`bound_tool_definition`，`_BudgetChatModel.bind_tools` 同用） | 实测两者对本地工具一致（全量 4497），对 fake MCP 工具不一致（实际发出 4716，注册口径 4724）；预算必须量"真正发出去的" |

---

## 1. 交付

| commit | 产物 | 要点 |
|---|---|---|
| `296ff43` | `tool_exposure_p0`（36 条）+ `harness/tool_exposure_eval.py` + 脚本模型记录 `tools` | 在未改动的暴露代码上跑出"改之前"（§2.1）；数据集自此冻结 |
| `9b1fdb4` | `plugins/exposure.py`、`PluginContribution.exposure` / `exposure_groups()`、内置组规则、`ToolAssembly.exposure` | 未接线，快照不变 |
| `6112e73` | 接线：`agent_loop` 决定、`_BudgetChatModel` 过滤（流式与同步）、指引渲染、trace 两类事件、会话与 runner 传 `exposure=` | 快照只有 `app_kwargs` 变化（§3.1） |
| `e447b97` | `scripts/compare_tool_exposure_p0.py`、入仓基线 `data/eval/tool_exposure_p0_main.json`、`ci_gate.sh` 接入 | 比较器拒绝手改 / full 策略 / 数据集变化 / 用例消失 / PASS_TO_PASS 回退 |
| `a4adeee` | 快照 surface 加暴露探针、两个版本号递进、F5 已知数字改为断言修复后的事实 | §3.2 |
| 本 commit | 规格 §2/§3.6/§5/§7、本记录、AGENTS.md | — |

---

## 2. 实测

### 2.1 `tool_exposure_p0` 改前 / 改后 / 回滚开关

| 指标 | 改之前（`296ff43`，baseline-s3） | intent（入仓基线） | `--strategy full` |
|---|---|---|---|
| 通过 | 7/36 | **36/36** | 7/36 |
| `needed_tool_exposed_rate` | 1.0 | 1.0 | 1.0 |
| `needed_tool_in_guide_rate` | 0.790323 | **1.0** | 0.790323 |
| `forbidden_tool_exposed_count` | 22 | **0** | 22 |
| `schema_budget_violation_count` | 6 | **0** | 6 |
| `guide_truncated_count` | 19 | **0** | 19 |
| `unexposed_call_executed_rate` | 0.0 | **1.0** | 0.0 |
| `harness_error_count` | 0 | 0 | 0 |
| `exposed_tool_count_mean` / max | 4.7 / 7 | 3.825 / 5 | 4.7 / 7 |
| `exposed_schema_chars_mean` / max | 4098.75 / 5674 | 3001.05 / 4497 | 4098.75 / 5674 |
| `overexposure_rate` | 0.725 | 0.125 | 0.725 |

- 改之前 `needed_tool_exposed_rate` 已是 1.0：全量暴露的问题不在漏发，而在指引截断（F5）与超预算（F4）。
- `full` 与改之前**逐轮**比对开放工具、指引列出的工具、schema 字符数、指引字符数：0 处差异（回滚开关回到的确实是 S3 行为）。
- `unexposed_call_executed_rate` 改之前为 0.0，是因为那时没有"未开放"这回事，读图工具本来就开放、也没有标记；不是执行失败。
- 入仓基线连跑两次逐字节相同，不含绝对路径。

### 2.2 数据集冻结

```
$ git diff 296ff43..HEAD -- data/eval/tool_exposure_p0.jsonl | wc -l
       0
```

关键词规则（`plugins/builtin.py` 的 `MATERIALS_EXPOSURE` / `DRAWING_EXPOSURE`）在 `296ff43` 之后编写；没有改过任何用例。

### 2.3 预算实测（实际发出口径）

| 组合 | schema | 按意图指引 |
|---|---|---|
| core（retrieve + cite + calc） | 2125 | 264–277 |
| core + materials | 3539 | 286–321 |
| core + materials + drawing | **4497** | 360 |
| core + MCP（fake，两个工具） | 3310 | 462 |
| core + materials + MCP | 4724 → MCP 让位 | 360 |

本地工具全开正好 4497，余量 3；再加任何一台 MCP 都必须让位。所以**下一个新工具（不论本地还是 MCP）一旦与"读图 + 材料"同时命中，就会被整组跳过**——这是策略的正确行为，但意味着 F4 的余量问题只是从"装不进注册集"变成了"最紧组合里装不进"（见 §5）。

### 2.4 门禁与测试

- 全量 pytest：3440 passed / 30 skipped / 0 failed（Step 5 后）。
- `COMPOSE_PROJECT_NAME=p7s4 bash scripts/ci_gate.sh`（worktree 内、不预设 PYTHONPATH）：**EXIT=0**；日志首行 `water_agent 导入自：<p7-s4 worktree>/src/water_agent`；`tool_exposure_p0 strategy=intent passed=36/36`，对比 `ok: true`；`agentic_p0` 21/21。
- 反证：
  - 比较器：手改指标、`full` 策略报告、删一条用例、让一条用例回退、改数据集摘要，各自判失败（`test_compare_rejects_tampered_and_regressed_reports`）；
  - harness：脚本多一步即失败、缺 `must_expose` 是加载问题、分母为 0 的速率判不达标；
  - 指引预算：把预算压到只够 core，intent 组以 `guide_budget` 让位；core 自身超预算构造期抛错。
- `ruff check src`、`mypy src/water_agent`（216 个文件）无问题；范围守卫通过。

---

## 3. 快照变化（`--diff` 原文）

### 3.1 Step 3（`6112e73`，lifecycle，不递进版本号）

```
/profiles/runner_drawing_env/lifecycle/app/app_kwargs: ["checkpointer", "max_tool_turns", "model", "prompt", "request_context", "tools", "trace"] -> ["checkpointer", "exposure", "max_tool_turns", "model", "prompt", "request_context", "tools", "trace"]
/profiles/runner_mcp_fake/lifecycle/app/app_kwargs: ["checkpointer", "max_tool_turns", "model", "prompt", "request_context", "tools", "trace"] -> ["checkpointer", "exposure", "max_tool_turns", "model", "prompt", "request_context", "tools", "trace"]
/profiles/runner_no_mcp/lifecycle/app/app_kwargs: ["checkpointer", "max_tool_turns", "model", "prompt", "request_context", "tools", "trace"] -> ["checkpointer", "exposure", "max_tool_turns", "model", "prompt", "request_context", "tools", "trace"]
/profiles/session_cli_default/lifecycle/app/app_kwargs: ["cache_service", "checkpointer", "model", "prompt", "request_context", "tools", "trace"] -> ["cache_service", "checkpointer", "exposure", "model", "prompt", "request_context", "tools", "trace"]
/profiles/session_drawing_arg/lifecycle/app/app_kwargs: ["cache_service", "checkpointer", "model", "prompt", "request_context", "tools", "trace"] -> ["cache_service", "checkpointer", "exposure", "model", "prompt", "request_context", "tools", "trace"]
/profiles/session_drawing_env/lifecycle/app/app_kwargs: ["cache_service", "checkpointer", "model", "prompt", "request_context", "tools", "trace"] -> ["cache_service", "checkpointer", "exposure", "model", "prompt", "request_context", "tools", "trace"]
/profiles/session_mcp_confirm_skip/lifecycle/app/app_kwargs: ["cache_service", "checkpointer", "model", "prompt", "request_context", "tools", "trace"] -> ["cache_service", "checkpointer", "exposure", "model", "prompt", "request_context", "tools", "trace"]
/profiles/session_mcp_fake/lifecycle/app/app_kwargs: ["cache_service", "checkpointer", "model", "prompt", "request_context", "tools", "trace"] -> ["cache_service", "checkpointer", "exposure", "model", "prompt", "request_context", "tools", "trace"]
/profiles/session_mock_default/lifecycle/app/app_kwargs: ["cache_service", "checkpointer", "model", "prompt", "request_context", "tools", "trace"] -> ["cache_service", "checkpointer", "exposure", "model", "prompt", "request_context", "tools", "trace"]
/profiles/session_web_member/lifecycle/app/app_kwargs: ["cache_service", "checkpointer", "model", "prompt", "request_context", "tools", "trace"] -> ["cache_service", "checkpointer", "exposure", "model", "prompt", "request_context", "tools", "trace"]
```

### 3.2 Step 5（`a4adeee`，surface 有意变更，两个版本号递进）

```
/profiles/runner_drawing_env/surface/exposure: "<absent>" -> {"groups": [["clauses", "core", ["retrieve_clauses", "cite_clauses"]], ["hydraulics", "core", ["calc_velocity"]], ["m...
/profiles/runner_mcp_fake/surface/exposure: "<absent>" -> {"groups": [["clauses", "core", ["retrieve_clauses", "cite_clauses"]], ["hydraulics", "core", ["calc_velocity"]], ["m...
/profiles/runner_no_mcp/surface/exposure: "<absent>" -> {"groups": [["clauses", "core", ["retrieve_clauses", "cite_clauses"]], ["hydraulics", "core", ["calc_velocity"]], ["m...
/profiles/session_cli_default/surface/exposure: "<absent>" -> {"groups": [["clauses", "core", ["retrieve_clauses", "cite_clauses"]], ["hydraulics", "core", ["calc_velocity"]], ["m...
/profiles/session_drawing_arg/surface/exposure: "<absent>" -> {"groups": [["clauses", "core", ["retrieve_clauses", "cite_clauses"]], ["hydraulics", "core", ["calc_velocity"]], ["m...
/profiles/session_drawing_env/surface/exposure: "<absent>" -> {"groups": [["clauses", "core", ["retrieve_clauses", "cite_clauses"]], ["hydraulics", "core", ["calc_velocity"]], ["m...
/profiles/session_mcp_confirm_skip/surface/exposure: "<absent>" -> {"groups": [["clauses", "core", ["retrieve_clauses", "cite_clauses"]], ["hydraulics", "core", ["calc_velocity"]], ["m...
/profiles/session_mcp_fake/surface/exposure: "<absent>" -> {"groups": [["clauses", "core", ["retrieve_clauses", "cite_clauses"]], ["hydraulics", "core", ["calc_velocity"]], ["m...
/profiles/session_mock_default/surface/exposure: "<absent>" -> {"groups": [["clauses", "core", ["retrieve_clauses", "cite_clauses"]], ["hydraulics", "core", ["calc_velocity"]], ["m...
/profiles/session_web_member/surface/exposure: "<absent>" -> {"groups": [["clauses", "core", ["retrieve_clauses", "cite_clauses"]], ["hydraulics", "core", ["calc_velocity"]], ["m...
/versions/PROMPT_TEMPLATE_VERSION: "p5-history-window-2026-10-01" -> "p7-s4-intent-tool-guide-2026-10-06"
/versions/TOOL_POLICY_VERSION: "p7-step1-2026-10-03" -> "p7-step4-2026-10-06"
```

- 装配抛错的 5 个 profile（配置损坏、确认门 ready、注册故障）surface 为 `null`，不出现 `exposure`；
- 注册顺序、发给模型的工具集合（ToolNode）、每个工具的 schema、回退格式的 `tool_guide`、系统提示摘要、主体能力、MCP 暴露 / 跳过、异常、行为探针**全部不变**（范围块 `frozen_json` 在 PR 上核对）。

---

## 4. 不做

- 不新增 / 删除 / 改写任何工具的描述与 schema；不改 `ToolRegistry`、护栏、`mcp/**`、Web、CLI。
- 不做模型路由、embedding / 语义相似度路由。
- 不给 MCP server 加关键词配置（S6 面板一并设计）；MCP 工具只按 ambient 处理。
- 不改 `FORMULA_SPECS` 等静态段。

## 5. 残余风险

- **真实模型的工具选择准确率未验证（P7-S4-A13，blocked）**：`tool_exposure_p0` 只证明"该开的工具开了、写进了指引、没超预算"。真实模型拿到裁剪后的工具集会不会更少误用、会不会在关键词漏判时说"我没有这个能力"，都要有 Key 才能测。
- **合成用例与规则同一作者**：数据集先于规则冻结，36 条里有"不带明显触发词"的改写（"要买多少米""看一下 P-01""从 P-01 的管线算出总长度"），但召回 1.0 只说明这些问法被覆盖，不说明任意用户表达都能命中。漏判时的兜底是：指引里的"本轮未开放"行列出工具名、模型照名字调用时照常执行（A06）。
- **关键词误开**：`统计`、`清单`、`合计` 等词在规范问答里也会出现，会多开材料统计（只多花 schema，不影响正确性）；图号正则 `[a-z]{1,4}-\d{1,3}` 在图纸目录可用时也会被"项目 A-12"这类写法触发。`overexposure_rate` 0.125 进基线对比作观测。
- **预算余量**：本地工具全开正好 4497（§2.3）。下一个新工具若与"读图 + 材料"同时命中，会被整组跳过；届时要么接受（trace 与 `dropped` 可见），要么评审是否调整 core 或工具描述——**不得**上调 4500。
- **sticky 只回看一轮**：隔一轮再追问"刚才那个管段表"不会自动开材料统计，依赖关键词或"本轮未开放"行兜底。
- **`full` 回滚开关仍有 F4/F5**：它就是 S3 的行为，只作为退路，不作为常态配置。
