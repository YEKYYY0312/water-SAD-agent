# P7-S2 执行 Prompt：工具插件契约与单一装配入口（Step A）

> **工作区**：仓库 `YEKYYY0312/water-SAD-agent`，基于最新 `main`（必须已包含 P7-S0 与 P7-S1）。新建分支 `YEK/p7-s2-tool-plugin-assembly`。
> **必读**：
> - `AGENTS.md`
> - `docs/design/16-p7-plugin-mcp.md`：§2 事实表、§3 插件契约、§4 不变量、§6 harness、§7 的 P7-S2 验收、§8 实施协议
> - `docs/design/p7-step-1-decision.md`（S1 改过快照基线与 `TOOL_POLICY_VERSION`）

---

## 0. 相对规格的修订（以本文为准）

暂无。规格 §3 写于 S0；开工前先确认 S1 没有改动 `runtime/agentic.py` 与 `harness/runner.py` 的装配段落
（S1 的范围块禁止改它们）。有出入按 §7 停止条件处理。

## 1. 目标与边界

**做**：

1. 新包 `src/water_agent/plugins/`：
   - `base.py`：`AssemblyContext` / `PluginContribution` / `ToolPlugin` / `GRANTABLE_CAPABILITIES`；
   - `builtin.py`：`clauses` / `hydraulics` / `materials` / `drawing` 四个内置插件与 `BUILTIN_PLUGIN_ORDER`；
   - `mcp_gateway.py`：`McpGatewayPlugin.owning` / `.borrowing`；
   - `assembly.py`：`assemble_tools()` 与 `ToolAssembly`；
   - 契约与语义严格按规格 §3。
2. `AgenticSession.__init__` 改为调用 `assemble_tools()`，**零行为变化**：会话侧快照 0 diff。
3. `HarnessRunner._run_real_agentic_case` 改为调用 `assemble_tools()`，并按规格 §3.5 **统一成会话语义**
   （第 1–7 条；第 8 条保留）。
4. 结构不变量测试 `tests/test_plugin_invariants.py`（P7-I01…I07）；F11 的源码文本断言改为 AST 断言。

**不做**：

- 配置分层、每台 server 一个插件实例、entry point（S3）；
- 改任何工具的描述或 schema（`tools/**` 在 forbid 里，F4 余量只有 3）；
- 改提示词装配（`prompting/**` 在 forbid 里）；
- 修 F5 / F6 / F8 / F9 / F10（各有后续步骤）。

## 2. 零行为变化的前提（会话侧，逐条落实；每条都有快照字段守着）

- **a. 异常照常上抛**：`assemble_tools` 不捕获插件异常；`MCP_CONFIG_INVALID` 仍由调用方的
  `load_mcp_gateway()` 抛出（快照 `session_mcp_config_invalid`）。
- **b. 关闭顺序不变**：`McpGatewayPlugin.owning` 打开后**立即** `stack.callback(assembly.close)`。
  - `config=None` 时也照样调用 `open_gateway_sessions(None)` 并登记关闭；
  - 装配仍发生在 `build_cli_checkpointer` 之前；
  - 快照 `lifecycle.stack` 必须仍是 `["callback:MCPGatewayAssembly.close", "callback:Connection.close"]`；
  - 登记时直接传绑定方法，不要包一层 lambda，否则限定名会变。
- **c. 用授权前的上下文建检索**：retriever / reranker 仍用授权前的 `request_context` 构建。
- **d. 公开属性的类型与身份不变**：
  - `self.registry` 必须是工具与 `PromptAssembler` 持有的同一个对象，`tests/test_agentic_runtime_robustness.py`
    会 patch 它；
  - `mcp_assembly` 恒为 `MCPGatewayAssembly`；
  - `mcp_tool_names` 是 `list[str]`；
  - `drawing_root_status` 是 `DrawingRootResult`；
  - `request_context` 用 `model_copy(update={"principal": assembly.principal})` 更新。
- **e. 授权条件不变**：只在 MCP 确实暴露了工具时才授予 `MCP_INVOKE`（快照 `session_mcp_confirm_skip`）。
- **f. `drawing_root` 原样透传**：绝不把 `""` 和 `None` 互相转换（快照 `session_mock_default` 与
  `session_drawing_env`）。

## 3. runner 统一成会话语义（规格 §3.5）

