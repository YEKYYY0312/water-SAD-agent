# P7 规格：工具插件化与 MCP 扩展（SDD，V1.0）

> 本文档是 P7 的**权威规格**：插件契约、装配语义、不变量、验收 → 测试映射与实施协议
> 都以本文为准。范围、步骤顺序与评审决策见任务书
> `docs/给排水agentP7插件化与MCP扩展任务书.md`。MCP 协议与治理边界仍以
> `docs/design/14-p4-mcp-gateway.md` 为准，本文**不得放宽**其中任何约束。
>
> 三条铁律在本规格的实例化：
> ① **数值程序化**：插件不改变"计算只走程序化工具"——插件贡献的仍是 `ToolSpec`，执行仍经
>    `ToolRegistry.invoke`；MCP 返回的数值是外部数据，不得当公式口径。
> ② **引用可追溯**：插件化不改变 `cite_clauses` 的可追溯检查；检索与引用两个工具必须在同一个
>    插件里装配（它们靠 `context_updater` 共享"本轮检索到了什么"）。
> ③ **评测先行**：每一步先有可判定的验收（§7），再动代码；装配快照（§6.2）证明重构没有
>    改变模型可见面，范围守卫（§6.4）证明改动没有越界。
>
> **安全定位**：插件**不是权限主体**。插件化不新增任何能力、不放宽任何护栏；插件能追加给
> 主体的能力只有白名单里的 `MCP_INVOKE`（P7-I06）。第三方能力一律经 MCP（进程外隔离）接入；
> 进程内插件等同于引入一个依赖，只允许受信任的包（S3）。

---

## 1. 范围与目标

### Goal

1. **插件化**：模型可调用的工具全部由插件贡献；面向模型的装配只有 `assemble_tools()`
   一个入口（S2）。内置工具是内置插件，每台 MCP server 是 MCP 插件的一个实例（S3）。
2. **可配置**：插件分层配置（仓库默认 + 部署覆盖层），可按实例启停（S3）；Web 管理面板只能
   改启停、白名单与凭据引用（S6）。
3. **MCP 可用性**：长驻连接、Streamable HTTP（S5）；危险操作人工确认门（S1 先封住、S7 接通）。
4. **不漂移**：每一步的完成都有机器可判的证据——装配快照、需求 ↔ 测试追溯、改动范围守卫、
   结构不变量（§6）。

### 非目标（P7 不做）

- **不做通用依赖注入框架**。dsh 的 Cordis 有服务注入、热重挂载；本项目每个 run 新建一次会话，
  "下一个 run 生效"就是热重载，不需要卸载/重挂机制。
- **不扫描任意目录加载插件**。进程内插件代码不受护栏约束；发现机制只有内置 + 白名单内的
  entry point（S3）。
- **Web 不能写 `command` / `args`**。多用户服务里那等于把远程代码执行交给浏览器；dsh 是单机
  桌面应用才可以这样做（S6）。
- **不声称已接通真实 CAD/BIM 或企业库**。P4 Step 26 的真实 server 验证仍是 blocked，插件化
  不改变这一点。

---

## 2. 事实表（2026-10-02，main `27c925a`）

数字一律由测试锁住，文档不手抄；"锁定"一列给出锁定它的测试。F5/F6/F7/F13 与 F2 是**已知缺陷的
现状**，修复它们的步骤必须在同一个 commit 里同步改事实表、锁定测试与快照基线。

