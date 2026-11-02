# P7-S5 执行 Prompt：MCP 长驻连接（stdio，Step D 第一部分）

> **工作区**：仓库 `YEKYYY0312/water-SAD-agent`，基于最新 `main`（必须已包含 P7-S4 与本 Prompt）。新建分支 `YEK/p7-s5-shared-mcp-connections`。
> **必读**：
> - `AGENTS.md`
> - `docs/design/16-p7-plugin-mcp.md`：§2 事实表（F8）、§3.4 MCP 插件的两种来源、§5 S5 约束、§6 harness、§7 的 P7-S5 验收、§8 实施协议
> - `docs/design/14-p4-mcp-gateway.md` §3（配置契约）、§4（治理绑定）
> - `docs/design/p7-step-4-decision.md`（S4 的快照基线与残余风险）
> - `src/water_agent/mcp/client.py`、`src/water_agent/mcp/wiring.py`、`src/water_agent/plugins/mcp_gateway.py`、`src/water_agent/webapp/run_manager.py`、`src/water_agent/webapp/app.py`

---

## 0. 相对规格的修订与本步决定（以本文为准）

规格 §5 的 S5 行写于 S0，原计划"长驻进程 + Streamable HTTP"一起做。2026-10-06 评审决定拆开，规格已在本 Prompt 的同一个 PR 里同步更新：

| 原计划 / 疑问 | 本步做法 | 原因 |
|---|---|---|
| S5 同时做长驻连接与 Streamable HTTP | **S5 只做 stdio 长驻连接**；Streamable HTTP 挪到新增的 **S8**（S6、S7 编号不变） | HTTP 要放开 `NETWORK_FETCH`（危险集，`Principal` 构造期即拒）并把出站清单 7 → 8，是单独的安全边界决定，不和连接层重构绑在一起评审。不叫"S5b"：范围守卫按 `YEK/p7-s<n>-` 映射执行 Prompt，带字母的步号映射不了 |
| 长驻进程被所有 Web 用户共享，跨租户状态怎么办 | server 配置新增 `lifecycle: "per_run" \| "shared"`，**缺省 `per_run`（即现状）**；只有运维显式声明 `shared` 的 server 才长驻 | stdio server 内部可能有状态（例如"当前打开的文档"），共享后 A 用户的状态会被 B 用户看到。agent 无法验证一台 server 是否无状态，只能由运维声明；声明即承担。**因此 F8 只对 `shared` 的 server 修复**，缺省行为不变 |
| 并发访问（`RunManager` 线程池 `max_workers=4`，单个 run 内 ToolNode 还会并行调用工具） | 同一个客户端上的 JSON-RPC 请求**串行**：`_request` 整体加锁（`_next_id` 递增也在锁内），等锁的时间计入本次调用的 deadline；不做管线化 | 实测（2026-10-06 读码）：`_request` 不是并发安全的——各请求共用一个 inbox，读到别人的响应时当成通知存起来继续等，真正的请求方只能超时；`_next_id += 1` 也没有锁。**这在今天就可能触发**（同一 run 里并行调用同一台 server 的两个工具），长驻共享后必然触发。串行最简单、最好验证；吞吐受限写进残余风险 |
| 配置改了以后怎么生效（重载语义） | 按**单台 server 的指纹**判断：指纹 = 该 server 的已校验配置字段 + 解析后子进程 env 的摘要（只用于比较，**不落日志、不进 trace**）。下一个 run 借连接时指纹变了就开新进程；旧进程等最后一个借用它的 run 释放后关闭。被删掉或停用的 server 同样退役 | 与规格"下一个 run 生效就是热重载"一致；把 env 摘要算进去，凭据文件轮换也会触发重启，不会出现"文件已换、进程还拿着旧 Key" |
| 长驻进程崩溃了怎么办 | 每次借用连接前做健康检查（进程仍在、握手完成、读线程未结束），死了就重开；**调用进行中**崩溃按现有语义变成工具错误 observation，不让 run 崩溃，下一个 run 重开 | 不在调用中途自动重试：重试可能让非幂等的远端操作执行两次 |
| 哪些路径用连接池 | **只有 Web**（`RunManager` 的会话工厂）。CLI 与 harness runner 保持每次现开现关（`McpGatewayPlugin.owning`） | runner 每条用例现开现关是 S2 定下的评测隔离（规格 §3.5 第 3 条），不改；CLI 一个进程一个会话，长驻没有收益 |

