# 给排水 Agent P7 项目任务书：工具插件化与 MCP 扩展

**版本**：V1.0
**日期**：2026-10-02
**状态**：S0 已交付（规格 + 防漂移 harness）；S1 起每一步开工前，该步执行 Prompt 必须先合入 main
**阶段编号**：P7，实施步骤 S0–S7
**权威关系**：本文档是 P7 范围、步骤顺序、完成定义与评审决策的单一权威来源；插件契约、不变量、
验收 → 测试映射与实施协议以 `docs/design/16-p7-plugin-mcp.md` 为准。MCP 协议与治理边界仍以
`docs/design/14-p4-mcp-gateway.md` 为准，本文档不得放宽其中任何约束。

---

## 0. 先给结论

P7 要做两件事：

1. **让 agent 用上 MCP**。网关地基（P4 Step 23–25）已经在了：stdio 客户端、配置驱动的桥接、
   `allow` 白名单 + `MCP_INVOKE` 两把钥匙、描述安检、schema 子集，并接进了 CLI / Web 共用的
   `AgenticSession`。缺的是：确认门执行期不检查（F7）、每个 Web run 重新 spawn 子进程（F8）、
   只有 stdio、没有管理入口。
2. **插件化**。参考 dsh（Deepseek Harness Desktop，基于 Cordis 插件框架）：每台 MCP server 是
   `dsh-mcp-client` 插件的一个实例，配置分层叠加，按实例启停。本项目的对应做法是：
   - 模型可调用的工具全部由插件贡献；
   - 面向模型的装配只有 `assemble_tools()` 一个入口；
   - 内置工具是内置插件，每台 MCP server 是 MCP 插件的一个实例。

P7 承诺：

- 插件化重构不改变模型可见面，由装配快照逐字节证明；
- 每一步的改动不越出声明的范围，由范围守卫判定；
- 规格里每条已交付的验收都有活着的测试，由追溯检查判定。

P7 **不承诺**：

- 已接通真实 CAD/BIM 或企业库（P4 Step 26 仍是 blocked）；
- 本地工具 schema 还能再加工具（余量只剩 3 字符，见 F4）；
- 第三方进程内插件的安全性（进程内代码不受护栏约束，第三方能力一律走 MCP）。

---

## 1. 前提、事实与假设

### 1.1 已确认的事实

F1–F14 的事实表在 `docs/design/16-p7-plugin-mcp.md` §2，每条数字都由测试锁住，这里不重复抄录。对排期影响最大的五条：

| # | 事实 | 影响 |
|---|---|---|
| F4 | 本地工具 schema 4497/4500，余量 3 | 不做按意图最小暴露（S4），一个工具都加不进去 |
| F5 | 工具指引已截掉 `read_drawing`，MCP 工具总是先被截掉 | 插件越多，模型越"看不见"工具 |
| F7 | 确认门执行期不检查 | 安全修复单独成步，排在 Step A 之前（S1） |
| F2 | runner real-agentic 路径另有一套装配，8 处差异 | 评审决定：Step A 中统一成会话语义 |
| F14 | worktree 里不设 PYTHONPATH 时，测试与门禁验证的是主检出的代码 | S0 首先堵住（H0），否则后面所有证据都不可信 |

### 1.2 设计假设

- 单实例自托管部署；多 worker 时 MCP 长驻进程的共享语义在 S5 另定。
- 官方 `mcp` SDK 仍不可用（P4 实测 `pypi.org` 不可达），客户端继续用标准库实现。
- 每个 run 新建一次会话：插件配置"下一个 run 生效"就够了，不需要热卸载与重挂载。

---

## 2. 目标与非目标

### 2.1 Goal

- G1：面向模型的工具装配只有一个入口，内置工具与 MCP server 都是插件（S2、S3）。
- G2：插件分层配置、按实例启停；部署方能在不改代码的情况下接入 / 停用一台 MCP server（S3）。
- G3：MCP 可用性：危险操作确认门（S1 封住、S7 接通）、长驻连接与 Streamable HTTP（S5）。
- G4：管理入口：Web 扩展面板，只能改启停、白名单与凭据引用（S6）。
- G5：全程不漂移：H0–H5 六道闸（规格 §6）。