| # | 现在 | 改成 | 快照里会看到 |
|---|---|---|---|
| 1 | cite 不接 `context_source` | 接一个"读本用例 thread checkpoint"的函数；app 尚未构造时返回空集合 | 探针 `cite` 结果不变 |
| 2 | 图纸根只读环境变量 | `AssemblyContext(drawing_root=None)` | 不变 |
| 3 | 模块级 `_MCP_ASSEMBLY_CACHE` | 每条用例 `McpGatewayPlugin.owning(load_mcp_gateway(), stack=…)`，用例结束即关闭；删除缓存和 `_get_or_create_mcp_assembly` / `_cleanup_mcp_assembly_cache` | `runner_mcp_fake.lifecycle.mcp_clients_alive_after_scope` 由 1 变 0 |
| 4 | 注册出错时降级为纯本地工具 | 不捕获，异常上抛（`run_bundle` 按用例隔离） | `runner_mcp_register_fault`：surface 变为 null、出现 exception |
| 5 | `mcp_assembly` / `mcp_assembly_error` 两个从未落盘的事件 | 删除 | `trace_record_calls` 变为 `[]` |
| 6 | chat model 拿到授权前的主体 | 装配后把授权后的 `request_context` 赋给 chat model | `runner_mcp_fake.lifecycle.app.chat_model_principal` 多出 `mcp_invoke` |
| 7 | `model_copy` 更新主体 | 由 `assemble_tools` 用构造器叠加 | 主体内容不变 |
| 8 | 不接缓存服务、传 `max_tool_turns` | **保留** | 不变 |

runner 侧 surface（除 `runner_mcp_register_fault`）与 `runner_mcp_config_invalid` 整体都被 `frozen_json` 冻结。

## 4. 实施步骤（一步一个 commit）

### Step 1：插件包 + 单元测试

- 先写 `tests/test_tool_plugins.py` 里规格 P7-S2-A01…A05 的测试，docstring 写验收 ID；再实现。
- `plugins/` 里不得出现 `ToolSpec(` 调用（P7-I05）：插件只调用 `tools/` 里现成的构建函数。

### Step 2：会话路径收编

- 改 `runtime/agentic.py`，跑 `python -m water_agent.harness.tool_assembly_snapshot --check`，**必须 0 diff**。
- 把 `tests/test_read_drawing_tool.py::TestWiring::test_cli_registers_tool_only_when_root_available`
  改为 AST 断言，docstring 写 P7-S2-A09：
  - 断言 `drawing` 插件调用 `maybe_build_read_drawing_tool`；
  - 断言 `runtime/agentic.py` 与 `harness/runner.py` 都调用 `assemble_tools`。

### Step 3：runner 路径收编与统一

- 改 `harness/runner.py`；补 P7-S2-A07 的四个测试。
- 跑 `--diff`：只允许出现 §3 表里列出的变化。确认后 `--write --reason "P7-S2：runner 统一成会话语义"`。
  surface 没变，不需要递进版本号。
- 同步改 `tests/test_p7_known_figures.py::test_runner_mcp_trace_events_never_land` 与
  `::test_runner_path_differs_from_session_path`，以及规格 §2 的 F2、F13：
  - 断言统一后的新事实；
  - 或改名为描述新事实的测试，同时更新规格 §7 S0 行的验证项。

### Step 4：结构不变量

- `tests/test_plugin_invariants.py::test_invariants_hold` 用 AST 判定 P7-I01…I07：
  - 白名单条目不再对应真实代码，同样判失败；
  - docstring 里出现 `ToolRegistry(` 不算。
- `tests/test_plugin_invariants.py::test_session_assembly_matches_baseline`（P7-S2-A06）：采集会话侧 profile，
  与基线对比。

### Step 5：收尾

- 规格 §7 的 P7-S2 行翻成 `delivered`（A11 维持 `untested`，见 §8）；
- 规格 §4 不变量状态改成 `delivered`；
- 写 `docs/design/p7-step-2-decision.md`；
- AGENTS.md 补一条，并把 `mcp/wiring.py` 文档串里"不存在第二条装配路径"的说法核对为事实。

## 5. 范围块

<!-- step-scope -->
```json
{
  "step": "P7-S2",
  "allow": [
    "src/water_agent/plugins/**",
    "src/water_agent/runtime/agentic.py",
    "src/water_agent/harness/runner.py",
    "tests/test_tool_plugins.py",
    "tests/test_plugin_invariants.py",
    "tests/test_read_drawing_tool.py",
    "tests/test_p7_known_figures.py",
    "data/eval/tool_assembly_main.json",
    "docs/design/p7-step-2-decision.md",
    "docs/design/16-p7-plugin-mcp.md",
    "docs/execution-prompts/P7-S2-tool-plugin-assembly.md",
    "AGENTS.md"
  ],
  "forbid": [
    "src/water_agent/tools/**",
    "src/water_agent/prompting/**",
    "src/water_agent/runtime/governance/**",
    "src/water_agent/mcp/**",
    "src/water_agent/graphs/**",
    "src/water_agent/webapp/**",
    "src/water_agent/cli.py",
    "src/water_agent/harness/tool_assembly_snapshot.py",
    "src/water_agent/harness/spec_trace.py",
    "src/water_agent/harness/local_model_compat.py",
    "src/water_agent/harness/memory_eval.py",
    "scripts/**",
    ".github/**",
    "tests/conftest.py",
    "tests/test_tool_assembly_snapshot.py",
    "tests/test_p7_traceability.py",
    "tests/test_check_step_scope.py",
    "tests/test_import_guard.py",
    "tests/test_answer_cache.py",
    "tests/test_governance_contracts.py",
    "tests/test_web_runs.py",
    "data/eval/baseline.json",
    "data/eval/*_p0_main.json",
    "data/eval/*.jsonl"
  ],
  "protect": [
    {
      "node": "tests/test_answer_cache.py::TestContextBudgetReview::test_tool_definitions_token_cost_is_bounded",
      "contains": "assert cost < 4500"
    },
    {
      "node": "tests/test_answer_cache.py::TestContextBudgetReview::test_system_prompt_within_segment_budget",
      "contains": "assert used < 1950"
    }
  ],
  "frozen_json": [
    {
      "path": "data/eval/tool_assembly_main.json",
      "pointers": [
        "/versions",
        "/profiles/session_*",
        "/profiles/runner_no_mcp/surface",
        "/profiles/runner_drawing_env/surface",
        "/profiles/runner_mcp_fake/surface",
        "/profiles/runner_mcp_config_invalid"
      ]
    }
  ]
}
```

