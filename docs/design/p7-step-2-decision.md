# P7-S2 决策记录：工具插件契约与单一装配入口（Step A）

**日期**：2026-10-03
**状态**：已完成
**规格来源**：`docs/design/16-p7-plugin-mcp.md` §3、§4、§7 P7-S2；执行 Prompt `docs/execution-prompts/P7-S2-tool-plugin-assembly.md`
**基线**：main `1268791`（P7-S1 合入后）

---

## 0. 偏离与修订

### 0.1 执行 Prompt §0 的修订

执行 Prompt §0 为"暂无"，照做。开工前确认过：S1 没有改动 `runtime/agentic.py` 与 `harness/runner.py` 的装配段落。

### 0.2 实测后追加的偏离

| 执行 Prompt / 规格原文 | 本步做法 | 原因 |
|---|---|---|
| runner 第 1 条："app 尚未构造时返回空集合" | 照做，并在**同一个** `MemorySaver` 上读本用例 thread 的 checkpoint | runner 原来在 `with MemorySaver()` 里才构图；装配与构图合进同一个 `with ExitStack() as stack, MemorySaver() as saver:`，MCP 的关闭登记在这个 stack 上，用例结束（含异常）即收掉子进程 |
| （未提及）runner 的延迟口径 | `started` 仍从**构图**开始计时，不含工具装配与 MCP 子进程启动 | 改为每条用例现开 MCP 后，若从装配前计时，`latency_ms` 会多出子进程启动时间，与历史报告不可比 |
| 规格 §7 只列 A01–A11 | 新增 **P7-S2-A12**（F13：装配期不再有从未落盘的 trace 调用） | runner 第 5 条删除两次 trace 调用是可观察行为，需要独立的验收行与测试 |
| P7-S0-A16 / A17 指向锁定 S0 现状的测试 | 两条测试改写为断言统一后的事实（`test_no_assembly_trace_calls_that_never_land`、`test_runner_path_matches_session_semantics`），A16/A17 改为 `untested` 并在"未覆盖项"说明 | 与 S1 处理 P7-S0-A15 的方式一致：现状已不存在，保留行作为历史记录 |
| F11 两条源码文本断言 | 只改 `tests/test_read_drawing_tool.py`（AST 判定真实调用）；`tests/test_web_runs.py` 保留 | 后者断言的是 Web 走 `build_agentic_session`，属于 Web 装配面，不在 P7 范围，且 S2 的范围块禁止改它 |
| `mcp/wiring.py` 文档串"不存在第二条装配路径" | 未改（`mcp/**` 在范围块 forbid 里） | S2 之后这句话已成为事实（两条路径都经 `assemble_tools`）；措辞更新留给 S3（配置分层时本来就要改 wiring） |

---

## 1. 交付

| commit | 产物 | 要点 |
|---|---|---|
| 1 | `src/water_agent/plugins/`：`base.py`（契约）、`builtin.py`（四个内置插件 + `BUILTIN_PLUGIN_ORDER`）、`mcp_gateway.py`（`owning` / `borrowing`）、`assembly.py`（`assemble_tools` / `ToolAssembly`）；`tests/test_tool_plugins.py` 的 A01–A05 | 两阶段装配：先收集贡献，再用最终主体建**一个** `ToolRegistry`；授权用 `Principal(...)` 构造器（重跑校验），没有新能力时原样返回同一个主体对象 |
| 2 | `runtime/agentic.py` 改调 `assemble_tools` | 会话侧快照 **0 diff**（10 个 profile 逐字段不变） |
| 3 | `harness/runner.py` 改调 `assemble_tools` 并统一成会话语义；删除 `_MCP_ASSEMBLY_CACHE` 及其两个函数；A07 的四个测试；`test_p7_known_figures.py` 两条改写；快照重写 | 只有 runner 侧 lifecycle 变化与 `runner_mcp_register_fault` 的 surface 由"有"变"无"（见 §3） |
| 4 | `tests/test_plugin_invariants.py`（P7-I01…I07 + A06）；`tests/test_read_drawing_tool.py` 的 F11 改为 AST 判定（A09） | 全部按 AST 判定真实调用；白名单条目不再对应真实调用同样判失败 |
| 5 | 规格状态、本决策记录、AGENTS.md | — |

---

## 2. 实测

- **会话路径零行为变化**：改 `runtime/agentic.py` 后 `python -m water_agent.harness.tool_assembly_snapshot --diff` 输出"装配快照与基线一致（15 个 profile）"。
- **runner 路径**：`--diff` 只出现 §3 列出的 11 行，全部对应规格 §3.5 第 3–6 条。
- **反证**：
  - 把 `ClausesPlugin` 的 `context_source` 改成 `None`，`test_clauses_plugin_forwards_context_source` 变红，还原后变绿；
  - 在 `cli.py` 末尾临时加一个 `ToolRegistry()` 构造，`test_invariants_hold[P7-I01]` 报"在装配入口之外构造了 ToolRegistry：['cli.py']"，还原后 `git diff` 为空。
  - runner 的 cite thread 来源：最初写的端到端测试（脚本模型先检索再引用）在把 `cite_context_source` 改成 `None` 后**照样通过**——单轮用例里进程内累积与 checkpoint 两个来源结果相同，测不出差别。改为直接拿 runner 交给 clauses 插件的来源函数、用 runner 自己构的图往本用例 thread 写入检索结果再读；之后两种变异（来源传 `None`、图构好后不登记给来源函数）都会让它变红。
