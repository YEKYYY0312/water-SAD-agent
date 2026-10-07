# P7-S7 决策记录：危险操作人工确认门（HITL，Step E2）

**日期**：2026-10-07
**状态**：已完成（浏览器手动验收 P7-S7-A10 未做，见 §5）
**规格来源**：`docs/design/16-p7-plugin-mcp.md` §2 F7、§3.7、§5 S7、§7 P7-S7；执行 Prompt `docs/execution-prompts/P7-S7-hitl-confirmation.md`（其 §0 的决定以 Prompt 为准）
**基线**：main `80dafa8`（P7-S7 执行 Prompt 合入后）

---

## 0. 偏离与修订

### 0.1 执行 Prompt §0 的决定（照做）

| 决定 | 落点 |
|---|---|
| 需确认的工具在 ToolNode 里是同名同 schema 的 `return_direct` 延迟工具 | `tools/ds_adapters.py::tool_spec_to_deferred_tool`；`plugins/assembly.py` 按运行时绑定 `requires_human_confirmation` 替换，`ToolAssembly.confirmation_tools` |
| 外层图确认门：挂起 → 批准凭证执行并同 id 替换 / 拒绝替换为 observation → 回 `agent_loop` | `graphs/confirmation.py`；`build_agentic_app(confirmation=)`，`None` 时图结构不变 |
| S1 的无凭证拒绝原样保留，凭证经 `invoke_confirmed` 交给护栏 | `contracts.ConfirmationGrant` / `confirmation_args_hash`；`pipeline.validate_tool_call(confirmation_grant=)`；`ToolRegistry.invoke_confirmed` 与 `invoke` 共用 `_invoke` |
| `confirmation_handler_ready` 仍加载期拒绝，可用性由入口决定 | `mcp/config.py` 文案；`confirmation_ready` 参数链只由 `WebAgenticSessionFactory` 传 `True` |
| run 状态 `waiting_confirmation`（无迁移）、SSE `confirmation_required`、决定接口在同一个 run 上恢复 | `webapp/run_manager.py`、`webapp/run_routes.py`、`storage/repository.py` |
| 批准者是 run 所有者且有项目写权限；`confirmation_id` 不匹配 409 | `POST /api/runs/{run_id}/confirmation` |
| 挂起期间发消息 409 `WEB_CONFIRMATION_PENDING`，读 checkpoint 判定 | `RunManager.pending_confirmation` |
| `confirmation_log` 审计 + 三类 trace 事件 | state 跨轮累积键；`confirmation_requested` / `decided` / `executed` |
| 每轮上限 3 次 | `MAX_CONFIRMATIONS_PER_TURN` |
| 前端最小确认卡片；CLI / runner 不接 | `ConfirmationCard.tsx` + `confirmationApi.ts` |

### 0.2 实测后追加的偏离

