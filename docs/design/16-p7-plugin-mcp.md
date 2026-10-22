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
   Skill 库（S9）：仓库内置 + 运维只读目录两级的操作规程，模型经 `load_skill` 按需加载，面板只读（管理员可启停）。
3. **MCP 可用性**：长驻连接（S5）、Streamable HTTP 与 OAuth 2.1 服务级授权（S8）；危险操作人工确认门（S1 先封住、S7 接通）。
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
| F2 | ~~`HarnessRunner._run_real_agentic_case` 手工重建注册表，与会话路径有 8 处差异~~ **S2 已统一**：两条路径都经 `assemble_tools`，§3.5 第 1–7 条按会话语义执行，第 8 条刻意保留 | `tests/test_p7_known_figures.py::test_runner_path_matches_session_semantics` | S2 已交付 |
| F3 | 注册顺序决定提示词工具指引的顺序与截断；发给模型的工具按 `list_all_names()` 字母序 | 快照 `surface.registration_order` / `model_tool_order` | 不变量 |
| F4 | 5 个本地工具 schema 共 **4497** 字符，离 4500 上限只剩 **3**（`read_drawing` 单独 956）；此前文档里的 4370/130 已过期。**S4 之后**：注册集的数字不变（S4 不增减工具），但每轮实际发出的 schema 由暴露策略约束在 4500 以内（core 2125；最紧的"读图 + 材料"组合正好 4497，MCP 整组让位）。**S9 之后**：4497 只数这 5 个本地工具；`load_skill` 只在开关 on 且有已启用 skill 时注册（内置 4 份目录 717 字符），与 MCP 同按 intent 暴露，最紧组合下整组让位 | `tests/test_p7_known_figures.py::test_local_tool_schema_headroom_is_three_characters`<br>`tests/test_p7_known_figures.py::test_exposed_tools_always_fit_the_tool_guide` | S4 已约束发送面 |
| F5 | ~~工具指引 800 字预算已截掉 `read_drawing`（"省略 1 行"）；两个 MCP 工具时截掉 2 行、schema 4724 字符~~ **S4 已修（intent 策略）**：指引只列本轮开放的工具（工具名 + 描述首句）并附"本轮未开放"行，两项预算整组裁决，`tool_exposure_p0` 36/36 无截断。`full` 回滚开关下仍是原样截断 | `tests/test_p7_known_figures.py::test_exposed_tools_always_fit_the_tool_guide` | S4 已交付 |
| F6 | ~~MCP 工具已暴露，缓存服务的 `mcp_server_manifest` 仍恒为 `""`~~ **S3 已修**：`AgenticSession` 构造缓存服务时传入 `mcp_assembly.manifest_hash` | `tests/test_p7_known_figures.py::test_mcp_manifest_reaches_the_cache_service` | S3 已交付 |
| F7 | ~~`requires_human_confirmation` 只在装配期检查，执行期不查；`confirmation_handler_ready=true` 时这类工具直接暴露并可调用~~ **S1 已修**：`confirmation_handler_ready=true` 在加载期即 `MCP_CONFIG_INVALID`；运行时绑定要求确认的工具没有凭证一律 `tool.confirmation_required` 拒绝。**S7 接通**（§3.7）：确认门是否可用由入口决定，只有 Web 会话工厂接线——这类工具在 Web 下暴露为 `return_direct` 延迟工具，执行须经外层 `confirmation_gate` 在人工批准后签发的一次性凭证；CLI / runner 仍整台 `MCP_CONFIRMATION_UNAVAILABLE` | `tests/test_p7_known_figures.py::test_confirmation_gate_is_enforced` | S1 + S7 已交付 |
| F8 | ~~每个 Web run 都重新 spawn 全部 MCP 子进程~~ **S5 已修（对 `lifecycle: shared` 的 server）**：Web 会话工厂经进程内连接池借用，shared 的 server 跨 run 复用同一进程；缺省 `per_run` 刻意保持每个 run 重开（共享即共享状态，须运维显式声明）。CLI 与 runner 不变 | `tests/test_p7_known_figures.py::test_shared_mcp_servers_survive_across_runs` | S5 已交付 |
| F9 | ~~`env_file` 相对路径按 cwd 解析；缺失时被 `open_bridge` 吞成跳过~~ **S3 已修**：相对路径按定义它的那一层配置文件所在目录解析，`env_file` / `cwd` 缺失在加载期即 `MCP_CONFIG_INVALID` | `tests/test_mcp_layered_config.py::test_env_file_and_cwd_resolved_and_checked_at_load` | S3 已交付 |
| F10 | ~~入仓 `data/mcp_servers.json` 写死个人机器路径、`allow` 与上游不符~~ **S3 已修**：入仓默认层改为空骨架，autocad 配置挪到 `data/mcp_servers.example.json`（占位路径、不进镜像） | `tests/test_mcp_wiring.py::TestConfigWiring::test_default_config_is_empty_skeleton` | S3 已交付 |
| F11 | 两条测试按源码文本断言装配位置：`tests/test_read_drawing_tool.py`（**S2 已改为 AST 判定真实调用**）、`tests/test_web_runs.py`（`build_agentic_session` 字面量，Web 不在 P7 范围内，保留） | `tests/test_read_drawing_tool.py::TestWiring::test_cli_registers_tool_only_when_root_available` | S2 前者已交付 |
| F12 | inventory 静态扫描只认 `tools/` 下 `ToolSpec(\n name="…"` 的写法；覆盖率锁死 `{7, 7, 5, 5}`（**S9 后** `{7, 7, 6, 6}`：新增只读的 `load_skill`，远程出口仍是 7；**S8 后** `{8, 8, 6, 6}`：新增 MCP 远程出口 `mcp_streamable_http`） | `tests/test_governance_contracts.py` | 不变量 P7-I05 |
| F13 | ~~runner 的 `mcp_assembly` / `mcp_assembly_error` trace 事件尝试写入但从未落盘~~ S2 删除这两次调用；**S3 改为先开 trace 再写装配摘要**（会话与 runner 都是），事件确实落盘 | `tests/test_p7_known_figures.py::test_no_assembly_trace_calls_that_never_land` | S3 已交付 |
| F14 | worktree 里不设 `PYTHONPATH=src` 时，pytest 与 harness 导入的是**主检出**的代码（editable install）；`ci_gate.sh` 此前不设 PYTHONPATH | `tests/test_import_guard.py::test_conftest_rejects_a_foreign_water_agent` | S0 H0 已修 |
| F15 | ~~MCP 子进程继承 agent 的工作目录，模型能借相对路径参数读 agent 自己的文件~~ **S3 已修**：未配 `cwd` 时每次连接新建空临时目录作为子进程工作目录，连接关闭（含启动失败）即删除。不是沙箱：server 进程照样能读进程用户能读的一切 | `tests/test_mcp_layered_config.py::test_child_runs_in_isolated_or_configured_cwd` | S3 已交付 |
| F17 | ~~`MCPStdioClient._request` 不是并发安全的：各请求共用一个 inbox，读到别人的响应时当成通知存起来继续等，真正的请求方只能超时（server 乱序应答时必然发生；同一 run 内并行调用同一台 server 的两个工具即可触发）~~ **S5 已修**：同一连接上的请求串行，等锁计入 deadline。fake server 按到达顺序应答时 `queue.Queue` 的 FIFO 恰好掩盖了它，复现需乱序应答 | `tests/test_mcp_pool.py::test_concurrent_calls_on_one_client_get_their_own_responses` | S5 已交付 |
| F16 | ~~配置没法指定 MCP server 的工作目录，autocad-mcp 启动即崩~~ **S3 已修**：新增 `cwd` 字段；本机覆盖层指向 autocad-mcp 仓库后暴露 `mcp_autocad_mcp_read_dwg`（手动验收，只证明能连上） | `tests/test_mcp_layered_config.py::test_child_runs_in_isolated_or_configured_cwd` | S3 已交付 |
| F18 | S8 读码发现：出站静态扫描的特征不含标准库 `http.client`，新增一个只开 `http.client` 连接的模块**扫描看不见**（反证：挪到未登记模块也不红）；且 starlette 的 `HTTPConnection`（入站请求视图）会让宽松的 `HTTPS?Connection(` 特征误报 | `tests/test_governance_contracts.py::TestInventoryCoverage::test_unregistered_http_egress_module_is_detected` | S8 已修（特征限定为 `http.client.HTTPS?Connection`） |
| F20 | S10 读码发现：协议层本来就是 OpenAI 兼容，写死的是配置——Web 会话工厂固定 `MODEL_NAME` / `https://api.deepseek.com` / `DEEPSEEK_API_KEY`，"模型已配置"只看 `DEEPSEEK_API_KEY`，embedding 固定 `MODEL_NAME` 且建库必须调百炼；**S10 已改**：端点 / 模型 / Key 名唯一来源 `generation/endpoints.py`，缺省仍是 DeepSeek + 百炼 | `tests/test_llm_endpoints.py::test_defaults_are_unchanged`<br>`tests/test_llm_endpoints.py::test_all_entry_points_share_one_resolution` | S10 已交付 |
| F21 | S10 实测：TriviumDB 的 `search_hybrid` 对**近乎同分**的条目排序不固定（同一进程两次调用、或跨进程都可能互换；返回的分数按名次换算、末位有并行求和的浮点抖动），所以关键词检索的前 5 条在尾部会变。`retrieval_colloquial_p0` 与 `rag_p1` 各连跑 10 次：全部指标、逐条命中与 gold 名次完全稳定，只有非 gold 的尾部条目变化。比较器因此不比检索到的列表 | `tests/test_retrieval_mode_eval.py::test_colloquial_baseline_comparator` | 记录（库行为，不在本仓库修） |
| F19 | S8 读码发现：`ProviderGateway._classify_error` 对非 HTTP 异常一律判永久错误，并把 `executor` 抛出的异常包成 `GovernanceError`——远程 MCP 的 `MCPError` 码若不还原，调用方只看到 `PROVIDER_UNAVAILABLE`；`RunCancelled` 是 `Exception` 子类，也会被包成永久错误 | `tests/test_mcp_http_client.py::test_tool_calls_are_never_replayed`、`::test_cancellation_propagates_out_of_the_event_stream` | S8 已处理（`run_governed` 还原原因；取消用 `BaseException` 哨兵穿过 gateway） |

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
- **池租约（S5）**：Web 会话工厂调用 `McpConnectionPool.lease(config)`（`mcp/pool.py`），把
  `lease.assembly` 交给 `borrowing`，`lease.release` 登记到 run 的 `ExitStack`。`lifecycle: shared`
  的 server 由池长驻并跨租约复用（按单台 server 指纹 = 已校验配置 + 确认位 + 子进程 env 摘要判断
  是否换新进程，退役连接在最后一个租约释放后关闭）；`per_run` 的 server 由租约现开、释放时关闭。
  池由 `WebAgenticSessionFactory` 持有，`RunManager.close()` 排空 run 之后关闭。

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

### 3.6 按意图最小暴露（S4）

实现：`src/water_agent/plugins/exposure.py`；决策记录 `docs/design/p7-step-4-decision.md`。

- **每轮决定一次**：外层图 `agent_loop` 在输入护栏放行之后、调用模型之前调用
  `ToolExposurePolicy.decide(query, previous_turn_tools=…)`，结果（`ExposureDecision.to_state()`）存进
  state 的 `tool_exposure`；引用校验回流再进 `agent_loop` 时复用，`PER_TURN_RESET` 逐轮归零。
- **分组**来自插件贡献（`PluginContribution.exposure` / `exposure_groups()`）：

  | 组 | 规则 | 说明 |
  |---|---|---|
  | `clauses`、`hydraulics` | core | 每轮必开、永不丢弃（铁律①②） |
  | `materials`、`drawing` | intent | 关键词 / 正则命中才开（`plugins/builtin.py` 的 `MATERIALS_EXPOSURE` / `DRAWING_EXPOSURE`） |
  | `mcp:<server_id>` | ambient | 每台暴露了工具的 server 一组，不看意图，预算不够最先让位 |
  | 未声明规则的插件 | ambient | 缺省值 |

- **入选顺序** core → 本轮命中的 intent → 上一轮实际调用过工具的组（sticky，只回看一轮，轮次切分与
  `prompting/history.py` 同口径）→ ambient；按这个顺序逐组入选，放不下的整组跳过并记进 `dropped`。
- **两项预算**：发出的 schema ≤ `EXPOSED_SCHEMA_BUDGET = 4500`（与 `tests/test_answer_cache.py` 同数、
  同序列化口径——`bound_tool_definition` 由 `_BudgetChatModel.bind_tools` 与预算判定共用）；按意图格式
  渲染的指引 ≤ `SEGMENT_BUDGETS["tool_guide"]`（800）。core 自身超任一预算 → 装配期 `ValueError`。
- **模型侧**：`_BudgetChatModel` 在 `_generate` 与 `_stream` 前把 `tools` 过滤到本轮开放的子集；ToolNode
  仍持全部已注册工具。
- **提示侧**：`PromptAssembler(exposure=…)` 在 state 有 intent 决定时按决定渲染指引；`full` 策略、没有决定
  或没有策略时走现行格式（逐字不变）。
- **暴露不是授权**：未开放的工具被调用时照常经 `ToolRegistry.invoke` 执行（护栏与主体授权不变），trace 另记
  `unexposed_call`；每轮另记一条 `tool_exposure`。两类事件都显式 `query=""`，不写用户原文。
- **回滚开关** `WATER_AGENT_TOOL_EXPOSURE=intent|full`（缺省 `intent`，其他值装配期 `ValueError`）；`full`
  下模型收到的工具与指引与 S3 逐字相同（`tool_exposure_p0` 逐轮比对 0 差异）。
- **结构约束**：两处生产 `build_agentic_app` 调用都传 `exposure=`（`tests/test_plugin_invariants.py::test_production_app_receives_exposure_policy`）。

### 3.7 危险操作人工确认门（S7）

实现：`src/water_agent/graphs/confirmation.py`；决策记录 `docs/design/p7-step-7-decision.md`。

- **入口决定可用性**：`open_gateway_sessions` / `pre_connection_skips` / `McpConnectionPool.lease` 都带
  `confirmation_ready`（缺省 `False`），池的单台 server 指纹纳入它；只有 `WebAgenticSessionFactory` 传 `True`
  并给会话 `AgenticSession(confirmation=True)`。配置里写 `confirmation_handler_ready=true` 仍在加载期拒绝。
- **延迟工具**：`assemble_tools` 对运行时绑定 `requires_human_confirmation=True` 的工具交给 ToolNode
  `tool_spec_to_deferred_tool`（同名、同 schema、`return_direct=True`），执行时不调用任何东西，只回
  `{"status": "pending_confirmation", "confirmation_id", "tool"}`，待确认标记写在 `ToolMessage.artifact`
  （远端输出伪造不了）；注册表里仍是真实 `ToolSpec`，`ToolAssembly.confirmation_tools` 列出它们。
  同一条消息里并行的不需确认的调用照常执行（ToolNode 按 `Send` 并行）。
- **外层图**：`agent_loop` 之后本轮有待确认消息 → `confirmation_request`（每个待确认调用经过一次，记
  `confirmation_requested`，执行每轮上限 `MAX_CONFIRMATIONS_PER_TURN = 3`，第 4 次即 `failed`）→
  `confirmation_gate`（`interrupt(request)`）。resume 值只认 `{"confirmation_id", "approved": bool, "reason"}`，
  非法值带 `error` 再挂起、不抛；批准 → `ConfirmationGrant`（工具名 + 参数摘要 + 主体 hash，单次有效，进程内
  已用集合）→ `ToolRegistry.invoke_confirmed` 执行一次 → 同 id 替换；拒绝 → 同 id 替换为
  `TOOL_CONFIRMATION_REJECTED` observation。同一条 AI 消息里多条逐条确认，之间不调模型。`confirmation=None`
  时图结构不变。
- **审计**：state `confirmation_log`（跨轮累积，不在 `PER_TURN_RESET`）：id、server、工具名、参数摘要、主体
  hash、决定、原因（输出护栏脱敏、截断 200 字）、决定时间、结果码；trace `confirmation_requested` /
  `confirmation_decided` / `confirmation_executed`，`query=""`，不写参数原文。
- **恢复时的本轮召回结果**：`memory_context` / `upload_context` / `attachment_context` 经 checkpoint 读回是 dict，
  直接给提示装配会 TypeError；agent_loop 把它们换成 `ConfirmationRuntime.turn_contexts` 交回的原对象（Web 同
  进程恢复），取不到（进程重启过）就换成 `None` 并记 `turn_recall_context_unavailable`，不写回状态。
- **Web**：run 状态 `waiting_confirmation`（无迁移）；SSE `confirmation_required` 后以
  `stream_end(waiting_confirmation)` 结束；挂起期间发消息 409 `WEB_CONFIRMATION_PENDING`（读 checkpoint 判定）；
  `POST /api/runs/{run_id}/confirmation`：归属 404 → 写权限 403 → 过期 409 `WEB_CONFIRMATION_STALE` → 202
  （库里比较交换 `waiting_confirmation → queued`，返回 `event_cursor`）；同一个 run 继续；历史投影带
  `pending_confirmation`。前端 `ConfirmationCard.tsx` + `confirmationApi.ts`。


### 3.8 Web 扩展面板（S6）

实现：`src/water_agent/mcp/web_layer.py`、`src/water_agent/mcp/credentials.py`、`src/water_agent/mcp/importer.py`、
`src/water_agent/webapp/mcp_admin_routes.py`、`web/app/src/McpAdminPanel.tsx`；决策记录 `docs/design/p7-step-6-decision.md`。

- **第三层配置 Web 管理层**：`WATER_AGENT_MCP_WEB_LAYER` 指向的 JSON（`{"version", "revision", "servers": [...]}`），
  合并顺序 仓库默认层 → 部署覆盖层 → Web 管理层（`config.load_layered_gateway_config`，会话 / runner / Web 共用）。
  每台只能写 `server_id` / `disabled` / `allow`（外加 `updated_at` / `updated_by` 元数据），`allow` 整体替换；
  不能新增 server——下层没有的 id 是孤立条目，加载时忽略、列表接口列出；损坏 / 写了不允许的键即
  `MCP_CONFIG_INVALID`。未设变量 = 没有这一层（与 S3 逐字相同）；设了但文件不存在 = 空层。运维两层先过模型
  校验（报错指向运维的文件），**路径存在性在最终配置上检查**：面板停用一台 `cwd` 已不存在的 server，其余照常加载。
- **写入**：`WebLayerStore.update` 持 `<文件>.lock` 的 `fcntl.flock`（只覆盖同一台主机）→ 比对 `expected_revision`
  （不符 409）→ 整份合并并完整校验（`check_candidate`，与加载同一套）→ 同目录临时文件 + `os.replace`；
  校验不过 422 且不落盘。整层恢复删除文件（损坏时也可用）。
- **凭据槽位**：运维在默认层 / 覆盖层声明 `credential_env`（环境变量名；大写、不与内联 `env` 重名、拒绝 `PATH` /
  `HOME` 与加载器 / 解释器会执行的变量），面板只给已声明的槽位写值（只写不读，`<credentials_dir>/mcp/<server_id>/<NAME>`，
  0600，不进备份）；`child_env` 注入顺序 `env_file` < 槽位 < 内联 `env`；有槽位没填值 → 连接前跳过
  `MCP_CREDENTIAL_MISSING`、不 spawn。值进子进程 env 摘要，改值即换 shared server 指纹。