### 2.2 非目标

- 不做 Cordis 式的通用依赖注入框架。
- 不扫描任意目录加载插件；entry point 只收白名单。
- Web 不能写 MCP server 的 `command` / `args`。
- 不改三条铁律在任何工具上的落点；不上调任何预算上限。

---

## 3. 参考：dsh 的做法与本项目的取舍

| dsh 机制（本机 `~/.dsh` 与安装包实读） | 本项目对应 | 取舍原因 |
|---|---|---|
| 插件 = `{name, inject, apply(ctx, config)}`，服务靠 provide/inject 协作，可卸载重挂 | `ToolPlugin.contribute(ctx) -> PluginContribution`，两阶段装配 | 每个 run 新建会话，不需要生命周期框架 |
| 每台 MCP server 是 `@deepseek-ai/dsh-mcp-client` 的一个实例：`{id, name, config:{serverName, transport, command, args, env}, disabled}` | `McpGatewayPlugin` 的实例（S3 起每台一个） | 直接借鉴：MCP 不是特殊子系统，而是插件的一种 |
| 配置分层：bundle → profile 的 `cordis.patch.yml` → `--patch` 覆盖层；global / profile 两个作用域 | 仓库默认配置 + 部署覆盖层（S3） | 借鉴分层；每层的 `allow` 整体替换、不合并 |
| 管理面板直接读写补丁文件，支持从 Claude Code / Codex / Cursor 导入 `mcpServers` | Web 扩展面板（S6） | **不照搬**：dsh 是单机桌面，面板可以写 `command`；我们是多用户服务，面板只能改启停 / 白名单 / 凭据引用，导入只生成禁用的待审提案 |
| 改完提示重启或热重挂 | 下一个 run 生效 | 每个 run 本来就重新装配 |

---

## 4. 分步实施（一步一个 PR，一个逻辑面一个 commit）

每一步的分支名都是 `YEK/p7-s<n>-<名>`，从最新 `origin/main` 拉出。S1 起，该步执行 Prompt 必须**先在 main 上**，
范围守卫从 merge-base 读取范围块（分支改不了自己的范围）。

### S0：规格与防漂移 harness（已交付）

**产出**：

- 本任务书；
- 规格 `docs/design/16-p7-plugin-mcp.md`；
- S1 / S2 执行 Prompt；
- H0 导入位置守卫；
- H1 装配快照与基线 `data/eval/tool_assembly_main.json`（在**未改动的** main 装配代码上采集）；
- H2 追溯检查；
- H3 范围守卫及其 PR workflow；
- 事实校正（AGENTS.md 与测试 docstring 里过期的 4370/130）。

**验收**：规格 §7 的 P7-S0-A01…A33 全部 delivered；`ci_gate.sh` EXIT=0；范围守卫以 `--bootstrap` 对附录里 S0 的范围块通过。

### S1：确认门执行期强制（F7）

**产出**：

- 在 HITL（S7）接线前，配置里 `confirmation_handler_ready=true` 一律拒绝；
- 执行期护栏对 `requires_human_confirmation` 的绑定拒绝调用；
- 快照**有意**变更，并递进 `TOOL_POLICY_VERSION`。

**执行 Prompt**：`docs/execution-prompts/P7-S1-confirmation-gate.md`。

**验收**：规格 §7 的 P7-S1-*。

### S2：插件契约与单入口（Step A）

**产出**：

- `src/water_agent/plugins/`：契约、内置插件、MCP 插件、`assemble_tools()`；
- `AgenticSession` 与 runner real-agentic 路径都改用它；
- runner 统一成会话语义（规格 §3.5）；
- 结构不变量测试（P7-I01…I07）。

**执行 Prompt**：`docs/execution-prompts/P7-S2-tool-plugin-assembly.md`。

**验收**：

- 会话侧快照 0 diff；
- runner 侧只有 §3.5 声明的生命周期字段变化；
- 规格 §7 的 P7-S2-*；
- 有 Key 时重跑 rag_p1 real，没有 Key 记为 blocked。

### S3–S7（开工前各自写执行 Prompt，先合入 main）