| # | 事实 | 锁定 | 处理 |
|---|---|---|---|
| F1 | 生产面向模型的装配只有 `AgenticSession.__init__`（`runtime/agentic.py`）；调用方：CLI 单次、CLI 交互、Web `build_agentic_session`、harness mock-agentic | 快照 `session_*` profile | S2 收编 |
| F2 | `HarnessRunner._run_real_agentic_case`（`harness/runner.py`）手工重建注册表，与会话路径有 8 处差异（§3.5） | `tests/test_p7_known_figures.py::test_runner_path_differs_from_session_path` | S2 统一 |
| F3 | 注册顺序决定提示词工具指引的顺序与截断；发给模型的工具按 `list_all_names()` 字母序 | 快照 `surface.registration_order` / `model_tool_order` | 不变量 |
| F4 | 5 个本地工具 schema 共 **4497** 字符，离 4500 上限只剩 **3**（`read_drawing` 单独 956）；此前文档里的 4370/130 已过期 | `tests/test_p7_known_figures.py::test_local_tool_schema_headroom_is_three_characters` | S4 |
| F5 | 工具指引 800 字预算已截掉 `read_drawing`（"省略 1 行"）；两个 MCP 工具时截掉 2 行、schema 4724 字符，且 `tests/test_answer_cache.py` 的预算只装配本地工具 | `tests/test_p7_known_figures.py::test_tool_guide_already_truncates_tools` | S4 |
| F6 | MCP 工具已暴露，缓存服务的 `mcp_server_manifest` 仍恒为 `""`（`_build_cache_service` 不传） | `tests/test_p7_known_figures.py::test_mcp_manifest_never_reaches_the_cache_service` | S3 |
| F7 | ~~`requires_human_confirmation` 只在装配期检查，执行期不查；`confirmation_handler_ready=true` 时这类工具直接暴露并可调用~~ **S1 已修**：确认门（S7）接线前 `confirmation_handler_ready=true` 在加载期即 `MCP_CONFIG_INVALID`；运行时绑定要求确认的工具执行期一律 `tool.confirmation_required` 拒绝 | `tests/test_p7_known_figures.py::test_confirmation_gate_is_enforced` | S1 已交付 |
| F8 | 每个 Web run 都重新 spawn 全部 MCP 子进程 | 无测试（`run_manager.py` 每个 run 新建会话） | S5 |
| F9 | `env_file` 相对路径按 cwd 解析；文件缺失时在 `connect()` 内抛错，被 `open_bridge` 转成该台 server 跳过——配置错误被静默降级 | 无测试 | S3 |
| F10 | 入仓 `data/mcp_servers.json` 的 `allow: ["read_dwg"]` 在上游 server 里不存在；`tests/test_mcp_wiring.py` 的相关用例已过期 | 无测试（依赖本机 autocad-mcp） | S3 |
| F11 | 两条测试按源码文本断言装配位置：`tests/test_read_drawing_tool.py`（`maybe_build_read_drawing_tool` 字面量）、`tests/test_web_runs.py`（`build_agentic_session` 字面量） | 测试本身 | S2 前者改为 AST 断言 |
| F12 | inventory 静态扫描只认 `tools/` 下 `ToolSpec(\n name="…"` 的写法；覆盖率锁死 `{7, 7, 5, 5}` | `tests/test_governance_contracts.py` | 不变量 P7-I05 |
| F13 | runner 的 `mcp_assembly` / `mcp_assembly_error` trace 事件尝试写入但**从未落盘**（recorder 尚未 start，异常被吞） | `tests/test_p7_known_figures.py::test_runner_mcp_trace_events_never_land` | S2 删除，S3 补真正落盘的装配摘要 |
| F14 | worktree 里不设 `PYTHONPATH=src` 时，pytest 与 harness 导入的是**主检出**的代码（editable install）；`ci_gate.sh` 此前不设 PYTHONPATH | `tests/test_import_guard.py::test_conftest_rejects_a_foreign_water_agent` | S0 H0 已修 |

---

## 3. 插件契约（S2 交付；S0 时为 planned）

### 3.1 类型

```python
# src/water_agent/plugins/base.py
GRANTABLE_CAPABILITIES = frozenset({Capability.MCP_INVOKE})          # P7-I06

@dataclass(frozen=True, slots=True, kw_only=True)
class AssemblyContext:
    retriever: Retriever                     # 已按需套好 RerankingRetriever 的检索器
    cite_context_source: Callable[[], set[str] | None] | None = None
    drawing_root: str | Path | None = None   # 原样透传："" = 关闭，None = 读 DRAWING_ROOT

@dataclass(frozen=True, slots=True, kw_only=True)
class PluginContribution:
    plugin_id: str
    tools: tuple[ToolSpec, ...] = ()
    status: Literal["active", "skipped"] = "active"
    detail: str = ""
    grants: frozenset[Capability] = frozenset()

class ToolPlugin(Protocol):
    plugin_id: str
    def contribute(self, ctx: AssemblyContext) -> PluginContribution: ...

# src/water_agent/plugins/assembly.py
@dataclass(frozen=True, slots=True)
class ToolAssembly:
    registry: ToolRegistry
    tools: list[BaseTool]                     # list_all_names() 顺序，全部绑定 registry
    principal: Principal
    contributions: tuple[PluginContribution, ...]

def assemble_tools(ctx: AssemblyContext, *, base_principal: Principal,
                   trace: Any | None, plugins: Sequence[ToolPlugin]) -> ToolAssembly: ...
```

### 3.2 装配语义（两阶段）

1. 按 `plugins` 的顺序调用 `contribute(ctx)`，**不捕获任何异常**（配置损坏、注册冲突都必须原样
   抛给调用方）。插件 id 重复即 `ValueError`。
2. 只有当某个插件**贡献了工具**且 `grants` 非空时，才用 `Principal(...)` 构造器（带校验）在
   `base_principal` 上叠加能力，保持 `kind` / `subject` / `auth_domain`；否则原样返回
   `base_principal`。`grants` 超出 `GRANTABLE_CAPABILITIES` 即 `ValueError`。
3. 用最终主体构造**一个** `ToolRegistry(principal=…, trace=…)`，按插件顺序、插件内工具顺序注册。
4. 按 `registry.list_all_names()`（字母序）转换成 `BaseTool`，每个都绑定同一个 registry；
   `PromptAssembler(tool_registry=registry)` 也持有这同一个对象。

`assemble_tools` 不接收 `ExitStack`：资源生命周期属于具体插件的来源模式（§3.4）。MCP 配置
仍由调用方加载，保证"坏配置响亮失败"的抛出点不变。

### 3.3 内置插件与顺序