## 1. 目标与边界

**做**（编号对应规格 §7 的 P7-S5-Axx）：

1. **客户端并发安全（A01，先修）**：`MCPStdioClient._request` 加请求锁，整段"发请求 → 等匹配 id 的响应"在锁内，`_next_id` 递增在锁内；等锁用 `deadline` 计时，等不到即 `MCP_TIMEOUT`，消息写明是"该 server 正忙"。新增只读属性 `alive`（进程未退出、已握手、读线程未结束）。**先写失败测试**：同一个客户端上两个线程并发调用 fake server 的慢工具，修复前其中一个超时，修复后两个都拿到**自己的**结果。
2. **配置字段（A02）**：`MCPServerConfig.lifecycle: Literal["per_run", "shared"] = "per_run"`；覆盖层可以写（它不放宽任何安全约束）；递进 `MCP_CONFIG_VERSION`，入仓骨架 `data/mcp_servers.json` 的 `version` 同步；示例文件 `data/mcp_servers.example.json` 的 autocad server 加 `"lifecycle": "shared"` 并在 `14-p4-mcp-gateway.md` §3 写明"声明 shared 即声明该 server 不持有按用户区分的状态"。
3. **连接池（A03、A04、A05、A06）**：新模块 `src/water_agent/mcp/pool.py`：

   ```python
   class McpConnectionPool:
       def lease(self, config: MCPGatewayConfig | None) -> McpLease: ...
       def close(self) -> None: ...            # 关闭全部进程；之后 lease 抛 RuntimeError

   class McpLease:
       assembly: MCPGatewayAssembly            # 本次 run 用的装配（shared 借用 + per_run 现开）
       def release(self) -> None: ...          # 幂等；per_run 的进程在这里关闭
   ```

   - `lease` 对每台 server：停用 / 确认门跳过的照现有规则记 skip；`per_run` 的照现在一样 `open_bridge`，归本次租约所有；`shared` 的查池——指纹相同且 `alive` 就借用（引用计数 +1），否则开新的、旧的标记退役（引用计数归零时关闭）。
   - 返回的 `MCPGatewayAssembly` 与现在同形（`sessions` / `skipped_servers` / `manifest_hash` / `raw_config`），**`manifest_hash` 仍由 `compute_manifest_hash(config)` 计算**（缓存键语义不变）。
   - 池内部一把锁保护映射与引用计数；开进程（握手 + `tools/list`）**不在池锁内**进行，避免一台慢 server 卡住其他 run 的借用（同一台 server 并发首次打开时只开一个，其余等它）。
   - `close()` 只在 Web 应用关闭、`RunManager.close()` 排空 run 之后调用；关闭后仍有未释放的租约，按"立即终止进程"处理并在返回值里报告数量，不静默。
   - 运行时 MCP 绑定登记（`register_mcp_tool_binding`）只增不删，同一绑定重复登记幂等——重开进程后桥接出的绑定与之前相同，不会冲突；若重开后清单变了（新工具名），按现有规则登记。
4. **接线（A07）**：
   - `AgenticSession.__init__` 新增关键字参数 `mcp_lease: McpLease | None = None`：给了就用 `McpGatewayPlugin.borrowing(mcp_lease.assembly)`，并把 `mcp_lease.release` 登记到 `stack`；没给走现在的 `owning`。`mcp_enabled=False` 与 `mcp_lease` 同时给是编程错误（`ValueError`）。
   - `WebAgenticSessionFactory` 持有池（由 `create_app` 构造、放在 `app.state`），`create()` 里先 `load_mcp_gateway()`（坏配置照样在任何子进程启动前响亮失败）再 `pool.lease(config)`，交给 `build_agentic_session`；`build_agentic_session` 透传 `mcp_lease`。
   - `create_app` 的关闭钩子顺序：`run_manager.close()`（排空 run）→ `pool.close()`；排空结果与池关闭结果都写进 `app.state`，供测试断言。
   - CLI、harness runner、`tool_assembly_snapshot` 不改。
5. **可观测（A08）**：`MCPGatewayAssembly.trace_summary()` 的每台 server 增加 `connection: "per_run" | "shared_new" | "shared_reused"`；不加别的字段，不写指纹值。
6. **F8 事实与快照（A09）**：规格 §2 的 F8 改为"`shared` 的 server 跨 run 复用同一进程；缺省 `per_run` 仍每个 run 重开"，锁定测试见 §4 Step 5。快照只允许 `/versions/MCP_CONFIG_VERSION` 变化（`frozen_json` 冻结 surface 与另外两个版本号）。

