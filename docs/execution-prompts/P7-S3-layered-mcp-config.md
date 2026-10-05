# P7-S3 执行 Prompt：MCP 配置分层、按实例启停与工作目录隔离（Step B）

> **工作区**：仓库 `YEKYYY0312/water-SAD-agent`，基于最新 `main`（必须已包含 P7-S2 与本 Prompt）。新建分支 `YEK/p7-s3-layered-mcp-config`。
> **必读**：
> - `AGENTS.md`
> - `docs/design/16-p7-plugin-mcp.md`：§2 事实表（F6/F9/F10/F13/F15/F16）、§3 插件契约、§5 S3 约束、§6 harness、§7 的 P7-S3 验收、§8 实施协议
> - `docs/design/14-p4-mcp-gateway.md` §3（配置契约）、§4（治理绑定）
> - `docs/design/p7-step-2-decision.md`（S2 的快照基线与残余风险）

---

## 0. 相对规格的修订（以本文为准）

规格 §5 的 S3 行写于 S0，下面几处依据 2026-10-05 的实测与评审决定修订。规格已在本 Prompt 的同一个 PR 里同步更新：

| 原计划 | 修订 | 原因 |
|---|---|---|
| S3 含"受信 entry point 发现" | **推迟**，S3 不做 | 评审决定（2026-10-05）：没有任何外部插件包，做了只能对着假包测；进程内插件不受护栏约束，正是要等真实需求再做的安全面。第三方能力继续经 MCP（进程隔离）接入 |
| S3 修 F10（入仓 `allow` 与上游不符） | 入仓 `data/mcp_servers.json` 改为**空骨架**，autocad 配置挪到 `data/mcp_servers.example.json`（占位路径） | 评审决定：入仓配置写死了个人机器路径 `/Users/yaerken/autocad-mcp/...`，其他机器一律起不来；真实 server 写进部署覆盖层，不入库 |
| （新增）F15 | S3 修 | 实测：MCP 工具参数里的**相对路径**（如 `secrets/deepseek_api_key`）护栏放行，而子进程继承 agent 的工作目录，相对路径正好解析到 agent 自己的仓库或 `/app` |
| （新增）F16 | S3 修 | 实测：autocad-mcp 启动即崩不是 server 坏了——它以 `-m autocad_mcp` 启动，需要在自己的仓库目录下运行，而配置没法指定工作目录。`cwd` 指到它的仓库后 `tools/list` 正常返回 `read_dwg` |
| 预算测试"计入 MCP 工具" | S3 **不新增**预算测试 | 本地 + 两个 fake MCP 工具的 schema 总长（4724）已由 S0 的 `tests/test_p7_known_figures.py::test_tool_guide_already_truncates_tools` 锁定；给它设上限属于按意图裁剪，是 S4 的范围 |

## 1. 目标与边界

**做**（编号对应规格 §7 的 P7-S3-Axx）：

1. **配置模型（A01）**：`MCPServerConfig` 新增两个字段：
   - `disabled: bool = False`
   - `cwd: str | None = None`：模型层只校验它是**绝对路径**，相对路径一律拒绝。
   - 同时收紧 `command`：要么是绝对路径，要么是不含 `/` 的裸命令名（按 `PATH` 查找）。含 `/` 的相对路径一律拒绝，因为子进程工作目录改成临时目录后，它的含义会悄悄改变。
2. **加载期校验（A02，修 F9）**：
   - `env_file` 的相对路径按**定义它的那一层配置文件所在目录**解析；
   - `env_file` 指向的文件、`cwd` 指向的目录不存在，都在**加载期**报 `MCP_CONFIG_INVALID`，不再拖到连接时被 `open_bridge` 吞成"这台 server 跳过"。
