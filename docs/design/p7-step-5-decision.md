# P7-S5 决策记录：MCP 长驻连接（stdio，Step D 第一部分）

**日期**：2026-10-06
**状态**：已完成
**规格来源**：`docs/design/16-p7-plugin-mcp.md` §2 F8/F17、§3.4、§7 P7-S5；执行 Prompt `docs/execution-prompts/P7-S5-shared-mcp-connections.md`（其 §0 的决定以 Prompt 为准）
**基线**：main `0c76626`（P7-S5 执行 Prompt 合入后）

---

## 0. 偏离与修订

### 0.1 执行 Prompt §0 的决定（照做）

| 决定 | 落点 |
|---|---|
| S5 只做 stdio 长驻；Streamable HTTP 挪到 S8 | 本步不碰能力模型与出站清单（受保护断言 `provider_call_sites: 7` 未动） |
| `lifecycle: per_run \| shared`，缺省 `per_run` | `MCPServerConfig.lifecycle`；示例文件的 autocad 声明 `shared` |
| 同一客户端请求串行，等锁计入 deadline | `MCPStdioClient._request_lock`；超时消息写明"正忙" |
| 按单台 server 指纹重载（配置 + 确认位 + 子进程 env 摘要） | `mcp/pool.py::server_fingerprint`（摘要不落日志、不进 trace） |
| 崩溃不在调用中途重试，下一次租约重开 | `McpConnectionPool._acquire_shared` 借用前查 `alive` |
| 只有 Web 用连接池 | `WebAgenticSessionFactory`；CLI / runner 仍 `McpGatewayPlugin.owning` |

### 0.2 实测后追加的偏离

| 执行 Prompt 原文 | 本步做法 | 原因 |
|---|---|---|
| 池"由 `create_app` 构造、放在 `app.state`"，关闭钩子里先 `run_manager.close()` 再 `pool.close()` | 池由 `WebAgenticSessionFactory` 持有（`mcp_pool=` 可注入），`RunManager.close()` 排空 run 之后调用工厂的 `close()` 关池，结果并入返回值的 `mcp_pool` 键（`app.state.shutdown_drain` 照旧承载） | 会话工厂本来就由 `RunManager` 构造，应用关闭本来就只调 `RunManager.close()`；放在同一个函数里，"先排空再关池"的顺序由代码结构保证，`app.py` 不用改 |
| 并发失败测试"两个线程并发调用慢工具，修复前其中一个超时" | 先实测：fake server **按到达顺序**应答时，`queue.Queue` 按等待者先后（FIFO）分发，先等的线程恰好拿到先到的响应，缺陷被掩盖（4 路并发 3 轮全部"正确"）。改为 `slow_echo` 在 fake server 里后台线程执行、**按完成顺序**应答（先发的睡得久），修复前稳定复现 | JSON-RPC 允许乱序应答，真实 server 会这样做；按到达顺序应答的测试替身证明不了任何事 |
| 事实表只有 F8 | 新增 **F17**（`_request` 并发不安全）并标为 S5 已修 | S0 事实表里没有这一条，是本步读码发现的现有缺陷 |
| `AgenticSession` 登记 `lease.release` | 会话工厂借到后**立即**登记一次，`AgenticSession` 再登记一次（`release` 幂等） | 借到连接与 `AgenticSession` 登记之间若抛错（例如模型构造失败），只靠后者会漏掉租约；有测试覆盖 |
| `RunManager.close()` 读 `self.session_factory` | 用 `getattr` 读，取不到就跳过关池 | 既有测试 `test_web_sse.py::test_shutdown_reports_runs_that_did_not_finalize` 用 `__new__` 构造 `RunManager`，没有这个属性 |

---

## 1. 交付

| commit | 产物 | 要点 |
|---|---|---|
| `4b5e6c2` | 客户端请求串行、`alive` 属性、`reader_done` 标记；fake server `--mode stateful`（`whoami` / `bump` / 乱序应答的 `slow_echo`） | 先红后绿（§2.1）；读线程结束后的调用立即 `MCP_CONNECT_FAILED`，不空等到超时 |
| `5de038b` | `lifecycle` 字段、`MCP_CONFIG_VERSION` → `p7-s5-2026-10-06`、入仓骨架与示例同步 | 快照只有版本号变化（§3） |
| `6ec3d22` | `mcp/pool.py`：`McpConnectionPool` / `McpLease`、指纹、退役与关闭；`wiring.py` 抽出 `pre_connection_skips` / `session_failed` / `session_skips` 供池复用；`MCPGatewayAssembly.connections` 与 trace 摘要的 `connection` 字段 | 开进程在池锁外；同一台 server 并发首次借用只开一个 |
| `8ad63f6` | `AgenticSession(mcp_lease=)`、`build_agentic_session` 透传、`WebAgenticSessionFactory` 持有并关闭池、`RunManager.close()` 排空后关池 | CLI / runner / 快照路径不变 |
| 本 commit | F8 已知数字测试、规格 §2/§3.4/§5/§7、P4 规格 §3 `lifecycle`、本记录、AGENTS.md | — |

---

## 2. 实测

### 2.1 A01：修复前 / 修复后

修复前（`tests/test_mcp_pool.py`，fake server 乱序应答）：

```
E                   AssertionError: ('r0-t0', MCPError("MCP 调用超时：MCP server 'poolconc' 无响应"))
E           assert '正忙' in "MCP 调用超时：MCP server 'poolbusy' 无响应"
2 failed in 4.37s
```