**不做**：

- Streamable HTTP、任何网络传输、`NETWORK_FETCH` 能力决策、出站清单变化（S8）；
- 管线化请求、每台 server 的并发上限配置、空闲回收、崩溃退避；
- 按用户 / 租户分池（单实例单租户部署；需要时由运维把有状态的 server 保持 `per_run`）；
- Web 面板（S6）、HITL（S7）；
- 不改工具暴露（S4）、不改任何工具 schema、不改护栏与能力模型。

## 2. 铁律与治理落点（不得违反）

- **治理不因复用而减少**：每次调用仍经 `ToolRegistry.invoke`（主体授权 `MCP_INVOKE`、参数护栏、执行期确认门检查）；复用的是进程，不是授权结果。
- **失败要响亮**：坏配置在借用之前就 `MCP_CONFIG_INVALID`；池已关闭后借用抛错；关闭时还有未释放租约要报告数量。
- **不泄露凭据**：env 摘要只用于指纹比较，不进 trace、日志、快照、错误消息。
- **评测先行**：A01 的并发测试先红后绿；每条验收先写失败测试。

## 3. 必须复用的现有组件（不得另写一份）

| 需要 | 用这个 | 位置 |
|---|---|---|
| 单台 server 的连接与桥接 | `open_bridge` / `MCPServerSession` | `mcp/bridge.py` |
| 停用 / 确认门的跳过规则 | `_disabled_skips` / `_confirmation_skips` | `mcp/wiring.py` |
| 装配产物与 manifest | `MCPGatewayAssembly` / `compute_manifest_hash` | `mcp/wiring.py` |
| 子进程 env 解析 | `child_env` | `mcp/config.py` |
| 借用模式 | `McpGatewayPlugin.borrowing` | `plugins/mcp_gateway.py` |
| fake server | `tests/fixtures/fake_mcp_server.py`（新增 `--mode stateful`，见 Step 1；**不得改变 `--mode ok` 的 tools/list**） | |

## 4. 实施步骤（一步一个 commit；每个 commit 结束时快照 `--check` 必须一致，Step 2 的版本号变化在同一 commit 内 `--write --reason`）

### Step 1：客户端并发安全（A01）

- fake server 新增 `--mode stateful`：tools/list 返回 `whoami`（返回进程 pid）、`bump`（进程内计数器 +1 并返回）、`slow_echo`（睡指定秒数后原样返回参数，睡眠上限 5 秒）；
- 新文件 `tests/test_mcp_pool.py`，先写 A01 的失败测试（两个线程并发 `slow_echo`，各自参数不同，断言各拿到自己的回显），再改 `client.py`。

### Step 2：配置字段（A02）

- `lifecycle` 字段、`MCP_CONFIG_VERSION` 递进、入仓骨架与示例文件同步；快照 `--write --reason`（只有 `/versions/MCP_CONFIG_VERSION`）。

### Step 3：连接池（A03–A06）

- `mcp/pool.py` + 测试（全部用 fake server，`--mode stateful`）：
  - 复用：两次 `lease` → 同一 pid；`per_run` 的 server 两次 pid 不同；
  - 状态共享如实可见：`shared` 的 server 上，两个租约（模拟两个用户）先后 `bump`，计数连续——证明"共享即共享状态"，文档的警告是真的；
  - 重载：改 `allow` 或 env 值 → 下一次 `lease` 新 pid；旧进程在旧租约 `release` 之前仍可用、之后被关闭；
  - 删除 / 停用：配置里去掉或 `disabled: true` → 旧进程在最后一个租约释放后关闭；
  - 健康：杀掉共享进程 → 下一次 `lease` 新 pid；调用中途崩溃 → 返回结构化错误，不抛出到 run 之外；
  - 关闭：`close()` 后不留活进程；有未释放租约时报告数量；`release()` 幂等；
  - 并发：多个线程同时首次 `lease` 同一台 shared server，只 spawn 一个进程。

### Step 4：接线（A07、A08）

- `AgenticSession(mcp_lease=)`、`build_agentic_session` 透传、`WebAgenticSessionFactory` 与 `create_app` 持有并关闭池、trace 摘要的 `connection` 字段；
- Web 层测试（沿用 `tests/test_web_runs.py` 的离线夹具或新增 `tests/test_web_mcp_pool.py`）：连续两个 run 用同一台 shared fake server，pid 相同；应用关闭后无活进程；会话构造中途抛错时租约被释放。

