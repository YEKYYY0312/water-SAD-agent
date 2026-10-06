# P7-S7 执行 Prompt：危险操作人工确认门（HITL，Step E2）

> **工作区**：仓库 `YEKYYY0312/water-SAD-agent`，基于最新 `main`（必须已包含 P7-S5 与本 Prompt）。新建分支 `YEK/p7-s7-hitl-confirmation`。
> **必读**：
> - `AGENTS.md`
> - `docs/design/16-p7-plugin-mcp.md`：§2 事实表（F7）、§3 插件契约与 §3.4 MCP 来源、§3.6 按意图暴露、§5 S7 约束、§7 的 P7-S7 验收、§8 实施协议
> - `docs/design/14-p4-mcp-gateway.md` §3（配置契约，`human_confirmation` / `confirmation_handler_ready`）、§4（治理绑定）
> - `docs/design/p7-step-1-decision.md`（S1：执行期确认门强制）、`docs/design/p7-step-5-decision.md`（S5：连接池）
> - 先例：`src/water_agent/graphs/docgen.py`（审批 interrupt、非法 resume 值不毒化会话）、`src/water_agent/multi_agent/orchestrator.py`（外层图审批 interrupt）

---

## 0. 相对规格的修订与本步决定（以本文为准）

规格 §5 的 S7 约束只写了"确认门必须放在外层图"。下面是 2026-10-06 读码后定下的做法，规格已在本 Prompt 的同一个 PR 里同步：

| 疑问 | 本步做法 | 原因 |
|---|---|---|
| 内层 react agent 是 `checkpointer=False`，怎么在"要调用需确认的工具"处停下 | 需确认的工具在 ToolNode 里替换成**延迟工具**：同名、同 schema、`return_direct=True`，执行时**不调用任何东西**，只回一条带 `confirmation_id` 的"待确认" ToolMessage。`create_react_agent` 执行到 `return_direct` 工具后直接结束内层循环、不再调模型 | 不需要内层 checkpointer，也不在工具执行中途 interrupt（那样恢复时外层节点会从头重跑、模型会被再调一次）。实测本机 langgraph 支持 `return_direct`（`chat_agent_executor.py` 的 `should_return_direct`） |
| 停下之后谁来确认 | 外层图新增 `confirmation_gate` 节点：`agent_loop` 之后，本轮有待确认 ToolMessage 时路由到它；它 `interrupt(request)` 挂起；恢复后**批准**→ 经 `ToolRegistry` 执行一次、用真实结果**同 id 替换**那条待确认消息、回 `agent_loop`；**拒绝**→ 同 id 替换为拒绝 observation、回 `agent_loop`（模型据此向用户说明） | 规格 §5 的约束；先例是 docgen 与多 Agent 的外层审批 interrupt。模型看到的始终是"一次调用、一个结果"，不会看到占位消息 |
| 执行期护栏（S1）怎么放行 | S1 的拒绝**保持不变**：没有凭证一律 `tool.confirmation_required`。确认门批准后签发一次性 `ConfirmationGrant`（`confirmation_id` + 工具名 + 参数摘要 + 主体 hash），经新方法 `ToolRegistry.invoke_confirmed(name, args, grant=)` 交给护栏；护栏核对三项一致且未用过才放行，用过即 `tool.confirmation_replayed` | 模型只能产出 JSON，传不出 Python 对象，凭证伪造不了；参数摘要绑定使"批准的是 A 参数、执行的是 B 参数"不可能发生。不给 `invoke` 加关键字参数：它的 `**args` 是工具参数，会撞名 |
| `confirmation_handler_ready` 配置键 | **继续在加载期拒绝**（S1 的受保护断言不变），只改报错文案：确认门是否可用由**入口**决定，不由配置声明 | 配置写 `true` 不能让一个没有确认 UI 的入口（CLI、评测 runner）凭空"可确认"。只有 Web 会话工厂接了确认门：Web 借连接时 `confirmation_ready=True`，`human_confirmation=true` 的 server 才暴露；CLI 与 runner 维持现状（整台跳过、`MCP_CONFIRMATION_UNAVAILABLE`） |
| Web run 怎么表示"等确认" | 新增 run 状态 `waiting_confirmation`（状态在 Python 里校验，`runs.status` 无数据库约束，**不需要迁移**）；SSE 新事件 `confirmation_required`（载荷：`confirmation_id`、server、工具名、参数、说明）；决定接口 `POST /api/runs/{run_id}/confirmation`，恢复执行用**同一个 run**（`waiting_confirmation → queued → running → 终态`） | 一个问题对应一个 run，前端不用拼接两个 run；挂起状态在 checkpoint 里（PostgreSQL），进程重启后照样能恢复 |
| 谁能批准 | **该 run 的所有者**，且对会话所在项目有写权限（与发消息同一套检查：归属 → 项目写权限），已改密、CSRF；请求体的 `confirmation_id` 必须等于当前挂起的那一个，否则 409（防止重复提交与过期页面） | 确认的是"我的 agent 要替我做这件事"；管理员代批、多人会签不在本步 |
| 挂起期间用户又发消息 | 409 `WEB_CONFIRMATION_PENDING`，不建 run 行；判定读 checkpoint 的挂起状态，不读进程内状态 | 否则新问题会叠在挂起的轮次上，checkpoint 里出现没有应答的 tool_call；按进程内状态判定，重启后会把会话放开 |
| 审计 | 图 state 新增 `confirmation_log`（随 checkpoint 持久化、跨轮累积，不进 `PER_TURN_RESET`）：`confirmation_id`、server、工具名、**参数摘要**、主体 hash、决定、原因（脱敏、截断 200 字）、决定时间、执行结果码；trace 写 `confirmation_requested` / `confirmation_decided` / `confirmation_executed` 三类事件（`query=""`，不写参数原文） | 与 docgen `approval_log` 同一做法；不新建审计表（不引入迁移） |
| 一轮里反复请求确认 | 每轮上限 `MAX_CONFIRMATIONS_PER_TURN = 3`，超过即本轮 `failed`（结构化错误，说明超过上限） | 每次确认都要人参与，但模型反复请求会让用户陷入点击循环 |
| 前端 | 最小确认卡片：显示 server、工具名、参数（纯文本、转义）、说明，批准 / 拒绝两个按钮与可选原因；刷新后从会话历史恢复卡片 | 没有 UI，Web 用户无法完成确认，功能等于不可用 |
| CLI | **不做**：CLI 与 runner 不接确认门 | 范围控制；CLI 的交互确认可另起一步 |