| plugin_id | 贡献 | 说明 |
|---|---|---|
| `clauses` | `retrieve_clauses`、`cite_clauses` | 两者经 `build_cite_clauses_tool_with_context_check` 的 `context_updater` 耦合，必须同一插件，retrieve 在前 |
| `hydraulics` | `calc_velocity` | |
| `materials` | `material_stats` | |
| `drawing` | `read_drawing`（条件） | 复用 `maybe_build_read_drawing_tool(ctx.drawing_root)`；不可用时 `status="skipped"`，`DrawingRootResult` 随贡献带回 |
| `mcp_gateway` | `mcp_<server>_<tool>` | 见 §3.4；有工具时 `grants={MCP_INVOKE}` |

顺序常量 `BUILTIN_PLUGIN_ORDER = ("clauses", "hydraulics", "materials", "drawing", "mcp_gateway")`
（P7-I04）。顺序就是注册顺序，决定提示词工具指引的截断位置（F3、F5）。

### 3.4 MCP 插件的两种来源

- `McpGatewayPlugin.owning(config, *, stack)`：调用 `open_gateway_sessions(config)`（`config=None`
  也调用，得到空装配），**紧接着** `stack.callback(assembly.close)`；只能 `contribute` 一次。
  会话路径用它，关闭顺序与现在相同（MCP 先于 SqliteSaver 打开，后于它关闭）。
- `McpGatewayPlugin.borrowing(assembly)`：使用调用方已打开的装配，从不打开也不关闭。

### 3.5 runner 路径统一成会话语义（S2，评审决定 2026-10-02）

| # | 现状（F2） | 统一后 |
|---|---|---|
| 1 | cite 不接 thread 级 `context_source` | 接，语义同 `AgenticSession._thread_retrieved_source_ids`；app 尚未构造时返回空集合。单轮用例结果不变 |
| 2 | 图纸根只读环境变量 | `AssemblyContext.drawing_root=None`，与 CLI 不给 `--drawing-root` 时同语义 |
| 3 | 模块级缓存的 MCP 装配，跨用例共享子进程 | 每条用例 `owning` 现开现关，与 Web 每个 run 同构；删除 `_MCP_ASSEMBLY_CACHE` 及其两个函数 |
| 4 | MCP 注册出错时降级为纯本地工具 | 响亮失败：异常上抛，由 `run_bundle` 按用例隔离记为 error |
| 5 | 两个从未落盘的 MCP trace 事件（F13） | 删除；S3 统一补上真正落盘的装配摘要事件 |
| 6 | 缓存的 chat model 拿到授权前的主体 | 装配后把授权后的 `request_context` 赋给 chat model |
| 7 | 主体用 `model_copy` 更新（不校验） | 用构造器（带校验） |
| 8 | 不接缓存服务、显式传 `max_tool_turns` | **保留**：评测不得命中回答缓存；这两项属于 app 构造，不属于工具装配 |

---

## 4. 不变量（Invariants）

| # | 不变量 | 强制位置 | 状态 |
|---|---|---|---|
| P7-I01 | src 里构造面向模型的 `ToolRegistry(` 只在 `plugins/assembly.py`；内部注册表白名单写死并附理由（`mcp/memory_adapter.py`、`runtime/governance/scenarios.py`、`harness/memory_eval.py`、探针 `harness/local_model_compat.py`），白名单条目不再对应真实代码同样判失败；用 AST 判定 | `tests/test_plugin_invariants.py` | planned |
| P7-I02 | `tool_spec_to_base_tool(` 只在 `plugins/assembly.py` 与上述探针里调用 | `tests/test_plugin_invariants.py` | planned |
| P7-I03 | `runtime/agentic.py` 与 `harness/runner.py` 都调用 `assemble_tools` | `tests/test_plugin_invariants.py` | planned |
| P7-I04 | 内置插件顺序 `BUILTIN_PLUGIN_ORDER == ("clauses", "hydraulics", "materials", "drawing", "mcp_gateway")` | `tests/test_plugin_invariants.py` | planned |
| P7-I05 | `plugins/` 里不出现 `ToolSpec(` 调用；`ToolSpec` 定义留在 `tools/`（F12 的扫描前提） | `tests/test_plugin_invariants.py` | planned |
| P7-I06 | 插件能追加给主体的能力只限 `GRANTABLE_CAPABILITIES = {MCP_INVOKE}` | `plugins/assembly.py` + `tests/test_tool_plugins.py` | planned |
| P7-I07 | 只有 `runtime/agentic.py` 与 `harness/runner.py` 调用 `build_agentic_app`（装配快照捕获点的前提） | `tests/test_plugin_invariants.py` | planned |

---

## 5. 分步交付

| 步 | 对应 | 内容 | 执行 Prompt | 状态 |
|---|---|---|---|---|
| S0 | — | 规格 + 任务书 + 防漂移 harness（H0–H5）+ 装配快照基线 | 范围块在任务书附录 | delivered |
| S1 | F7 | 确认门执行期强制 | `docs/execution-prompts/P7-S1-confirmation-gate.md` | delivered |
| S2 | Step A | 插件契约 + `assemble_tools()` 单入口 + runner 统一 | `docs/execution-prompts/P7-S2-tool-plugin-assembly.md` | planned |
| S3 | Step B | 配置分层、每台 server 一个插件实例、`disabled`、受信 entry point；修 F6/F9/F10/F13；预算测试计入 MCP 工具 | 开工前另写 | planned |
| S4 | Step C | 按意图最小暴露 / 工具分组；修 F4/F5（先建 `tool_exposure_p0`） | 开工前另写 | planned |
| S5 | Step D | MCP 连接层：长驻进程（修 F8）+ Streamable HTTP | 开工前另写 | planned |
| S6 | Step E1 | Web 扩展面板（只改启停 / allow / 凭据引用；导入 `mcpServers` 只生成禁用的待审提案） | 开工前另写 | planned |
| S7 | Step E2 | HITL 确认门：外层图 interrupt、waiting 态、批准/拒绝 API、审计 | 开工前另写 | planned |