- `tests/test_tool_plugins.py` 16 passed；`tests/test_plugin_invariants.py` + `tests/test_read_drawing_tool.py` 55 passed；`tests/test_p7_traceability.py` 4 passed；runner 相关的 `tests/test_ci_gate.py`、`tests/test_harness.py`、`tests/test_docgen_quality.py` 全部通过。
- `ruff check src` 与 `mypy src/water_agent` 无问题。
- 范围守卫：`python scripts/check_step_scope.py docs/execution-prompts/P7-S2-tool-plugin-assembly.md --base origin/main` 通过（15 个文件，冻结的会话侧 profile、`/versions` 与 runner 侧 surface 未变）。
- CLI 冒烟：`python -m water_agent.cli --mode agentic --mock-generator --json "DN100 管道流量 10 L/s 的流速是多少"` → `status=succeeded`，引用 `FIX 1.2.1`，stderr 为空。
- 全量门禁：`COMPOSE_PROJECT_NAME=p7s2 bash scripts/ci_gate.sh`（worktree 内、不预设 PYTHONPATH）**EXIT=0**；日志首行 `water_agent 导入自：<p7-s2 worktree>/src/water_agent`，agentic_p0 21/21、governance_p0 32/32，各基线对比通过。
- **真实质量层（P7-S2-A11）**：本机没有可用的 `DEEPSEEK_API_KEY`，**blocked**，没有重跑 `rag_p1 --variant real --judge`，**不声称真实质量未回退**。runner 改动对真实路径的影响面：cite 多了一个读 checkpoint 的可追溯来源（只会让原本被拒的跨轮引用通过，单轮用例结果不变）；chat model 现在拿到含 `MCP_INVOKE` 的主体（只影响 provider 调用记账里的主体能力集，不影响模型输入）；MCP 每条用例现开（有配置时每条用例多一次子进程启动，不计入 `latency_ms`）。

---

## 3. 快照变化（`--diff` 原文）

```
/profiles/runner_drawing_env/lifecycle/trace_record_calls: [{"event": "mcp_assembly", "landed": false, "node": "mcp"}] -> []
/profiles/runner_mcp_fake/lifecycle/app/chat_model_principal/capabilities: ["calculate", "cite_clauses", "embedding", "export_to_allowed_dir", "llm_inference", "material_stats", "read_drawing"... -> ["calculate", "cite_clauses", "embedding", "export_to_allowed_dir", "llm_inference", "material_stats", "mcp_invoke", ...
/profiles/runner_mcp_fake/lifecycle/mcp_clients_alive_after_scope: 1 -> 0
/profiles/runner_mcp_fake/lifecycle/trace_record_calls: [{"event": "mcp_assembly", "landed": false, "node": "mcp"}] -> []
/profiles/runner_mcp_register_fault/lifecycle/app: {"app_kwargs": ["checkpointer", "max_tool_turns", "model", "prompt", "request_context", "tools", "trace"], "cache_ser... -> null
/profiles/runner_mcp_register_fault/lifecycle/captures: 1 -> 0
/profiles/runner_mcp_register_fault/lifecycle/exception: null -> {"code": null, "type": "ValueError"}
/profiles/runner_mcp_register_fault/lifecycle/mcp_clients_alive_after_scope: 1 -> 0
/profiles/runner_mcp_register_fault/lifecycle/trace_record_calls: [{"event": "mcp_assembly", "landed": false, "node": "mcp"}, {"event": "mcp_assembly_error", "landed": false, "node": ... -> []
/profiles/runner_mcp_register_fault/surface: {"model_tool_order": ["calc_velocity", "cite_clauses", "material_stats", "retrieve_clauses"], "principal": {"auth_dom... -> null
/profiles/runner_no_mcp/lifecycle/trace_record_calls: [{"event": "mcp_assembly", "landed": false, "node": "mcp"}] -> []
```

| 变化 | 对应规格 §3.5 |
|---|---|
| 各 runner profile 的 `trace_record_calls` 变为 `[]` | 第 5 条：删除两次从未落盘的 trace 调用 |
| `runner_mcp_fake` 作用域结束后存活子进程 1 → 0 | 第 3 条：每条用例现开现关 |
| `runner_mcp_fake` 的 chat model 主体多出 `mcp_invoke` | 第 6 条：chat model 拿到授权后的主体 |
| `runner_mcp_register_fault` 从"降级为纯本地工具"变为 `ValueError`，surface 为 null，子进程 1 → 0 | 第 4 条：响亮失败 |

会话侧 10 个 profile、`/versions`、其余 runner profile 的 surface 逐字段不变（范围块 `frozen_json` 冻结，PR 上的 `p7-scope` 核对）。没有任何 surface 的值变化，故不递进版本号。`--write --reason "P7-S2：runner real-agentic 路径收编进 assemble_tools 并统一成会话语义（规格 §3.5 第 1–7 条）"` 写入。

---

## 4. 不做

- 配置分层、每台 server 一个插件实例、`disabled`、entry point 发现（S3）。
- 修 F5（工具指引截断）/ F6（manifest 不进缓存键）/ F8（每个 run 重新 spawn）/ F9 / F10。
- runner 接回答缓存（§3.5 第 8 条，刻意保留）。
- 不改任何工具的描述与 schema（本地工具 schema 余量仍只有 3）。

## 5. 残余风险

- runner 现在每条用例都会按配置拉起 MCP 子进程。入仓 `data/mcp_servers.json` 指向本机 `autocad-mcp`：真实评测（`--variant real`）跑 58 条时会启动 58 次。此前的模块级缓存只启动一次。这是统一语义的直接代价（与 Web 每个 run 同构）；长驻连接是 S5 的范围。
- 插件化不改变"桥接是 MCP 运行时绑定的唯一登记入口"这一前提（S1 的执行期检查依赖它）；S3 引入外部插件时必须保持。
- 真实质量层未重跑（见 §2），属于 blocked，不是通过。