| 步 | 对应 | 内容 | 关键门槛 |
|---|---|---|---|
| S3 | Step B | 配置分层（仓库默认层 + 部署覆盖层）、`disabled`、子进程工作目录隔离、每台 server 结果可观察；修 F6 / F9 / F10 / F15 / F16；补真正落盘的装配摘要 trace 事件（F13 的后续）。执行 Prompt：`docs/execution-prompts/P7-S3-layered-mcp-config.md` | `MCP_CONFIG_VERSION` 递进；模型可见面不变（`TOOL_POLICY_VERSION` 不递进） |
| S4 | Step C | 按意图最小暴露 / 工具分组；修 F4 / F5 | 先建 `tool_exposure_p0`；`PROMPT_TEMPLATE_VERSION` 递进；快照有意重写 |
| S5 | Step D（一） | MCP 长驻连接（stdio，修 F8）。执行 Prompt：`docs/execution-prompts/P7-S5-shared-mcp-connections.md` | 共享须按 server 显式声明 `lifecycle: shared`（缺省 `per_run`）；客户端请求串行；按单台 server 指纹重载；出站清单不变 |
| S6 | Step E1 | Web 扩展面板 | admin + CSRF；不能写 `command` / `args` |
| S7 | Step E2 | HITL 确认门（外层图 interrupt、waiting 态、批准 / 拒绝 API、审计、前端卡片）。执行 Prompt：`docs/execution-prompts/P7-S7-hitl-confirmation.md` | 内层 react agent 无 checkpointer，确认门必须在外层图；一次性执行凭证；只有 Web 入口接线 |
| S8 | Step D（二） | Streamable HTTP 传输（2026-10-06 从 S5 拆出） | `NETWORK_FETCH` 能力决策；出站清单 7→8 |

---

## 5. 防漂移机制（摘要；完整定义见规格 §6）

| 闸 | 防的是什么 | 怎么判定 |
|---|---|---|
| H0 导入位置守卫 | 测试和门禁验证的不是本分支的代码 | conftest 收集前核对；`ci_gate.sh` 钉 PYTHONPATH 并核对 |
| H1 装配快照 | 重构悄悄改了模型看到的工具、顺序、提示词或主体能力 | 15 个 profile 与入仓基线逐字段比对；surface 变化必须递进版本号 |
| H2 需求 ↔ 测试追溯 | 规格写着"已交付"，测试里找不到 | delivered 行的测试必须存在、未 skip、docstring 写明 ID；反向检查未定义 ID |
| H3 改动范围守卫 | 实施时越界改文件、改受保护断言、改冻结的基线片段、放宽自己的范围 | 从 merge-base 读范围块，核对 PR 的全部改动 |
| H4 结构不变量 | 有人另开第二条装配路径 | AST 判定 P7-I01…I07（随 S2 落地） |
| H5 既有门禁 | 机制层回退 | `ci_gate.sh` 全套 + 入仓基线对比 |

---

## 6. 测试策略与验收映射

- 单元与机制层：每条验收映射到规格 §7 的测试节点。
- 门禁：H0 / H1 / H2 的测试随 pytest 进入 `ci_gate.sh`；H3 由 `.github/workflows/p7-scope.yml` 在 PR 上运行。
- 质量层：真实模型的工具选择与回答质量不在离线门禁内。S2 有 Key 时重跑 `rag_p1 --variant real --judge`，
  S4 先建 `tool_exposure_p0`。

---

## 7. 完成定义（Definition of Done，每一步）

- 该步在规格 §7 的验收行全部翻成 `delivered`，`tests/test_p7_traceability.py` 通过；
- `bash scripts/ci_gate.sh` EXIT=0（worktree 内同样有效：H0 已钉住导入位置）；
- `python scripts/check_step_scope.py <该步执行 Prompt> --base origin/main` 通过，PR 上的 `p7-scope` 检查通过；
- 快照：
  - 没有变化时，`--check` 输出一致；
  - 有意变化时，版本号已递进，`--diff` 已贴进决策记录；
- 决策记录 `docs/design/p7-step-<n>-decision.md` 按规格 §8 的结构写完；
- AGENTS.md 补一条，只写可验证的事实，并注明锁定它的测试；
- 汇报按规格 §8 的格式：改动、验证命令与实际输出、范围守卫输出、快照差异、未验证项、残余风险。