3. **配置分层（A03、A04）**：
   - `load_gateway_config(path)` **保持单层语义不变**（测试与示例校验用）。
   - 新增 `load_layered_gateway_config()`，`wiring.load_mcp_gateway()` 改为调用它，所以会话、runner 和 Web 能力端点都会自动生效。分两层：
     - 默认层：`default_config_path()`（仍尊重 `WATER_AGENT_MCP_CONFIG`）；
     - 覆盖层：环境变量 `WATER_AGENT_MCP_OVERLAY` 指向的文件，不入库。
   - 合并规则（纯函数 `merge_overlay(base: dict, overlay: dict) -> dict`，最后统一过 `MCPGatewayConfig.model_validate`）：
     - 覆盖层顶层只允许 `version`、`servers` 两个键。写了 `confirmation_handler_ready`，或任何其他键，都是 `MCP_CONFIG_INVALID`，S1 的限制不能从覆盖层绕开。
     - 覆盖层每台 server 必须有 `server_id`，其余字段可选：
       - 默认层没有这个 id，就新增一台 server，合并后按完整校验；
       - 默认层有这个 id，就逐字段覆盖，没写的字段沿用默认层。
     - **列表与字典字段（`allow`、`args`、`env`）整体替换，不合并**。覆盖层不能把白名单合并成比两层都更宽的集合。
     - 文件缺失的处理：

       | 默认层 | 覆盖层 | 结果 |
       |---|---|---|
       | 有 | 无 | 只用默认层 |
       | 缺失 | 有 | 默认层按空骨架处理，覆盖层照常合并（镜像里本来就不带默认层文件，见 `.dockerignore`） |
       | 缺失 | 无 | `None`，功能关闭 |

     - `WATER_AGENT_MCP_OVERLAY` 给了、但文件不存在：`MCP_CONFIG_INVALID`。
4. **按实例启停（A05）**：`disabled=true` 的 server：
   - 不 spawn，也不暴露任何工具；
   - 在 `skipped_servers` 里记一条新错误码 `MCP_SERVER_DISABLED`（加进 `MCP_ERROR_CODES` 和 `_CODE_HINTS`）；
   - 停用判定先于确认门判定，所以停用的 server 不再报 `MCP_CONFIRMATION_UNAVAILABLE`；
   - **不进 `manifest_hash`**。全部停用时，manifest 与"没有 server"一样是 `""`。
5. **子进程工作目录隔离（A06，修 F15、F16）**：
   - 配置了 `cwd` 就在该目录下启动；
   - 没配置时，每次连接新建一个空临时目录（`tempfile.mkdtemp(prefix="wa-mcp-")`）作为工作目录。连接关闭时删除它，**启动失败时也要删**。
   - `cwd` 会进入 `manifest_hash`。
   - **这不是沙箱**：server 进程照样能读进程用户能读的一切，隔离要靠容器。这里只堵一条具体的缝：模型借 MCP 工具的相对路径参数读 agent 自己的文件。绝对路径、`~`、`../` 已经被护栏拦下，相对路径护栏刻意不管（见 `p2.5-threat-model.md` §5 第 23 条）。
6. **F6：manifest 进缓存键（A07）**：
   - `_build_cache_service(args, *, mcp_server_manifest="")` 新增这个关键字参数，默认值不变，所以 `cli.py` 的旧调用不用改；
   - `AgenticSession` 构造缓存服务时传入 `self.mcp_assembly.manifest_hash`。