S3–S7 的已知约束（开工写执行 Prompt 时必须吸收）：

- **S3**：每层的 `allow` 整体替换、不合并；entry point 只收白名单，外部插件只能贡献经 MCP 桥接的
  工具（本地工具会被 `capability_for_tool` 拒绝）；预算测试基于装配快照 profile 计入 MCP 工具。
- **S4**：本地工具 schema 只剩 3 字符余量（F4），`< 4500` 断言不得上调；先有工具选择准确率的
  评测证据，再改暴露策略；`PROMPT_TEMPLATE_VERSION` 递进，快照**有意**重写。
- **S5**：长驻 stdio 进程被所有 Web 用户共享——跨租户状态、并发访问（客户端 `max_workers=4`）、
  重载语义都要先定；HTTP 需要 `NETWORK_FETCH` 级别的能力决策（它在危险集里）与出站清单登记
  （7 → 8）。
- **S6**：导入 `mcpServers` 会带来 `command` / `args`，面板不能写它们，只能生成"已禁用的待审提案"，
  由运维离线应用。
- **S7**：内层 react agent 是 `checkpointer=False`（`graphs/agent_rag.py`），工具执行中的 interrupt
  无法恢复，确认门必须放在**外层图**（先例：`multi_agent/orchestrator.py` 的审批 interrupt）。

---

## 6. 防漂移 harness

### 6.1 H0 导入位置守卫

- `tests/conftest.py::pytest_configure` 用 `importlib.util.find_spec("water_agent")` 核对导入位置，
  不是本检出的 `src/water_agent/__init__.py` 就 `pytest.UsageError` 退出（exit 4），守卫自身不
  import `water_agent`。
- `scripts/ci_gate.sh` 在一切检查之前 `export PYTHONPATH="$ROOT/src…"`，并用实际解释器核对
  导入位置，不一致 exit 2。

### 6.2 H1 装配快照

- 实现：`src/water_agent/harness/tool_assembly_snapshot.py`；基线：`data/eval/tool_assembly_main.json`
  （S0 在**未改动的** main 装配代码上采集）。
- **捕获点**：`build_agentic_app`。所有 `water_agent.*` 模块里指向它的名字一并替换；装配没抛异常的
  profile 必须恰好捕获一次，否则 harness 报"捕获点移位"并 exit 2。runner 侧在捕获点抛哨兵中止，
  不构图、不调用模型。
- **profile**：会话侧 10 个、runner 侧 5 个（mock 默认、CLI 默认、图纸根参数/环境变量、fake MCP、
  确认门跳过、`confirmation_handler_ready=true`、配置损坏、Web 真生成器 + member 主体、注册故障；
  runner 侧无 MCP、图纸根、fake MCP、配置损坏、注册故障）。
- **两层记录**：
  - `surface`（模型可见面）：注册顺序、发给模型的顺序、每个工具 schema 的 sha256 / 字符数 /
    超时 / 是否有 guard / 执行函数限定名、schema 总长、截断后的工具指引全文、系统提示摘要
    （版本快照段替换为占位符）、请求主体与注册表主体（kind / auth_domain / 能力集）。
  - `lifecycle`：捕获次数、异常（类型 + code）、装配期 trace 调用（是否真落盘）、ExitStack 登记、
    MCP 暴露 / 跳过（server, tool, code）、作用域结束后仍存活的 MCP 子进程数、`build_agentic_app`
    入参名 / `max_tool_turns` / checkpointer 类型 / 缓存服务的 manifest、工具是否绑定同一注册表、
    chat model 携带的主体、行为探针（retrieve → cite 一真一假、retrieve 闭包里的检索器链、经护栏
    调用 fake MCP 的 echo）。
  - 不比较：subject、thread / request id、manifest hash 的值（含 `sys.executable`）、tmp 路径、
    跳过原因的 detail 文本；库版本与 HEAD 写进不比较的 `info` 块。
- **隔离**：快照期间清掉并按 profile 显式设置 `DRAWING_ROOT`、`WATER_AGENT_MCP_CONFIG`、`LOCAL_MODEL_*`、
  `WEB_MOCK_GENERATOR`、模型 Key 等环境变量，结束时整体恢复；每个 profile 一个独立的 MCP server id。
- **变更协议**：
  - surface 的值变了 → 必须递进 `TOOL_POLICY_VERSION`（工具集 / 能力映射 / 策略）或
    `PROMPT_TEMPLATE_VERSION`（提示文案 / 分段），否则 `--write` 拒绝写入；
  - 任何变化（含 lifecycle、某个 profile 从"采到"变成"装配抛错"）→ `--write` 必须带 `--reason`，
    并把 `--diff` 的输出贴进该步决策记录；
  - 哪些片段在某一步必须保持不变，由该步范围块的 `frozen_json` 声明（§6.4）。