### Step 5：收尾（A09）

- `tests/test_p7_known_figures.py` 新增 `test_shared_mcp_servers_survive_across_runs`（读池行为的直接断言，不读快照）；
- 规格 §2 F8、§3.4（补"第三种来源：池租约"）、§5 S5 行与 §7 S5 验收行翻成 delivered；
- 写 `docs/design/p7-step-5-decision.md`；`docs/design/14-p4-mcp-gateway.md` §3 补 `lifecycle` 字段；AGENTS.md 补一条。

## 5. 范围块

<!-- step-scope -->
```json
{
  "step": "P7-S5",
  "allow": [
    "src/water_agent/mcp/client.py",
    "src/water_agent/mcp/config.py",
    "src/water_agent/mcp/wiring.py",
    "src/water_agent/mcp/pool.py",
    "src/water_agent/mcp/__init__.py",
    "src/water_agent/plugins/mcp_gateway.py",
    "src/water_agent/runtime/agentic.py",
    "src/water_agent/webapp/run_manager.py",
    "src/water_agent/webapp/app.py",
    "data/mcp_servers.json",
    "data/mcp_servers.example.json",
    "data/eval/tool_assembly_main.json",
    "tests/fixtures/fake_mcp_server.py",
    "tests/test_mcp_pool.py",
    "tests/test_web_mcp_pool.py",
    "tests/test_mcp_gateway.py",
    "tests/test_mcp_behaviors.py",
    "tests/test_mcp_wiring.py",
    "tests/test_mcp_layered_config.py",
    "tests/test_web_runs.py",
    "tests/test_p7_known_figures.py",
    "tests/test_tool_assembly_snapshot.py",
    "docs/design/p7-step-5-decision.md",
    "docs/design/16-p7-plugin-mcp.md",
    "docs/design/14-p4-mcp-gateway.md",
    "docs/execution-prompts/P7-S5-shared-mcp-connections.md",
    "AGENTS.md"
  ],
  "forbid": [
    "src/water_agent/tools/**",
    "src/water_agent/mcp/bridge.py",
    "src/water_agent/mcp/schema.py",
    "src/water_agent/mcp/protocol.py",
    "src/water_agent/mcp/memory_adapter.py",
    "src/water_agent/plugins/assembly.py",
    "src/water_agent/plugins/base.py",
    "src/water_agent/plugins/builtin.py",
    "src/water_agent/plugins/exposure.py",
    "src/water_agent/graphs/**",
    "src/water_agent/prompting/**",
    "src/water_agent/runtime/governance/**",
    "src/water_agent/harness/runner.py",
    "src/water_agent/harness/spec_trace.py",
    "src/water_agent/harness/tool_exposure_eval.py",
    "src/water_agent/cli.py",
    "scripts/**",
    ".github/**",
    "web/**",
    "tests/conftest.py",
    "tests/test_p7_traceability.py",
    "tests/test_check_step_scope.py",
    "tests/test_import_guard.py",
    "tests/test_plugin_invariants.py",
    "tests/test_answer_cache.py",
    "tests/test_governance_contracts.py",
    "tests/test_tool_policy.py",
    "tests/test_tool_exposure.py",
    "tests/test_tool_exposure_eval.py",
    "data/eval/baseline.json",
    "data/eval/*.jsonl",
    "data/eval/drawing_p0_main.json",
    "data/eval/drawing_gen_p0_main.json",
    "data/eval/memory_p0_main.json",
    "data/eval/multi_agent_p0_main.json",
    "data/eval/tool_exposure_p0_main.json"
  ],
  "protect": [
    {
      "node": "tests/test_answer_cache.py::TestContextBudgetReview::test_tool_definitions_token_cost_is_bounded",
      "contains": "assert cost < 4500"
    },
    {
      "node": "tests/test_tool_policy.py::TestConfirmationGate::test_confirmation_required_tool_blocked_at_call_time",
      "contains": "assert blocked.decision.rule_id == \"tool.confirmation_required\""
    },
    {
      "node": "tests/test_mcp_wiring.py::test_ready_flag_rejected_until_confirmation_gate_exists",
      "contains": "assert exc.value.code == \"MCP_CONFIG_INVALID\""
    },
    {
      "node": "tests/test_p7_known_figures.py::test_local_tool_schema_headroom_is_three_characters",
      "contains": "assert surface[\"schema_chars_total\"] == 4497"
    }
  ],
  "frozen_json": [
    {
      "path": "data/eval/tool_assembly_main.json",
      "pointers": [
        "/versions/TOOL_POLICY_VERSION",
        "/versions/PROMPT_TEMPLATE_VERSION",
        "/profiles/*/surface"
      ]
    }
  ]
}
```