## 1. 目标与边界

**做**（编号对应规格 §7 的 P7-S7-Axx）：

1. **延迟工具（A01）**：`tools/ds_adapters.py` 新增 `tool_spec_to_deferred_tool(registry, name)`：与 `tool_spec_to_base_tool` 同名、同 `args_schema`，`return_direct=True`；执行时返回 `{"status": "pending_confirmation", "confirmation_id": <uuid>, "tool": name}`，**不调用 `ToolRegistry.invoke`、不碰 MCP**。`assemble_tools` 对运行时绑定 `requires_human_confirmation=True` 的工具用它（注册表里仍是真实 `ToolSpec`）；`ToolAssembly` 新增 `confirmation_tools: frozenset[str]`。
2. **执行凭证（A02）**：
   - `runtime/governance/contracts.py`：`ConfirmationGrant`（frozen；`confirmation_id`、`tool_name`、`args_hash`、`principal_hash`、`decided_at`）与 `confirmation_args_hash(args)`（规范化 JSON 的 sha256）。
   - `GuardrailPipeline.validate_tool_call(..., confirmation_grant=None)`：绑定要求确认时，没有凭证 → `tool.confirmation_required`（原样）；凭证三项不一致 → `tool.confirmation_mismatch`；凭证已用过 → `tool.confirmation_replayed`（进程内已用集合）。
   - `ToolRegistry.invoke_confirmed(name, args, *, grant)`：与 `invoke` 同一条执行路径（护栏 → schema → guard → 执行 → 工具事件），只多传凭证。
   - 递进 `RULESET_VERSION` 与 `TOOL_POLICY_VERSION`。