- **导入预览**：`preview_import`（Claude Code / Cursor JSON、Codex TOML；函数本身 ≤ 64 KiB UTF-8，经 HTTP ≤ 16 000 字符——
  请求体上限要给最大的合法 JSON 请求体留 4 倍余量）产出覆盖层格式提案：每台
  `disabled: true`、不写 `allow`、`env` 值一律丢弃（键变槽位，不能当槽位的键只列名字）、url / `type: http` 在 S8 起生成 `streamable_http` 禁用提案（请求头 / 令牌值丢弃），旧 HTTP+SSE 仍不支持、
  冲突 id 改名；逐条模型层校验。不落盘、不 spawn、不应用。
- **探测**：只对启用的 server，经 Web 会话工厂的连接池借一次租约（`confirmation_ready=True`），视图只含目标与池里
  已在用的 shared server（不顺带 spawn 其他 per_run server，也不让在用的 shared 连接退役）；只返回远端工具名、
  是否暴露与跳过码（`SkippedServer.remote_tool` 承载"全部未放行"时的工具名），不返回远端描述与跳过 detail；同一台
  server 同时只允许一个探测。
- **API**：`GET /api/admin/mcp/servers`、`PATCH /api/admin/mcp/servers/{id}`、`DELETE …/{id}/overrides`、
  `DELETE /api/admin/mcp/web-layer`、`PUT|DELETE …/{id}/credentials/{name}`、`POST …/{id}/probe`、
  `POST /api/admin/mcp/import-preview`；全部 `require_admin`，写接口加 CSRF；读接口不返回 `args`、`env` 的值、
  `env_file` 内容、完整命令路径（只给文件名）与任何凭据值；错误不回显请求值。变更进 `webapp/auth/audit.py`
  的结构化事件（`mcp.server_updated` 带字段名与 `allow` 增减；凭据只记槽位名与动作）。能力端点只给计数。
- **生效时机**：下一个 run——Web 每个 run 都重新 `load_mcp_gateway()`，连接池按单台 server 指纹换新进程；正在跑的
  run 不受影响。

### 3.9 Skill 库（S9）

实现：`src/water_agent/skills/`、`src/water_agent/tools/load_skill.py`、`src/water_agent/plugins/builtin.py::SkillsPlugin`、
`src/water_agent/webapp/skill_routes.py`、`web/app/src/SkillLibraryPanel.tsx`；评测 `src/water_agent/harness/skill_eval.py` +
`data/eval/skill_p0.jsonl`；决策记录 `docs/design/p7-step-9-decision.md`；威胁模型 T12。

- **格式**：`<root>/<name>/SKILL.md`，YAML frontmatter（白名单 `name` / `description` / `version` / `triggers` /
  `requires_tools`，`extra="forbid"`）+ Markdown 正文；`name` 是 kebab-case 且与目录名一致；描述 ≤ 300、正文 ≤ 6000 字符、
  文件 ≤ 64 KiB、triggers ≤ 12 个 × 24 字、requires_tools ≤ 8 个；只扫根目录的直接子目录，指向根目录之外的符号链接不加载。
- **两级来源**：随包内置（`skills/builtin/`，仓库评审，package-data 分发）+ 运维只读目录（`WATER_AGENT_SKILLS_DIR`）。
  同名时内置优先，运维那份记 `SKILL_SHADOWED`；目录设了却不存在 / 不是目录 → `SKILL_LIBRARY_INVALID` 装配期失败；
  单个坏 skill 只记错误码（`SKILL_INVALID` / `SKILL_UNSAFE` / `SKILL_CONFLICTS_IRON_RULE` / `SKILL_TOOL_UNAVAILABLE`），其余照常。
  不扫描项目 / 用户目录、上传文件或 MCP 资源。
- **装配期安检**（`skills/safety.py::check_skill`，顺序固定）：描述与正文过 `detect_injection`（命中即 `SKILL_UNSAFE`，
  不脱敏放行）→ 五条铁律否定式改写规则（不调计算工具、自行估算数值、不声明引用、不检索、编造规范号；
  `SKILL_CONFLICTS_IRON_RULE`）→ `requires_tools` 按**本次装配里先于 skills 的插件实际贡献的工具**核对。
  规则是确定性正则，与 P2.5 护栏同一局限：必然存在未覆盖的改写变体。
- **开关**：`WATER_AGENT_SKILLS=off|on`（代码缺省 off，取值非法装配期失败；Web compose 设 on）；off 或没有已启用 skill 时
  插件 `skipped`、不注册 `load_skill`，系统提示与工具逐字同 S7。
- **`load_skill`**：`ToolSpec` 在 `tools/`，能力 `LOAD_SKILL`（只读，进默认能力集与 Web 成员能力集）；参数 `skill` 是目录名
  `enum`（不是 `name`——与 `ToolRegistry.invoke(name, **args)` 的位置参数冲突）；目录写在工具描述里（≤ 900 字符，超出按字母序
  从末尾截掉并记 `SKILL_CATALOG_FULL`）；只回回执（名字 / 来源 / 版本 / 摘要），不回正文。skills 组排在 `mcp_gateway` 之后，
  intent 暴露（各 skill 的 triggers ∪ `SKILL_FIXED_KEYWORDS`），sticky；最紧组合下整组让位并记 dropped。
- **正文注入**：动态段 `skill_context`（工具指引之后，6000 字预算，超出整份丢弃最早加载的并写明）只从本次装配的
  `SkillLibrary` 取正文、以 `SYSTEM_POLICY` 信封包装；"加载过哪些"只认 thread 里 `load_skill` 的成功回执，state 键
  `loaded_skills` 只存名字 / 来源 / 版本 / 摘要，正文从不进 checkpoint；skill 被删 / 改（摘要变化）/ 停用 / 未过安检后不再
  注入并记 `skill_unavailable`；正文里形似 `<skill_content` 的文本改成全角尖括号。`INSTRUCTION_BEARING_SOURCES` 不变。
- **缓存键**：新失效因子 `skill_library_digest`（全部已启用 skill 的 name + 来源 + 摘要；用全量库而非本轮加载集合）；
  没有 skill 时为空串，键与加字段前逐字相同。
- **面板与 API**：`GET /api/skills`（已改密用户：名称 / 来源 / 版本 / 描述 / triggers / requires_tools / 状态，外加
  `can_manage`；未加载的只给名字与错误码；不含正文与运维目录路径）、`GET /api/admin/skills/{name}`（`require_admin`，正文与
  摘要，只读）、`PATCH /api/admin/skills/{name}`（`require_admin` + CSRF，只认 `{"disabled": bool, "expected_revision": int}`）。
  启停写进 S6 Web 管理层的新顶层键 `skills`（每条只有 `name` / `disabled` + 元数据，同一份文件、同一套 revision / 文件锁；
  启用即删条目，没有停用记录时不写该键）；会话与 runner 装配时读停用列表（`disabled_skill_names()`），层文件坏即
  `MCP_CONFIG_INVALID` 响亮失败。审计 `skill.updated` 只记名字与动作。面板不能新增、编辑或上传 skill。设置页 Skill 库区下方
  复用能力端点的计数展示 MCP（不新增 MCP 列表接口）。
- **部署**：compose 给 app 挂 `app_skills:/etc/water-agent/skills:ro`，在 `/var/lib/water-agent` 之外、不随备份流转；空卷照常启动。
- **评测**：`skill_p0`（26 条：目录与暴露 9 / 注入与来源 7 / 停用 · 删除 · 遮蔽 5 / 铁律 5），十项硬指标，接进
  `scripts/ci_gate.sh` 与入仓基线 `data/eval/skill_p0_main.json`（`scripts/compare_skill_p0.py`）。脚本模型，不证明真实模型
  的加载与遵循（P7-S9-A13）。


### 3.10 Streamable HTTP 传输与 OAuth 2.1 服务级授权（S8）

实现：`src/water_agent/mcp/http_transport.py`（**唯一出站模块**）、`mcp/http_client.py`、`mcp/oauth.py`、`mcp/oauth_store.py`、
`mcp/config.py`（`transport` / `url` / `oauth` / `bearer_credential`）、`webapp/mcp_admin_routes.py`（授权路由）、
`web/app/src/McpAdminPanel.tsx`；评测 `src/water_agent/harness/mcp_http_eval.py` + `data/eval/mcp_http_p0.jsonl` +
`tests/fixtures/fake_mcp_http_server.py`；决策记录 `docs/design/p7-step-8-decision.md`；威胁模型 T13。

- **出口只由运维决定**：`url` 与 `command` 同属装配期配置，只能写在仓库默认层 / 部署覆盖层（Web 管理层可写键不变）；模型输入、
  远端响应、请求头都改变不了连接目标。远程工具仍持 `Capability.MCP_INVOKE`（与 stdio 一致）；`NETWORK_FETCH` 仍在危险集、
  `Principal` 构造期仍拒、护栏 `tool.dangerous_capability` 不变。
- **URL 加载期校验**（`parse_server_url`）：只 `https://`（`allow_private_network=true` 才收 `http://`），拒 userinfo / fragment /
  控制字符；主机名 IDNA 归一；最后一个标签是十进制或 `0x` 十六进制数字的主机名必须是规范点分十进制 IPv4
  （`2130706433`、`0177.0.0.1`、`127.1`、`0x7f.0.0.1` 一律拒）。
- **连接期 SSRF 校验（每一次建连）**：解析出**全部**地址、整体分类，任一落在禁止段即 `MCP_DESTINATION_FORBIDDEN`，发生在发出
  第一个字节之前。回环 / 私有（RFC 1918、`fc00::/7`、CGNAT）只在 `allow_private_network=true` 时放行；链路本地、云元数据
  （含 `100.100.100.200`、`fd00:ec2::254`）、未指定、组播、保留段永远拒；IPv4-mapped IPv6、6to4、NAT64、Teredo 按内嵌 IPv4 分类。
  连接**钉死**到校验过的 IP（TLS 的 SNI 与证书仍按主机名），3xx 一律不跟随。**每个请求一次新连接、各自重新解析并校验**
  （没有 keep-alive）：DNS 在首次校验后换成元数据地址，下一个请求会被拒绝、对它零连接。
- **协议**：以 2025-11-25 会话制为主（`initialize` 不带会话号 / 版本头，之后每个请求带 `MCP-Session-Id` 与
  `MCP-Protocol-Version`；响应是单个 JSON 或 SSE 流；404 → 重新 initialize 一次并重发被拒绝的请求；关闭时 `DELETE`，405 视为正常；
  server→client 请求回 `MethodNotFound`、通知记录不响应；不开 GET 监听流、不做 `Last-Event-ID` 续传）。接受 server 协商出的
  `2025-06-18` / `2025-03-26`。只讲 2026-07-28 的 server（`initialize` 被 `400` + `-32020 / -32021 / -32022` 或 `data.supported`
  含 `2026-07-28` 的 JSON-RPC 错误拒绝）记 `MCP_PROTOCOL_UNSUPPORTED`，不回落到已废弃的 HTTP+SSE。同一会话上的请求串行。
  **工具调用不自动重发**：流在响应前断开、超时、5xx 都直接报错；唯一的重发是 404（会话已终止、请求被拒绝在处理之前）与一次 401
  之后的令牌刷新——两者都是 server 明确表示没处理。
- **治理**：`PROVIDER_CALL_SITES` 7 → 8（`mcp_streamable_http`，`ProviderKind.MCP_REMOTE`、`CallCategory.MCP_TOOL_CALL`、
  `required_capability=MCP_INVOKE`）；initialize、通知、`tools/list`、`tools/call`、对 server 请求的回绝、`DELETE`、授权端点的
  发现 / 注册 / 令牌 / 撤销请求**每一个**都经 `ProviderGateway.call`（画像 `max_attempts=1`，熔断按 `mcp:<server_id>` 计，usage 记
  未知）。`tools/call` 的主体是调用者的 `RequestContext`（`AgenticSession` 的每个入口与 runner 经 `mcp_request_scope` 下传，缺失
  fail closed）；装配期的生命周期请求与授权端点用 server 级服务主体（运维配置驱动）。出站静态扫描新增特征
  `http.client.HTTPS?Connection`。
- **认证二选一**（同时配即 `MCP_CONFIG_INVALID`）：**静态 Bearer**——`bearer_credential` 引用本 server `credential_env` 里的槽位
  （S6 的凭据卷，值只写不读），值只进 `Authorization` 头，缺值 `MCP_CREDENTIAL_MISSING`，server 拒绝 `MCP_CONNECT_FAILED`；
  **OAuth 2.1 服务级授权**——管理员在面板授权一次，令牌属于这台 server 的部署、所有用户的 run 共用（授权前面板明确提示）。
- **OAuth 流程**（客户端 MUST 项逐条有测试，见 `tests/test_mcp_oauth.py`）：不带令牌的探测请求取 401 → `resource_metadata`
  优先，否则路径插入与根路径两个 well-known → `authorization_servers` 取第一个且必须在 `allowed_issuers`（没配只接受第一个并记录）、
  元数据里的 `resource` 必须等于本 server 的规范 URI → RFC 8414 与 OIDC 三 / 两个发现地址（`issuer` 必须一致）→ 缺 `S256` 拒绝
  继续 → 预注册或动态注册（RFC 7591，按 issuer 与回调地址缓存）→ 授权请求带 PKCE S256 / 一次性 `state`（10 分钟、总量 64）/ 规范
  `resource` / `scope`（挑战 > `scopes_supported` > 运维配置）→ 回调只认服务端 state、`iss` 必须等于记录的 issuer → 令牌请求带
  `code_verifier` 与同一个 `resource`。所有授权服务器端点必须 `https://`，同样过 SSRF 校验与不跟随重定向。
  `403 insufficient_scope` 如实报出所需 scope（`MCP_OAUTH_INSUFFICIENT_SCOPE`），**不自动升级**。
- **令牌**：存凭据卷 `<credentials_dir>/mcp/<server_id>/oauth.json`（文件 0600、目录 0700、原子替换，不进备份，只写不读，`repr` 隐去令牌
  字段）；只经 `Authorization: Bearer` 头发给签发它的 issuer 对应的这一个 MCP endpoint（URL 变了 / issuer 不在白名单就不发）；
  即将过期（提前量 `min(60s, 寿命一半)`）或收到 401 时刷新**一次**，轮换后的 refresh token 替换旧值；刷新被拒 → 清除令牌、
  `MCP_OAUTH_REQUIRED`、记审计事件；授权服务器暂时不可用 → 令牌保留。连接池指纹含**授权身份摘要**（`authorization_id`，刷新不换、
  重新授权 / 清除换）与静态 Bearer 值的摘要。
- **Web**：`POST /api/admin/mcp/servers/{id}/oauth/start`（返回授权 URL 与"所有用户共用该授权身份"的提示）、
  `GET /api/admin/mcp/oauth/callback`（303 回应用首页，查询串只有结果 / server id / 稳定错误码）、
  `DELETE /api/admin/mcp/servers/{id}/oauth`（撤销：尽力调用 revocation endpoint，再删本地令牌），全部 `require_admin`，写接口 CSRF。
  `WEB_PUBLIC_BASE_URL` 是回调地址的唯一来源（未配 / 不合法 = 不提供授权，**不从请求的 Host 头推导**）。审计
  `mcp.oauth_authorized / revoked / failed / refresh_failed` 只记 server_id、issuer、scope 与稳定码，永不记令牌 / 授权码 / verifier / state。
  列表接口新增 `transport` / `url_host`（只有主机名）/ `allow_private_network` / `auth_kind` / `oauth` 状态；面板改不了 URL 与 oauth。
- **评测**：`mcp_http_p0`（44 条：传输 5 / 协议 8 / SSRF 7 / 静态 Bearer 3 / OAuth 14 / 确认门 2 / 入口 5，会话 38 · Web 4 · runner 2 三条入口），八项硬指标，
  接进 `scripts/ci_gate.sh` 与入仓基线 `data/eval/mcp_http_p0_main.json`（`scripts/compare_mcp_http_p0.py`，报告逐字可复现）。
  只对本地假 server 与假授权服务器验证，不证明任何真实远程 MCP 或企业单点登录（P7-S8-A14 / A15）。

### 3.11 OpenAI 兼容端点与检索模式（S10）

S10 让对话模型与 embedding 都可以换成任意 OpenAI 兼容端点，并让"只配一个对话 Key"的部署也能用 RAG（检索走关键词模式）。
权威决定见 `docs/execution-prompts/P7-S10-openai-compatible-endpoints.md` §0，实测与偏离见 `docs/design/p7-step-10-decision.md`。

- **端点解析单一来源** `generation/endpoints.py`：`resolve_chat_endpoint()` 优先级 显式参数（CLI `--model` / `--base-url` /
  `--api-key-env`）> 运维 `WATER_AGENT_LLM_BASE_URL` / `WATER_AGENT_LLM_MODEL` > 设置面板写的 `<凭据卷>/llm_endpoint.json` > 缺省
  DeepSeek；`resolve_embedding_endpoint()` 对称（`WATER_AGENT_EMBEDDING_*`，缺省百炼 `MODEL_NAME`）。CLI / Web / runner /
  legacy 生成器 / docgen 润色 / judge / 探针 / 预检全部经它（`build_chat_model` 只是构造器）。base_url 只收 http(s)、拒 userinfo 与
  fragment；只解析，不发请求。
- **密钥只发给所属服务商**：专用 Key `WATER_AGENT_LLM_API_KEY` / `WATER_AGENT_EMBEDDING_API_KEY`（进凭据白名单，env > `_FILE` >
  凭据卷）未配置时，只有端点仍是 DeepSeek / 百炼才回落 `DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY`；端点改到别家就不回落，预检写明缺
  哪一项。`LOCAL_MODEL_*`（P5 Step 46）保留且语义不变，与 `WATER_AGENT_LLM_*` 同时启用即配置错误（Web 启动期失败）。
- **兼容性硬门槛** `generation/compatibility.py`：非缺省对话端点（`base_url` 或 `model` 与 DeepSeek 缺省值不同）必须有一份端点 +
  模型精确匹配、探针版本一致（`ENDPOINT_PROBE_VERSION`）、恰好七项全部 passed 的报告（`WATER_AGENT_LLM_COMPATIBILITY_REPORT`，或面板
  "运行兼容性检查"写进凭据卷的那份）才允许装配——会话、CLI legacy、runner 真实组件、docgen 润色在装配期拒绝，Web 预检 424
  `WEB_MODEL_NOT_VERIFIED`。**缺省 DeepSeek 端点免检是唯一例外**。检查集：`function_calling` / `structured_citations` /
  `programmatic_calculation` / `tool_schema_budget`（与本地模型探针同一实现）+ `streaming_usage` / `error_handling` /
  `cancellation_timeout`（真实测量）；离线的 `memory_p0` 不在远程端点的检查集里。探针 `harness/endpoint_compat.py`（会产生约五次计费
  调用；不重试；报告 0600，不含 Key 与请求原文；无 Key 结构化 SKIP）。judge 是评测工具，不设门槛。
- **治理与缓存**：不新增 `ProviderKind`、出站清单仍是 8——`DEEPSEEK_CHAT` / `BAILIAN_EMBEDDING` 与对应调用点名字保留、语义扩为"OpenAI
  兼容对话 / embedding 端点"；`WATER_AGENT_LLM_TIMEOUT_SECONDS` 覆盖对话画像超时、`WATER_AGENT_LLM_PRICE` 挂在当前模型名上
  （`confirmed=False`，不做硬拒绝）；回答缓存键加 `model_endpoint_digest`（缺省为空串，与历史键同键）；`GOVERNANCE_CONFIG_VERSION`
  → `p7-s10-2026-10-13`。
- **索引与 embedding 绑定**：`scripts/build_index.py` 建库时写 `<db>.index_meta.json`（模型、端点主机名、维度、`index_version`、
  `retrieval_modes`，入库前写出），往别的模型建的索引里追加即拒绝；检索装配比对当前 embedding 配置，模型或维度不一致、纯关键词索引、
  元数据读不出 → 不做向量检索（`RETRIEVAL_EMBEDDING_MISMATCH`）。没有元数据的旧索引按百炼缺省模型对待，运行状态写 `index_meta=absent`。