---

## 8. 已知限制与 blocked 项

- 真实 CAD/BIM、企业库 MCP server 未验证（P4 Step 26）。
- 真实质量层需要 Key 与网络，离线门禁覆盖不到；没有 Key 时一律记为 blocked。
- 范围守卫只在 PR 上运行；直接推到 main 的提交不受它约束。
- 追溯检查不证明断言写得对（规格 §6.3）。

---

## 9. 回滚与特性开关

- S0：纯新增（文档、harness、测试、workflow），`git revert` 即可回滚；对运行时零影响。
- S1：回滚即恢复 F7 的现状（确认门执行期不检查）；不建议单独回滚。
- S2：插件化是内部重构，回滚即恢复两套装配；装配快照可证明回滚前后模型可见面一致。
- S3 起：新能力一律在配置里默认关闭（文件不存在 = 功能关闭，沿用 P4 约定）。

---

## 10. 评审决策记录（2026-10-02）

| 决策 | 结论 | 理由 |
|---|---|---|
| 阶段编号与规格落点 | P7；规格 `docs/design/16-p7-plugin-mcp.md` | 插件化覆盖全部工具，不只是 MCP；P4 续编会与 P4.5 的 Step 27–34 撞号 |
| F7 安全修复 | 单独一个小 PR，排在 Step A 之前 | 改动小、好审；顺带首次演练快照"有意变更"流程 |
| runner 第二装配路径 | Step A 中统一成会话语义 | 消除第二条装配路径；代价是真实质量层需要有 Key 时重跑才能证明不回退 |
| 执行 Prompt | 入库 `docs/execution-prompts/`，中性工程措辞 | 范围守卫要从 main 读取范围块，执行 Prompt 必须入库 |
| runner 是否接回答缓存 | 不接（规格 §3.5 第 8 条） | 评测不得命中回答缓存；它属于 app 构造，不属于工具装配 |
| `docs/P5_TASK_BRIEFING.md` 里"P7：批量处理性能优化" | 标注编号已改用，原设想待重新编号 | 避免同号两义 |
| （2026-10-05）入仓 `data/mcp_servers.json` | 改为空骨架，真实 server 写进部署覆盖层；autocad 配置挪到示例文件 | 入仓配置写死了个人机器路径，任何其他机器上都起不来 |
| （2026-10-05）entry point 发现 | 推迟，不在 S3 | 没有任何外部插件包可验证；进程内插件不受护栏约束，等出现真实需求再做 |

---

## 附：S0 范围块

S0 首次引入本文档，用 `python scripts/check_step_scope.py docs/给排水agentP7插件化与MCP扩展任务书.md --bootstrap --base origin/main` 核对。

<!-- step-scope -->
```json
{
  "step": "P7-S0",
  "allow": [
    "AGENTS.md",
    "docs/给排水agentP7插件化与MCP扩展任务书.md",
    "docs/design/16-p7-plugin-mcp.md",
    "docs/execution-prompts/P7-S1-confirmation-gate.md",
    "docs/execution-prompts/P7-S2-tool-plugin-assembly.md",
    "docs/P5_TASK_BRIEFING.md",
    "src/water_agent/harness/tool_assembly_snapshot.py",
    "src/water_agent/harness/spec_trace.py",
    "data/eval/tool_assembly_main.json",
    "scripts/check_step_scope.py",
    "scripts/ci_gate.sh",
    "tests/conftest.py",
    "tests/test_import_guard.py",
    "tests/test_tool_assembly_snapshot.py",
    "tests/test_p7_known_figures.py",
    "tests/test_p7_traceability.py",
    "tests/test_check_step_scope.py",
    "tests/test_answer_cache.py",
    ".github/workflows/p7-scope.yml"
  ],
  "forbid": [
    "src/water_agent/runtime/**",
    "src/water_agent/tools/**",
    "src/water_agent/mcp/**",
    "src/water_agent/graphs/**",
    "src/water_agent/prompting/**",
    "src/water_agent/webapp/**",
    "src/water_agent/plugins/**",
    "data/eval/baseline.json",
    "web/**"
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
    }
  ]
}
```