3. **外层确认门（A03）**：新模块 `graphs/confirmation.py`（节点、请求构造、resume 校验），接进 `build_agentic_app(..., confirmation=ConfirmationRuntime | None)`；`None` 时图结构不变。
   - 路由：`agent_loop` 后，本轮存在 `status=pending_confirmation` 的 ToolMessage → `confirmation_gate`，否则照旧。
   - 节点：每次处理**一条**待确认调用（同一 AI 消息里有多条时按出现顺序逐条确认）；`interrupt(request)`；resume 值只接受 `{"confirmation_id": str, "approved": bool, "reason": str | None}`（严格 bool，`confirmation_id` 必须匹配），非法值**不抛异常**、带 `error` 再 interrupt（照 `docgen.approval_decision_error`）。
   - 批准：签发凭证 → `invoke_confirmed` → 结果同 id 替换待确认 ToolMessage → `agent_loop`。拒绝：替换为 `{"ok": false, "code": "TOOL_CONFIRMATION_REJECTED", "message": "用户拒绝执行……"}` → `agent_loop`。
   - 每轮上限 3 次，超过 → `failed`。
   - `agent_loop` 再次进入时**不重置**工具轮数预算之外的本轮状态（暴露决定、`confirmation_log` 均复用）。
4. **审计（A04）**：`confirmation_log` 与三类 trace 事件（§0 表）。参数只记摘要；原因经输出护栏脱敏后截断 200 字。
5. **入口决定可用性（A05）**：
   - `pre_connection_skips(config, *, confirmation_ready=False)`、`open_gateway_sessions(config, *, confirmation_ready=False)`、`McpConnectionPool.lease(config, *, confirmation_ready=False)`；Web 会话工厂传 `True`，其余调用方不变。指纹纳入 `confirmation_ready`。
   - `AgenticSession(..., confirmation=True)` 只由 Web 会话工厂传；它让装配包含确认工具、并给 `build_agentic_app` 传 `ConfirmationRuntime`。
   - `MCPGatewayConfig` 对 `confirmation_handler_ready=true` 的拒绝保留，文案改为"确认门是否可用由入口决定（Web 已接线），删除该键"。
6. **Web（A06）**：
   - `RUN_STATUSES` 与 `RunRepository` 的允许集合加 `waiting_confirmation`；允许 `waiting_confirmation → queued` 迁移。
   - `AgenticSession.aask` 结束时若图挂起在 `confirmation_gate`，返回 `status="waiting_confirmation"` 与 `confirmation` 请求；`RunManager` 发 SSE `confirmation_required`，run 状态置 `waiting_confirmation`（不是终态：线程互斥照常释放，但发消息被下一条拦住）。草稿通道按 run 收口处理（`draft_retract(run_ended)`）。
   - `POST /api/sessions/{thread_id}/messages`：会话有挂起的确认 → 409 `WEB_CONFIRMATION_PENDING`（读 checkpoint 判定，检查顺序在"归属 → 项目写权限"之后、建 run 之前）。
   - `POST /api/runs/{run_id}/confirmation`（CSRF、已改密）：归属 404 → 项目写权限 403 → run 不在 `waiting_confirmation` 或 `confirmation_id` 不匹配 409 `WEB_CONFIRMATION_STALE` → 202，`RunManager.resume(run_id, decision)` 在同一 run 上继续执行（同一套 SSE 事件流）。
   - 会话历史投影带出挂起的确认请求（刷新后前端能恢复卡片）。
7. **前端（A07，TypeScript）**：`web/app/src` 新增 `ConfirmationCard.tsx`（React 组件）与 `confirmationApi.ts`（请求 / 响应类型 + 调用函数，错误码到文案的映射照 `uploadApi.ts` 的写法），在收到 `confirmation_required` 或历史里有挂起请求时显示；批准 / 拒绝后按 202 返回继续接收同一 run 的事件。样式只用 `styles.css` 里的现有 token，不写字面色值。测试照现有约定写在 `web/app/tests/confirmation.test.mjs`（`node --experimental-strip-types` 直接导入 `.ts` 模块），`npm run build` 的 `tsc` 类型检查必须通过。
8. **快照（A08）**：只允许 `/versions/TOOL_POLICY_VERSION` 变化（`RULESET_VERSION` 不在快照里）；surface、其余版本号、全部 lifecycle 由 `frozen_json` 冻结——快照的会话 profile 不经 Web 会话工厂，CLI 路径的 `session_mcp_confirm_skip` 仍是整台跳过。

**不做**：

- CLI / runner 的交互确认；管理员代批、多人会签、确认超时自动拒绝；
- 按工具（而非按 server）声明需确认（配置仍是 server 级 `human_confirmation`）；
- 新建审计表或数据库迁移；
- Streamable HTTP（S8）、Web 扩展面板（S6）；
- 不改工具暴露（S4）、`bridge.py` / `protocol.py` / `schema.py`、提示词。