- **检索模式** `WATER_AGENT_RETRIEVAL_MODE=auto|hybrid|text`（缺省 auto）：auto 下 embedding 已配置且与索引匹配走混合检索，否则走关键词
  检索（BM25，`search_hybrid(零向量, hybrid_alpha=0)`，**一次 embedding 都不调用**）；hybrid 强制（缺 embedding / 不匹配即失败）；text
  强制。会话与 runner 共用 `runtime/agentic.py::build_index_retriever`。`runtime_status` 的 `retrieval_mode` / `retrieval_mode_reason` /
  `retrieval_mode_detail` / `index_meta` 进 trace（`retrieval_mode` 事件）、会话结果、Web 结果投影、CLI 与 runner 输出，前端检查面板如实
  标出关键词模式；关键词检索的回答缓存键与混合检索不同。Web 预检的语义从"必须有 embedding"变为"必须能检索"：只在强制 hybrid 时
  424。`build_index.py --text-only` 不调 embedding、不要求 Key。
- **Web 面板**：`GET/PUT /api/settings/llm-endpoint`、`POST /api/settings/llm-endpoint/compatibility`——成员只看主机名与模型名；管理员 +
  CSRF 才能写，只收 https（`http://localhost` / `127.0.0.1` / `local-model` 例外）；运维 env 设了或本地模型启用即只读；改端点或模型即
  作废面板写的兼容报告；检查同一时间只一个；审计只记主机名、模型名与结果码。
- **评测** `retrieval_colloquial_p0`（54 条，与 rag_p1 同一批 gold 条款的口语化 / 同义改写问法，Step 1 冻结）：text 模式接进
  `ci_gate.sh` 与入仓基线（`scripts/compare_retrieval_colloquial_p0.py`）；hybrid 需要百炼 Key，不进门禁。**text 模式口语化 Recall@5
  只有 46.30%**（rag_p1 上 98.28%），只配对话 Key 时口语化问法召回明显不足，面板与决策记录如实写明。

**S15 增补：模型目录与按会话 / 按消息选择模型**（权威规格 `docs/design/18-p7-multi-model.md`，实测与偏离见 `docs/design/p7-step-15-decision.md`）。
S10 解决"整个部署用哪个模型"，S15 解决"同一端点、同一把 Key 下，用户这一次想用哪个模型"。

- **模型目录** `generation/model_catalog.py`：凭据卷 `llm_models.json`（0600、原子写），≤ 32 条；条目 `{id, label ≤ 40, tier: fast|advanced,
  thinking: default|off|roundtrip, enabled, allowed_roles?, price?}`，**不收 `base_url` / `api_key`**（首版同端点、同 Key 槽）。`tier` 只是标签，
  不参与任何路由。`thinking: off` 才附加 `extra_body={"thinking":{"type":"disabled"}}`；`default` 什么都不传（缺省）；`roundtrip` 只能保存、不能被选
  （S16 才放行）。
- **逐对兼容性验证**：报告按 (端点, 模型) 存 `llm_compatibility_reports/<sha256(端点|模型)[:16]>.json`，`endpoint_verification` 先查逐对、再查原单文件，
  `matches()` 复核（文件名只是索引、不是信任根）；原 7 项检查与 `ENDPOINT_PROBE_VERSION` 不变；探针 `run_endpoint_compatibility(model=…)` 与生产装配
  带**同一份** `extra_body`。免检仍只属于缺省端点 + 字面 `MODEL_NAME`，目录里的模型一律走逐对验证。
- **选择链路**：优先级 消息 `model` > 会话默认 `web_sessions.model` > 全局；`PATCH /api/sessions/{id}` 改会话默认（放在 `run_routes.py`，与消息路由同处，
  避免与 `workspace_routes` 循环导入）。校验顺序（`webapp/model_choice.py`）：锁定（`LOCAL_MODEL_ENABLED` 或 env 锁定端点）→ 目录 →
  `roundtrip` 拒绝 → 角色 → 真实工厂下 `chat_model_problem(model=…)`；**403 先于 424**。选了模型而会话工厂没声明 `supports_model_choice`
  直接报错，绝不静默用全局模型作答。`runs.model` 落库，`resume` 以它为准（`NULL` = 当时用全局模型）。迁移 `0008_step15_model_choice`。
- **模型从不被自动更换**：失败、超时、预算、resume 都不能换成目录里的另一个模型或全局模型；会话默认模型之后被停用 / 删除，下一条消息 422，
  由用户改选或清除。
- **治理与可观测**：不新增 `ProviderKind` / `Capability` / 出站点（仍是 8）；成本按模型归因（`estimate_model_cost`，运行期查目录价，**未登记价格记
  `None` 而不是 0**，目录之外与此前逐字相同）；`model_selected` trace 事件；finalize payload 有值才带 `model`。
- **前端**：输入框旁的模型选择器（目录非空且有可选项才渲染）、会话默认模型、Inspector「本次模型」、模型页里的管理员目录管理区；文案不暗示"进阶 = 更准"。
- **评测** `model_choice_p0`（50 条合成，11 项硬指标）：生产 Web 层 + 记录型会话工厂 + 本地假端点；接进 `ci_gate.sh` 与入仓基线
  `data/eval/model_choice_p0_main.json`（`scripts/compare_model_choice_p0.py`）。**不证明任何真实模型的作答质量**，更不证明换模型后质量不变。

---

## 4. 不变量（Invariants）

| # | 不变量 | 强制位置 | 状态 |
|---|---|---|---|
| P7-I01 | src 里构造面向模型的 `ToolRegistry(` 只在 `plugins/assembly.py`；内部注册表白名单写死并附理由（`mcp/memory_adapter.py`、`runtime/governance/scenarios.py`、`harness/memory_eval.py`、探针 `harness/local_model_compat.py`），白名单条目不再对应真实代码同样判失败；用 AST 判定 | `tests/test_plugin_invariants.py` | delivered |
| P7-I02 | `tool_spec_to_base_tool(` 只在 `plugins/assembly.py` 与上述探针里调用 | `tests/test_plugin_invariants.py` | delivered |
| P7-I03 | `runtime/agentic.py` 与 `harness/runner.py` 都调用 `assemble_tools` | `tests/test_plugin_invariants.py` | delivered |
| P7-I04 | 内置插件顺序 `BUILTIN_PLUGIN_ORDER == ("clauses", "hydraulics", "materials", "drawing", "mcp_gateway", "skills")`（P7-S9 末尾追加 `skills`：它按先于它的插件贡献的工具核对 `requires_tools`） | `tests/test_plugin_invariants.py` | delivered |
| P7-I05 | `plugins/` 里不出现 `ToolSpec(` 调用；`ToolSpec` 定义留在 `tools/`（F12 的扫描前提） | `tests/test_plugin_invariants.py` | delivered |
| P7-I06 | 插件能追加给主体的能力只限 `GRANTABLE_CAPABILITIES = {MCP_INVOKE}` | `plugins/assembly.py` + `tests/test_tool_plugins.py` | delivered |
| P7-I07 | 只有 `runtime/agentic.py` 与 `harness/runner.py` 调用 `build_agentic_app`（装配快照捕获点的前提） | `tests/test_plugin_invariants.py` | delivered |

---

## 5. 分步交付

| 步 | 对应 | 内容 | 执行 Prompt | 状态 |
|---|---|---|---|---|
| S0 | — | 规格 + 任务书 + 防漂移 harness（H0–H5）+ 装配快照基线 | 范围块在任务书附录 | delivered |
| S1 | F7 | 确认门执行期强制 | `docs/execution-prompts/P7-S1-confirmation-gate.md` | delivered |
| S2 | Step A | 插件契约 + `assemble_tools()` 单入口 + runner 统一 | `docs/execution-prompts/P7-S2-tool-plugin-assembly.md` | delivered |
| S3 | Step B | 配置分层（仓库默认层 + 部署覆盖层）、每台 server 结果可观察、`disabled`、子进程工作目录隔离；修 F6/F9/F10/F13/F15/F16；**entry point 发现推迟**（见下） | `docs/execution-prompts/P7-S3-layered-mcp-config.md` | delivered |
| S4 | Step C | 按意图最小暴露 / 工具分组（每轮确定性决定，core = 检索 + 引用 + 流速计算）；修 F4/F5（先建 `tool_exposure_p0`）；回滚开关 `WATER_AGENT_TOOL_EXPOSURE=full` | `docs/execution-prompts/P7-S4-intent-tool-exposure.md` | delivered |
| S5 | Step D（一） | MCP 长驻连接（stdio）：`lifecycle: shared` 的 server 跨 run 复用进程（修 F8，缺省 `per_run` 不变）、客户端请求串行化（修 F17 并发取错响应）、按单台 server 指纹重载 | `docs/execution-prompts/P7-S5-shared-mcp-connections.md` | delivered |
| S6 | Step E1 | Web 扩展面板：第三层配置 Web 管理层（只改启停 / allow）、运维声明的凭据槽位（值只写不读）、导入 `mcpServers` 只生成禁用的待审提案、探测、审计 | `docs/execution-prompts/P7-S6-mcp-admin-panel.md` | delivered |
| S7 | Step E2 | HITL 确认门：需确认的工具在内层以 `return_direct` 延迟工具停下、外层 `confirmation_gate` interrupt、一次性执行凭证、`waiting_confirmation` 态、批准/拒绝 API、`confirmation_log` 审计、前端确认卡片（只有 Web 入口接线） | `docs/execution-prompts/P7-S7-hitl-confirmation.md` | delivered |
| S8 | Step D（二） | Streamable HTTP 传输（以 2025-11-25 会话制为主，2026-07-28 无状态只识别）+ OAuth 2.1 服务级授权（预注册 / 动态注册 + PKCE + `resource` + `iss` 校验）+ 静态 Bearer 槽位；出口由运维写死的 URL 决定、连接期 SSRF 校验与钉死 IP、不跟随重定向；远程工具仍持 `MCP_INVOKE`，`NETWORK_FETCH` 不放宽；出站清单 7 → 8（2026-10-06 从 S5 拆出，2026-10-12 写执行 Prompt） | `docs/execution-prompts/P7-S8-streamable-http.md` | delivered |
| S9 | Step F | Skill 库：两级来源（仓库内置 + 运维只读目录）的 `SKILL.md` 操作规程，`load_skill` 工具（目录在工具描述里、intent 暴露）按需加载，正文以 `SYSTEM_POLICY` 分片进动态段 `skill_context`；装配期安检；设置页只读 Skill 库（管理员可启停）；`skill_p0` 评测（2026-10-09 新增） | `docs/execution-prompts/P7-S9-skill-library.md` | delivered |
| S10 | Step G | OpenAI 兼容端点可配置与关键词检索模式（交付见 §3.11）：对话与 embedding 端点 / 模型 / Key 由运维或管理员配置（缺省仍是 DeepSeek + 百炼，已有部署零变化）；非缺省对话端点必须有通过的兼容性报告才能装配（硬门槛，缺省免检）；`WATER_AGENT_RETRIEVAL_MODE=auto` 下没配 embedding 或与索引不匹配时走关键词检索（BM25）并在运行状态里如实标出；索引旁写 `index_meta.json` 绑定 embedding 模型与维度；`build_index.py --text-only`；先建口语化检索评测集（2026-10-12 新增，归入 P7 以复用范围守卫与快照） | `docs/execution-prompts/P7-S10-openai-compatible-endpoints.md` | delivered |
| S15 | Step H | 模型目录与按会话 / 按消息选择模型（交付见 §3.11 的 S15 增补；规格 `18-p7-multi-model.md`）：管理员目录白名单（同端点、同 Key）+ 逐对兼容性验证 + `thinking: off` + 迁移 0008 + 前端选择器与目录管理区 + `model_choice_p0`；模型从不被自动更换；`thinking: roundtrip` 只能保存、不能选（S16） | `docs/execution-prompts/P7-S15-model-catalog-and-selection.md` | delivered |
| S11 | Step I | 外部内容底座（规格 `17-p7-external-capabilities.md`，交付见 §7 的 S11 小节）：`SourceTag.WEB_CONTENT`、信封与核心边界归一化、受治理的 GET 内核 `external/http_get.py`、回合污点（缓存拒绝 / 披露行 / 终答网址追溯 / 记忆写入 fail closed）、`open_exchange` 调用方登记与静态出站扫描扩展、答案渲染不加载外部图片、`external_content_p0`；**不新增出站站点**（仍是 8，8 → 9 属 S12） | `docs/execution-prompts/P7-S11-external-content-foundation.md` | delivered |

S3–S7 的已知约束（开工写执行 Prompt 时必须吸收）：

- **S3**：每层的 `allow` 整体替换、不合并；覆盖层不得写 `confirmation_handler_ready`（S1 的限制不能从
  覆盖层绕开）；F15 修在进程边界（子进程工作目录），不放宽也不收紧护栏。修订与理由见 S3 执行 Prompt §0。
- **推迟项：entry point 发现**（原计划 S3，2026-10-05 评审决定推迟）。没有任何外部插件包，做了只能对着
  假包测；进程内插件不受护栏约束，要等出现第一个真实插件包再做。届时的约束不变：只收白名单、外部插件
  只能贡献经 MCP 桥接的工具（本地工具会被 `capability_for_tool` 拒绝）。
- **S4**：本地工具 schema 只剩 3 字符余量（F4），`< 4500` 断言不得上调；先有工具选择准确率的
  评测证据，再改暴露策略；`PROMPT_TEMPLATE_VERSION` 递进，快照**有意**重写。2026-10-06 写执行 Prompt
  时定下（理由见 Prompt §0）：每轮在 `agent_loop` 入口用确定性规则决定一次；`calc_velocity` 与检索、
  引用同属 core、每轮必开（铁律①）；未开放的工具被调用时照常经 `ToolRegistry.invoke` 执行（暴露不是
  授权边界）；MCP 每台 server 一组、预算不够时最先让位，本步不加 per-server 关键词配置；全量本地工具
  的 `< 4500` 断言原样保留，F4 的修复面是"每轮实际发出的 schema 受策略约束"。
- **S5**：长驻 stdio 进程被所有 Web 用户共享——跨租户状态、并发访问（客户端 `max_workers=4`）、
  重载语义都要先定。2026-10-06 写执行 Prompt 时定下（理由见 Prompt §0）：共享须运维按 server 显式声明
  `lifecycle: shared`，缺省 `per_run`；同一客户端上的请求串行（读码发现 `_request` 并发时会把别人的响应
  当通知丢掉，今天同一 run 内并行调用同一台 server 就可能触发）；按单台 server 指纹（配置 + 子进程 env
  摘要）重载，旧进程在最后一个租约释放后关闭；只有 Web 用连接池，CLI 与 runner 不变。
- **S8**（从 S5 拆出，2026-10-06）：Streamable HTTP 需要 `NETWORK_FETCH` 级别的能力决策（它在危险集里，
  `Principal` 构造期即拒）与出站清单登记（7 → 8），单独评审。不叫"S5b"：范围守卫按 `YEK/p7-s<n>-`
  映射执行 Prompt。2026-10-12 写执行 Prompt 时定下（用户决定，理由见 Prompt §0）：**出口由运维决定、不碰危险集**——
  远程工具仍持 `MCP_INVOKE`，URL 与 `command` 同属装配期配置、模型改不了目标；连接期对每次建连做 SSRF 校验
  （拒绝回环 / 私有 / 链路本地 / 元数据 / CGNAT 等段，`allow_private_network` 只放行回环与私有段）、连接钉死到
  校验过的 IP、不跟随重定向；`mcp/http_transport.py` 是唯一出站模块，登记进 `PROVIDER_CALL_SITES` 并经
  `ProviderGateway`（工具调用零重试）。**OAuth 2.1 一并做，但只做服务级授权**（管理员授权一次、所有用户的 run
  共用该身份；预注册与动态注册，不做 Client ID Metadata Documents 与按用户委托）；令牌存凭据卷、只写不读、只发给
  签发它的那台 server。**协议以 2025-11-25 为主**（initialize + `MCP-Session-Id` + `MCP-Protocol-Version`，JSON
  与 SSE 两种响应），2026-07-28 无状态协议只识别为 `MCP_PROTOCOL_UNSUPPORTED`，不回落到已废弃的 HTTP+SSE。
  URL / oauth / transport 只能写在运维两层，Web 管理层可写键不变。
- **S10**（2026-10-12 新增）：用户要"对话模型换成任意 OpenAI 兼容端点、只填一个 LLM API 也能用 RAG"。写执行 Prompt 时定下（用户决定，理由见 Prompt §0）：端点解析单一来源 `generation/endpoints.py`，CLI / Web / runner / docgen / judge / 探针全部经它；端点改了就不回落旧 Key（密钥只发给所属服务商）；非缺省对话端点必须有精确匹配端点 + 模型、全部通过的兼容性报告（复用 P5 Step 46 探针的检查集，硬门槛，缺省 DeepSeek 免检）；**不新增 `ProviderKind`、不改名**（`DEEPSEEK_CHAT` 语义扩为"OpenAI 兼容对话端点"），出站清单数量不变；缓存键加 `model_endpoint_digest`（空串不进键）；embedding 端点对称可配，索引旁 `index_meta.json` 记录 embedding 模型与维度，不匹配拒绝向量检索；检索模式 `auto|hybrid|text`，`auto` 下缺 embedding 走关键词检索且**必须可见**（不得宣称语义检索）；rerank 与视觉审阅仍是百炼、可选。实测：纯关键词在 rag_p1 上 Recall@5 98.28%（问法与条文同构、偏乐观），口语化问法有漏召回——质量证据以新建的 `retrieval_colloquial_p0` 为准。
- **S6**：导入 `mcpServers` 会带来 `command` / `args`，面板不能写它们，只能生成"已禁用的待审提案"，
  由运维离线应用。2026-10-07 写执行 Prompt 时定下（理由见 Prompt §0）：面板的改动写进第三层配置
  **Web 管理层**（`WATER_AGENT_MCP_WEB_LAYER` 指向的文件，合并在部署覆盖层之后，只能写 `server_id` /
  `disabled` / `allow`，不能新增 server；孤立条目忽略并列出；损坏即 `MCP_CONFIG_INVALID`，面板可整层恢复）；
  "凭据引用"是运维在覆盖层声明的**凭据槽位** `credential_env`（环境变量名由运维定、拒绝加载器 / 解释器变量），
  面板只给已声明的槽位写值（只写不读，存凭据卷），缺值 `MCP_CREDENTIAL_MISSING`；面板可以放宽 `allow`、
  可以启用运维停用的 server（放行不是授权，执行期检查不变）；导入是无状态预览（`env` 值丢弃、`disabled: true`、
  不写 `allow`）；探测只对启用的 server 经连接池借一次租约；全部接口 `require_admin`，写接口加 CSRF，变更进
  结构化审计；不建数据库表。
- **S9**（2026-10-09 新增）：用户要求 skill 正文能当指令用、来源分仓库内置与运维目录两级。写执行 Prompt 时定下（理由见
  Prompt §0）：skill 不是新的 `SourceTag`——正文是 `SYSTEM_POLICY` 的按需加载分片，`INSTRUCTION_BEARING_SOURCES` 不改，
  扩大的是"谁能写系统策略"（运维只读目录 `WATER_AGENT_SKILLS_DIR`），在威胁模型登记为 T12；只有这两级来源，不扫描
  项目 / 用户目录、上传文件或 MCP 资源，面板不能新增 / 编辑 skill；装配期安检（注入检测、铁律否定式改写、`requires_tools`
  核对），单个 skill 出错只跳过它；目录放在 `load_skill` 工具描述里（≤ 900 字符）、按 intent 暴露，不进静态提示段，
  `< 4500` 与 `< 1950` 不上调；正文进动态段 `skill_context`，checkpoint 只存名字与摘要；`LOAD_SKILL` 进默认能力集
  与 Web 成员能力集（2026-10-09 修订：原写法"由插件授予"与 `test_every_inventoried_tool_capability_is_granted_locally`
  冲突），`GRANTABLE_CAPABILITIES` / P7-I06 不改；特性开关 `WATER_AGENT_SKILLS`（代码缺省 off，Web compose 设 on）——
  现有快照场景只有主体能力集多一项；缓存键加 `skill_library_digest`；skill 正文不得给出数值限值或规范号（铁律①②），`skill_p0` 锁住加载规程后铁律仍成立。