### 6.3 H2 需求 ↔ 测试追溯

- 实现：`src/water_agent/harness/spec_trace.py`；检查：`tests/test_p7_traceability.py`。
- 解析本文 §4 与 §7 的表格行：首列是 ID（`P7-I<nn>` 或 `P7-S<n>-A<nn>`），§7 第三列用反引号列出
  验证项（pytest 节点 `path::[Class::]name`，或 `gate:<名>` 表示 `scripts/ci_gate.sh` 里的套件），
  末列是状态：
  - `delivered`：至少一个验证项；pytest 节点必须存在、没有 skip/xfail、**docstring 含该 ID**；
    `gate:` 项必须出现在 `ci_gate.sh` 里；
  - `planned`：格式检查，验证项不要求已存在；
  - `untested`：显式声明无自动化验证，必须在 §7 末尾"未覆盖项"里写明原因。
- 反向：`tests/` 里出现的任何 `P7-…` ID 都必须在本文定义；重复 ID、未知状态都判失败。
- 测试只用 AST 检查，不 import、不收集。
- **局限**：只能证明"测试存在且声明了覆盖"，不能证明断言写得对——断言质量仍靠评审与反证测试。

### 6.4 H3 改动范围守卫

- 实现：`scripts/check_step_scope.py`（只用标准库）；测试：`tests/test_check_step_scope.py`；
  CI：`.github/workflows/p7-scope.yml`（PR 触发、`fetch-depth: 0`、`--auto`）。
- 范围块：执行 Prompt 里 `<!-- step-scope -->` 标记之后的第一个 fenced JSON 块：

  ```json
  {
    "step": "P7-S1",
    "allow": ["src/water_agent/mcp/config.py", "tests/test_mcp_*.py"],
    "forbid": ["src/water_agent/tools/**"],
    "protect": [{"node": "tests/x.py::TestY::test_z", "contains": "assert cost < 4500"}],
    "frozen_json": [{"path": "data/eval/tool_assembly_main.json", "pointers": ["/versions"]}]
  }
  ```

- 规则：
  - 范围块**从 merge-base 上的执行 Prompt 读**；工作区里的范围块与之不同 → 违规。首次引入该文档的
    步骤用 `--bootstrap`（S0）。
  - 改动集合 = `git diff --no-renames --name-only -z <merge-base>` ∪ 未跟踪文件；删除计入；重命名按
    新旧两个路径计入。
  - glob：`**` 匹配 0 到多段，`*` / `?` 不跨 `/`，整串匹配；必须命中 allow 且不命中 forbid，forbid 优先。
  - protect：节点存在、未被 skip / xfail（含模块级 `pytestmark` 与函数体内 `pytest.skip`），去掉注释后
    按 token 比较仍包含该字面量。
  - frozen_json：pointer 段内支持 `*` 通配；命中的片段必须与 merge-base 完全相同；pointer 在 base 上一个
    片段都匹配不到也判失败（防止写错 pointer 让冻结形同虚设）。
- 退出码：0 通过 / 不适用；1 违规；2 无法判定（base 不存在、浅克隆、范围块缺失或损坏）。
- `--auto`：分支 `YEK/p7-s<n>-…` 对应 `docs/execution-prompts/P7-S<n>-*.md`（必须已在 merge-base 上且
  唯一）；S0 与非 P7 分支跳过。

### 6.5 H4 结构不变量

§4 的 P7-I01…I07，由 S2 的 `tests/test_plugin_invariants.py` 用 AST 判定（docstring 里也出现
`ToolRegistry(` 字样，文本 grep 会误报）。

### 6.6 H5 既有门禁

`scripts/ci_gate.sh` 全套不变：pytest、ruff、mypy、机制层 runner 六套（含 `agentic_p0`）、
drawing_p0 / drawing_gen_p0 / multi_agent_p0 / memory_p0 与入仓基线对比。H1 / H2 / H0 测试随 pytest
进入门禁；H3 只在 PR 上由独立 workflow 运行（main 上没有"当前步"）。

---

## 7. 验收 → 测试映射（Acceptance → Test）

### S0：规格与防漂移 harness