## 2. 铁律与治理落点（不得违反）

- **没有凭证就不执行**：S1 的执行期拒绝原样保留（受保护断言）；凭证只由确认门在合法 resume 之后签发，单次有效，绑定参数摘要。
- **确认的参数就是执行的参数**：执行用 state 里记下的那份参数，不重新从模型取；摘要不一致即拒绝。
- **非法 resume 不毒化会话**：照 docgen 的做法再 interrupt，不抛异常。
- **审计不泄露**：参数原文不进 trace / 日志；主体只记 hash；原因脱敏。
- **评测先行**：每条验收先写失败测试；端到端用 fake server `--mode stateful` 的 `bump`（有副作用的计数器）证明"批准执行恰好一次、拒绝零次"。

## 3. 必须复用的现有组件（不得另写一份）

| 需要 | 用这个 | 位置 |
|---|---|---|
| 非法 resume 处理范式 | `approval_decision_error` 的思路（再 interrupt、不抛） | `graphs/docgen.py` |
| 挂起判定 | `get_state(config).interrupts`（不是只看 `next`） | 同上的说明 |
| 同 id 替换消息 | `_rewrite` / `add_messages` 按 id 替换 | `graphs/agent_rag.py` |
| 执行路径 | `ToolRegistry.invoke` 的同一段实现（`invoke_confirmed` 只多一个凭证） | `tools/registry.py` |
| 运行时绑定的确认位 | `McpToolBinding.requires_human_confirmation` | `runtime/governance/inventory.py` |
| 归属 / 项目写权限检查 | 发消息路由现有的检查函数 | `webapp/run_routes.py` |
| fake server | `tests/fixtures/fake_mcp_server.py --mode stateful`（`bump` 有副作用） | |

## 4. 实施步骤（一步一个 commit；每个 commit 结束时快照 `--check` 必须一致，Step 2 的版本号变化在同一 commit 内 `--write --reason`）

### Step 1：延迟工具（A01）

- `tool_spec_to_deferred_tool` + `assemble_tools` 的替换 + `ToolAssembly.confirmation_tools`；测试：延迟工具执行不调用注册表（用计数的假 spec 证明）、`create_react_agent` 在它之后结束内层循环。

### Step 2：执行凭证（A02）

- `ConfirmationGrant`、护栏三条规则、`invoke_confirmed`、两个版本号；新文件 `tests/test_confirmation_grant.py`：无凭证仍 `tool.confirmation_required`（与 S1 测试同一断言）、参数不一致 `mismatch`、主体不一致 `mismatch`、重放 `replayed`、正确凭证执行一次。

### Step 3：外层确认门 + 审计（A03、A04）

- `graphs/confirmation.py`、`build_agentic_app(confirmation=)`、`confirmation_log`、trace 事件；新文件 `tests/test_confirmation_gate.py`（生产 `AgenticSession(confirmation=True)` + 脚本模型 + fake server `--mode stateful`，server `human_confirmation=true`）：
  - 脚本调 `bump` → 挂起，`bump` 计数仍为 0；批准 → 计数 1、模型下一步看到真实结果；拒绝 → 计数 0、模型看到拒绝 observation；
  - 非法 resume（`"yes"`、`confirmation_id` 不匹配）→ 仍挂起、带 error，之后合法值照常生效；
  - 同一 AI 消息两条需确认调用 → 两次挂起、逐条决定；一轮第 4 次请求 → `failed`；
  - `confirmation_log` 与 trace 不含参数原文。

### Step 4：入口决定可用性（A05）

- `confirmation_ready` 参数链、指纹、`AgenticSession(confirmation=)`、配置文案；测试：Web 借连接时 `human_confirmation` 的 server 暴露、CLI 会话仍 `MCP_CONFIRMATION_UNAVAILABLE`；快照 `--check` 一致。

### Step 5：Web（A06）

- 状态、SSE、两个路由、历史投影、`RunManager.resume`；新文件 `tests/test_web_confirmation.py`（沿用 `test_web_runs.py` 的 SQLite 夹具与 `test_web_mcp_pool.py` 的工厂替身）：挂起 → 409 发消息 → 别人的 run 404 → viewer 403 → 过期 `confirmation_id` 409 → 正确决定 202 并在同一 run 上收尾；新建 `RunManager` 实例（模拟重启）后照样能决定。