| 执行 Prompt 原文 | 本步做法 | 原因 |
|---|---|---|
| 外层新增一个 `confirmation_gate` 节点 | 两个节点：`confirmation_request`（记 `confirmation_requested`、执行每轮上限，不挂起）→ `confirmation_gate`（只挂起与决定） | interrupt 的节点在每次恢复时**从头重跑**；把"请求"事件放在挂起节点里，每次恢复（含非法 resume 再挂起）都会再记一次。拆开后每个待确认调用恰好经过一次 `confirmation_request`。外层每次调用（首轮与每次恢复）各有完整的 `recursion_limit`，最坏路径约 13 步，不超 20 |
| 恢复时继续同一轮（未提召回结果） | 三个 `*_context` 经 `ConfirmationRuntime.turn_contexts` 交回给 agent_loop，**不写回状态**；取不到（重启过）换成 `None` 并记 `turn_recall_context_unavailable` | 实测：`memory_context` 等经生产 serde 读回是 dict（`Blocked deserialization of water_agent.memory.recall.MemoryRecallResult`），恢复后 agent_loop 再进入时 `PromptAssembler` 当场 `TypeError`。先试了 `Command(resume=…, update={…})`：单次恢复可行，但**非法 resume 再挂起之后第二次带 update 恢复**，LangGraph 报 `InvalidUpdateError: At key 'memory_context': Can receive only one value per step`——而且是在确认门节点**执行完工具之后**才报，等于批准的调用执行了、结果却没落盘。于是改为不写状态的旁路 |
| Step 4 才做 `confirmation_ready` 参数链 | 现开路径（`open_gateway_sessions` / `McpGatewayPlugin.owning` / `AgenticSession(confirmation=)`）随 Step 3 提交，池租约、指纹、Web 工厂与配置文案在 Step 4 | Step 3 的端到端测试需要一个真的暴露 `human_confirmation` server 的会话 |
| 不改 `bridge.py` | `wiring.bridge_gateway_view`：入口接了确认门时用 `model_copy(update={"confirmation_handler_ready": True})` 生成只在本次连接里用的视图交给 `open_bridge` | `open_bridge` 从网关配置读确认位；`model_copy` 不重跑那条加载期校验，视图不进 manifest、不进 `raw_config`、不回写配置 |
| 拒绝 observation：`{"ok": false, "code": "TOOL_CONFIRMATION_REJECTED", "message": …}` | 另带 `"error": true` 与 `"suggestions": []`，原因**不**进 observation（只进审计） | 与 `ToolRegistry` 结构化错误同形，Web 工具卡片据 `error` 标为失败；用户写的原因若进 ToolMessage 就是一条不经输入护栏的文本通道 |
| 判挂起看 `get_state().interrupts` | 会话侧照做；`RunManager` 没有图，读 checkpoint 元组的 `pending_writes`，按公开类型 `langgraph.types.Interrupt` 识别（不按通道名） | `langgraph.constants.INTERRUPT` 在 1.2.11 是私有、已弃用；实测非法 resume 再挂起后 pending writes 里的 interrupt 被替换成带 error 的那份，决定之后新 checkpoint 里不再有 |
| 发消息时读 checkpoint 判定挂起 | 只对声明 `supports_confirmation` 的会话工厂读；读不了返回 503 `WEB_RUN_UNAVAILABLE`（不建 run） | 协议兼容（测试 / 评测替身的会话没有确认门，不读）；判定不了就不放行——在可能挂起的轮次上再叠一个 run，checkpoint 里会出现没有应答的 tool_call。`tests/test_web_runs.py::test_embedding_key_is_not_required_without_a_real_index` 用真实工厂跑在 SQLite 上，给它换了内存 checkpointer（用例本意是验 embedding 预检，不受影响） |
| 202 后"继续接收同一 run 的事件" | 挂起时流以合成的 `stream_end(waiting_confirmation)` 结束；202 返回 `event_cursor`，前端用它作 Last-Event-ID 重新订阅 | 合成帧不在事件日志里，客户端自己的游标会跳过恢复后的第一条事件；进程重启后日志从 1 开始（`event_cursor="0"`） |
| （未提及） | 挂起的 run：取消是 no-op；`RunManager.close()` 把它算作已排空，另报 `waiting_confirmation_run_ids` | 挂起时没有在跑的东西；取消它只会让会话卡住（新消息被挂起拦下、决定接口又找不到等待中的 run）。不执行请拒绝 |
| fake server `bump` | `bump` 加可选 `note`（原样回显），新增只读 `peek` | `note` 证明"批准的参数就是执行的参数"并用来检查审计里没有参数原文；`peek` 证明挂起 / 拒绝时副作用没发生 |
| §7 验证项 | A02 加 `test_args_hash_is_canonical`；A03 加 `test_fourth_confirmation_in_a_turn_fails`、`test_resume_restores_turn_contexts_or_warns` | 每轮上限与召回结果恢复各有独立用例 |
| （测试改动） | `tests/test_tool_plugins.py` 的 `open_gateway_sessions` 替身接受关键字参数；`web/app/tests/workspace.test.mjs` 发送按钮的源码断言加入 `waiting` | 插件现在以关键字传 `confirmation_ready`；停在确认门时发送按钮禁用 |

---

## 1. 交付