## 6. 受保护断言

同 S1：schema `< 4500`、系统提示 `< 1950`、治理覆盖率 `{7, 7, 5, 5}`。本步不增减任何工具。

## 7. 停止条件

满足任一条就停下，写清现象后等确认：

- 会话侧快照出现任何 diff，或任何 profile 的 surface 出现 diff（`runner_mcp_register_fault` 由"有"变"无"除外）；
- runner 侧出现 §3 表以外的 lifecycle 变化；
- 需要改 `forbid` 里的文件、`allow` 之外的文件，或受保护断言；
- 规格 §3 的契约与代码现实冲突（例如某个构建函数的签名与规格假设不同）；
- 门禁出现与本步无关的红灯。

## 8. 验证命令（完成前全部跑过，贴出输出）

```bash
export VENV=/Users/yaerken/Desktop/给排水agent/_venv PYTHONPATH=src
git fetch origin main
$VENV/bin/pytest -q tests/test_tool_plugins.py tests/test_plugin_invariants.py tests/test_read_drawing_tool.py tests/test_agentic_runtime_robustness.py tests/test_p7_known_figures.py tests/test_tool_assembly_snapshot.py tests/test_p7_traceability.py
$VENV/bin/python -m water_agent.harness.tool_assembly_snapshot --check
$VENV/bin/python -m water_agent.harness.runner data/eval/agentic_p0.jsonl          # 期望 21/21
$VENV/bin/python -m water_agent.cli --mode agentic --mock-generator --json "DN100 管道流量 10 L/s 的流速是多少"
$VENV/bin/python scripts/check_step_scope.py docs/execution-prompts/P7-S2-tool-plugin-assembly.md --base origin/main
COMPOSE_PROJECT_NAME=p7s2 VENV=$VENV bash scripts/ci_gate.sh
```

真实质量层（P7-S2-A11）：

- 本机有 `DEEPSEEK_API_KEY`（及检索需要的 `DASHSCOPE_API_KEY`）时运行：
  `$VENV/bin/python -m water_agent.harness.runner data/eval/rag_p1.jsonl --variant real --judge`
  与 55/58（机制）、49/58（质量）对比，结果写进决策记录。
- 没有 Key 就写"blocked：无 Key"，**不得**声称真实质量未回退。

## 9. 完成定义

- 规格 §8 第 5 条的全部项；
- PR 上的 `p7-scope` 与 `p5-ci` 通过；
- 决策记录 §3 贴出 `--diff` 原文。

## 10. 汇报格式

- 改了哪些文件；
- 验证命令与实际输出（失败照贴）；
- 范围守卫的输出；
- 快照差异；
- 未验证项（含 A11 的状态）；
- 残余风险。

## 11. 常见坑（本仓库实测过）

- `ToolRegistry.list_tools()` 是**注册顺序**（决定提示词工具指引，800 字预算会截断），`list_all_names()` 是**字母序**
  （发给模型）。两者都被快照钉住，插件顺序就是注册顺序。
- inventory 静态扫描只认 `tools/` 下 `ToolSpec(\n name="…"` 的写法：把 `ToolSpec` 构造搬出 `tools/`，或者
  改了写法，`tests/test_governance_contracts.py` 会红。插件只调用现成的构建函数。
- 运行时 MCP 绑定登记只增不删、同名不同配置会冲突：测试里用不会冲突的 server id，或用 `clear_mcp_tool_bindings` 清理。
- `tool_spec_to_base_tool` 返回的工具持有 registry 引用；先建 registry、再全部注册、最后统一转换。
- worktree 里不设 `PYTHONPATH=src` 时，pytest 会被 H0 拦下（exit 4）。