### Step 6：前端（A07）

- 确认卡片 + `confirmationApi.ts` + 测试；`npm run build` 与前端测试通过。

### Step 7：收尾（A08、A09）

- `tests/test_p7_known_figures.py` 的 `test_confirmation_gate_is_enforced` 补一段：Web 入口下需确认的工具暴露为延迟工具、无凭证执行仍被拒（F7 的最终形态）；
- 规格 §2 F7、§3（新增 3.7 确认门）、§5 S7 行与 §7 S7 验收行翻成 delivered；`14-p4-mcp-gateway.md` §3 更新 `confirmation_handler_ready` 与 `human_confirmation` 的含义；
- 写 `docs/design/p7-step-7-decision.md`；AGENTS.md 补一条，并改写 P4 条目里"确认门未接线 blocked"那句。

## 5. 范围块

<!-- step-scope -->
```json
{
  "step": "P7-S7",
  "allow": [
    "src/water_agent/graphs/agent_rag.py",
    "src/water_agent/graphs/confirmation.py",
    "src/water_agent/runtime/agentic.py",
    "src/water_agent/runtime/governance/contracts.py",
    "src/water_agent/runtime/governance/pipeline.py",
    "src/water_agent/runtime/governance/rules.py",
    "src/water_agent/runtime/governance/inventory.py",
    "src/water_agent/tools/registry.py",
    "src/water_agent/tools/ds_adapters.py",
    "src/water_agent/plugins/assembly.py",
    "src/water_agent/plugins/mcp_gateway.py",
    "src/water_agent/plugins/__init__.py",
    "src/water_agent/mcp/wiring.py",
    "src/water_agent/mcp/pool.py",
    "src/water_agent/mcp/config.py",
    "src/water_agent/webapp/run_manager.py",
    "src/water_agent/webapp/run_routes.py",
    "src/water_agent/webapp/workspace_routes.py",
    "src/water_agent/storage/repository.py",
    "web/app/src/**",
    "web/app/tests/**",
    "data/eval/tool_assembly_main.json",
    "tests/fixtures/fake_mcp_server.py",
    "tests/test_confirmation_grant.py",
    "tests/test_confirmation_gate.py",
    "tests/test_web_confirmation.py",
    "tests/test_agent_rag.py",
    "tests/test_tool_plugins.py",
    "tests/test_mcp_wiring.py",
    "tests/test_mcp_pool.py",
    "tests/test_web_mcp_pool.py",
    "tests/test_web_runs.py",
    "tests/test_web_sse.py",
    "tests/test_p7_known_figures.py",
    "tests/test_tool_assembly_snapshot.py",
    "docs/design/p7-step-7-decision.md",
    "docs/design/16-p7-plugin-mcp.md",
    "docs/design/14-p4-mcp-gateway.md",
    "docs/execution-prompts/P7-S7-hitl-confirmation.md",
    "AGENTS.md"
  ],
  "forbid": [
    "src/water_agent/mcp/bridge.py",
    "src/water_agent/mcp/protocol.py",
    "src/water_agent/mcp/schema.py",
    "src/water_agent/mcp/client.py",
    "src/water_agent/plugins/exposure.py",
    "src/water_agent/plugins/builtin.py",
    "src/water_agent/prompting/**",
    "src/water_agent/graphs/docgen.py",
    "src/water_agent/multi_agent/**",
    "src/water_agent/harness/runner.py",
    "src/water_agent/harness/spec_trace.py",
    "src/water_agent/harness/tool_exposure_eval.py",
    "src/water_agent/cli.py",
    "src/water_agent/storage/models.py",
    "alembic/**",
    "alembic.ini",
    "scripts/**",
    ".github/**",
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
    "data/mcp_servers.json",
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
      "node": "tests/test_governance_contracts.py::TestInventoryCoverage::test_all_provider_sites_governed_after_step12",
      "contains": "\"provider_call_sites\": 7"
    },
    {
      "node": "tests/test_governance_contracts.py::TestInventoryCoverage::test_all_provider_sites_governed_after_step12",
      "contains": "\"tools_guardrail_validated\": 5"
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
        "/versions/PROMPT_TEMPLATE_VERSION",
        "/versions/MCP_CONFIG_VERSION",
        "/profiles/*/surface",
        "/profiles/*/lifecycle"
      ]
    }
  ]
}
```