7. **F13 后续：装配摘要真正写进 trace（A08、A11）**：
   - 会话与 runner 都要在 trace **开始之后**补记一条 `node="mcp", event="mcp_assembly"`。
   - 事件内容是新的 `MCPGatewayAssembly.trace_summary()`：只含 server id、暴露的工具名、跳过的错误码，以及 manifest 是否非空。**不含 `detail` 文本**，因为 detail 里可能有第三方 server 的 stderr 尾部和本机路径。
   - 装配里既没有暴露的工具、也没有跳过或停用记录时，不写这条事件。入仓默认层是空骨架，不写就不会给每个 run 加噪声。
   - 已核对的实现要点：
     - Web 路径由 `RunManager` 在建会话后调用 `start_trace`，`aask` 也会调。在 `start_trace` **真正启动** recorder 的那一次补记即可。
     - CLI 同步路径 `ask()` **不调** `start_trace`，trace 由图的 `agent_loop` 开启。要让 `ask()` 先调 `start_trace`（与 `aask` 对齐），否则事件会在 recorder 启动前写入，重演 F13。
     - runner 的 recorder 也是由 `agent_loop` 开启的。要在 `invoke` 之前先 `recorder.start(...)` 再记事件。
8. **每台 server 的结果可观察（A09）**：`McpContribution` 能逐台读出每台 server 的结果（暴露、跳过或停用）。插件 id 仍是 `mcp_gateway`，`BUILTIN_PLUGIN_ORDER` 不变。
9. **F10：入仓配置改为空骨架（A10）**：
   - `data/mcp_servers.json` 改为 `{"version": <新的 MCP_CONFIG_VERSION>, "confirmation_handler_ready": false, "servers": []}`。
   - 新增 `data/mcp_servers.example.json`，作为部署方写覆盖层的参考，**不被任何代码加载**，不含任何真实机器路径。内容为一台 autocad server：
     - `command: "/path/to/autocad-mcp/.venv/bin/python"`、`args: ["-m", "autocad_mcp"]`；
     - `cwd: "/path/to/autocad-mcp"`；
     - `allow: ["read_dwg"]`。
   - 在 `.dockerignore` 的 data 拒绝清单里加一行，排除示例文件。
   - 从 `tests/test_mcp_wiring.py` 删掉依赖入仓 autocad 配置、在任何机器上都恒为 skip 的**两条**用例：`test_autocad_exposes_eight_tools`、`test_autocad_call_and_registry_invoke`。
   - 把 `test_default_config_loads` 改写为 `test_default_config_is_empty_skeleton`，并新增 `test_example_config_validates`。后者只过模型层校验（`MCPGatewayConfig.model_validate`），因为占位路径在哪台机器上都不存在；同时断言 `.dockerignore` 排除了示例文件。

**不做**：

- entry point 发现、进程内第三方插件（推迟）；
- 长驻 MCP 连接、Streamable HTTP（S5）；
- 按意图最小暴露、MCP 工具 schema 上限（S4）；
- Web 面板（S6）、HITL（S7）；
- **不给 MCP 工具开放读目录**。实测 `read_dwg` 的绝对路径参数会被护栏以 `tool.path_not_allowed` 拦下：MCP 工具走 `allowed_export_dirs`，默认为空。S3 之后 autocad 能连上、`read_dwg` 能暴露，但只有当图纸放在 `cwd` 下、模型传相对路径时才调得通。要不要给 `MCP_INVOKE` 配读目录白名单是单独的能力决策。
- Web 能力端点的 `configured_server_count` 会把停用的 server 也算进去。S3 不改，因为 `webapp/` 在 forbid 里，留给 S6。
- `tests/test_mcp_wiring.py` 里另外两条 autocad 用例（`test_validate_dwg_version_accepts_none`、`test_autocad_save_as_dwg_mocked_odafc`）测的是 autocad-mcp 仓库自己的内部函数，不读入仓配置，与 S3 无关，不动。
- 不改任何本地工具的描述与 schema；**不新增装配快照 profile**。`frozen_json` 冻结了 `/profiles/*/surface`，新增 profile 会被范围守卫判成"改动了冻结片段"。覆盖层和停用用单元测试覆盖。

## 2. 铁律与治理落点（不得违反）