| commit | 产物 | 要点 |
|---|---|---|
| `1220399` | 延迟工具 + `ToolAssembly.confirmation_tools`（A01） | 待确认标记写在 `ToolMessage.artifact`（`content_and_artifact`），远端输出只能写正文、伪造不了；延迟工具仍挂在同一个 registry 上（快照的 `tools_bound_to_prompt_registry` 不受影响） |
| `b7c48d0` | `ConfirmationGrant`、护栏两条新规则、`invoke_confirmed`、两个版本号（A02） | 先比对三项再记已用：不一致的凭证不消耗；已用集合是进程级（跨注册表、跨护栏实例） |
| `67af995` | `graphs/confirmation.py`、`build_agentic_app(confirmation=)`、`confirmation_log`、trace 事件、`AgenticSession(confirmation=)` / `aresume`、现开路径的 `confirmation_ready`（A03、A04） | 执行用 state 里 AI 消息记下的参数；同一条消息多条逐条确认、之间不调模型 |
| `039643a` | 池租约 / 指纹的 `confirmation_ready`、Web 工厂接线、配置文案（A05） | 指纹纳入入口确认位：同一台 server 在两种入口下桥接出的工具集不同，不能互相复用 |
| `c035205` | Web：状态、SSE、两个路由、历史投影、`RunManager.resume`、召回结果旁路（A06） | 首轮与恢复共用 `_drive` 收口；挂起时先落库、再在同一把锁里发事件并置 `suspended`；恢复时库里比较交换 `waiting_confirmation → queued` |
| `90dde2f` | 前端确认卡片（A07） | 参数纯文本（React 转义），标出脱敏 / 截断；刷新或重启后从历史恢复；样式只用现有 token |
| 本 commit | F7 已知数字测试（A09）、快照测试 docstring（A08）、规格 §2/§3.7/§5/§7、P4 规格 §3、本记录、AGENTS.md | — |

---

## 2. 实测

### 2.1 读码与机制实验（`/tmp` 下的独立脚本，不入库）

- `create_react_agent`（langgraph-prebuilt 1.1.0）：一条 AI 消息同时调用普通工具与 `return_direct` 工具时，ToolNode 按 `Send` 并行执行两者，`route_tool_responses` 随即 `END`；普通工具执行一次，延迟工具的待确认消息在 state 里。外层 `interrupt` → `Command(resume="yes")`（非法）→ 再挂起，`get_state().next == ()`、`interrupts` 为带 error 的那份；之后合法值照常恢复，`log` 只记合法决定。——没有触发 Prompt §7 的停止条件（并行调用里先执行的只是不需确认的工具，模型并行发出的调用之间本无顺序约定），写进 §5。
- 召回结果：`InMemorySaver(serde=default_checkpoint_serde())` 下 `MemoryRecallResult` 读回是 dict，并打印 `Blocked deserialization …`；`Command(resume=…, update=…)` 单次可用、二次在同一任务上冲突（§0.2）。

### 2.2 先红后绿与反证

- A01：`AttributeError: 'ToolAssembly' object has no attribute 'confirmation_tools'` → 实现后通过。
- A02：`ImportError: cannot import name 'ConfirmationGrant'` → 实现后 3/3。
- A03/A04：实现先于用例写成，补做反证——把 `route_after_agent` 里"有待确认 → confirmation_request"那一支去掉，5 条外层门用例全部失败（`KeyError: 'confirmation'` 等），恢复后通过。
- 召回结果旁路：把 `_restore_turn_contexts` 改成恒不替换，`test_resume_restores_turn_contexts_or_warns` 报 `TypeError: memory_context must be a recall result, text, or memory envelopes`，恢复后通过。
- A05：`TypeError: McpConnectionPool.lease() got an unexpected keyword argument 'confirmation_ready'` → 实现后通过。
- A06：去掉发消息路由里的 409 `WEB_CONFIRMATION_PENDING`，`test_confirmation_round_trip_on_the_same_run` 与 `test_pending_confirmation_survives_restart` 失败，恢复后 3/3。
- A07：`confirmation.test.mjs` 写好时 5/7 通过（解析与运行态逻辑已写）、卡片与控制器 2 条失败 → 实现后 7/7；全量前端 74/74。