- **S7**：内层 react agent 是 `checkpointer=False`（`graphs/agent_rag.py`），工具执行中的 interrupt
  无法恢复，确认门必须放在**外层图**（先例：`multi_agent/orchestrator.py` 的审批 interrupt）。2026-10-06
  写执行 Prompt 时定下（理由见 Prompt §0）：需确认的工具在 ToolNode 里换成同名同 schema 的
  `return_direct` 延迟工具，内层循环在它之后结束；外层 `confirmation_gate` 挂起，批准后签发绑定工具名 +
  参数摘要 + 主体的一次性 `ConfirmationGrant`，经 `ToolRegistry.invoke_confirmed` 执行一次并同 id 替换
  待确认消息；S1 的无凭证拒绝原样保留；`confirmation_handler_ready` 仍在加载期拒绝，确认门是否可用由
  **入口**决定（只有 Web）；新增 run 状态 `waiting_confirmation`（无迁移），批准者是 run 所有者；
  CLI / runner 不接。

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
| P7-S0-A13 | F5：工具指引已截断；MCP 工具不计入现有预算（S0 时的现状；S4 修复后由 P7-S4-A11 取代，见未覆盖项） | 无 | untested |
| P7-S0-A14 | F6：manifest 不进缓存服务（S0 时的现状；S3 修复后由 P7-S3-A07 取代，见未覆盖项） | 无 | untested |
| P7-S0-A15 | F7：确认门执行期不检查（S0 时的现状；S1 修复后由 P7-S1-A03 取代，见未覆盖项） | 无 | untested |
| P7-S0-A16 | F13：runner 的 MCP trace 事件从未落盘（S0 时的现状；S2 删除后由 P7-S2-A12 取代，见未覆盖项） | 无 | untested |
| P7-S0-A17 | F2：runner 路径与会话路径的可观察差异（S0 时的现状；S2 统一后由 P7-S2-A07 取代，见未覆盖项） | 无 | untested |
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
| P7-S2-A01 | `clauses` 插件耦合 retrieve 与 cite，并透传 `context_source` | `tests/test_tool_plugins.py::test_clauses_plugin_couples_retrieve_and_cite`<br>`tests/test_tool_plugins.py::test_clauses_plugin_forwards_context_source` | delivered |
| P7-S2-A02 | `drawing` 插件原样透传 `drawing_root`（`""` 与 `None` 不互换） | `tests/test_tool_plugins.py::test_drawing_plugin_passes_root_verbatim` | delivered |
| P7-S2-A03 | MCP 插件 `owning` 打开后立即登记关闭、只能用一次；`borrowing` 从不打开或关闭 | `tests/test_tool_plugins.py::test_mcp_owning_closes_on_stack_exit`<br>`tests/test_tool_plugins.py::test_mcp_owning_is_single_use`<br>`tests/test_tool_plugins.py::test_mcp_borrowing_never_closes` | delivered |
| P7-S2-A04 | 授权只在贡献了工具时发生、保持 kind / auth_domain、白名单外拒绝 | `tests/test_tool_plugins.py::test_grant_only_with_contributed_tools`<br>`tests/test_tool_plugins.py::test_grant_preserves_kind_and_auth_domain`<br>`tests/test_tool_plugins.py::test_grants_outside_whitelist_rejected` | delivered |
| P7-S2-A05 | 插件异常原样上抛；插件 id 重复拒绝；发给模型的工具按字母序且绑定同一注册表 | `tests/test_tool_plugins.py::test_plugin_exception_propagates`<br>`tests/test_tool_plugins.py::test_duplicate_plugin_ids_rejected`<br>`tests/test_tool_plugins.py::test_tools_sorted_and_bound_to_registry` | delivered |
| P7-S2-A06 | 会话路径零行为变化：会话侧 profile 与全部 surface 与基线一致 | `tests/test_plugin_invariants.py::test_session_assembly_matches_baseline` | delivered |
| P7-S2-A07 | runner 统一成会话语义（§3.5 第 1–7 条） | `tests/test_tool_plugins.py::test_runner_mcp_opened_and_closed_per_case`<br>`tests/test_tool_plugins.py::test_runner_mcp_register_fault_fails_loudly`<br>`tests/test_tool_plugins.py::test_runner_chat_model_sees_granted_principal`<br>`tests/test_tool_plugins.py::test_runner_cite_uses_thread_context`<br>`tests/test_p7_known_figures.py::test_runner_path_matches_session_semantics` | delivered |
| P7-S2-A08 | 不变量 P7-I01…I07 | `tests/test_plugin_invariants.py::test_invariants_hold` | delivered |
| P7-S2-A09 | F11：装配位置断言改为 AST 判定 | `tests/test_read_drawing_tool.py::TestWiring::test_cli_registers_tool_only_when_root_available` | delivered |
| P7-S2-A10 | 机制层门禁不回退（agentic_p0 21/21） | `gate:agentic_p0` | delivered |
| P7-S2-A12 | F13：装配期不再有尝试写入却从未落盘的 trace 调用 | `tests/test_p7_known_figures.py::test_no_assembly_trace_calls_that_never_land` | delivered |
| P7-S2-A11 | 真实质量不回退：有 Key 时重跑 `rag_p1 --variant real --judge`，与 55/58、49/58 对比 | 无 | untested |

### S3：配置分层、按实例启停与工作目录隔离（Step B）

| ID | 验收 | 验证 | 状态 |
|---|---|---|---|
| P7-S3-A01 | `MCPServerConfig` 新增 `disabled`、`cwd`；`cwd` 必须是绝对路径；`command` 只能是绝对路径或裸命令名 | `tests/test_mcp_layered_config.py::test_server_config_validates_cwd_and_command`<br>`tests/test_mcp_layered_config.py::test_model_level_validation_does_not_touch_the_filesystem` | delivered |
| P7-S3-A02 | F9：`env_file` 相对路径按定义它的那一层配置文件所在目录解析；`env_file` / `cwd` 不存在在加载期即 `MCP_CONFIG_INVALID` | `tests/test_mcp_layered_config.py::test_env_file_and_cwd_resolved_and_checked_at_load`<br>`tests/test_mcp_layered_config.py::test_overlay_env_file_resolves_against_the_overlay_directory` | delivered |
| P7-S3-A03 | 覆盖层合并：新增 server、逐字段覆盖、`allow` / `args` / `env` 整体替换；只有覆盖层时默认层按空骨架处理 | `tests/test_mcp_layered_config.py::test_overlay_merges_by_server_id`<br>`tests/test_mcp_layered_config.py::test_load_mcp_gateway_uses_the_layered_loader` | delivered |
| P7-S3-A04 | 覆盖层不得写 `confirmation_handler_ready` 或未知键；覆盖层路径给了但不存在 → `MCP_CONFIG_INVALID` | `tests/test_mcp_layered_config.py::test_overlay_cannot_widen_the_gate` | delivered |
| P7-S3-A05 | `disabled` 的 server 不 spawn、不暴露、记 `MCP_SERVER_DISABLED`、不进 manifest（全部停用时为 `""`） | `tests/test_mcp_layered_config.py::test_disabled_server_is_skipped_and_excluded_from_manifest` | delivered |
| P7-S3-A06 | F15 / F16：未配 `cwd` 时子进程工作目录是新建的空临时目录（相对路径读不到 agent 的文件，连接关闭后目录被删）；配了就用它 | `tests/test_mcp_layered_config.py::test_child_runs_in_isolated_or_configured_cwd`<br>`tests/test_mcp_layered_config.py::test_temporary_cwd_is_removed_when_the_server_fails_to_start` | delivered |
| P7-S3-A07 | F6：会话装配暴露了 MCP 工具时，缓存服务的 `mcp_server_manifest` 非空 | `tests/test_p7_known_figures.py::test_mcp_manifest_reaches_the_cache_service` | delivered |
| P7-S3-A08 | F13 后续：会话与 runner 的 trace 里确有 `mcp_assembly` 事件，内容不含 detail 文本；没有 MCP 配置时不写 | `tests/test_mcp_layered_config.py::test_assembly_summary_lands_in_trace`<br>`tests/test_mcp_layered_config.py::test_runner_trace_records_assembly_summary` | delivered |
| P7-S3-A09 | `McpContribution` 可逐台读出每台 server 的结果（暴露 / 跳过 / 停用） | `tests/test_tool_plugins.py::test_mcp_contribution_reports_each_server` | delivered |
| P7-S3-A10 | F10：入仓默认层是空骨架；示例文件通过模型层校验且被 `.dockerignore` 排除 | `tests/test_mcp_wiring.py::TestConfigWiring::test_default_config_is_empty_skeleton`<br>`tests/test_mcp_wiring.py::TestConfigWiring::test_example_config_validates` | delivered |
| P7-S3-A11 | 装配期的每一次 trace 调用都已真正落盘（F13 的最终形态，与 P7-S2-A12 同一测试） | `tests/test_p7_known_figures.py::test_no_assembly_trace_calls_that_never_land` | delivered |
| P7-S3-A12 | 模型可见面不变：全部 profile 的 surface 与基线一致（范围守卫 `frozen_json` 在 PR 上核对） | `tests/test_tool_assembly_snapshot.py::test_snapshot_matches_baseline` | delivered |
| P7-S3-A13 | 手动验收：本机覆盖层接上真实 autocad-mcp 后能看到 `mcp_autocad_mcp_read_dwg`（只证明能连上，不是 CAD 验收） | 无 | untested |

### S4：按意图最小暴露与工具分组（Step C）

| ID | 验收 | 验证 | 状态 |
|---|---|---|---|
| P7-S4-A01 | 评测集先行：`tool_exposure_p0` 经生产会话 + 记录型脚本模型运行，在未改动的暴露代码上跑出"改之前"的报告（F4/F5 现状可复现）；数据集此后冻结 | `tests/test_tool_exposure_eval.py::test_harness_records_what_the_model_saw`<br>`tests/test_tool_exposure_eval.py::test_script_must_be_consumed_exactly`<br>`tests/test_tool_exposure_eval.py::test_full_strategy_reproduces_f4_f5`<br>`tests/test_tool_exposure_eval.py::test_missing_must_expose_is_a_load_problem`<br>`tests/test_tool_exposure_eval.py::test_unmeasured_rates_are_not_a_pass`<br>`tests/test_tool_exposure_eval.py::test_dataset_covers_every_category` | delivered |
| P7-S4-A02 | 暴露契约：组来自插件贡献（core / intent / ambient），MCP 每台 server 一组；决定是确定性纯函数，不 import 模型 / HTTP / random / time | `tests/test_tool_exposure.py::test_groups_come_from_contributions`<br>`tests/test_tool_exposure.py::test_decision_is_deterministic_and_pure`<br>`tests/test_tool_exposure.py::test_intent_groups_open_only_on_matching_questions`<br>`tests/test_tool_exposure.py::test_rules_are_validated_at_construction` | delivered |
| P7-S4-A03 | 优先级与两项预算：schema ≤ 4500、按意图指引 ≤ 800 且不截断；按优先级逐组入选、放不下的整组跳过（偏离见决策记录 §0.2）、core 永不丢弃、core 自身超预算装配期抛错 | `tests/test_tool_exposure.py::test_budget_drops_whole_groups_in_reverse_priority`<br>`tests/test_tool_exposure.py::test_core_over_budget_fails_at_assembly` | delivered |
| P7-S4-A04 | sticky：上一轮实际调用过工具的组本轮保留，只回看一轮 | `tests/test_tool_exposure.py::test_previous_turn_tools_stay_exposed_for_one_turn`<br>`tests/test_tool_exposure.py::test_previous_turn_tools_reads_only_the_last_turn` | delivered |
| P7-S4-A05 | 接线：模型每次调用只收到本轮开放的工具（流式与非流式）、ToolNode 仍持全部工具、指引按本轮决定渲染、回流复用同一决定、每轮一条 `tool_exposure` trace 事件且不含 query 原文 | `tests/test_tool_exposure.py::test_model_receives_only_exposed_tools`<br>`tests/test_tool_exposure.py::test_reflow_reuses_the_turn_decision` | delivered |
| P7-S4-A06 | 未开放的工具被调用时照常经 `ToolRegistry.invoke` 执行，trace 标记 `unexposed_call` | `tests/test_tool_exposure.py::test_unexposed_call_executes_and_is_marked` | delivered |
| P7-S4-A07 | 回滚开关 `WATER_AGENT_TOOL_EXPOSURE`：`full` 与 S3 行为逐字一致，非法值装配期抛错 | `tests/test_tool_exposure.py::test_full_strategy_matches_s3_behavior`<br>`tests/test_tool_exposure.py::test_invalid_strategy_fails_loudly` | delivered |
| P7-S4-A08 | 两处生产 `build_agentic_app` 调用都传 `exposure=`（AST 判定；未登记为 §4 不变量，原因见 `p7-step-4-decision.md` §0.2） | `tests/test_plugin_invariants.py::test_production_app_receives_exposure_policy` | delivered |
| P7-S4-A09 | `tool_exposure_p0` 硬指标在 intent 策略下全部达标，并入 `ci_gate.sh` 与入仓基线对比 | `gate:tool_exposure_p0`<br>`tests/test_tool_exposure_eval.py::test_baseline_is_a_passing_intent_report`<br>`tests/test_tool_exposure_eval.py::test_compare_accepts_identical_reports`<br>`tests/test_tool_exposure_eval.py::test_compare_rejects_tampered_and_regressed_reports` | delivered |
| P7-S4-A10 | 快照有意重写：两个版本号递进，只有声明允许的片段变化（范围块 `frozen_json` 在 PR 上核对） | `tests/test_tool_assembly_snapshot.py::test_snapshot_matches_baseline` | delivered |
| P7-S4-A11 | F5：每条快照探针的开放工具都出现在指引里、指引未截断、schema ≤ 4500；回退格式（`full`）仍截断 | `tests/test_p7_known_figures.py::test_exposed_tools_always_fit_the_tool_guide` | delivered |
| P7-S4-A12 | 机制层门禁不回退（agentic_p0 21/21） | `gate:agentic_p0` | delivered |
| P7-S4-A13 | 真实模型的工具选择准确率：有 Key 时用真实模型重跑 `tool_exposure_p0` 的问题并人工核对所选工具 | 无 | untested |

### S5：MCP 长驻连接（stdio）

| ID | 验收 | 验证 | 状态 |
|---|---|---|---|
| P7-S5-A01 | 客户端并发安全：同一客户端上并发调用各拿到自己的响应（修复前其中一个超时）；等锁计入 deadline；`alive` 属性 | `tests/test_mcp_pool.py::test_concurrent_calls_on_one_client_get_their_own_responses`<br>`tests/test_mcp_pool.py::test_waiting_for_a_busy_server_counts_against_the_deadline` | delivered |
| P7-S5-A02 | `lifecycle: per_run / shared`（缺省 `per_run`），覆盖层可写；`MCP_CONFIG_VERSION` 递进 | `tests/test_mcp_pool.py::test_lifecycle_field_defaults_to_per_run` | delivered |
| P7-S5-A03 | 复用：shared 的 server 连续两次租约同一进程；per_run 的每次新进程；共享状态如实可见（计数连续） | `tests/test_mcp_pool.py::test_shared_server_is_reused_and_per_run_is_not`<br>`tests/test_mcp_pool.py::test_shared_server_state_is_visible_across_leases` | delivered |
| P7-S5-A04 | 重载：单台 server 指纹（配置 + 子进程 env 摘要）变化 → 下一次租约新进程；旧进程在最后一个租约释放后关闭；删除 / 停用同样退役 | `tests/test_mcp_pool.py::test_fingerprint_change_retires_the_old_process` | delivered |
| P7-S5-A05 | 健康：死掉的共享进程在下一次租约时重开；调用中途崩溃返回结构化错误、不抛出 run | `tests/test_mcp_pool.py::test_dead_shared_process_is_reopened` | delivered |
| P7-S5-A06 | 生命周期：`release` 幂等；池关闭后不留活进程，未释放租约数量被报告；并发首次租约只 spawn 一个进程 | `tests/test_mcp_pool.py::test_pool_close_leaves_no_processes`<br>`tests/test_mcp_pool.py::test_concurrent_first_lease_spawns_once` | delivered |
| P7-S5-A07 | 接线：Web 会话工厂用池租约，连续两个 run 复用同一进程；应用关闭先排空 run 再关池；会话构造抛错时租约被释放；CLI / runner 不变 | `tests/test_web_mcp_pool.py::test_web_runs_reuse_shared_server`<br>`tests/test_web_mcp_pool.py::test_lease_is_released_when_session_construction_fails`<br>`tests/test_web_mcp_pool.py::test_bad_config_fails_before_any_lease`<br>`tests/test_web_mcp_pool.py::test_run_manager_close_drains_runs_then_closes_the_pool` | delivered |
| P7-S5-A08 | trace 摘要每台 server 带 `connection: per_run / shared_new / shared_reused`，不含指纹或 env | `tests/test_mcp_pool.py::test_trace_summary_reports_connection_kind` | delivered |
| P7-S5-A09 | F8：shared 的 server 跨 run 不再重开；快照只有 `/versions/MCP_CONFIG_VERSION` 变化 | `tests/test_p7_known_figures.py::test_shared_mcp_servers_survive_across_runs` | delivered |
| P7-S5-A10 | 手动验收：本机 autocad-mcp 设为 shared，两次 Web run 的 trace 依次为 `shared_new`、`shared_reused`（只证明连接复用，不是 CAD 验收） | 无 | untested |

### S7：危险操作人工确认门（Step E2）