`r0-t0` 是最先发出、最后完成的那个调用：它的响应被别的线程读到并当成通知丢掉，自己等到超时。

修复后：`2 passed`；之后整个文件 10/10，连跑 3 次稳定。

### 2.2 连接池与 Web 接线

- `tests/test_mcp_pool.py` 10 条：复用（同 pid）、per_run 每次新进程且释放即退出、共享状态如实可见（两个租约 `bump` 计数连续）、改 `allow` / 改 env 值各自换新进程且旧租约释放前旧进程仍可用、停用后退役、`SIGKILL` 后下一次租约重开且持有死连接的调用 < 2 s 失败、关闭后不留进程并报告未释放租约、4 线程并发首次借用只 spawn 一个进程、trace 摘要不含 env 值 / 变量名 / 命令路径。
- `tests/test_web_mcp_pool.py` 4 条：真实 `build_agentic_session` → `AgenticSession`（mock 生成器）连续两个 run 同 pid、工具已注册且主体持有 `MCP_INVOKE`；会话构造抛错时租约被释放；坏配置在借用之前就 `MCP_CONFIG_INVALID` 且池里无任何租约；`RunManager.close()` 排空后关池、进程退出。
- 进程退出用 `os.kill(pid, 0)` 判定；被 `SIGKILL` 的子进程在 `poll()` 回收前是僵尸、`kill(pid, 0)` 仍成功，那条用例改为等客户端 `alive` 变 `False`（`poll()` 顺带回收）。

### 2.3 门禁与测试

- 全量 pytest：3454 passed / 30 skipped / 0 failed（Step 4 后）。
- `COMPOSE_PROJECT_NAME=p7s5 bash scripts/ci_gate.sh`（worktree 内、不预设 PYTHONPATH）：**EXIT=0**（§2.4）。
- `ruff check src`、`mypy src/water_agent`（217 个文件）无问题；范围守卫 `--auto` 通过；快照 `--check` 一致。
- 过程中的一次失误：Step 3 第一次提交时把 `git commit` 串在测试命令后面，一条 S3 测试（trace 摘要逐字段比对，新增了 `connection` 字段）失败也照样提交了；随即修正测试期望并 `--amend` 进同一个 commit（未推送）。之后每次提交前单独确认测试退出码。

### 2.4 最终门禁

```
$ env -u PYTHONPATH COMPOSE_PROJECT_NAME=p7s5 VENV=…/_venv bash scripts/ci_gate.sh; echo EXIT=$?
water_agent 导入自：<p7 worktree>/src/water_agent
==> CI 门禁通过：pytest + ruff + mypy + rag_p0(12/12) + calc_p0(28/28) + docgen_p0(11/11) + material_p0(10/10) + governance_p0(32/32) + agentic_p0(21/21) + tool_exposure_p0(36/36) + drawing_p0(合成 smoke 回归) + drawing_gen_p0(参数化出图) + multi_agent_p0 + memory_p0(§13.2 十二项硬指标) + 基线对比
EXIT=0
```

---

## 3. 快照变化（`--diff` 原文）

Step 2（`5de038b`）：

```
/versions/MCP_CONFIG_VERSION: "p7-s3-2026-10-05" -> "p7-s5-2026-10-06"
```

其余 commit 无快照变化。`surface`、`TOOL_POLICY_VERSION`、`PROMPT_TEMPLATE_VERSION` 由范围块 `frozen_json` 冻结。快照捕获点在 `build_agentic_app`，看不到跨 run 的进程身份，所以 F8 的锁定测试直接断言池的行为（`test_shared_mcp_servers_survive_across_runs`）。

---

## 4. 不做

- Streamable HTTP、`NETWORK_FETCH`、出站清单变化（S8）。
- 管线化请求、每台 server 的并发上限配置、空闲回收、崩溃退避、按用户 / 租户分池。
- 不改 `bridge.py` / `protocol.py` / `schema.py`、能力模型、护栏、工具暴露、harness runner。
- Web 能力端点（`configured_server_count`）不区分 shared / per_run（S6 面板一并处理）。

## 5. 残余风险

- **共享即共享状态**：`lifecycle: shared` 的 server 里任何进程内状态对所有 Web 用户可见（有测试演示计数器）。这是运维的声明责任，agent 无法验证；有状态的 server 必须保持 `per_run`。
- **吞吐**：同一连接串行，一个慢调用会让同一台 shared server 上的其他调用排队，最长等到各自的 `call_timeout_seconds` 后报"正忙"。4 个 Web 工作线程 × 串行连接在负载高时会放大等待。
- **崩溃循环**：一台 shared server 若每次都起不来，每个 run 都会重试启动（与 S5 之前的 per_run 代价相同），没有退避。
- **多 worker / 多实例**：池是进程内的；多个 uvicorn worker 各自持有一份，进程数随 worker 数增长。单实例单 worker 是当前部署假设。
- **退役等待**：被长时间占用的旧连接（例如卡住的调用）在最后一个租约释放前不会关闭，配置变更后短时间内新旧两个进程并存。
- **手动验收未做**（P7-S5-A10）：本机 autocad-mcp + Web 栈的 `shared_new` → `shared_reused` 未实测；真实 CAD/BIM 验收仍是 P4 Step 26 blocked。