### 2.3 "批准恰好执行一次 / 拒绝零次 / 非法 resume 不毒化"的实测

`tests/test_confirmation_gate.py`（生产 `AgenticSession(confirmation=True)` + `ScriptedChatModel` + fake server `--mode stateful`，`peek` 直接读子进程计数器）：

- `test_approve_executes_exactly_once`：挂起时 `peek == 0`、模型被调用 1 次；批准后 `peek == 1`，模型第 2 次调用收到的 ToolMessage 为 `structured_content.counter == 1`、`note == "approve-note"`，任何收到的消息里都没有 `pending_confirmation`。
- `test_reject_executes_nothing`：拒绝后 `peek == 0`，模型收到 `TOOL_CONFIRMATION_REJECTED`；thread 里该工具的 ToolMessage 只有一条（同 id 替换）。
- `test_invalid_resume_does_not_poison_the_session`：`"yes"`、`confirmation_id` 不匹配、`approved="true"` 三次都返回 `waiting_confirmation` + error、`peek == 0`、`confirmation_log` 为空；随后合法批准 → 第二条调用挂起（`peek == 1`、模型仍只调用过 1 次）→ 拒绝 → `succeeded`，`log == [approved, rejected]`。
- `test_fourth_confirmation_in_a_turn_fails`：连批 3 次后第 4 次请求直接 `failed`（`TOOL_CONFIRMATION_LIMIT`），`peek == 3`。

`tests/test_web_confirmation.py`（真实 `WebAgenticSessionFactory`、`lifecycle: shared`、HTTP 往返）：挂起 → SSE 以 `stream_end(waiting_confirmation)` 结束、无 finalize、`peek == 0` → 发消息 409 且 run 行仍只有 1 条 → 批准 202 → 按 `event_cursor` 订阅收到 `confirmation_decided, run_queued, …, finalize(succeeded)`，`peek == 1`，同一个 `run_id` → 重复提交 409 `WEB_CONFIRMATION_STALE`、`peek` 仍为 1。重启用例：新 `RunManager` 下旧事件日志 404、历史仍带挂起请求、新消息 409、决定 202（`event_cursor == "0"`）后收尾，`peek == 1`（新进程）。

### 2.4 命令与输出

```
$ PYTHONPATH=src _venv/bin/pytest -q tests/test_confirmation_grant.py tests/test_confirmation_gate.py tests/test_web_confirmation.py tests/test_tool_policy.py tests/test_mcp_wiring.py tests/test_mcp_pool.py tests/test_web_mcp_pool.py tests/test_web_runs.py tests/test_web_sse.py tests/test_agent_rag.py tests/test_tool_plugins.py tests/test_p7_known_figures.py tests/test_tool_assembly_snapshot.py tests/test_p7_traceability.py
227 passed（0 skipped），EXIT=0

$ (cd web/app && npm test && npm run build)
ℹ tests 74 / ℹ pass 74 / ℹ fail 0
tsc -b && vite build → ✓ built

$ _venv/bin/python -m water_agent.harness.tool_assembly_snapshot --check
装配快照与基线一致（15 个 profile）

$ _venv/bin/python scripts/check_step_scope.py --auto --base origin/main
step=P7-S7 base=origin/main merge-base=80dafa8261fe … 范围守卫通过

$ env -u PYTHONPATH COMPOSE_PROJECT_NAME=p7s7 VENV=…/_venv bash scripts/ci_gate.sh; echo EXIT=$?
water_agent 导入自：<p7-s7 worktree>/src/water_agent
==> CI 门禁通过：pytest + ruff + mypy + rag_p0(12/12) + calc_p0(28/28) + docgen_p0(11/11) + material_p0(10/10) + governance_p0(32/32) + agentic_p0(21/21) + tool_exposure_p0(36/36) + drawing_p0(合成 smoke 回归) + drawing_gen_p0(参数化出图) + multi_agent_p0 + memory_p0(§13.2 十二项硬指标) + 基线对比
EXIT=0
（门禁内全量 pytest：3469 passed / 30 skipped / 0 failed；mypy 218 个文件无问题）
```