| ID | 验收 | 验证 | 状态 |
|---|---|---|---|
| P7-S7-A01 | 需确认的工具在 ToolNode 里是 `return_direct` 延迟工具：执行不调用注册表、不碰 MCP，内层循环随即结束 | `tests/test_confirmation_gate.py::test_deferred_tool_never_executes_and_ends_the_inner_loop` | delivered |
| P7-S7-A02 | 执行凭证：无凭证仍 `tool.confirmation_required`；工具名 / 参数摘要 / 主体不一致 `tool.confirmation_mismatch`；重放 `tool.confirmation_replayed`；正确凭证执行一次 | `tests/test_confirmation_grant.py::test_grant_must_match_tool_args_and_principal`<br>`tests/test_confirmation_grant.py::test_grant_is_single_use`<br>`tests/test_confirmation_grant.py::test_args_hash_is_canonical` | delivered |
| P7-S7-A03 | 外层确认门：挂起时副作用未发生；批准恰好执行一次、模型看到真实结果；拒绝零次、模型看到拒绝 observation；非法 resume 不毒化会话；同一消息多条逐条确认；每轮上限 3 次；恢复后本轮召回结果交回或如实记 warning | `tests/test_confirmation_gate.py::test_approve_executes_exactly_once`<br>`tests/test_confirmation_gate.py::test_reject_executes_nothing`<br>`tests/test_confirmation_gate.py::test_invalid_resume_does_not_poison_the_session`<br>`tests/test_confirmation_gate.py::test_fourth_confirmation_in_a_turn_fails`<br>`tests/test_confirmation_gate.py::test_resume_restores_turn_contexts_or_warns` | delivered |
| P7-S7-A04 | 审计：`confirmation_log` 随 checkpoint 持久化、跨轮累积；三类 trace 事件；参数只记摘要、主体只记 hash、原因脱敏截断 | `tests/test_confirmation_gate.py::test_confirmation_audit_has_no_raw_arguments` | delivered |
| P7-S7-A05 | 入口决定可用性：Web 借连接时 `human_confirmation` 的 server 暴露；CLI / runner 仍 `MCP_CONFIRMATION_UNAVAILABLE`；`confirmation_handler_ready=true` 仍加载期拒绝 | `tests/test_confirmation_gate.py::test_only_the_web_entry_point_enables_confirmation` | delivered |
| P7-S7-A06 | Web：`waiting_confirmation` 态与 `confirmation_required` 事件；挂起期间发消息 409；决定接口的归属 404 / 写权限 403 / 过期 409 / 正确 202；同一 run 上恢复；重启后仍可决定 | `tests/test_web_confirmation.py::test_confirmation_round_trip_on_the_same_run`<br>`tests/test_web_confirmation.py::test_confirmation_endpoint_authorization`<br>`tests/test_web_confirmation.py::test_pending_confirmation_survives_restart` | delivered |
| P7-S7-A07 | 前端确认卡片：显示请求、批准 / 拒绝、刷新后从历史恢复（由 `web/app/tests/` 的 node 测试覆盖，见未覆盖项） | 无 | untested |
| P7-S7-A08 | 快照只有 `/versions/TOOL_POLICY_VERSION` 变化 | `tests/test_tool_assembly_snapshot.py::test_snapshot_matches_baseline` | delivered |
| P7-S7-A09 | F7 最终形态：Web 入口下需确认的工具暴露为延迟工具、无凭证执行仍被拒 | `tests/test_p7_known_figures.py::test_confirmation_gate_is_enforced` | delivered |
| P7-S7-A10 | 手动验收：浏览器里看到确认卡片，批准后回答带计数结果、拒绝后计数不变 | 无 | untested |

### S6：Web 扩展面板（Step E1）

| ID | 验收 | 验证 | 状态 |
|---|---|---|---|
| P7-S6-A01 | Web 管理层：只能写 `server_id` / `disabled` / `allow`、不能新增 server；合并在部署覆盖层之后、`allow` 整体替换；`confirmation_handler_ready` / 未知键 / 坏 JSON → `MCP_CONFIG_INVALID`；孤立条目忽略且可列出；写入原子、revision 冲突拒绝 | `tests/test_mcp_web_layer.py::test_web_layer_can_only_toggle_and_allow`<br>`tests/test_mcp_web_layer.py::test_web_layer_merges_last_and_fails_loudly`<br>`tests/test_mcp_web_layer.py::test_orphan_entries_are_ignored_and_listed`<br>`tests/test_mcp_web_layer.py::test_writes_are_atomic_and_revisioned`<br>`tests/test_mcp_web_layer.py::test_store_reports_an_unwritable_directory` | delivered |
| P7-S6-A02 | 凭据槽位：名字只由运维声明（拒绝加载器 / 解释器变量与内联 `env` 重名，Web 管理层写不了）；值只写不读、文件 0600、注入子进程；缺值 `MCP_CREDENTIAL_MISSING` 不 spawn；改值后 shared server 换新进程 | `tests/test_mcp_credentials.py::test_credential_slots_are_declared_by_the_operator`<br>`tests/test_mcp_credentials.py::test_credential_values_are_write_only_and_reach_the_child`<br>`tests/test_mcp_credentials.py::test_missing_credential_skips_the_server` | delivered |
| P7-S6-A03 | 导入预览：Claude Code / Cursor JSON 与 Codex TOML → 覆盖层格式提案，每台 `disabled: true`、无 `allow`、`env` 值全部丢弃（键变成槽位）；不支持的条目逐条说明；不落盘、不 spawn | `tests/test_mcp_import.py::test_import_produces_disabled_proposals_only`<br>`tests/test_mcp_import.py::test_import_never_echoes_env_values`<br>`tests/test_web_mcp_admin.py::test_import_preview_endpoint_is_stateless` | delivered |
| P7-S6-A04 | 管理 API：admin + CSRF；读接口不含 `args` / `env` 值 / 凭据值 / 完整命令路径；PATCH 乐观并发与整份校验（不合法 422 且不落盘）；单台与整层恢复部署配置（配置损坏时也可用） | `tests/test_web_mcp_admin.py::test_admin_api_authorization`<br>`tests/test_web_mcp_admin.py::test_server_update_is_validated_and_versioned`<br>`tests/test_web_mcp_admin.py::test_reset_restores_deployment_config`<br>`tests/test_web_mcp_admin.py::test_capabilities_report_counts_only` | delivered |
| P7-S6-A05 | 下一个 run 生效：停用 / 改 `allow` / 改凭据后，下一次会话工厂装配随之变化，shared server 换新进程 | `tests/test_web_mcp_admin.py::test_changes_take_effect_on_the_next_run` | delivered |
| P7-S6-A06 | 探测：只对启用的 server、经连接池租约（`confirmation_ready=True`），返回工具名与跳过码、不含远端描述原文；停用 409、并发 409 | `tests/test_web_mcp_admin.py::test_probe_reports_tools_without_remote_text` | delivered |
| P7-S6-A07 | 审计：每次变更一条结构化事件（user_id、server_id、字段名、`allow` 增减），不含凭据值；层文件条目带 `updated_at` / `updated_by` | `tests/test_web_mcp_admin.py::test_admin_changes_are_audited_without_secrets` | delivered |
| P7-S6-A08 | 部署：新卷挂在 `/var/lib/water-agent/mcp-admin`（在备份范围内）、环境变量指向卷内文件、镜像预建目录且属主 `wateragent`；凭据值仍只在 `app_credentials` 卷 | `tests/test_web_deployment.py::test_mcp_admin_layer_volume_is_backed_up` | delivered |
| P7-S6-A09 | 前端 MCP 扩展区：启停、`allow` 编辑、凭据只写、探测、恢复、导入预览（由 `web/app/tests/` 的 node 测试覆盖，见未覆盖项） | 无 | untested |
| P7-S6-A10 | 快照只有 `/versions/MCP_CONFIG_VERSION` 变化；`WATER_AGENT_MCP_WEB_LAYER` 被快照钉住 | `tests/test_tool_assembly_snapshot.py::test_snapshot_matches_baseline`<br>`tests/test_tool_assembly_snapshot.py::test_snapshot_is_hermetic_and_deterministic` | delivered |
| P7-S6-A11 | 手动验收：浏览器里探测 → 放行 → 填凭据 → 工具出现；停用 → 工具消失；导入含密钥的配置，提案里没有密钥值 | 无 | untested |

### S9：Skill 库（Step F）

| ID | 验收 | 验证 | 状态 |
|---|---|---|---|
| P7-S9-A01 | 格式与加载：`<root>/<name>/SKILL.md` 单层、frontmatter 白名单、名字与目录一致、长度上限、符号链接不逃逸；两级来源合并、同名内置优先（`SKILL_SHADOWED`）；单个坏 skill 只记错误码；`WATER_AGENT_SKILLS_DIR` 设了却不可读响亮失败 | `tests/test_skill_loader.py::test_skill_manifest_is_strict`<br>`tests/test_skill_loader.py::test_two_sources_builtin_wins`<br>`tests/test_skill_loader.py::test_bad_skill_is_skipped_not_fatal` | delivered |
| P7-S9-A02 | 安检：注入命中 `SKILL_UNSAFE`（不脱敏放行）、铁律否定式改写 `SKILL_CONFLICTS_IRON_RULE`、`requires_tools` 缺失 `SKILL_TOOL_UNAVAILABLE` | `tests/test_skill_safety.py::test_unsafe_skills_are_not_loaded`<br>`tests/test_skill_safety.py::test_iron_rule_conflicts_are_rejected` | delivered |
| P7-S9-A03 | `load_skill` 工具与能力：`ToolSpec` 在 `tools/`、`LOAD_SKILL` 进 `TOOL_CAPABILITIES`、默认能力集与 Web 成员能力集；开关 `WATER_AGENT_SKILLS` 缺省 off，off 或没有已启用 skill 时插件 skipped、不注册工具；开关取值非法装配期失败 | `tests/test_load_skill_tool.py::test_load_skill_is_governed_and_locally_granted`<br>`tests/test_load_skill_tool.py::test_switch_off_or_no_skills_means_no_tool` | delivered |
| P7-S9-A04 | 暴露：skills 组 intent（triggers ∪ 固定词表）、sticky；目录 schema ≤ 900（超出按字母序截断并记 `SKILL_CATALOG_FULL`）；最紧组合下整组让位并记 dropped | `tests/test_load_skill_tool.py::test_catalog_fits_and_skills_group_yields_under_budget` | delivered |
| P7-S9-A05 | 正文注入：`skill_context` 段只来自 `SkillLibrary`、以 `SYSTEM_POLICY` 信封包装；checkpoint 只存名字与摘要；skill 被删 / 停用 / 摘要变化后不再注入并记 `skill_unavailable`；段为空不渲染 | `tests/test_skill_prompting.py::test_loaded_skill_becomes_system_policy_segment`<br>`tests/test_skill_prompting.py::test_removed_or_changed_skill_is_not_injected` | delivered |
| P7-S9-A06 | 缓存键：`skill_library_digest` 变化 → miss；没有 skill 时与历史键同键 | `tests/test_answer_cache.py::TestInvalidationFactors::test_each_factor_change_causes_miss` | delivered |
| P7-S9-A07 | 首批内置 skill：4 份全部过安检、`requires_tools` 都在默认装配里、正文不含规范号与带单位数值 | `tests/test_builtin_skills.py::test_builtin_skills_pass_safety_and_carry_no_numbers` | delivered |
| P7-S9-A08 | 面板与 API：成员可读列表（不含正文与运维路径）、正文与启停只给管理员（启停写进 Web 管理层 `skills` 键，revision / 锁 / 整份校验同 S6）、写接口 CSRF、审计；停用后下一个 run 不在目录里；S6 的 `servers` 改动不受影响 | `tests/test_web_skills.py::test_skill_listing_is_read_only_for_members`<br>`tests/test_web_skills.py::test_admin_can_disable_a_skill_for_the_next_run` | delivered |
| P7-S9-A09 | 部署：`WATER_AGENT_SKILLS_DIR` 指向只读挂载的 `app_skills` 卷，不在备份范围；内置 skill 随包分发 | `tests/test_web_deployment.py::test_operator_skills_volume_is_read_only` | delivered |
| P7-S9-A10 | `skill_p0`：目录与暴露、正文注入与来源、停用 / 不安全 / 遮蔽 / 删除不注入、加载规程后数值仍只来自计算工具且引用仍过校验器；接进 `ci_gate.sh` 与入仓基线对比 | `gate:skill_p0`<br>`tests/test_skill_eval.py::test_skill_p0_hard_metrics` | delivered |
| P7-S9-A11 | 快照：现有 15 个 profile 除主体能力集多 `load_skill` 外逐字不变（范围块 `frozen_json` 冻结其余全部片段），新增 `session_skills_builtin`；`WATER_AGENT_SKILLS` / `WATER_AGENT_SKILLS_DIR` 被钉住 | `tests/test_tool_assembly_snapshot.py::test_snapshot_matches_baseline`<br>`tests/test_tool_assembly_snapshot.py::test_snapshot_is_hermetic_and_deterministic` | delivered |
| P7-S9-A12 | 前端 Skill 库区：成员只读列表、管理员启停与查看正文、MCP 计数卡片（由 `web/app/tests/` 的 node 测试覆盖，见未覆盖项） | 无 | untested |
| P7-S9-A13 | 真实模型加载并遵循规程（需 `DEEPSEEK_API_KEY`，见未覆盖项） | 无 | untested |

| P7-S8-A01 | 配置：`transport: streamable_http` 与 `url` / `allow_private_network` / `oauth` / `bearer_credential`；按 transport 互斥（混写 stdio 字段即 `MCP_CONFIG_INVALID`）；URL 加载期校验（只 https，私网开关才收 http，拒 userinfo / fragment）；这些字段 Web 管理层写不了 | `tests/test_mcp_http_config.py::test_transport_fields_are_mutually_exclusive`<br>`tests/test_mcp_http_config.py::test_url_is_validated_at_load_time`<br>`tests/test_mcp_http_config.py::test_web_layer_cannot_write_transport_fields` | delivered |
| P7-S8-A02 | 出口与 SSRF：每次建连解析并分类拒绝保留地址（`MCP_DESTINATION_FORBIDDEN`，零字节发出）、非常规 IPv4 写法与 IPv4-mapped IPv6、连接钉死校验过的 IP（防 DNS 重绑定）、3xx 不跟随、响应字节上限、Content-Type 校验 | `tests/test_mcp_http_transport.py::test_reserved_destinations_are_refused_before_sending`<br>`tests/test_mcp_http_transport.py::test_connection_is_pinned_to_the_checked_address`<br>`tests/test_mcp_http_transport.py::test_redirects_are_never_followed`<br>`tests/test_mcp_http_transport.py::test_response_limits_and_content_type` | delivered |
| P7-S8-A03 | 协议客户端：与 stdio 客户端同一外部接口；JSON 与 SSE 两种响应；`MCP-Session-Id` 回发、`MCP-Protocol-Version` 头、404 重建一次、关闭发 DELETE；server→client 请求回 MethodNotFound；新版无状态协议记 `MCP_PROTOCOL_UNSUPPORTED`；同一连接请求串行；工具调用不自动重发 | `tests/test_mcp_http_client.py::test_json_and_sse_responses`<br>`tests/test_mcp_http_client.py::test_session_header_and_expiry`<br>`tests/test_mcp_http_client.py::test_modern_only_server_is_identified`<br>`tests/test_mcp_http_client.py::test_tool_calls_are_never_replayed` | delivered |
| P7-S8-A04 | 治理：`ProviderKind.MCP_REMOTE`、`CallCategory.MCP_TOOL_CALL`、`PROVIDER_CALL_SITES` 7 → 8、静态扫描命中新出站模块（反证：挪到未登记模块即红）；每次远程请求经 `ProviderGateway`、工具调用零重试、usage 记未知 | `tests/test_governance_contracts.py::TestInventoryCoverage::test_all_provider_sites_governed_after_step12`<br>`tests/test_mcp_http_client.py::test_remote_calls_go_through_the_gateway` | delivered |
| P7-S8-A05 | 静态 Bearer：`bearer_credential` 必须是本 server 声明的槽位、与 `oauth` 互斥；值只进 `Authorization` 头；缺值 `MCP_CREDENTIAL_MISSING` | `tests/test_mcp_http_client.py::test_bearer_slot_is_sent_only_as_a_header` | delivered |
| P7-S8-A06 | OAuth 发现：401 的 `resource_metadata` 优先、否则两个 well-known；授权服务器元数据按规范顺序试 RFC 8414 与 OIDC；`authorization_servers` 受 `allowed_issuers` 约束；缺 `S256` 拒绝继续 | `tests/test_mcp_oauth.py::test_protected_resource_metadata_discovery_order`<br>`tests/test_mcp_oauth.py::test_authorization_server_metadata_discovery_order`<br>`tests/test_mcp_oauth.py::test_pkce_support_is_required` | delivered |
| P7-S8-A07 | OAuth 流程：PKCE S256、一次性 state、`resource` 规范 URI 两处都带、`iss` 校验、端点只 https、注册信息按 issuer 存、refresh token 轮换、`insufficient_scope` 如实报出不自动升级 | `tests/test_mcp_oauth.py::test_authorization_request_parameters`<br>`tests/test_mcp_oauth.py::test_callback_rejects_bad_state_and_issuer`<br>`tests/test_mcp_oauth.py::test_refresh_rotation_and_expiry`<br>`tests/test_mcp_oauth.py::test_registration_is_bound_to_the_issuer` | delivered |
| P7-S8-A08 | 令牌保管与 Web：令牌只存凭据卷（0600、只写不读、不进备份）、只经 `Authorization` 头发给签发它的 server；授权 / 回调 / 撤销路由 `require_admin` + CSRF、回调只认服务端 state；`WEB_PUBLIC_BASE_URL` 未配不提供授权；审计不含令牌 / code / verifier / state | `tests/test_web_mcp_oauth.py::test_oauth_round_trip_is_admin_only`<br>`tests/test_web_mcp_oauth.py::test_tokens_never_leave_the_credential_volume` | delivered |
| P7-S8-A09 | 连接池：HTTP server 支持 `per_run` / `shared`，shared 复用 MCP 会话；指纹含令牌文件摘要（刷新不换、重新授权换）；会话 404 透明重建一次 | `tests/test_mcp_pool.py::test_http_server_sessions_are_pooled` | delivered |
| P7-S8-A10 | 导入：HTTP 条目生成 `streamable_http` 禁用提案、头里的值丢弃并提示改用槽位或 OAuth；旧 HTTP+SSE 仍不支持 | `tests/test_mcp_import.py::test_http_entries_become_disabled_proposals` | delivered |
| P7-S8-A11 | 面板：HTTP server 卡片显示传输、主机名、私网标记、授权状态；授权 / 撤销按钮；面板改不了 URL 与 oauth（前端由 node 测试覆盖，见未覆盖项） | `tests/test_web_mcp_admin.py::test_http_server_view_hides_url_path_and_tokens` | delivered |
| P7-S8-A12 | `mcp_http_p0`：会话 / runner / Web 三条路径经本地假 HTTP server 的暴露、跳过码、调用结果、SSRF 拦截零字节、令牌零泄漏、工具调用零重放；接进 `ci_gate.sh` 与入仓基线对比 | `gate:mcp_http_p0`<br>`tests/test_mcp_http_eval.py::test_mcp_http_p0_hard_metrics` | delivered |
| P7-S8-A13 | 快照：现有 16 个 profile 逐字不变、只有 `/versions/MCP_CONFIG_VERSION` 变化；新增 `session_http_mcp_fake`；`WEB_PUBLIC_BASE_URL` 被钉住 | `tests/test_tool_assembly_snapshot.py::test_snapshot_matches_baseline`<br>`tests/test_tool_assembly_snapshot.py::test_snapshot_is_hermetic_and_deterministic` | delivered |
| P7-S8-A14 | 真实 HTTP MCP server 与真实授权服务器（含企业 TLS / 代理）上的连通与授权（见未覆盖项） | 无 | untested |
| P7-S8-A15 | 手动验收：浏览器里授权跳转、回调、工具出现、撤销后工具记 `MCP_OAUTH_REQUIRED`（见未覆盖项） | 无 | untested |

