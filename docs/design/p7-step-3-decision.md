# P7-S3 决策记录：MCP 配置分层、按实例启停与工作目录隔离（Step B）

**日期**：2026-10-05
**状态**：已完成
**规格来源**：`docs/design/16-p7-plugin-mcp.md` §2 F6/F9/F10/F13/F15/F16、§7 P7-S3；执行 Prompt `docs/execution-prompts/P7-S3-layered-mcp-config.md`（其 §0 对原计划的修订以 Prompt 为准）
**基线**：main `ec9ee2e`（P7-S3 执行 Prompt 合入后）

---

## 0. 偏离与修订

### 0.1 执行 Prompt §0 的修订（照做）

| 原计划 | 本步做法 |
|---|---|
| S3 含"受信 entry point 发现" | 推迟，未做（规格 §5 记为推迟项） |
| S3 修 F10 | 入仓 `data/mcp_servers.json` 改为空骨架；autocad 配置挪到 `data/mcp_servers.example.json`（占位路径） |
| 新增 F15、F16 | 已修，见 §1 |
| 预算测试"计入 MCP 工具" | 未新增预算测试（S0 的已知数字测试已锁定 4724；设上限属于 S4） |

### 0.2 实测后追加的偏离

| 执行 Prompt 原文 | 本步做法 | 原因 |
|---|---|---|
| 覆盖层规则"缺 `command` 的部分 server 在没有默认层时新增" | 照做，并额外断言这种情况**响亮失败**（`MCP_CONFIG_INVALID`） | 只有覆盖层时，部分字段的 server 会被当成"新增"；必须报错，不能悄悄拿不完整的 server 去装配 |
| `merge_overlay` 对形状错误抛 `ValueError` | 改抛 `TypeError`，调用方同时捕获 `TypeError`/`ValueError` 并转成 `MCP_CONFIG_INVALID` | ruff 的 TRY004；行为不变——形状不对的覆盖层照样在加载期响亮失败 |
| trace 摘要里的 `skipped` 顺序 | 保持 server `tools/list` 的原始顺序，不排序 | 原始顺序是确定的（server 侧定义），排序反而会掩盖 server 清单的变化 |
| 停用的 server 是否检查 `env_file` / `cwd` 存在 | **不检查** | 停用就是为了让一台暂时不可用的 server（例如目录还没准备好）不影响其余 server；查了就失去意义 |

---

## 1. 交付

| commit | 产物 | 要点 |
|---|---|---|
| 1 | `MCPServerConfig` 新增 `disabled` / `cwd`；`command` 收紧；`env_file` 与 `cwd` 的加载期校验；`MCP_CONFIG_VERSION` → `p7-s3-2026-10-05` | 模型层只校验形状（示例文件的占位路径能过）；存在性在 `load_gateway_config` 里查（F9） |
| 2 | `merge_overlay` + `load_layered_gateway_config`；`wiring.load_mcp_gateway()` 改为调用它；`WATER_AGENT_MCP_OVERLAY` 加进快照的隔离环境变量 | 会话、runner、Web 能力端点自动生效；`load_gateway_config(path)` 单层语义不变 |
| 3 | 停用（`MCP_SERVER_DISABLED`，先于确认门判定，不进 manifest）；子进程工作目录隔离；fake server 新增 `--mode cwd` | F15：未配 `cwd` 时每次连接新建 `wa-mcp-*` 空临时目录，关闭（含启动失败）即删除 |
| 4 | F6：缓存服务带上 manifest；F13 后续：`start_trace` 开启时补记 `mcp_assembly`（会话），runner 先 `recorder.start` 再记；CLI 同步 `ask()` 先开 trace；`McpContribution.servers` 逐台结果；`MCPGatewayAssembly.trace_summary()`（不含 detail 文本） | 快照 harness 把 `cache_service.mcp_server_manifest` 改为只记是否非空（摘要含机器相关路径） |
| 5 | 入仓配置改为空骨架、示例文件、`.dockerignore`；删除两条恒 skip 的 autocad 用例，改写默认配置测试 | 示例文件不含真实机器路径（有测试断言） |
| 6 | 规格状态、P4 规格配置契约、本决策记录、AGENTS.md | — |

---

## 2. 实测

- 每个 Step 都先写失败测试：Step 1 三条（`disabled`/`cwd` 不存在）、Step 2 四条（加载器不存在）、Step 3 两条（停用仍 spawn；子进程能按相对路径读到仓库根的 `pyproject.toml`）、Step 4 三条。
- 反证：
  - 把 `env_file` 的相对路径解析改回原样（不拼目录），`test_env_file_and_cwd_resolved_and_checked_at_load` 变红；
  - 去掉 `Popen` 的 `cwd=`，`test_child_runs_in_isolated_or_configured_cwd` 变红（子进程又能读到 agent 仓库里的文件）。