| ID | 验收 | 验证 | 状态 |
|---|---|---|---|
| P7-S0-A01 | pytest 导入的 `water_agent` 必须来自本检出，否则收集前报错退出（含反证） | `tests/test_import_guard.py::test_conftest_rejects_a_foreign_water_agent`<br>`tests/test_import_guard.py::test_current_session_imports_this_checkout` | delivered |
| P7-S0-A02 | 门禁把 PYTHONPATH 钉到本检出，并在任何检查前核对实际导入位置 | `tests/test_import_guard.py::test_gate_pins_import_location` | delivered |
| P7-S0-A03 | 当前代码的装配快照与入仓基线逐字段一致 | `tests/test_tool_assembly_snapshot.py::test_snapshot_matches_baseline` | delivered |
| P7-S0-A04 | 快照确定，且不受本机环境变量影响；环境原样恢复 | `tests/test_tool_assembly_snapshot.py::test_snapshot_is_hermetic_and_deterministic` | delivered |
| P7-S0-A05 | 装配未抛异常的 profile 恰好捕获一次；两条装配路径都被覆盖 | `tests/test_tool_assembly_snapshot.py::test_capture_fires_once_per_profile`<br>`tests/test_tool_assembly_snapshot.py::test_profile_set_covers_both_assembly_paths` | delivered |
| P7-S0-A06 | 基线里的版本号与代码常量一致 | `tests/test_tool_assembly_snapshot.py::test_baseline_versions_match_code` | delivered |
| P7-S0-A07 | 反证：改一条工具描述被识别 | `tests/test_tool_assembly_snapshot.py::test_negative_control_description_change_detected` | delivered |
| P7-S0-A08 | 反证：调换注册顺序被识别，且不误报字母序 | `tests/test_tool_assembly_snapshot.py::test_negative_control_registration_order_detected` | delivered |
| P7-S0-A09 | 反证：注册表主体丢掉 `MCP_INVOKE` 被识别 | `tests/test_tool_assembly_snapshot.py::test_negative_control_dropped_grant_detected` | delivered |
| P7-S0-A10 | 反证：工具指引预算改动被识别 | `tests/test_tool_assembly_snapshot.py::test_negative_control_guide_budget_detected` | delivered |
| P7-S0-A11 | 写入守卫与 CLI：surface 变而版本号未递进拒绝写入；任何变化须带原因；差异以 JSON pointer 报告；`--check` 退出码 | `tests/test_tool_assembly_snapshot.py::test_write_guard_refuses_surface_change_without_version_bump`<br>`tests/test_tool_assembly_snapshot.py::test_write_guard_requires_reason_for_any_change`<br>`tests/test_tool_assembly_snapshot.py::test_diff_reports_json_pointers_and_ignores_info`<br>`tests/test_tool_assembly_snapshot.py::test_cli_check_exit_codes` | delivered |
| P7-S0-A12 | F4：本地工具 schema 4497、余量 3 | `tests/test_p7_known_figures.py::test_local_tool_schema_headroom_is_three_characters` | delivered |
| P7-S0-A13 | F5：工具指引已截断；MCP 工具不计入现有预算 | `tests/test_p7_known_figures.py::test_tool_guide_already_truncates_tools` | delivered |
| P7-S0-A14 | F6：manifest 不进缓存服务 | `tests/test_p7_known_figures.py::test_mcp_manifest_never_reaches_the_cache_service` | delivered |
| P7-S0-A15 | F7：确认门执行期不检查（S0 时的现状；S1 修复后由 P7-S1-A03 取代，见未覆盖项） | 无 | untested |
| P7-S0-A16 | F13：runner 的 MCP trace 事件从未落盘（现状） | `tests/test_p7_known_figures.py::test_runner_mcp_trace_events_never_land` | delivered |
| P7-S0-A17 | F2：runner 路径与会话路径的可观察差异（现状） | `tests/test_p7_known_figures.py::test_runner_path_differs_from_session_path` | delivered |
| P7-S0-A18 | 本规格的追溯关系成立（delivered 行的测试存在、未 skip、docstring 含 ID；测试里的 ID 都有定义） | `tests/test_p7_traceability.py::test_spec_traceability_holds` | delivered |
| P7-S0-A19 | 追溯检查能识别：测试缺失、docstring 未声明 ID、测试被 skip、`gate:` 项不存在 | `tests/test_p7_traceability.py::test_delivered_rows_must_point_at_live_tests` | delivered |
| P7-S0-A20 | 追溯检查能识别：测试里引用了规格未定义的 ID | `tests/test_p7_traceability.py::test_unknown_ids_in_tests_are_reported` | delivered |
| P7-S0-A21 | 追溯检查能识别：重复 ID、未知状态、delivered 行没有验证项、untested 行未在未覆盖项说明 | `tests/test_p7_traceability.py::test_malformed_spec_tables_are_reported` | delivered |
| P7-S0-A22 | 范围守卫 glob 语义与范围块解析 | `tests/test_check_step_scope.py::test_glob_semantics`<br>`tests/test_check_step_scope.py::test_parse_scope_rejects_malformed_blocks` | delivered |
| P7-S0-A23 | 越界改动与 forbid 优先 | `tests/test_check_step_scope.py::test_clean_branch_passes`<br>`tests/test_check_step_scope.py::test_change_outside_allow_fails`<br>`tests/test_check_step_scope.py::test_forbid_wins_over_allow` | delivered |
| P7-S0-A24 | 重命名、删除、未跟踪文件、中文路径都计入 | `tests/test_check_step_scope.py::test_rename_out_of_forbidden_dir_and_deletion_are_counted`<br>`tests/test_check_step_scope.py::test_untracked_and_chinese_paths_are_counted` | delivered |
| P7-S0-A25 | 受保护断言被改值、注释、skip、改名、整模块 skip 都判违规；只改排版不误报 | `tests/test_check_step_scope.py::test_protected_assertion_must_survive`<br>`tests/test_check_step_scope.py::test_protected_assertion_tolerates_formatting` | delivered |
| P7-S0-A26 | frozen_json 只冻结声明的片段；写错的 pointer 被报告 | `tests/test_check_step_scope.py::test_frozen_json_blocks_frozen_parts_only`<br>`tests/test_check_step_scope.py::test_frozen_json_pointer_that_matches_nothing_is_reported` | delivered |
| P7-S0-A27 | 分支内改自己的范围块判违规；范围块以外的正文可以补充 | `tests/test_check_step_scope.py::test_scope_block_edited_in_branch_fails`<br>`tests/test_check_step_scope.py::test_text_outside_the_block_may_change` | delivered |
| P7-S0-A28 | base 不存在、浅克隆 → 无法判定（exit 2） | `tests/test_check_step_scope.py::test_missing_base_or_shallow_clone_cannot_be_judged` | delivered |
| P7-S0-A29 | `--bootstrap` 只用于尚未进入 base 的文档 | `tests/test_check_step_scope.py::test_bootstrap_only_for_prompts_not_yet_on_base` | delivered |
| P7-S0-A30 | `--auto` 按分支名找该步执行 Prompt；找不到判违规 | `tests/test_check_step_scope.py::test_auto_mode_maps_branch_to_step_prompt` | delivered |
| P7-S0-A31 | 本仓库任务书与各步执行 Prompt 的范围块可解析、protect 完好、frozen pointer 有匹配 | `tests/test_check_step_scope.py::test_repo_scope_blocks_are_usable` | delivered |
| P7-S0-A32 | CI 在 P7 分支的 PR 上以完整历史运行范围守卫 | `tests/test_check_step_scope.py::test_ci_runs_scope_guard_on_p7_pull_requests` | delivered |
| P7-S0-A33 | 既有门禁不回退（机制层六套 + 各基线对比） | `gate:agentic_p0`<br>`gate:compare_baseline` | delivered |