- **失败要响亮**：配置错误一律在**加载期**报 `MCP_CONFIG_INVALID`，不得降级成"这台 server 跳过"。包括覆盖层坏、覆盖层写了 `confirmation_handler_ready`、`cwd` 或 `command` 非法、`env_file` 或 `cwd` 不存在。
- **白名单 fail closed 不变**：合并后 `allow=None` 仍然全拒。
- **护栏不放宽**：本步不改 `GuardrailPipeline`。F15 修在进程边界，不靠收紧 `_looks_like_path`。那个正则刻意保守，收紧会误伤 `DN200/DN150` 这类写法。
- **评测先行**：每条验收先写会失败的测试，再改实现。

## 3. 必须复用的现有组件（不得另写一份）

| 需要 | 用这个 | 位置 |
|---|---|---|
| 配置校验 | `MCPGatewayConfig` / `MCPServerConfig`（`extra="forbid"`、`frozen`） | `mcp/config.py` |
| 单层加载与错误码 | `load_gateway_config` / `MCP_CONFIG_INVALID` | `mcp/config.py`、`mcp/errors.py` |
| 子进程 env | `child_env(config, env_file_dir=...)` | `mcp/config.py` |
| 装配产物与摘要 | `MCPGatewayAssembly` / `compute_manifest_hash` | `mcp/wiring.py` |
| MCP 插件 | `McpGatewayPlugin` / `McpContribution` | `plugins/mcp_gateway.py` |
| 缓存服务 | `AnswerCacheService(mcp_server_manifest=...)`（参数早就有，只是没人传） | `runtime/governance/cache_service.py` |
| 快照与守卫 | `tool_assembly_snapshot --diff / --write --reason`、`check_step_scope.py` | `harness/`、`scripts/` |

## 4. 实施步骤（一步一个 commit；每个 commit 结束时快照 `--check` 必须一致）

### Step 1：配置模型 + 加载期校验（A01、A02）

- 加字段与校验；把 `env_file` 和 `cwd` 的解析与存在性检查放进 `load_gateway_config`：先在 dict 里把相对的 `env_file` 改成绝对路径，再做模型校验。模型本身不感知文件位置。
- 递进 `MCP_CONFIG_VERSION`。同一个 commit 里用 `--write --reason` 重写快照基线：`/versions/MCP_CONFIG_VERSION` 会变化，它不在冻结片段里。
- 新文件 `tests/test_mcp_layered_config.py`，写 A01、A02 的测试。

### Step 2：配置分层（A03、A04）

- 新增 `merge_overlay` 和 `load_layered_gateway_config`，`wiring.load_mcp_gateway()` 改为调用后者。
- 把 `WATER_AGENT_MCP_OVERLAY` 加进 `tool_assembly_snapshot.PINNED_ENV_KEYS`，否则本机设了覆盖层时快照会漂移。

### Step 3：停用 + 工作目录隔离（A05、A06）

- 停用：在 `open_gateway_sessions` 里，确认门判定之前就跳过。`compute_manifest_hash` 只对未停用的 server 求摘要。
- 新错误码 `MCP_SERVER_DISABLED` 让 MCP 稳定错误码从 13 个变成 14 个（只增不减，符合"不缩小"的约定），Step 6 同步 `14-p4-mcp-gateway.md` §2.3 与 AGENTS.md 里的计数。
- 工作目录：`MCPStdioClient._spawn` 传入 `cwd`；临时目录在 `_terminate` 和启动失败路径里都要删掉。
- `tests/fixtures/fake_mcp_server.py` 新增 `--mode cwd`：tools/list 只返回一个只读工具 `read_relative`，它报告 `os.getcwd()`，以及参数里的相对路径是否存在。**不得改变 `--mode ok` 的 tools/list**，快照依赖它。
- F15 的反证测试：
  - 不配 `cwd` 时，调 `read_relative(path="pyproject.toml")`，结果是不存在。测试进程的工作目录是仓库根，那里有这个文件。
  - 配 `cwd=<仓库根>` 时，结果是存在。
  - 测试里**不要**用 `secrets/` 这类敏感路径做例子。