- **手动验收（P7-S3-A13）**：本机按示例文件写了临时覆盖层（`command` / `cwd` 指向 `/Users/yaerken/autocad-mcp`），`open_gateway_sessions(load_mcp_gateway())` 暴露 `('mcp_autocad_mcp_read_dwg',)`、无跳过。此前入仓配置在任何机器上都暴露 0 个工具。**这只证明能连上，不构成真实 CAD 验收**（P4 Step 26 仍 blocked）；`read_dwg` 的绝对路径参数仍会被护栏 `tool.path_not_allowed` 拦下（见 §4）。
- 受影响的测试集（MCP 全部、插件、快照、已知数字、会话/CLI/Web/trace/runner 相关）：233 passed / 2 skipped（skip 是依赖本机 autocad-mcp 的既有用例）。
- `ruff check src`、`mypy src/water_agent` 无问题；范围守卫通过（23 个文件）。
- CLI 冒烟：`--mode agentic --mock-generator --json` → `status=succeeded`，引用 `FIX 1.2.1`，stderr 为空，trace 正常落盘。
- 全量门禁：`COMPOSE_PROJECT_NAME=p7s3 bash scripts/ci_gate.sh`（worktree 内、不预设 PYTHONPATH）**EXIT=0**；日志首行 `water_agent 导入自：<p7-s3 worktree>/src/water_agent`。

---

## 3. 快照变化（`--diff` 原文）

**Step 1**（`MCP_CONFIG_VERSION` 递进）：

```
/versions/MCP_CONFIG_VERSION: "p4-step23-2026-09-08" -> "p7-s3-2026-10-05"
```

**Step 4**（F6 + F13 后续）：

```
/profiles/runner_drawing_env/lifecycle/trace_record_calls: [] -> [{"event": "run_start", "landed": true, "node": "graph"}]
/profiles/runner_mcp_fake/lifecycle/trace_record_calls: [] -> [{"event": "run_start", "landed": true, "node": "graph"}, {"event": "mcp_assembly", "landed": true, "node": "mcp"}]
/profiles/runner_no_mcp/lifecycle/trace_record_calls: [] -> [{"event": "run_start", "landed": true, "node": "graph"}]
/profiles/session_web_member/lifecycle/app/cache_service/mcp_server_manifest: "" -> "<absent>"
/profiles/session_web_member/lifecycle/app/cache_service/mcp_server_manifest_nonempty: "<absent>" -> true
```

- runner 现在在构图前就开启 trace，所以快照能看到 `run_start` 与 `mcp_assembly`，**全部已落盘**；
- 会话 profile 的装配摘要在 `start_trace` 时才写，快照在构图处捕获，所以会话 profile 的 `trace_record_calls` 不变（由 `tests/test_mcp_layered_config.py::test_assembly_summary_lands_in_trace` 端到端验证）；
- Web profile 的缓存服务现在带上了 manifest（F6）；字段改为只记是否非空。
- **没有任何 profile 的 surface 变化**，`TOOL_POLICY_VERSION` / `PROMPT_TEMPLATE_VERSION` 不递进（范围块 `frozen_json` 在 PR 上核对）。

---

## 4. 不做

- entry point 发现（推迟）、长驻连接与 Streamable HTTP（S5）、按意图最小暴露（S4）、Web 面板（S6）、HITL（S7）。
- **不给 MCP 工具开放读目录**：`read_dwg` 的绝对路径参数仍被护栏拦下（MCP 工具走 `allowed_export_dirs`，默认空）。S3 之后它只在图纸放在 server 的 `cwd` 下、模型传相对路径时调得通。是否给 `MCP_INVOKE` 配读目录白名单是单独的能力决策。
- Web 能力端点的 `configured_server_count` 仍把停用的 server 算进去（`webapp/` 不在 S3 范围，留给 S6）。

## 5. 残余风险

- 工作目录隔离**不是沙箱**：server 进程照样能读进程用户能读的一切。它只关掉"相对路径解析到 agent 仓库"这一条缝；部署侧的真正隔离靠容器只读根文件系统与非 root 用户。
- 覆盖层文件含部署方的真实路径与凭据引用，**不入库**；`WATER_AGENT_MCP_OVERLAY` 指向哪里由运维决定，代码只校验它存在且合法。
- trace 摘要只写 server id、工具名与错误码，不写 detail 文本；若以后要把跳过原因写进 trace，必须先过 trace 的脱敏，并评估第三方 stderr 的内容风险。
- 门禁里仍有一个与 P7 无关的间歇性失败（多 Agent CLI 测试偶发退出码 1，另起任务排查）；本 PR 若被它打红，重跑即可，需逐次确认是同一现象。