### S1：确认门执行期强制（F7）

| ID | 验收 | 验证 | 状态 |
|---|---|---|---|
| P7-S1-A01 | 确认门接线（S7）之前，配置里 `confirmation_handler_ready=true` 在加载期即被拒绝（`MCP_CONFIG_INVALID`，说明原因与解锁条件） | `tests/test_mcp_wiring.py::test_ready_flag_rejected_until_confirmation_gate_exists` | delivered |
| P7-S1-A02 | 执行期纵深防御：运行时绑定 `requires_human_confirmation=True` 的工具调用被护栏拒绝（规则 id `tool.confirmation_required`），绕过配置加载直接构造配置也拦得住 | `tests/test_tool_policy.py::TestConfirmationGate::test_confirmation_required_tool_blocked_at_call_time` | delivered |
| P7-S1-A03 | 快照有意变更：`session_mcp_ready_flag` 由"暴露并放行"变为配置错误，其余 profile 不变；`TOOL_POLICY_VERSION` 递进；事实 F7 更新 | `tests/test_p7_known_figures.py::test_confirmation_gate_is_enforced` | delivered |

### S2：插件契约与单入口（Step A）

| ID | 验收 | 验证 | 状态 |
|---|---|---|---|
| P7-S2-A01 | `clauses` 插件耦合 retrieve 与 cite，并透传 `context_source` | `tests/test_tool_plugins.py::test_clauses_plugin_couples_retrieve_and_cite`<br>`tests/test_tool_plugins.py::test_clauses_plugin_forwards_context_source` | planned |
| P7-S2-A02 | `drawing` 插件原样透传 `drawing_root`（`""` 与 `None` 不互换） | `tests/test_tool_plugins.py::test_drawing_plugin_passes_root_verbatim` | planned |
| P7-S2-A03 | MCP 插件 `owning` 打开后立即登记关闭、只能用一次；`borrowing` 从不打开或关闭 | `tests/test_tool_plugins.py::test_mcp_owning_closes_on_stack_exit`<br>`tests/test_tool_plugins.py::test_mcp_owning_is_single_use`<br>`tests/test_tool_plugins.py::test_mcp_borrowing_never_closes` | planned |
| P7-S2-A04 | 授权只在贡献了工具时发生、保持 kind / auth_domain、白名单外拒绝 | `tests/test_tool_plugins.py::test_grant_only_with_contributed_tools`<br>`tests/test_tool_plugins.py::test_grant_preserves_kind_and_auth_domain`<br>`tests/test_tool_plugins.py::test_grants_outside_whitelist_rejected` | planned |
| P7-S2-A05 | 插件异常原样上抛；插件 id 重复拒绝；发给模型的工具按字母序且绑定同一注册表 | `tests/test_tool_plugins.py::test_plugin_exception_propagates`<br>`tests/test_tool_plugins.py::test_duplicate_plugin_ids_rejected`<br>`tests/test_tool_plugins.py::test_tools_sorted_and_bound_to_registry` | planned |
| P7-S2-A06 | 会话路径零行为变化：会话侧 profile 与全部 surface 与基线一致 | `tests/test_plugin_invariants.py::test_session_assembly_matches_baseline` | planned |
| P7-S2-A07 | runner 统一成会话语义（§3.5 第 1–7 条） | `tests/test_tool_plugins.py::test_runner_mcp_opened_and_closed_per_case`<br>`tests/test_tool_plugins.py::test_runner_mcp_register_fault_fails_loudly`<br>`tests/test_tool_plugins.py::test_runner_chat_model_sees_granted_principal`<br>`tests/test_tool_plugins.py::test_runner_cite_uses_thread_context` | planned |
| P7-S2-A08 | 不变量 P7-I01…I07 | `tests/test_plugin_invariants.py::test_invariants_hold` | planned |
| P7-S2-A09 | F11：装配位置断言改为 AST 判定 | `tests/test_read_drawing_tool.py::TestWiring::test_cli_registers_tool_only_when_root_available` | planned |
| P7-S2-A10 | 机制层门禁不回退（agentic_p0 21/21） | `gate:agentic_p0` | planned |
| P7-S2-A11 | 真实质量不回退：有 Key 时重跑 `rag_p1 --variant real --judge`，与 55/58、49/58 对比 | 无 | untested |