| P7-S10-A01 | 端点解析：`resolve_chat_endpoint` / `resolve_embedding_endpoint` 是唯一来源，CLI / Web / runner / docgen / judge / 探针都经它；缺省 = 现状；端点改了不回落旧 Key；与 `LOCAL_MODEL_*` 同时启用即配置错误 | `tests/test_llm_endpoints.py::test_all_entry_points_share_one_resolution`<br>`tests/test_llm_endpoints.py::test_key_is_never_sent_to_another_provider`<br>`tests/test_llm_endpoints.py::test_defaults_are_unchanged`<br>`tests/test_llm_endpoints.py::test_endpoint_url_is_validated_and_local_model_is_exclusive` | delivered |
| P7-S10-A02 | 兼容性硬门槛：非缺省对话端点没有精确匹配、全部 passed 的报告即装配期拒绝、Web 424 `WEB_MODEL_NOT_VERIFIED`；缺省免检；改端点或模型即作废报告；探针报告不含 Key | `tests/test_endpoint_compatibility.py::test_unverified_endpoint_is_refused`<br>`tests/test_endpoint_compatibility.py::test_report_must_match_endpoint_and_model`<br>`tests/test_endpoint_compatibility.py::test_probe_report_has_no_secrets` | delivered |
| P7-S10-A03 | 治理与缓存：不新增 `ProviderKind`、出站清单数量不变；超时 / 价格覆盖（价格只软预警）；`model_endpoint_digest` 变化即 miss、空串与历史键同键 | `tests/test_answer_cache.py::TestInvalidationFactors::test_each_factor_change_causes_miss`<br>`tests/test_llm_endpoints.py::test_price_override_is_not_a_hard_cap` | delivered |
| P7-S10-A04 | embedding 端点与索引绑定：任意 OpenAI 兼容 embedding 端点；`index_meta.json` 记录模型与维度；不匹配 → `RETRIEVAL_EMBEDDING_MISMATCH`（auto 降级到关键词并标出、hybrid 强制即失败）；缺 meta 的旧索引按百炼缺省处理 | `tests/test_embedding_endpoint.py::test_mismatched_embedding_never_searches_vectors`<br>`tests/test_embedding_endpoint.py::test_legacy_index_without_meta`<br>`tests/test_embedding_endpoint.py::test_any_openai_compatible_embedding_endpoint` | delivered |
| P7-S10-A05 | 检索模式：`auto` 缺 embedding → text（不调 embedding）、`hybrid` 强制、`text` 强制；`retrieval_mode` / `_reason` 进运行状态、trace 与 Web 结果；Web 预检只在强制 hybrid 时要求 embedding | `tests/test_retrieval_mode.py::test_auto_falls_back_to_text_visibly`<br>`tests/test_retrieval_mode.py::test_text_mode_makes_no_embedding_calls`<br>`tests/test_web_runs.py::test_embedding_key_is_optional_in_auto_mode` | delivered |
| P7-S10-A06 | 纯关键词建库：`build_index.py --text-only` 零 embedding 调用、不要求 Key；产出的索引 auto 下走 text、hybrid 强制失败 | `tests/test_build_index_text_only.py::test_text_only_index_needs_no_key` | delivered |
| P7-S10-A07 | 口语化检索评测 `retrieval_colloquial_p0`：先冻结；text 与 hybrid 两种模式的 Recall@1/5、MRR 与字面重合率；text 模式接进 `ci_gate.sh` 与入仓基线对比；hybrid 需 Key（见未覆盖项） | `gate:retrieval_colloquial_p0`<br>`tests/test_retrieval_mode_eval.py::test_colloquial_set_is_frozen_and_measured`<br>`tests/test_retrieval_mode_eval.py::test_colloquial_baseline_comparator` | delivered |
| P7-S10-A08 | Web 面板：对话端点与模型的读写（管理员 + CSRF、只接受 https 与列明的本机例外、运维 env 设了即只读）、Key 槽位只写不读、兼容性状态与"运行检查"（同时只一个）、检索模式显示、审计不含 Key | `tests/test_web_llm_endpoint.py::test_endpoint_settings_are_admin_only_and_validated`<br>`tests/test_web_llm_endpoint.py::test_changing_endpoint_invalidates_compatibility` | delivered |
| P7-S10-A09 | 部署与冒烟：compose 透传新变量（缺省为空 = 现状）；冒烟断言 `retrieval_mode` 字段与配置一致 | `tests/test_web_deployment.py::test_endpoint_variables_are_passed_through`<br>`tests/test_container_smoke.py::test_probe_reports_retrieval_mode` | delivered |
| P7-S10-A10 | 快照：现有 17 个 profile（S8 先合入，多 `session_http_mcp_fake`）逐字不变、三个版本号不变；新增 `session_endpoint_custom`（chat model 指向配置的端点与模型、兼容状态 verified）；新变量被钉住 | `tests/test_tool_assembly_snapshot.py::test_snapshot_matches_baseline`<br>`tests/test_tool_assembly_snapshot.py::test_snapshot_is_hermetic_and_deterministic`<br>`tests/test_tool_assembly_snapshot.py::test_endpoint_profile_is_added_and_the_existing_ones_are_untouched` | delivered |
| P7-S10-A11 | 非 DeepSeek 端点上的真实作答质量（`rag_p1 --variant real`）与 hybrid 模式在口语化集上的实测（需对话 / 百炼 Key，见未覆盖项） | 无 | untested |
| P7-S10-A12 | 手动验收：只配一个对话 Key 时带引用作答且显示关键词模式；换端点未验证即 424；改 embedding 模型未重建索引即显示不匹配（见未覆盖项） | 无 | untested |

### S15：模型目录与按会话 / 按消息选择模型（Step H）

规格 `docs/design/18-p7-multi-model.md` §7 的同名验收行在此登记，让追溯检查能核对（18 号文档的不变量 I15 / I16 / I17 / I21 不在这里登记为行，
因为本节追溯检查固定只认 P7-I01…I07 七条不变量；测试 docstring 里用 `SDD18-Ixx` 引用）。

| # | 验收 | 验证项 | 状态 |
|---|---|---|---|
| P7-S15-A01 | 目录：格式 / 条数 / id 校验、原子写 0600、成员只读、管理员 + CSRF 写、env 锁定只读、审计只记 id；缺省不配目录 = 行为逐字不变 | `tests/test_model_catalog.py::test_no_catalog_file_means_an_empty_catalog_without_error`<br>`tests/test_model_catalog.py::test_write_then_read_round_trips_with_a_private_atomic_file`<br>`tests/test_model_catalog.py::test_invalid_catalogs_are_rejected_without_echoing_values`<br>`tests/test_model_catalog.py::test_a_corrupt_or_foreign_file_fails_closed_with_an_error`<br>`tests/test_model_catalog.py::test_members_only_see_identity_fields`<br>`tests/test_web_model_selection.py::test_catalog_is_admin_managed_and_member_readable`<br>`tests/test_web_model_selection.py::test_catalog_is_read_only_when_the_deployment_locks_the_endpoint` | delivered |
| P7-S15-A02 | 逐对验证：存储与回落、`matches()` 复核、原单文件路径不变、改一个模型只作废它自己、面板按模型跑检查；探针用与生产相同的 `extra_body` | `tests/test_model_verification.py::test_a_pair_report_verifies_exactly_its_own_pair`<br>`tests/test_model_verification.py::test_the_file_name_is_an_index_not_a_trust_root`<br>`tests/test_model_verification.py::test_stale_probe_version_and_failed_checks_do_not_verify`<br>`tests/test_model_verification.py::test_the_legacy_single_report_path_is_unchanged`<br>`tests/test_model_verification.py::test_default_exemption_is_not_widened_by_the_catalog`<br>`tests/test_model_verification.py::test_probe_for_a_catalog_model_sends_the_production_extra_body`<br>`tests/test_model_verification.py::test_probe_sends_nothing_extra_for_default_profile_models`<br>`tests/test_web_model_selection.py::test_panel_runs_the_compatibility_check_for_a_catalog_model` | delivered |
| P7-S15-A03 | 构造：`extra_body` 仅设置时传；`thinking` 三种取值映射到请求体；默认构造参数与 `temperature=0.0` 逐字不变 | `tests/test_chat_model.py::test_extra_body_is_forwarded_only_when_given`<br>`tests/test_model_catalog.py::test_thinking_profiles_map_to_the_request_body` | delivered |
| P7-S15-A04 | 选择校验顺序与错误码；不收 `base_url` / `api_key`；403 先于 424；LOCAL / env 锁定拒绝；`roundtrip` 条目不可选 | `tests/test_web_model_selection.py::test_selection_refuses_unknown_disabled_thinking_and_endpoint_fields_without_creating_a_run`<br>`tests/test_web_model_selection.py::test_role_forbidden_precedes_the_verification_failure`<br>`tests/test_web_model_selection.py::test_local_model_mode_refuses_every_override` | delivered |
| P7-S15-A05 | 下传与协议兼容：选了模型而工厂不支持直接报错；缺省不传 kwarg | `tests/test_web_model_selection.py::test_a_factory_that_cannot_honour_the_choice_refuses_instead_of_ignoring_it`<br>`tests/test_model_governance.py::test_starting_with_a_model_on_a_factory_without_support_is_an_error` | delivered |
| P7-S15-A06 | 迁移 0008（SQLite 真跑 upgrade / downgrade）与三处版本同步 | `tests/test_p5_step38_storage.py::test_step15_migration_adds_and_removes_the_nullable_model_columns` | delivered |
| P7-S15-A07 | 会话默认与优先级；`PATCH` 授权；会话默认被停用后失败关闭；`resume` 以 `runs.model` 为准；payload 键集合 | `tests/test_web_model_selection.py::test_message_beats_session_default_beats_global_and_is_persisted`<br>`tests/test_web_model_selection.py::test_session_default_authorization_and_validation`<br>`tests/test_web_model_selection.py::test_a_disabled_session_default_fails_closed_instead_of_swapping_models`<br>`tests/test_web_model_selection.py::test_resume_after_a_restart_restores_the_persisted_run_model` | delivered |
| P7-S15-A08 | 治理：成本按模型、未登记为 `None`；`model_selected` 事件；`payload.model`；选中的模型失败时从不被换成别的 | `tests/test_model_governance.py::test_cost_is_attributed_per_catalog_model_and_unknown_prices_are_none`<br>`tests/test_model_governance.py::test_llm_call_record_and_trace_keep_unknown_cost_as_none`<br>`tests/test_model_governance.py::test_a_failing_selected_model_is_never_swapped_for_another` | delivered |
| P7-S15-A09 | 缺省行为不变：快照现有 18 个 profile 与三个版本号逐字不变；新增 `session_pick_model_off`（chat model 指向所选模型、带关闭思考的 `extra_body`、兼容状态按逐对报告判为 verified） | `tests/test_tool_assembly_snapshot.py::test_pick_model_profile_is_added_and_the_existing_ones_are_untouched` | delivered |
| P7-S15-A10 | 前端：选择器仅目录非空且有可选项才渲染；请求体仅选了才带 `model`；Inspector「本次模型」；目录管理区在模型页（不新增副栏子页）；文案不暗示更准（见未覆盖项） | 无 | untested |
| P7-S15-A11 | `model_choice_p0` 门禁 + 入仓基线；每个硬指标一条反证（拿掉对应保护即变红） | `gate:model_choice_p0`<br>`tests/test_model_choice_eval.py::test_model_choice_p0_hard_metrics`<br>`tests/test_model_choice_eval.py::test_each_hard_metric_goes_red_when_its_protection_is_removed` | delivered |
| P7-S15-A12 | 部署：compose 冒烟期望的迁移头与代码一致；迁移服务先于 app（既有约束） | `tests/test_web_deployment_overlay.py::test_smoke_expected_migration_head_matches_the_code_constant` | delivered |
| P7-S15-A13 | 真实 Key 实测：目录模型（含 `thinking: off`）的兼容性检查、真实带工具问答、`rag_p1 --variant real`（见未覆盖项） | 无 | untested |
| P7-S15-A14 | 手动验收：浏览器里选择器与目录管理区的交互（见未覆盖项） | 无 | untested |

### S11：外部内容底座（Step I）

规格 `docs/design/17-p7-external-capabilities.md` §11 的同名验收行在此登记，让追溯检查能核对。17 号 SDD 的不变量 I08…I14 不在这里登记为行
（本节追溯检查固定只认 P7-I01…I07 七条不变量），测试 docstring 里用 `SDD17-Ixx` 引用。

| # | 验收 | 验证项 | 状态 |
|---|---|---|---|
| P7-S11-A01 | `SourceTag.WEB_CONTENT`：信任 5（`TOOL_OUTPUT` 10 之下、`GENERATED_OUTPUT` 0 之上）、不可承载指令、`relabel` 只降不升；`INSTRUCTION_BEARING_SOURCES` 不变 | `tests/test_governance_contracts.py::TestSourceTagNotDowngradable::test_web_content_sits_between_tool_output_and_generated_output` | delivered |
| P7-S11-A02 | 信封与核心边界归一化：随机 id、固定声明行、伪造边界 / 模板 token / 不可见 Unicode / 外发语法逐条中和（敌意语料 64 条）、注入命中整条丢弃、良性文本不改写、截断不切替换串 | `tests/test_external_envelope.py::test_corpus_is_frozen_ascii_and_complete`<br>`tests/test_external_envelope.py::test_hostile_corpus_is_neutralized_and_benign_text_untouched`<br>`tests/test_external_envelope.py::test_injection_rows_are_dropped_not_laundered`<br>`tests/test_external_envelope.py::test_wrap_uses_a_fresh_random_id_and_a_fixed_declaration`<br>`tests/test_external_envelope.py::test_forged_boundary_inside_a_field_cannot_close_the_envelope`<br>`tests/test_external_envelope.py::test_urls_and_titles_pass_through_the_same_normalization_and_limits`<br>`tests/test_external_envelope.py::test_normalization_is_deterministic_and_never_calls_a_model`<br>`tests/test_external_envelope.py::test_truncation_never_cuts_through_a_replacement_token` | delivered |
| P7-S11-A03 | `governed_get`：经 `open_exchange` + `ProviderGateway`；禁止目的地零字节、3xx 不跟随、超限中止、媒体类型、超时；错误映射稳定码且不含上游正文 / 网址 / 密钥；usage 记未知；模块不含原始网络原语 | `tests/test_external_http_get.py::test_ok_returns_the_body_and_records_the_request`<br>`tests/test_external_http_get.py::test_forbidden_destination_sends_zero_bytes`<br>`tests/test_external_http_get.py::test_metadata_address_is_refused_even_with_allow_private_network`<br>`tests/test_external_http_get.py::test_dns_rebinding_is_pinned_to_the_validated_address`<br>`tests/test_external_http_get.py::test_redirects_are_never_followed`<br>`tests/test_external_http_get.py::test_oversize_response_is_aborted`<br>`tests/test_external_http_get.py::test_media_type_must_match_when_required`<br>`tests/test_external_http_get.py::test_deadline_is_enforced`<br>`tests/test_external_http_get.py::test_http_errors_map_to_stable_codes_without_echoing_anything`<br>`tests/test_external_http_get.py::test_the_request_goes_through_the_provider_gateway`<br>`tests/test_external_http_get.py::test_a_forbidden_destination_never_reaches_the_gateway_peer_and_is_not_retried`<br>`tests/test_external_http_get.py::test_gateway_rate_limit_is_mapped_without_leaking_bucket_details`<br>`tests/test_external_http_get.py::test_module_has_no_raw_network_primitive` | delivered |
| P7-S11-A04 | 结构不变量（17 号 SDD 的 I08 / I09）：`open_exchange(` 的调用方 ∈ 登记表（含反证）；本地工具参数不含 `url` / `uri` / `href` / `endpoint` / `host`（含反证） | `tests/test_plugin_invariants.py::test_open_exchange_callers_are_registered`<br>`tests/test_plugin_invariants.py::test_open_exchange_registry_goes_red_for_an_unregistered_caller_and_a_stale_entry`<br>`tests/test_plugin_invariants.py::test_local_tools_take_no_destination_argument`<br>`tests/test_plugin_invariants.py::test_destination_argument_check_catches_a_url_parameter` | delivered |
| P7-S11-A05 | 静态出站扫描扩展：socket / `urllib.request` / `aiohttp` / 子进程 `curl`·`wget`（AST）各有"放进未登记模块即红"的反证；良性写法不误判；扫描豁免恰为评审过的三项；站点数仍为 8 | `tests/test_governance_contracts.py::TestInventoryCoverage::test_each_new_egress_pattern_catches_an_unregistered_module`<br>`tests/test_governance_contracts.py::TestInventoryCoverage::test_benign_modules_are_not_flagged_as_egress`<br>`tests/test_governance_contracts.py::TestInventoryCoverage::test_the_scan_reads_comments_too`<br>`tests/test_governance_contracts.py::TestInventoryCoverage::test_scan_exemptions_are_exactly_the_reviewed_three`<br>`tests/test_governance_contracts.py::TestInventoryCoverage::test_no_new_provider_call_site_in_s11` | delivered |
| P7-S11-A06 | 回合污点：`ToolSpec.result_source` → `web_tainted`（逐轮复位）、`web_refs` 跨轮累积；缓存拒绝 `web_content_used`；披露行由程序追加、不写进会话里的模型消息；记忆写入对联网内容 fail closed | `tests/test_web_taint.py::test_result_source_defaults_to_none_and_existing_tools_are_unchanged`<br>`tests/test_web_taint.py::test_a_web_tool_sets_the_taint_and_the_program_appends_the_disclosure_line`<br>`tests/test_web_taint.py::test_no_taint_without_a_web_tool_and_the_answer_is_unchanged`<br>`tests/test_web_taint.py::test_taint_is_per_turn_but_web_refs_accumulate_across_turns`<br>`tests/test_web_taint.py::test_web_tainted_turns_are_never_eligible_for_the_answer_cache`<br>`tests/test_web_taint.py::test_web_content_can_never_be_written_to_long_term_memory` | delivered |
| P7-S11-A07 | 终答网址追溯：不可追溯网址回流一次、再失败即 `failed`；回流后改用真实网址可成功；无污点且无 `web_refs` 的回合不校验；联网网址不进 `cite_clauses` 可追溯集合 | `tests/test_web_taint.py::test_url_helpers_normalize_and_compare`<br>`tests/test_web_taint.py::test_a_fabricated_url_is_reflowed_once_then_fails`<br>`tests/test_web_taint.py::test_a_fabricated_url_then_a_genuine_one_succeeds`<br>`tests/test_web_taint.py::test_a_genuine_url_passes_without_a_reflow`<br>`tests/test_web_taint.py::test_urls_are_not_checked_when_nothing_web_was_used`<br>`tests/test_web_taint.py::test_web_urls_never_enter_the_citable_source_set` | delivered |
| P7-S11-A08 | 答案渲染不加载外部图片（Markdown 图片 / 引用式图片 / data 图片以纯文本占位，只留主机名）；`skipHtml` 与链接 `noopener noreferrer nofollow` 不退化（前端 node 测试，见未覆盖项） | 无 | untested |
| P7-S11-A09 | `external_content_p0` 门禁 + 入仓基线；14 项硬指标各一条反证；现有 19 个快照 profile 与三个版本号逐字不变 | `gate:external_content_p0`<br>`tests/test_external_content_eval.py::test_external_content_p0_hard_metrics`<br>`tests/test_external_content_eval.py::test_each_hard_metric_goes_red_when_its_protection_is_removed`<br>`tests/test_external_content_eval.py::test_every_hard_metric_has_a_counterexample` | delivered |

### S12：联网搜索（Step J）

规格 `docs/design/17-p7-external-capabilities.md` §11 的同名验收行在此登记（同 S11：17 号 SDD 的不变量 I09…I11 不在这里登记为行，
测试 docstring 里用 `SDD17-Ixx` 引用）。