## 6. 受保护断言

- 出站清单 `provider_call_sites: 7`：本步不新增任何网络出口（HTTP 是 S8）。
- 本地工具 schema `< 4500` 与 F4 的 `4497`、治理覆盖率 `tools_guardrail_validated: 5`、S1 的两条确认门断言。

## 7. 停止条件

满足任一条就停下，写清现象后等确认：

- 任何 profile 的 `surface` 出现 diff，或 `TOOL_POLICY_VERSION` / `PROMPT_TEMPLATE_VERSION` 需要变；
- 修并发问题需要改 `protocol.py` 或 `bridge.py`（forbid），或需要改 JSON-RPC 的消息格式；
- 需要改 `runtime/governance/**`（能力、护栏、出站清单）；
- 需要改 harness runner 的每用例现开现关语义；
- 现有测试依赖"每个 Web run 都重开 MCP 进程"（例如断言 spawn 次数）；
- 门禁出现与本步无关的红灯。

## 8. 验证命令（完成前全部跑过，贴出输出）

```bash
export VENV=/Users/yaerken/Desktop/给排水agent/_venv PYTHONPATH=src
git fetch origin main
$VENV/bin/pytest -q tests/test_mcp_pool.py tests/test_web_mcp_pool.py tests/test_mcp_gateway.py tests/test_mcp_behaviors.py tests/test_mcp_wiring.py tests/test_mcp_layered_config.py tests/test_tool_plugins.py tests/test_p7_known_figures.py tests/test_tool_assembly_snapshot.py tests/test_p7_traceability.py tests/test_web_runs.py
$VENV/bin/python -m water_agent.harness.tool_assembly_snapshot --check
$VENV/bin/python scripts/check_step_scope.py docs/execution-prompts/P7-S5-shared-mcp-connections.md --base origin/main
COMPOSE_PROJECT_NAME=p7s5 VENV=$VENV bash scripts/ci_gate.sh
```

手动验收（本机有 autocad-mcp 时做，没有就在决策记录里写明未做）：覆盖层里把 autocad server 设为 `"lifecycle": "shared"`，用 Web 栈连续发两条会用到 `mcp_autocad_mcp_read_dwg` 的问题，确认两次 run 的 trace 里该 server 的 `connection` 依次为 `shared_new`、`shared_reused`。**这只证明连接复用，不构成真实 CAD 验收**（P4 Step 26 仍 blocked）。

## 9. 完成定义

- 满足规格 §8 第 5 条的全部项；
- PR 上的 `p7-scope` 与 `p5-ci` 通过；
- 决策记录贴出 A01 修复前的失败输出与修复后的通过输出、Step 2 的 `--diff` 原文。

## 10. 汇报格式

- 改了哪些文件；
- 验证命令与实际输出（失败照贴）；
- 范围守卫的输出；
- 快照差异；
- 未验证项（含手动验收是否做了）；
- 残余风险。

## 11. 常见坑（本仓库实测过）

- 运行时 MCP 绑定登记只增不删：测试里用独立的 server id，或用 `clear_mcp_tool_bindings` 清理；同一绑定重复登记是幂等的，重开进程不会冲突。
- fake server 子进程要在测试结束时确认已退出（断言 pid 不存在），否则"不留活进程"的验收只是没去看。
- 本机如果设了 `WATER_AGENT_MCP_OVERLAY`、`WATER_AGENT_MCP_CONFIG` 或 `WATER_AGENT_TOOL_EXPOSURE`，跑测试和快照前先 `unset`。
- worktree 里不设 `PYTHONPATH=src` 时，pytest 会被 H0 拦下（exit 4）。
- 本机另一个 compose 栈在跑时，门禁的 memory_p0 备份用例会误报，用 `COMPOSE_PROJECT_NAME=<别名>` 隔离。
- 只含执行 Prompt 的文档 PR，分支名不能以 `YEK/p7-s<n>-` 开头。