### 未覆盖项（显式声明）

- **P7-S0-A15**：它锁的是 S0 时的缺陷现状（确认门执行期不检查）。S1 修复后这个现状已不存在，原测试改写为断言修复后的事实，由 **P7-S1-A03** 承担；本行保留为历史记录，不再有对应测试。
- **P7-S2-A11**：真实质量层需要 `DEEPSEEK_API_KEY` 与网络，不在离线门禁内。没有 Key 时记为
  blocked，**不得**声称真实质量未回退。
- **F8 / F9 / F10**：留到对应步骤（S5 / S3）写测试；现在没有锁定测试。
- **H2 的局限**：追溯检查证明测试存在且声明了覆盖，不证明断言正确（§6.3）。

---

## 8. 实施协议（每一步都照此执行）

1. **开工前**：
   - 读 `AGENTS.md`、本规格（尤其 §2 事实表与 §7 该步验收）、该步执行 Prompt；
   - 确认该步执行 Prompt **已在 main 上**；
   - 从最新 `origin/main` 拉 `YEK/p7-s<n>-<名>`；
   - 跑一遍 `python -m water_agent.harness.tool_assembly_snapshot --check` 确认起点干净。
2. **循环**：先写或补失败测试（docstring 写上验收 ID）→ 最小改动 → 本地验证 → 记录证据。一个逻辑面
   一个 commit。
3. **停止条件**（满足任一条就停下，写清现象后等确认，不自行扩大设计）：
   - 快照出现该步未声明允许的差异；
   - 需要改范围块 `forbid` 里的文件，或 `allow` 之外的文件；
   - 需要改受保护断言，或想上调任何预算上限；
   - 规格与代码现实冲突（例如规格假设的函数不存在、行为与事实表不符）；
   - 门禁出现与本步无关的红灯。
4. **快照有意变更**：按 §6.2 递进版本号，`--write --reason`，`--diff` 原样贴进决策记录。
5. **完成时**：
   - 把本步验收行的状态翻成 `delivered`，验证项写成真实存在的测试节点；
   - 更新事实表里被本步改变的事实；
   - 在 AGENTS.md "架构现状"补一条，只写可验证的事实，并注明锁定它的测试。
6. **决策记录** `docs/design/p7-step-<n>-decision.md`，结构为：
   - §0 偏离与修订：0.1 执行 Prompt §0 的修订、0.2 实测后追加的偏离；
   - §1 交付；
   - §2 实测：命令与输出；
   - §3 快照变化：`--diff` 原文，或"无变化"；
   - §4 不做；
   - §5 残余风险。
7. **汇报格式**：
   - 改了哪些文件；
   - 跑了哪些验证命令以及实际输出（失败照贴）；
   - 范围守卫的输出；
   - 快照差异；
   - 未验证项与原因；
   - 遗留风险。

---

## 9. 已知限制与 blocked 项

- 真实 CAD/BIM、企业库 MCP server 未验证（P4 Step 26，blocked）；官方 `mcp` SDK 仍未安装，客户端是
  标准库实现。
- 装配快照只覆盖进程内可离线复现的装配；真实模型如何使用这些工具不在其中（属于质量层评测）。
- 范围守卫只在 PR 上运行；直接推到 main 的提交不受它约束（由分支保护与评审兜底）。
- 追溯检查只能证明"有测试且声明覆盖"（§6.3）。

## 10. 相关文件索引

| 文件 | 作用 |
|---|---|
| `docs/给排水agentP7插件化与MCP扩展任务书.md` | 计划书：范围、顺序、评审决策、S0 范围块 |
| `docs/execution-prompts/P7-S1-confirmation-gate.md` | S1 执行 Prompt（含范围块） |
| `docs/execution-prompts/P7-S2-tool-plugin-assembly.md` | S2 执行 Prompt（含范围块） |
| `src/water_agent/harness/tool_assembly_snapshot.py` | H1 装配快照 |
| `data/eval/tool_assembly_main.json` | H1 基线 |
| `src/water_agent/harness/spec_trace.py` | H2 追溯检查 |
| `scripts/check_step_scope.py` | H3 范围守卫 |
| `.github/workflows/p7-scope.yml` | H3 的 PR 检查 |
| `tests/conftest.py` | H0 导入位置守卫 |