### Step 4：F6 + F13 后续 + 每台 server 可观察（A07、A08、A09、A11）

- 按 §1 第 6、7、8 条实现。
- 快照 harness：把 `cache_service.mcp_server_manifest` 改成只记录"是否非空"，字段名为 `mcp_server_manifest_nonempty`，与 `manifest_hash_nonempty` 同一做法。manifest 摘要里含 `sys.executable` 和临时路径，原样写进基线会在不同机器上不一致。然后用 `--write --reason` 重写基线。**surface 不得变化**，也不递进 `TOOL_POLICY_VERSION` 和 `PROMPT_TEMPLATE_VERSION`。
- `tests/test_p7_known_figures.py` 的改法，沿用 S1、S2 处理"现状测试"的方式：
  - `test_mcp_manifest_never_reaches_the_cache_service` 改写为 `test_mcp_manifest_reaches_the_cache_service`（A07）。规格里 **P7-S0-A14** 改为 `untested`，在"未覆盖项"注明由 P7-S3-A07 取代。
  - `test_no_assembly_trace_calls_that_never_land` **保留函数名**，断言从"没有任何装配期 trace 调用"改为"每一次装配期 trace 调用都已真正写入"。docstring 同时写 P7-S2-A12 和 P7-S3-A11。

### Step 5：入仓配置改为空骨架 + 示例文件（A10）

- 按 §1 第 9 条。

### Step 6：收尾

- 规格 §2 事实表更新 F6、F9、F10、F13、F15、F16；§5 的 S3 行翻成 delivered；§7 的 S3 验收行翻成 delivered，验证项写成真实存在的测试节点。
- 写 `docs/design/p7-step-3-decision.md`。
- `docs/design/14-p4-mcp-gateway.md` §3 补上分层、停用、`cwd` 的配置契约。
- AGENTS.md 补一条，并更新"P4 MCP 客户端网关"条目里关于入仓配置的描述。

## 5. 范围块