---

## 3. 快照变化（`--diff` 原文）

Step 2（`b7c48d0`）：

```
/versions/TOOL_POLICY_VERSION: "p7-step4-2026-10-06" -> "p7-step7-2026-10-07"
```

`--write --reason "P7-S7 Step 2：确认门执行凭证（ConfirmationGrant + invoke_confirmed + tool.confirmation_mismatch/replayed），可用工具集变化，TOOL_POLICY_VERSION 递进；surface 与 lifecycle 不变"`。其余 commit 无快照变化。`RULESET_VERSION` 不在快照里。快照的会话 profile 走 CLI 路径，`session_mcp_confirm_skip` 仍是整台跳过；`AgenticSession` 只在 `confirmation=True` 时才给 `build_agentic_app` 传 `confirmation=`，`lifecycle.app.app_kwargs` 不变。

---

## 4. 不做

- CLI / 评测 runner 的交互确认；管理员代批、多人会签、确认超时自动拒绝。
- 按工具（而非按 server）声明需确认；确认前的参数预校验（schema 不合法的调用照样请人确认，批准后由注册表报 `INVALID_ARGUMENTS`）。
- 新审计表或数据库迁移；Streamable HTTP（S8）、Web 扩展面板（S6）。
- 不改工具暴露（S4）、`bridge.py` / `protocol.py` / `schema.py` / `client.py`、提示词、`mcp/errors.py`（`MCP_CONFIRMATION_UNAVAILABLE` 的说明文字仍写"确认门未接线"，对没有确认门的入口仍成立）。

## 5. 残余风险

- **批准后、执行结果落 checkpoint 之前进程崩溃**：已用凭证集合只在进程内。重启后恢复会从头重跑 `confirmation_gate`，以同一个 `confirmation_id` 签发新凭证并再执行一次——有副作用的远端操作可能执行两次。同进程内的重跑会被 `tool.confirmation_replayed` 拦下。
- **挂起已落 checkpoint、run 行还没改成 `waiting_confirmation` 之间崩溃（或那次写库失败）**：会话被挂起拦住（409 `WEB_CONFIRMATION_PENDING`），决定接口按库里的状态报 409 `WEB_CONFIRMATION_STALE`，界面上无路可走，需要人工改库。窗口是一次数据库写入。
- **重启后的召回结果**：确认挂起期间进程重启，本轮剩下的部分不带长期记忆 / 上传资料 / 附件作答（如实记 `turn_recall_context_unavailable`）。
- **并行调用的顺序**：同一条 AI 消息里不需确认的调用在人做决定之前就已执行；只有需确认的那条等待。
- **凭证在护栏前段消耗**：凭证通过比对后即记为已用，之后若被参数大小 / 路径 / 注入检查拦下，这次批准作废，要重新请求。
- **卡片显示的是脱敏后的参数**：参数含 PII / 凭据时卡片打码并标注，批准后执行的是原始参数。
- **多 worker**：挂起判定读 checkpoint、恢复靠库里的比较交换，跨 worker 不会重复执行；但挂起的 handle 与事件日志在进程内，决定落到别的 worker 时原 worker 的 handle 留在内存里（与 S5 的单实例单 worker 部署假设一致）。
- **trace 分段**：挂起前与恢复后是两份 trace（同一 `run_id`、不同 `trace_id`），`GET /api/runs/{id}/trace` 只返回当前进程最近一段。
- **粒度与入口**：确认是 server 级；CLI / runner 下这类 server 仍不可用；真实 CAD/BIM server 上未验证（P4 Step 26 blocked）。
- **浏览器手动验收未做**（P7-S7-A10）：需要本机 Web 栈（PostgreSQL + 镜像内可达的 fake server）与浏览器，本步没有搭；自动化覆盖在 A06（HTTP 往返）与 A07（前端单测 + 类型检查）。