| # | 验收 | 验证项 | 状态 |
|---|---|---|---|
| P7-S12-A01 | 能力与开关：`WEB_SEARCH` 不在默认集与 Web 成员集；`GRANTABLE` 扩为 `{MCP_INVOKE, WEB_SEARCH}`（P7-I06）；开关 / provider / 基址 / 密钥缺一即插件 skipped；开关取值非法 / provider 拼错装配期失败；`NETWORK_FETCH` 仍被拒 | `tests/test_web_search_exposure.py::test_session_off_is_byte_identical_and_unticked_registers_nothing`<br>`tests/test_web_search_tool.py::test_switch_defaults_to_off`<br>`tests/test_web_search_tool.py::test_invalid_switch_values_fail_loudly`<br>`tests/test_web_search_tool.py::test_misspelled_provider_fails_loudly`<br>`tests/test_web_search_tool.py::test_missing_items_mean_skipped`<br>`tests/test_web_search_tool.py::test_base_url_shape_is_judged_at_request_time_not_here`<br>`tests/test_web_search_tool.py::test_allow_private_is_an_explicit_operator_flag`<br>`tests/test_web_search_tool.py::test_brave_needs_its_own_key_and_searxng_never_reads_one`<br>`tests/test_web_search_tool.py::test_repr_and_host_leak_nothing`<br>`tests/test_web_search_tool.py::test_capability_surface`<br>`tests/test_web_search_tool.py::test_tool_contract_is_two_parameters_and_marked_as_web_content`<br>`tests/test_web_search_tool.py::test_no_request_context_means_no_request` | delivered |
| P7-S12-A02 | 适配器：纯函数（不 import 网络原语，SDD17-I10）、假服务响应解析、provider 拼错装配期失败、显式 provider 失败不回退、无密钥 provider 不读别家密钥 | `tests/test_web_search_adapters.py::test_adapters_import_no_network_primitive`<br>`tests/test_web_search_adapters.py::test_network_import_check_catches_a_raw_primitive`<br>`tests/test_web_search_adapters.py::test_searxng_builds_a_keyless_json_query`<br>`tests/test_web_search_adapters.py::test_searxng_parses_only_title_url_content`<br>`tests/test_web_search_adapters.py::test_searxng_rejects_bad_shapes_without_echoing`<br>`tests/test_web_search_adapters.py::test_brave_puts_the_token_only_in_its_header`<br>`tests/test_web_search_adapters.py::test_brave_parses_web_results_and_tolerates_a_missing_web_block`<br>`tests/test_web_search_adapters.py::test_unknown_provider_ids_fail_and_there_is_no_fallback`<br>`tests/test_web_search_adapters.py::test_a_failing_explicit_provider_is_not_retried_elsewhere` | delivered |
| P7-S12-A03 | 目的地：基址由运维写死，禁止目的地零字节（含 DNS 重绑定）、3xx 不跟随、响应上限、Content-Type；`web_search/` 不直接调用 `open_exchange` | `tests/test_web_search_transport.py::test_loopback_needs_the_operator_flag_and_sends_zero_bytes_without_it`<br>`tests/test_web_search_transport.py::test_forbidden_destinations_are_refused_before_any_byte`<br>`tests/test_web_search_transport.py::test_dns_rebinding_to_metadata_is_refused_with_zero_requests`<br>`tests/test_web_search_transport.py::test_redirects_are_not_followed`<br>`tests/test_web_search_transport.py::test_unusable_responses_are_bad_responses`<br>`tests/test_web_search_transport.py::test_successful_request_shape`<br>`tests/test_web_search_transport.py::test_web_search_package_has_no_raw_network_primitive_and_never_calls_open_exchange`<br>`tests/test_web_search_transport.py::test_raw_network_check_catches_a_socket_import` | delivered |
| P7-S12-A04 | `QueryEgressGuard`：长度 / 单行 / 不可见字符 / 网址形态 / PII / 凭据 / 注入 / 私有上下文回显（与用户本人消息的重合豁免）/ 每回合 3 次；良性查询不误拦 | `tests/test_web_query_guard.py::test_leaky_queries_are_refused_with_a_stable_reason`<br>`tests/test_web_query_guard.py::test_private_context_echo_is_refused`<br>`tests/test_web_query_guard.py::test_overlap_threshold_is_exactly_sixteen_characters`<br>`tests/test_web_query_guard.py::test_overlap_with_the_users_own_message_is_allowed`<br>`tests/test_web_query_guard.py::test_overlap_check_survives_whitespace_and_width_variants`<br>`tests/test_web_query_guard.py::test_turn_limit_is_three_and_is_reported_as_quota`<br>`tests/test_web_query_guard.py::test_benign_queries_are_not_blocked`<br>`tests/test_web_query_guard.py::test_longest_common_run_edges`<br>`tests/test_web_query_guard.py::test_guard_never_touches_the_network`<br>`tests/test_web_query_guard.py::test_known_limit_paraphrased_leaks_pass` | delivered |
| P7-S12-A05 | 结果流水线：网址校验与规范化、去重、注入命中丢弃且不占名额、至多 count 条、信封、预算、`urls` 只列保留的条目 | `tests/test_web_search_results.py::test_unacceptable_urls_are_dropped`<br>`tests/test_web_search_results.py::test_urls_are_normalized`<br>`tests/test_web_search_results.py::test_duplicates_are_merged_by_normalized_url`<br>`tests/test_web_search_results.py::test_at_most_count_and_count_is_clamped`<br>`tests/test_web_search_results.py::test_injected_items_are_dropped_whole_and_do_not_take_a_slot`<br>`tests/test_web_search_results.py::test_all_dropped_returns_the_fixed_text_only`<br>`tests/test_web_search_results.py::test_kept_items_are_wrapped_in_exactly_one_envelope`<br>`tests/test_web_search_results.py::test_total_budget_is_bounded`<br>`tests/test_web_search_results.py::test_hostile_fields_are_neutralized_in_every_text_field` | delivered |
| P7-S12-A06 | 治理：`ProviderKind` / `CallCategory.WEB_SEARCH` 与画像；站点 8 → 9（`web_search`，egress 在 `mcp/http_transport.py`）；`PLUGIN_GRANTED_TOOL_CAPABILITIES` 与扫描目录扩到 `web_search/`；usage 未知；按主机熔断；`GOVERNANCE_CONFIG_VERSION` 递进 | `tests/test_governance_contracts.py::TestInventoryCoverage::test_web_search_site_profile_and_tool_are_registered`<br>`tests/test_governance_contracts.py::TestInventoryCoverage::test_a_toolspec_in_web_search_without_a_capability_goes_red`<br>`tests/test_governance_contracts.py::TestInventoryCoverage::test_web_search_usage_is_recorded_as_unknown` | delivered |
| P7-S12-A07 | 回答纪律 R1–R5（含"只有网络证据的规范性回答不能 succeeded"）；勾选了却没开放时告警 `WEB_SEARCH_NOT_EXPOSED` | `tests/test_web_search_discipline.py::test_r1_web_urls_never_become_citations`<br>`tests/test_web_search_discipline.py::test_r2_fabricated_url_reflows_once_then_fails`<br>`tests/test_web_search_discipline.py::test_r2_genuine_url_passes_without_a_reflow`<br>`tests/test_web_search_discipline.py::test_r3_a_normative_answer_backed_only_by_the_web_cannot_succeed`<br>`tests/test_web_search_discipline.py::test_r4_disclosure_line_is_appended_by_the_program_only`<br>`tests/test_web_search_discipline.py::test_r5_taint_refuses_the_answer_cache`<br>`tests/test_web_search_discipline.py::test_ticked_but_unused_search_leaves_no_taint`<br>`tests/test_web_search_discipline.py::test_ticked_while_the_deployment_is_off_warns_instead_of_silently_dropping`<br>`tests/test_web_search_discipline.py::test_the_tightest_combination_yields_and_warns` | delivered |
| P7-S12-A08 | 暴露：`request` 模式（入口标志决定、不看 query、不参与 sticky）；入选顺序 core → intent → request；schema ≤ 500；最紧组合让位 | `tests/test_web_search_discipline.py::test_ticked_while_the_deployment_is_off_warns_instead_of_silently_dropping`<br>`tests/test_web_search_discipline.py::test_the_tightest_combination_yields_and_warns`<br>`tests/test_web_search_exposure.py::test_request_group_opens_only_when_the_entry_enables_it`<br>`tests/test_web_search_exposure.py::test_request_group_is_never_sticky`<br>`tests/test_web_search_exposure.py::test_request_group_yields_after_intent_under_the_tightest_budget`<br>`tests/test_web_search_exposure.py::test_request_rule_rejects_keywords`<br>`tests/test_web_search_exposure.py::test_web_search_tool_schema_fits_and_common_combinations_fit`<br>`tests/test_web_search_exposure.py::test_session_off_is_byte_identical_and_unticked_registers_nothing`<br>`tests/test_web_search_exposure.py::test_exposure_module_stays_deterministic` | delivered |
| P7-S12-A09 | 错误模板：7 个稳定码、内核 `EXTERNAL_*` 一一映射、文本不含上游正文 / 网址 / 密钥 | `tests/test_web_search_errors.py::test_the_code_set_is_exactly_seven`<br>`tests/test_web_search_errors.py::test_every_kernel_code_maps_into_the_web_search_namespace`<br>`tests/test_web_search_errors.py::test_messages_are_fixed_and_contain_no_upstream_material`<br>`tests/test_web_search_errors.py::test_error_result_shape_and_unknown_codes` | delivered |
| P7-S12-A10 | 审计 `web_search.performed` 字段白名单与 trace 字段；密钥只在发给服务商的请求头里（响应 / 审计 / trace / 日志 / 异常 / 查询串零出现） | `tests/test_web_search_audit.py::test_audit_event_has_exactly_the_whitelisted_fields`<br>`tests/test_web_search_audit.py::test_rejected_queries_are_audited_by_code_only`<br>`tests/test_web_search_audit.py::test_extra_fields_never_reach_the_audit_and_a_failing_sink_is_harmless`<br>`tests/test_web_search_audit.py::test_trace_event_has_no_urls_and_only_whitelisted_data`<br>`tests/test_web_search_audit.py::test_provider_key_is_only_in_the_request_header`<br>`tests/test_web_search_audit.py::test_config_repr_never_shows_the_key` | delivered |
| P7-S12-A11 | Web：`MessageRequest.web_search`（严格布尔、缺省 false、不落库）、工厂不支持即 422、部署未就绪即 422；`GET /api/settings/web-search` 只读状态；密钥槽位只写；前端"联网"开关与"联网搜索"子页（node 测试，见未覆盖项） | `tests/test_web_search_web.py::test_message_field_defaults_to_false_and_is_strictly_boolean`<br>`tests/test_web_search_web.py::test_ticked_message_reaches_a_supporting_factory`<br>`tests/test_web_search_web.py::test_ticked_message_is_refused_when_it_cannot_be_honoured`<br>`tests/test_web_search_web.py::test_status_endpoint_is_read_only_and_shows_only_the_host`<br>`tests/test_web_search_web.py::test_invalid_operator_config_is_reported_without_echoing_values`<br>`tests/test_web_search_web.py::test_key_slot_is_admin_write_only_and_never_echoed`<br>`tests/test_web_search_web.py::test_the_panel_has_no_way_to_change_address_provider_or_switch`<br>`tests/test_web_search_web.py::test_the_tick_is_never_persisted` | delivered |
| P7-S12-A12 | 部署：compose 透传四个环境变量、缺省为空 = off；密钥不经环境变量传；新变量被快照钉住 | `tests/test_web_deployment.py::test_web_search_is_off_by_default_and_its_key_never_travels_by_env` | delivered |
| P7-S12-A13 | `web_search_p0` 门禁 + 入仓基线；13 项硬指标各一条反证；快照新增 `session_web_search_fake`，现有 19 个 profile 逐字不变 | `gate:web_search_p0`<br>`tests/test_web_search_eval.py::test_web_search_p0_hard_metrics`<br>`tests/test_web_search_eval.py::test_report_is_reproducible_and_matches_the_baseline`<br>`tests/test_web_search_eval.py::test_dataset_shape_is_frozen`<br>`tests/test_web_search_eval.py::test_each_hard_metric_goes_red_when_its_protection_is_removed`<br>`tests/test_web_search_eval.py::test_every_hard_metric_has_a_counterexample`<br>`tests/test_web_search_eval.py::test_harness_error_is_counted_not_skipped` | delivered |
| P7-S12-A14 | 真实搜索服务 / 真实模型是否滥用搜索、是否遵循披露（需 Key 与网络，见未覆盖项） | 无 | untested |

### S13：Skill 下载（Step K）

规格 `docs/design/17-p7-external-capabilities.md` §11 的同名验收行在此登记（同 S11 / S12：17 号 SDD 的不变量 I12 不在这里登记为行，测试 docstring 里用 `SDD17-I12` 引用）。