<!-- step-scope -->
```json
{
  "step": "P7-S3",
  "allow": [
    "src/water_agent/mcp/config.py",
    "src/water_agent/mcp/client.py",
    "src/water_agent/mcp/wiring.py",
    "src/water_agent/mcp/errors.py",
    "src/water_agent/mcp/__init__.py",
    "src/water_agent/plugins/mcp_gateway.py",
    "src/water_agent/plugins/__init__.py",
    "src/water_agent/runtime/agentic.py",
    "src/water_agent/harness/runner.py",
    "src/water_agent/harness/tool_assembly_snapshot.py",
    "data/mcp_servers.json",
    "data/mcp_servers.example.json",
    "data/eval/tool_assembly_main.json",
    ".dockerignore",
    "tests/fixtures/fake_mcp_server.py",
    "tests/test_mcp_layered_config.py",
    "tests/test_mcp_wiring.py",
    "tests/test_mcp_gateway.py",
    "tests/test_mcp_behaviors.py",
    "tests/test_tool_plugins.py",
    "tests/test_p7_known_figures.py",
    "tests/test_tool_assembly_snapshot.py",
    "docs/design/p7-step-3-decision.md",
    "docs/design/16-p7-plugin-mcp.md",
    "docs/design/14-p4-mcp-gateway.md",
    "docs/execution-prompts/P7-S3-layered-mcp-config.md",
    "AGENTS.md"
  ],
  "forbid": [
    "src/water_agent/tools/**",
    "src/water_agent/prompting/**",
    "src/water_agent/runtime/governance/**",
    "src/water_agent/graphs/**",
    "src/water_agent/webapp/**",
    "src/water_agent/cli.py",
    "src/water_agent/plugins/assembly.py",
    "src/water_agent/plugins/base.py",
    "src/water_agent/plugins/builtin.py",
    "src/water_agent/harness/spec_trace.py",
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

- 本地工具 schema `< 4500`、系统提示 `< 1950`、治理覆盖率 `{7, 7, 5, 5}`。本步不增减任何本地工具。
- S1 的两条确认门断言。覆盖层不得成为绕开确认门的通道。

## 7. 停止条件

满足任一条就停下，写清现象后等确认：

- 任何 profile 的 `surface` 出现 diff：本步不应改变模型看到的任何东西；
- 需要改 `GuardrailPipeline` 或任何护栏规则才能修好 F15：说明修复位置选错了；
- 需要改范围块 `forbid` 里的文件、`allow` 之外的文件，或受保护断言；
- 现有测试依赖"子进程继承 agent 工作目录"的旧行为；
- 门禁出现与本步无关的红灯。

## 8. 验证命令（完成前全部跑过，贴出输出）

```bash
export VENV=/Users/yaerken/Desktop/给排水agent/_venv PYTHONPATH=src
git fetch origin main
$VENV/bin/pytest -q tests/test_mcp_layered_config.py tests/test_mcp_wiring.py tests/test_mcp_gateway.py tests/test_mcp_behaviors.py tests/test_tool_plugins.py tests/test_p7_known_figures.py tests/test_tool_assembly_snapshot.py tests/test_p7_traceability.py
$VENV/bin/python -m water_agent.harness.tool_assembly_snapshot --check
$VENV/bin/python scripts/check_step_scope.py docs/execution-prompts/P7-S3-layered-mcp-config.md --base origin/main
COMPOSE_PROJECT_NAME=p7s3 VENV=$VENV bash scripts/ci_gate.sh
```

手动验收：本机有 autocad-mcp 时做，没有就在决策记录里写明未做。

1. 照着示例文件，把真实路径写进一个临时覆盖层文件；
2. 执行下面的命令，应看到 `('mcp_autocad_mcp_read_dwg',)`：

```bash
WATER_AGENT_MCP_OVERLAY=<覆盖层文件> $VENV/bin/python -c "from water_agent.mcp.wiring import load_mcp_gateway, open_gateway_sessions; a = open_gateway_sessions(load_mcp_gateway()); print(a.exposed_tool_names); a.close()"
```

**这只证明能连上，不构成真实 CAD 验收**：P4 Step 26 仍是 blocked。

## 9. 完成定义

- 满足规格 §8 第 5 条的全部项；
- PR 上的 `p7-scope` 与 `p5-ci` 通过；
- 决策记录 §3 贴出 `--diff` 原文（Step 1 和 Step 4 各一次）。

## 10. 汇报格式

- 改了哪些文件；
- 验证命令与实际输出（失败照贴）；
- 范围守卫的输出；
- 快照差异；
- 未验证项（含手动验收是否做了）；
- 残余风险。

## 11. 常见坑（本仓库实测过）

- `.dockerignore` 是"默认全拒 + 显式放行，最后一条匹配为准"。`!data/` 会把 data 下没被再次拒绝的文件都放进镜像，所以新加的 `data/` 文件要在拒绝清单里显式加一行。
- 运行时 MCP 绑定登记只增不删：测试里用独立的 server id，或用 `clear_mcp_tool_bindings` 清理。
- 本机如果设了 `WATER_AGENT_MCP_OVERLAY`，跑测试前先 `unset`。`conftest.py` 在 forbid 里，S3 不改它。
- worktree 里不设 `PYTHONPATH=src` 时，pytest 会被 H0 拦下（exit 4）。
- 本机终端可能连不上 GitHub（推送超时、Empty reply），推送失败时重试，或让终端走代理。
- **只含执行 Prompt 的文档 PR，分支名不能以 `YEK/p7-s<n>-` 开头**：`p7-scope` 的 `--auto` 会按分支名去 base 上找这一步的 Prompt，而它正是这个 PR 要引入的，必然判失败。文档 PR 用 `YEK/p7-docs-s<n>-prompt` 这类名字，实现分支才用 `YEK/p7-s<n>-<名>`。