## 6. 受保护断言

- S1 的两条：没有凭证的需确认调用仍被 `tool.confirmation_required` 拒绝；配置里 `confirmation_handler_ready=true` 仍 `MCP_CONFIG_INVALID`。S7 只**新增**一条合法放行路径（凭证），不放宽这两条。
- 出站清单 `provider_call_sites: 7`、本地工具 schema `< 4500` 与 `4497`、`tools_guardrail_validated: 5`。

## 7. 停止条件

满足任一条就停下，写清现象后等确认：

- 快照里 `TOOL_POLICY_VERSION` 之外的任何片段变化（尤其 surface 或 lifecycle）；
- 需要改 `bridge.py`、`client.py`、`exposure.py`、提示词，或需要数据库迁移；
- 延迟工具方案在本机 langgraph 上不能让内层循环在 `return_direct` 后结束（例如并行调用时 ToolNode 先执行了同一条消息里的其他工具，导致不可接受的副作用顺序）——写清实测再定；
- 需要放宽 S1 的任何一条受保护断言；
- 门禁出现与本步无关的红灯。

## 8. 验证命令（完成前全部跑过，贴出输出）

```bash
export VENV=/Users/yaerken/Desktop/给排水agent/_venv PYTHONPATH=src
git fetch origin main
$VENV/bin/pytest -q tests/test_confirmation_grant.py tests/test_confirmation_gate.py tests/test_web_confirmation.py tests/test_tool_policy.py tests/test_mcp_wiring.py tests/test_mcp_pool.py tests/test_web_mcp_pool.py tests/test_web_runs.py tests/test_web_sse.py tests/test_agent_rag.py tests/test_tool_plugins.py tests/test_p7_known_figures.py tests/test_tool_assembly_snapshot.py tests/test_p7_traceability.py
(cd web/app && npm test && npm run build)
$VENV/bin/python -m water_agent.harness.tool_assembly_snapshot --check
$VENV/bin/python scripts/check_step_scope.py docs/execution-prompts/P7-S7-hitl-confirmation.md --base origin/main
COMPOSE_PROJECT_NAME=p7s7 VENV=$VENV bash scripts/ci_gate.sh
```

手动验收（有条件时做，没有就在决策记录里写明未做）：本机 Web 栈 + fake server（`human_confirmation=true`），浏览器里发一条会触发 `bump` 的问题，看到确认卡片 → 批准 → 回答里出现计数结果；再发一次 → 拒绝 → 回答说明被拒、计数不变。

## 9. 完成定义

- 满足规格 §8 第 5 条的全部项；
- PR 上的 `p7-scope` 与 `p5-ci` 通过；
- 决策记录贴出"批准恰好执行一次 / 拒绝零次"的实测、非法 resume 不毒化的实测、Step 2 的 `--diff` 原文。

## 10. 汇报格式

- 改了哪些文件；
- 验证命令与实际输出（失败照贴）；
- 范围守卫的输出；
- 快照差异；
- 未验证项（含手动验收是否做了）；
- 残余风险（至少包括：批准后执行期间进程崩溃时的重复执行窗口——单次凭证的已用集合在进程内，重启后恢复会重跑执行节点）。

## 11. 常见坑（本仓库实测过）

- 判挂起要看 `get_state().interrupts`，不能只看 `next`（docgen 实测：重新挂起后 `next` 可能为 `()`）。
- 非法 resume 值一旦让节点抛异常，LangGraph 会把它连同 ERROR 落进 checkpoint，之后每次恢复都先重放它——会话永久毒化。
- 运行时 MCP 绑定登记只增不删：测试里用独立的 server id。
- 前端测试用 node strip-types 直跑，被测模块间相对导入须带 `.ts` 后缀，且不能用 TS 参数属性。
- 本机如果设了 `WATER_AGENT_MCP_OVERLAY`、`WATER_AGENT_MCP_CONFIG` 或 `WATER_AGENT_TOOL_EXPOSURE`，跑测试和快照前先 `unset`。
- worktree 里不设 `PYTHONPATH=src` 时，pytest 会被 H0 拦下（exit 4）。
- 只含执行 Prompt 的文档 PR，分支名不能以 `YEK/p7-s<n>-` 开头。