| # | 验收 | 验证项 | 状态 |
|---|---|---|---|
| P7-S13-A01 | 注册表协议与客户端：仅 https（回环需运维显式放行）、固定版本、index 整份严格校验（任一条目不合法即整份拒绝）、大小上限、index/文件摘要一致、媒体类型与 3xx 不跟随、错误不回显上游正文 / 网址 / 路径；不可达 / 不一致 → 失败关闭且磁盘零写入 | `tests/test_skill_registry_client.py::test_index_and_file_round_trip`<br>`tests/test_skill_registry_client.py::test_not_configured_and_forbidden_destinations_are_unavailable_with_zero_requests`<br>`tests/test_skill_registry_client.py::test_bad_statuses_and_media_types_are_refused_without_echoing_the_upstream_body`<br>`tests/test_skill_registry_client.py::test_redirects_are_not_followed`<br>`tests/test_skill_registry_client.py::test_oversize_responses_are_refused`<br>`tests/test_skill_registry_client.py::test_size_and_sha_mismatches_are_registry_mismatch`<br>`tests/test_skill_registry_client.py::test_index_is_validated_as_a_whole`<br>`tests/test_skill_registry_client.py::test_duplicate_names_and_versions_reject_the_whole_index`<br>`tests/test_skill_registry_client.py::test_request_arguments_are_validated_before_they_reach_a_path`<br>`tests/test_skill_registry_client.py::test_lookup_distinguishes_missing_versions`<br>`tests/test_skill_registry_client.py::test_config_repr_never_shows_the_full_url`<br>`tests/test_skill_lifecycle.py::test_failures_never_echo_the_registry_url_path_or_upstream_body`<br>`tests/test_skill_fetch_eval.py::test_fake_registry_serves_the_protocol_and_records_requests`<br>`tests/test_skill_fetch_eval.py::test_fake_registry_modes_change_only_the_response_shape` | delivered |
| P7-S13-A02 | 严格解析与隔离区：按原始字节（NUL / 回车 / BOM / 非法 UTF-8 / 超限）、名字与版本一律 `fullmatch`、同形异义拒绝、frontmatter 严格；候选永不出现在任何加载根下（四个根目录互不包含，目录 0700 / 文件 0600，`candidate.json` 键集合恒定且不含 URL / 路径） | `tests/test_skill_quarantine.py::test_candidate_layout_and_permissions`<br>`tests/test_skill_quarantine.py::test_candidate_json_has_exactly_the_documented_keys_and_no_urls_or_paths`<br>`tests/test_skill_quarantine.py::test_storing_is_idempotent_and_distinct_digests_coexist`<br>`tests/test_skill_quarantine.py::test_no_partial_directory_is_visible_after_a_failed_write`<br>`tests/test_skill_quarantine.py::test_finding_requires_valid_names_and_versions`<br>`tests/test_skill_quarantine.py::test_a_corrupt_candidate_json_is_skipped_not_fatal`<br>`tests/test_skill_quarantine.py::test_a_quarantined_candidate_is_invisible_to_the_library`<br>`tests/test_skill_quarantine.py::test_roots_overlap_detects_equal_ancestor_descendant_and_symlinks`<br>`tests/test_skill_quarantine.py::test_discard_removes_the_candidate`<br>`tests/test_skill_remote_safety.py::test_a_benign_package_parses`<br>`tests/test_skill_remote_safety.py::test_byte_level_rejections`<br>`tests/test_skill_remote_safety.py::test_manifest_and_consistency_rules`<br>`tests/test_skill_remote_safety.py::test_names_and_versions_use_fullmatch`<br>`tests/test_skill_lifecycle.py::test_argument_errors_send_nothing`<br>`tests/test_skill_lifecycle.py::test_config_errors_exit_two_and_overlapping_roots_are_refused`<br>`tests/test_skill_fetch_eval.py::test_hostile_generators_produce_the_bytes_they_claim` | delivered |
| P7-S13-A03 | 安检：字符正向白名单（`SKILL_REMOTE_CHARSET`，NFKC 封闭）、YAML 事件扫描先于 `safe_load`（`SKILL_REMOTE_YAML_UNSAFE`）、`SKILL_REMOTE_NO_LINKS`、希腊同形字折叠后再跑注入检测、既有 `check_skill` 照常再跑；与内置 / 运维同名预检即拒且零请求 | `tests/test_skill_remote_safety.py::test_charset_rejects_the_known_smuggling_carriers`<br>`tests/test_skill_remote_safety.py::test_charset_admits_engineering_and_cjk_text`<br>`tests/test_skill_remote_safety.py::test_charset_is_nfkc_closed`<br>`tests/test_skill_remote_safety.py::test_charset_excludes_latin_lookalike_greek_but_folding_catches_the_rest`<br>`tests/test_skill_remote_safety.py::test_charset_violations_in_body_and_in_frontmatter_are_remote_charset`<br>`tests/test_skill_remote_safety.py::test_yaml_hazards_are_rejected_before_safe_load`<br>`tests/test_skill_remote_safety.py::test_alias_bomb_is_flagged_without_expanding_it`<br>`tests/test_skill_remote_safety.py::test_benign_headers_have_no_yaml_hazards`<br>`tests/test_skill_remote_safety.py::test_a_benign_body_passes_remote_safety`<br>`tests/test_skill_remote_safety.py::test_links_and_shell_fences_are_refused`<br>`tests/test_skill_remote_safety.py::test_plain_fences_and_chinese_brackets_are_not_links`<br>`tests/test_skill_remote_safety.py::test_existing_checks_still_run_for_downloads`<br>`tests/test_skill_remote_safety.py::test_greek_folded_injection_is_caught_by_the_second_pass`<br>`tests/test_skill_remote_safety.py::test_no_report_text_echoes_package_content`<br>`tests/test_skill_lifecycle.py::test_names_taken_by_builtin_or_operator_are_refused_with_zero_requests` | delivered |
| P7-S13-A04 | `approve`：必须 TTY（非 TTY 在展示与读取之前即拒绝）、键入名字（EOF 不算确认）、`--sha256` / 隔离区文件 / `candidate.json` 三方摘要一致；没有 `--yes`、没有环境变量旁路；三个崩溃点（暂存后 / 替换后 / lock 更新前）无半安装；lock 原子更新；旧版进 `.previous/` | `tests/test_skill_approve.py::test_a_non_tty_is_refused_before_anything_is_shown_or_read`<br>`tests/test_skill_approve.py::test_the_typed_name_must_match_and_eof_is_not_confirmation`<br>`tests/test_skill_approve.py::test_the_three_way_digest_must_agree`<br>`tests/test_skill_approve.py::test_a_missing_candidate_and_a_taken_name_are_refused`<br>`tests/test_skill_approve.py::test_the_review_shows_full_text_report_and_a_diff_against_the_installed_version`<br>`tests/test_skill_approve.py::test_happy_path_installs_enables_and_records_the_lock`<br>`tests/test_skill_approve.py::test_an_upgrade_keeps_exactly_one_previous_version`<br>`tests/test_skill_approve.py::test_a_crash_during_a_first_install_leaves_no_loadable_half_install`<br>`tests/test_skill_approve.py::test_a_crash_during_an_upgrade_never_loads_bytes_the_lock_did_not_approve`<br>`tests/test_skill_approve.py::test_lock_temporary_files_do_not_survive_a_crash`<br>`tests/test_skill_approve.py::test_the_production_terminal_never_reads_the_environment_to_skip_the_tty_check`<br>`tests/test_skill_installed_layer.py::test_the_lock_is_written_atomically_and_deterministically`<br>`tests/test_skill_lifecycle.py::test_the_installed_lock_is_the_only_record_of_what_was_approved` | delivered |
| P7-S13-A05 | 第三来源层：加载期按原始字节重算 sha256 与大小，篡改 → `SKILL_TAMPERED`（不加载、不删除）；无 lock 项 → `SKILL_UNLOCKED`；lock 损坏 / `version` 不认 / 目录不存在 → 装配期 `SKILL_LIBRARY_INVALID`；遮蔽顺序 内置 > 运维 > 已安装；`enabled` 在现行顺序之后追加；`fit_catalog` 先截已安装层；`Skill` / `SkillManifest` 字段不变；缺省不设变量时逐字不变 | `tests/test_skill_installed_layer.py::test_a_locked_file_loads_as_installed`<br>`tests/test_skill_installed_layer.py::test_tampered_bytes_or_size_are_not_loaded_and_not_deleted`<br>`tests/test_skill_installed_layer.py::test_only_the_byte_digest_counts_not_a_matching_read_text_digest`<br>`tests/test_skill_installed_layer.py::test_unlocked_directories_and_missing_files`<br>`tests/test_skill_installed_layer.py::test_a_symlinked_skill_file_is_not_loaded`<br>`tests/test_skill_installed_layer.py::test_a_broken_lock_fails_loudly`<br>`tests/test_skill_installed_layer.py::test_duplicate_or_invalid_lock_entries_fail_loudly`<br>`tests/test_skill_installed_layer.py::test_a_missing_installed_directory_fails_loudly`<br>`tests/test_skill_installed_layer.py::test_shadowing_order_is_builtin_then_operator_then_installed`<br>`tests/test_skill_installed_layer.py::test_installed_skills_are_appended_after_the_existing_order`<br>`tests/test_skill_installed_layer.py::test_the_default_is_byte_identical_without_the_variable`<br>`tests/test_skill_installed_layer.py::test_installed_skills_still_pass_the_existing_safety_checks`<br>`tests/test_skill_installed_layer.py::test_the_skill_models_are_unchanged_except_the_source_literal`<br>`tests/test_load_skill_tool.py::test_installed_skills_never_push_a_builtin_out_of_the_catalog`<br>`tests/test_load_skill_tool.py::test_without_installed_skills_the_catalog_and_truncation_are_unchanged`<br>`tests/test_load_skill_tool.py::test_builtin_and_operator_relative_order_is_independent_of_the_installed_layer` | delivered |
| P7-S13-A06 | 无模型 / 面板入口（SDD17-I12）：`skills/remote/` 只被 `skills/cli.py` 导入（AST，带反证）；模型工具清单、Web 路由表（OpenAPI）与前端源码不含取回 / 批准 / 安装；`SKILL_FETCH` 只在 CLI 主体上 | `tests/test_skill_fetch_invariants.py::test_only_the_operator_cli_imports_the_remote_package`<br>`tests/test_skill_fetch_invariants.py::test_the_import_check_goes_red_for_a_rogue_importer_and_a_stale_entry`<br>`tests/test_skill_fetch_invariants.py::test_the_panel_and_library_read_the_lock_only_through_installed`<br>`tests/test_skill_fetch_invariants.py::test_the_model_sees_no_fetch_approve_or_install_tool`<br>`tests/test_skill_fetch_invariants.py::test_the_web_route_table_has_no_fetch_approve_or_install_route`<br>`tests/test_skill_fetch_invariants.py::test_the_frontend_source_cannot_reach_a_fetch_approve_or_install_endpoint`<br>`tests/test_skill_fetch_invariants.py::test_remote_modules_import_without_side_effects` | delivered |
| P7-S13-A07 | 更新是新候选、`rollback` / `remove` 同样需 TTY 且键入名字、`list` 只读；无静默更新；降级 / 相等版本被拒（`SKILL_VERSION_NOT_NEWER`，降级零请求）；重放被拒（`SKILL_REGISTRY_REPLAY`，不请求 `SKILL.md`）；审计只记主机名与短摘要 | `tests/test_skill_approve.py::test_equal_and_lower_versions_are_refused`<br>`tests/test_skill_approve.py::test_rollback_needs_a_human_and_restores_only_the_previous_version`<br>`tests/test_skill_approve.py::test_a_tampered_previous_version_is_not_rolled_back_to`<br>`tests/test_skill_approve.py::test_remove_needs_a_human_and_takes_the_lock_entry_out_first`<br>`tests/test_skill_lifecycle.py::test_fetch_approve_update_rollback_remove`<br>`tests/test_skill_lifecycle.py::test_human_gate_exit_codes_and_zero_writes`<br>`tests/test_skill_lifecycle.py::test_downgrade_and_equal_versions_are_refused_with_zero_requests`<br>`tests/test_skill_lifecycle.py::test_a_replayed_version_is_refused_before_the_file_is_requested`<br>`tests/test_skill_lifecycle.py::test_list_reports_status_and_candidates` | delivered |
| P7-S13-A08 | 面板：来源徽记「已安装」与 lock 元数据（摘要前 8 位 / 批准人 / 批准时间 / 注册表主机名，**无路径**）；篡改的已安装 skill 只显示名字与错误码；管理员只能启停；前端 node 测试锁住操作区只有「查看正文 / 启停」 | `tests/test_web_skills.py::test_installed_skills_show_provenance_without_paths`<br>`tests/test_web_skills.py::test_a_tampered_installed_skill_is_listed_only_by_name_and_code`<br>`tests/test_web_skills.py::test_the_panel_can_disable_an_installed_skill_but_nothing_else` | delivered |
| P7-S13-A09 | 治理：`Capability.SKILL_FETCH`（不在默认集 / 成员集 / `GRANTABLE`，不在危险集）、`ProviderKind` / `CallCategory.SKILL_REGISTRY` 与画像、站点 9 → 10（`skill_registry`，egress 在 `mcp/http_transport.py`）、`skills/remote/` 无原始网络原语且不直接调用 `open_exchange`、每个请求经 `ProviderGateway`、`GOVERNANCE_CONFIG_VERSION` 递进 | `tests/test_skill_registry_client.py::test_every_request_goes_through_the_governed_gateway_with_the_cli_capability`<br>`tests/test_skill_fetch_invariants.py::test_skill_fetch_capability_is_held_by_nobody_but_the_cli_principal`<br>`tests/test_skill_fetch_invariants.py::test_the_remote_package_has_no_raw_network_primitive_and_never_calls_open_exchange` | delivered |
| P7-S13-A10 | 部署：`skills-admin`（profile `ops`、复用 app 镜像、不挂 secrets 不连 postgres）；app 只读挂载安装区且不挂隔离区、注册表基址不进 app 环境；三个新卷都在 `/var/lib/water-agent` 之外（不进备份）；Dockerfile 预建并归 `wateragent`；新变量被快照钉住 | `tests/test_web_deployment.py::test_skills_download_is_an_ops_only_service_and_its_volumes_stay_out_of_the_backup`<br>`tests/test_web_deployment.py::test_skill_download_variables_are_pinned_by_the_assembly_snapshot` | delivered |
| P7-S13-A11 | `skill_fetch_p0` 门禁 + 入仓基线；十一项硬指标各一条反证 + 完备性断言；快照新增 `session_skills_installed`，现有 20 个 profile 与三个版本号逐字不变 | `gate:skill_fetch_p0`<br>`tests/test_skill_fetch_eval.py::test_skill_fetch_p0_hard_metrics`<br>`tests/test_skill_fetch_eval.py::test_report_is_reproducible_and_matches_the_baseline`<br>`tests/test_skill_fetch_eval.py::test_dataset_shape_is_frozen`<br>`tests/test_skill_fetch_eval.py::test_each_hard_metric_goes_red_when_its_protection_is_removed`<br>`tests/test_skill_fetch_eval.py::test_every_hard_metric_has_a_counterexample`<br>`tests/test_skill_fetch_eval.py::test_harness_error_is_counted_not_skipped`<br>`tests/test_skill_fetch_eval.py::test_an_unmeasured_metric_is_none_and_fails_the_gate`<br>`tests/test_skill_fetch_eval.py::test_a_missing_mechanism_is_reported_as_absent`<br>`tests/test_skill_fetch_eval.py::test_comparator_rejects_hand_edited_reports_and_regressions` | delivered |
| P7-S13-A12 | 真实注册表 / 真实模型是否遵循下载的规程（需要真实注册表与 Key，见未覆盖项） | 无 | untested |

### 未覆盖项（显式声明）

- **P7-S0-A15**：它锁的是 S0 时的缺陷现状（确认门执行期不检查）。S1 修复后这个现状已不存在，原测试改写为断言修复后的事实，由 **P7-S1-A03** 承担；本行保留为历史记录，不再有对应测试。
- **P7-S0-A16 / P7-S0-A17**：它们锁的是 S0 时 runner 路径的缺陷现状（trace 调用从未落盘、与会话路径有 8 处差异）。S2 统一后这些现状已不存在，原测试改写为断言统一后的事实，分别由 **P7-S2-A12** 与 **P7-S2-A07** 承担；两行保留为历史记录，不再有对应测试。
- **P7-S2-A11**：真实质量层需要 `DEEPSEEK_API_KEY` 与网络，不在离线门禁内。没有 Key 时记为
  blocked，**不得**声称真实质量未回退。
- **P7-S0-A13**：它锁的是 S0 时的缺陷现状（工具指引截掉读图 / MCP 工具）。S4 修复后由 **P7-S4-A11** 承担（同一测试同时锁住 `full` 回滚开关下的回退格式仍截断）；本行保留为历史记录，不再有对应测试。
- **P7-S0-A14**：它锁的是 S0 时的缺陷现状（manifest 不进缓存服务）。S3 修复后由 **P7-S3-A07** 承担；本行保留为历史记录，不再有对应测试。
- **P7-S4-A13**：`tool_exposure_p0` 只证明"该开的工具开了、写进了指引、没超预算"，不证明真实模型拿到工具后选得对。
  需要 `DEEPSEEK_API_KEY` 与网络，不在离线门禁内；没有 Key 时记为 blocked，**不得**声称真实工具选择准确率。
- **P7-S10-A11**：需要非 DeepSeek 的对话端点 Key 与百炼 Key、网络；离线门禁只跑 text 模式。没有 Key 时记为 blocked，**不得**声称换模型后作答质量不变，也不得声称关键词模式与混合检索等效。
- **P7-S10-A12**：需要本机 Web 栈与浏览器，CI 上没有；自动化覆盖在 A05 / A08（HTTP 往返）与前端 node 测试。
- **P7-S15-A10**：前端测试是 `web/app/tests/model_choice.test.mjs`（`npm test`），由 `p5-ci` 的 frontend job 执行，不是追溯检查能识别的 pytest 节点。
- **P7-S11-A08**：前端测试是 `web/app/tests/answer_markdown.test.mjs`（`npm test`），由 `p5-ci` 的 frontend job 执行，不是追溯检查能识别的 pytest 节点；另用 `vite build --ssr` + `react-dom/server` 对四种图片写法做过一次真实渲染（无 `<img>`、目标网址不在任何属性里），该脚本是临时的、未入库。
- **S11 的整体边界**：归一化是确定性正则，**必有语料之外的写法**——`external_content_p0` 全绿只说明语料里的写法被覆盖，**不得声称能拦住任意注入**。S11 没有任何生产工具声明 `result_source=WEB_CONTENT`，污点逻辑只经夹具工具验证；`governed_get` 没有生产调用方，网关画像与出站站点（8 → 9）随 S12 的 `web_search` 新增。
- **P7-S15-A13**：需要对话端点 Key 与网络；离线门禁只跑假端点。没有 Key 时记为 blocked，**不得**声称换模型后作答质量不变，也不得声称"进阶档更准"；`thinking: off` 下函数调用与 `cite_clauses` 的真实质量同样未验证。
- **P7-S15-A14**：需要本机 Web 栈与浏览器；自动化覆盖在 A04 / A07（HTTP 往返）与前端 node 测试。
- **P7-S8-A14**：本机与 CI 都没有真实的 HTTP MCP server 与授权服务器；`mcp_http_p0` 只对本地假 server 与假授权服务器验证协议与治理语义，**不得**声称已接通任何真实远程 MCP 或企业单点登录。
- **P7-S8-A15**：需要本机 Web 栈、浏览器与可回调的 `WEB_PUBLIC_BASE_URL`，CI 上没有；自动化覆盖在 A08（HTTP 往返，测试直接带 state 调回调）与前端 node 测试。
- **P7-S9-A12**：前端测试是 `web/app/tests/skills.test.mjs`（`npm test`），由 `p5-ci` 的 frontend job 执行，不是追溯检查能识别的 pytest 节点。
- **P7-S9-A13**：`skill_p0` 用脚本模型，只证明"目录发出去了、加载了就进系统提示、铁律不被绕过"，不证明真实模型会在该加载时加载、加载后会照做。需要 `DEEPSEEK_API_KEY` 与网络，不在离线门禁内；没有 Key 时记为 blocked，**不得**声称规程遵循率。
- **P7-S6-A09**：前端测试是 `web/app/tests/mcp_admin.test.mjs`（`npm test`），由 `p5-ci` 的 frontend job 执行，不在 `scripts/ci_gate.sh` 里，也不是追溯检查能识别的 pytest 节点。
- **P7-S6-A11**：需要本机 Web 栈与浏览器，CI 上没有；自动化覆盖在 A04–A07（HTTP 往返）与 A09（前端单测）。2026-10-07 在本机 SQLite 开发服务器（真实鉴权 + 三层配置 + fake server）上用浏览器做了探测 → 放行 → 填凭据 → 导入含密钥配置这几段；"发一条问题、看到工具卡片"需要 PostgreSQL 栈与真实模型，未做（见 `docs/design/p7-step-6-decision.md` §2）。
- **P7-S7-A07**：前端测试是 `web/app/tests/*.test.mjs`（`npm test`），由 `p5-ci` 的 frontend job 执行，不在 `scripts/ci_gate.sh` 里，也不是追溯检查能识别的 pytest 节点；不是没有测试，而是验证项格式表达不了。
- **P7-S7-A10**：需要本机 Web 栈与浏览器，CI 上没有；自动化覆盖在 A06（HTTP 往返）与 A07（前端单测）。
- **P7-S5-A10**：依赖本机装了 autocad-mcp 与 Web 栈，CI 上没有；做了也只证明连接复用，真实 CAD/BIM 验收仍是 P4 Step 26 blocked。
- **F17**：S0 事实表里没有，是 S5 读码时发现的现有缺陷（规格补登记）。
- **P7-S3-A13**：依赖本机装了 autocad-mcp，CI 上没有。2026-10-05 本机已做：覆盖层指向 autocad-mcp 仓库后暴露 `mcp_autocad_mcp_read_dwg`。这只证明能连上，真实 CAD/BIM 验收仍是 P4 Step 26 blocked。
- **F8**：留到 S5 写测试。F9 / F10 / F15 / F16 已由 S3 的 A02 / A10 / A06 锁定。
- **P7-S12-A14**：只对本地假搜索服务验证（searxng / brave 的线上格式取自官方文档与源码原文，标 `unverified_against_real_service`）；真实服务的可用性、数据出境、真实模型是否滥用搜索或遵循披露都需要网络与 Key，记 blocked，**不得**声称已接通任何真实搜索服务。
- **P7-S13-A12**：只对本地假注册表验证（协议是我们自己定义的最小静态协议，`unverified_against_real_registry`；OpenClaw 的 ClawHub 不是静态协议，其服务端不在 OpenClaw 仓库里，本机读不到）；真实注册表的可用性、真实模型加载下载的 skill 后是否遵循、字符白名单与正则安检对语料之外改写的有效性、批准人是否认真读了全文（批准人只是 OS 用户名，不是认证；人审是唯一信任根）都需要真实环境或 Key，记 blocked，**不得**声称已接通任何真实注册表，**不得**声称“安检能防住恶意 skill”。
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
| `docs/execution-prompts/P7-S3-layered-mcp-config.md` | S3 执行 Prompt（含范围块） |
| `docs/execution-prompts/P7-S4-intent-tool-exposure.md` | S4 执行 Prompt（含范围块） |
| `docs/execution-prompts/P7-S5-shared-mcp-connections.md` | S5 执行 Prompt（含范围块） |
| `docs/execution-prompts/P7-S6-mcp-admin-panel.md` | S6 执行 Prompt（含范围块） |
| `docs/execution-prompts/P7-S7-hitl-confirmation.md` | S7 执行 Prompt（含范围块） |
| `docs/execution-prompts/P7-S8-streamable-http.md` | S8 执行 Prompt（含范围块） |
| `docs/execution-prompts/P7-S9-skill-library.md` | S9 执行 Prompt（含范围块） |
| `docs/execution-prompts/P7-S10-openai-compatible-endpoints.md` | S10 执行 Prompt（含范围块） |
| `src/water_agent/mcp/http_transport.py` + `src/water_agent/mcp/http_client.py` | S8 Streamable HTTP：唯一出站模块（URL 校验 / SSRF / 钉死 IP）与协议客户端 |
| `src/water_agent/mcp/oauth.py` + `src/water_agent/mcp/oauth_store.py` | S8 OAuth 2.1 服务级授权与令牌保管 |
| `src/water_agent/harness/mcp_http_eval.py` + `data/eval/mcp_http_p0.jsonl` | S8 评测集与 harness（入仓基线 `data/eval/mcp_http_p0_main.json`，假 server 见 `tests/fixtures/fake_mcp_http_server.py`） |
| `docs/design/p7-step-8-decision.md` | S8 决策记录（实测事实、偏离、残余风险、快照 diff） |
| `src/water_agent/generation/endpoints.py` + `src/water_agent/generation/compatibility.py` | S10 对话 / embedding 端点解析唯一来源（含面板端点）、兼容性证据与装配门槛 |
| `src/water_agent/harness/endpoint_compat.py` | S10 远程 OpenAI 兼容端点兼容性探针（测试用假端点见 `tests/fixtures/endpoint_compat/`） |
| `src/water_agent/harness/retrieval_mode_eval.py` + `data/eval/retrieval_colloquial_p0.jsonl` | S10 检索模式评测与口语化检索集（入仓基线 `data/eval/retrieval_colloquial_p0_main.json`） |
| `docs/design/p7-step-10-decision.md` | S10 决策记录（两种检索模式的指标对照、偏离、残余风险、快照 diff） |
| `src/water_agent/plugins/exposure.py` | S4 按意图暴露策略 |
| `src/water_agent/graphs/confirmation.py` | S7 外层人工确认门（请求 / 挂起 / 凭证执行 / 审计） |
| `src/water_agent/mcp/web_layer.py` + `src/water_agent/mcp/credentials.py` + `src/water_agent/mcp/importer.py` | S6 Web 管理层、凭据槽位、导入预览 |
| `src/water_agent/webapp/mcp_admin_routes.py` + `web/app/src/McpAdminPanel.tsx` | S6 管理 API 与设置页 MCP 扩展区 |
| `src/water_agent/skills/` + `src/water_agent/tools/load_skill.py` | S9 Skill 格式、加载、安检、两级来源、正文注入 |
| `src/water_agent/webapp/skill_routes.py` + `web/app/src/SkillLibraryPanel.tsx` | S9 Skill 库 API 与设置页 Skill 库区 |
| `src/water_agent/harness/skill_eval.py` + `data/eval/skill_p0.jsonl` | S9 评测集与 harness（入仓基线 `data/eval/skill_p0_main.json`） |
| `src/water_agent/harness/tool_exposure_eval.py` + `data/eval/tool_exposure_p0.jsonl` | S4 评测集与 harness（入仓基线 `data/eval/tool_exposure_p0_main.json`） |
| `src/water_agent/harness/tool_assembly_snapshot.py` | H1 装配快照 |
| `data/eval/tool_assembly_main.json` | H1 基线 |
| `src/water_agent/harness/spec_trace.py` | H2 追溯检查 |
| `scripts/check_step_scope.py` | H3 范围守卫 |
| `.github/workflows/p7-scope.yml` | H3 的 PR 检查 |
| `tests/conftest.py` | H0 导入位置守卫 |
