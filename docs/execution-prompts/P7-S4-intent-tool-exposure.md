# P7-S4 执行 Prompt：按意图最小暴露与工具分组（Step C）

> **工作区**：仓库 `YEKYYY0312/water-SAD-agent`，基于最新 `main`（必须已包含 P7-S3 与本 Prompt）。新建分支 `YEK/p7-s4-intent-tool-exposure`。
> **必读**：
> - `AGENTS.md`
> - `docs/design/16-p7-plugin-mcp.md`：§2 事实表（F3/F4/F5）、§3 插件契约、§5 S4 约束、§6 harness、§7 的 P7-S4 验收、§8 实施协议
> - `docs/design/p7-step-3-decision.md`（S3 的快照基线与残余风险）
> - `src/water_agent/prompting/assembler.py`（工具指引段与 800 字预算）、`src/water_agent/graphs/agent_rag.py`（`_BudgetChatModel`、`make_agent_loop`、`PER_TURN_RESET`）

---

## 0. 相对规格的修订与本步决定（以本文为准）

规格 §5 的 S4 行写于 S0，下面几处依据 2026-10-06 的实测与设计决定修订。规格已在本 Prompt 的同一个 PR 里同步更新：

| 原计划 / 疑问 | 本步做法 | 原因 |
|---|---|---|
| "按意图最小暴露"在哪一层决定 | **每轮**决定，在外层图 `agent_loop` 入口算一次，存进 state，本轮回流复用 | 工具在会话构造时绑定，但 CLI 交互跨轮复用同一会话，Web 每个 run 一个会话、一个 run 一轮；只有"每轮"对两条路径都成立 |
| 用什么判定意图 | **确定性关键词 / 正则**（NFKC + ASCII 小写后匹配），不调模型 | 铁律①的同类纪律：路由可测、可复现，进门禁；调模型选工具会多一次计费调用且离线无法验证 |
| `calc_velocity` 是否按意图裁剪 | **不裁**：检索、引用、流速计算三者组成 core 组，每轮必开 | 铁律①：计算工具一旦没暴露，模型只能心算；漏判的代价远大于 953 字符 |
| 未开放的工具被模型调用（名字可从指引里看到） | **照常经 `ToolRegistry.invoke` 执行**，同一套护栏与主体授权，trace 标 `unexposed_call` | 暴露不是授权边界（规格"插件不是权限主体"），授权只看 `Principal`；关键词漏判时模型还能自愈，而不是答"我没有这个能力" |
| 工具指引格式 | 按意图模式下每个已开放工具一行"工具名 + 描述首句"，末尾一行列出本轮未开放的工具名；`full` 模式与"state 里没有暴露决定"的回退保持**现行格式逐字不变** | 实测（2026-10-06）：现行整行格式下 core 三行 449 字，再加材料统计 + 读图共 840 字 > 800，最现实的组合（从 DXF 出材料表）必被截断。完整描述模型在 schema 里照样看得到，指引只是索引 |
| MCP 工具怎么分组 | 每台 server 一组 `mcp:<server_id>`，模式 `ambient`（不看意图、预算不够时最先让位）；**本步不新增 per-server 关键词配置** | 加配置字段要动 `mcp/config.py` 与 `MCP_CONFIG_VERSION`，属于 S6 面板一并设计的面；S4 只修"MCP 工具总被截掉"（F5） |
| F4 怎么算修好 | 每轮**实际发给模型**的 schema 受 4500 上限约束并由策略强制；`tests/test_answer_cache.py` 的全量本地工具 `< 4500` 断言**原样保留**，F4 的 4497 / 余量 3 仍然成立 | 全量断言是受保护断言，不得上调也不得改它测什么；S4 不新增工具。等哪一步真要加第 6 个本地工具时，"改成测最坏暴露集"必须作为那一步的显式评审决定 |
| 回滚 | 环境变量 `WATER_AGENT_TOOL_EXPOSURE=intent|full`，缺省 `intent`；`full` 等同 S3 行为（全量暴露、现行指引），其他值装配期响亮失败 | 真实模型的工具选择准确率离线测不到（见 §1 第 10 条），需要一个不改代码的退路 |

实测数字（schema 字符数取自 `data/eval/tool_assembly_main.json`，指引行长按现行格式实测）：

| 组 | 工具 | schema | 现行指引行 |
|---|---|---|---|
| core | retrieve_clauses + cite_clauses + calc_velocity | 590 + 576 + 953 = 2119 | 140 + 162 + 140 |
| materials | material_stats | 1412 | 171 |
| drawing | read_drawing | 956 | 220 |
| mcp:<id>（fake，两个工具） | echo_query + lookup_project | 约 1181 | — |

core + materials + drawing = **4487**（余量 13）；再加任何一台 MCP 都超 4500，所以预算裁决必须按整组、按优先级进行。

## 1. 目标与边界

**做**（编号对应规格 §7 的 P7-S4-Axx）：

1. **评测集先行（A01）**：在改任何暴露逻辑之前，先建 `tool_exposure_p0` 并在**当前代码**（全量暴露）上跑一遍，把"改之前"的指标写进决策记录：
   - 数据：`data/eval/tool_exposure_p0.jsonl`，30–40 条合成用例；harness：`src/water_agent/harness/tool_exposure_eval.py`；
   - **执行方式**：每条用例走生产 `AgenticSession`（真实 `assemble_tools`、提示装配、护栏、checkpoint），只把模型换成一个**记录型脚本模型**——每次被调用时记下本次 `tools` 参数里的工具名、schema 总长与系统提示里的工具指引段，然后按脚本作答。脚本必须恰好用完（多调抛 `ScriptExhausted`、少调报剩余步），与 `agentic_p0` 同一纪律；可以复用 `harness/doubles.py::ScriptedChatModel`，在它旁边加记录能力，不要另写一套脚本语义；
   - **部署面按用例给出**：`deployment.drawing`（`true` 时用 `drawing/synth.py` 在临时目录生成 `P-01` 并作为 `drawing_root`）、`deployment.mcp`（`true` 时用 `tests/fixtures/fake_mcp_server.py --mode ok` 写一份临时配置，经 `WATER_AGENT_MCP_CONFIG` 生效，环境变量在用例结束后恢复），沿用 `tool_assembly_snapshot` 的做法；
   - **用例格式**（每轮）：`query`、`script`、`must_expose`（本轮必须开放的工具）、`must_not_expose`（本轮明显无关、不应开放的工具），可选 `expect_unexposed_call`；
   - **覆盖类别**（每类都要有"不含明显触发词"的改写变体，用来测漏判）：规范问答、计算（含只给数字与单位不说"流速"的问法）、材料统计、读图（含只给图号的问法）、读图 + 材料统计组合、图纸未部署时问图纸、MCP 已部署、多轮跟随（上一轮用过材料统计，追问"那 PPR 的呢"）、寒暄与能力询问、预算压力（全部组同时命中 + MCP）；
   - **脚本里的最终回答一律用非规范性文本**（例如"收到。"），避免引用校验回流改变调用次数——本集测的是"模型看到了什么"，不是作答质量；
   - **数据集在第一个 commit 定稿**：之后的 commit 不得修改 `tool_exposure_p0.jsonl`（完成时 `git diff <第一个 commit>..HEAD -- data/eval/tool_exposure_p0.jsonl` 必须为空，贴进决策记录）。规则写完仍有用例过不了，按停止条件处理，**不得改用例去迁就规则**。
2. **硬指标与观测指标（A01、A09）**：
   - 硬指标（任何一项不达标 harness 非零退出）：`needed_tool_exposed_rate == 1.0`、`needed_tool_in_guide_rate == 1.0`、`forbidden_tool_exposed_count == 0`、`schema_budget_violation_count == 0`（单次调用发出的 schema > 4500）、`guide_truncated_count == 0`、`unexposed_call_executed_rate == 1.0`（脚本调用了未开放工具的用例：工具照常执行、trace 有 `unexposed_call` 标记）；
   - 观测指标（只报告、进基线对比）：每轮开放工具数与 schema 字符数的均值 / 最大值、`overexposure_rate`（开放了既不在 `must_expose` 也不在 core 里的工具的轮次占比）；
   - 速率在分母为 0 时记 `None`，不记 `0.0`（同 `UsageRecord.usage_known` 纪律）；
   - harness 支持 `--strategy intent|full`（经 `WATER_AGENT_TOOL_EXPOSURE` 生效）；第一个 commit 时只有现行行为，报告里如实写 `strategy: "baseline-s3"`。
3. **暴露契约（A02）**：新模块 `src/water_agent/plugins/exposure.py`，**纯函数、确定性**（不 import 模型、HTTP 客户端、`random`、`time`；有 AST 断言）：

   ```python
   ExposureMode = Literal["core", "intent", "ambient"]

   @dataclass(frozen=True, slots=True, kw_only=True)
   class ExposureRule:
       mode: ExposureMode
       keywords: tuple[str, ...] = ()      # 子串匹配，构造期规范化（NFKC + ASCII 小写）
       patterns: tuple[str, ...] = ()      # 正则，构造期编译，非法即 ValueError

   @dataclass(frozen=True, slots=True)
   class ToolGroup:
       group_id: str                        # 内置组 = plugin_id；MCP = "mcp:<server_id>"
       tools: tuple[str, ...]               # 注册顺序
       rule: ExposureRule

   @dataclass(frozen=True, slots=True)
   class ExposureDecision:
       strategy: Literal["intent", "full"]
       exposed: tuple[str, ...]             # 注册顺序
       reasons: tuple[tuple[str, str], ...] # (group_id, "core" | "intent" | "sticky" | "ambient" | "full")
       dropped: tuple[tuple[str, str], ...] # (group_id, "schema_budget" | "guide_budget")
       schema_chars: int
       guide_chars: int

   class ToolExposurePolicy:
       def decide(self, query: str, *, previous_turn_tools: Collection[str]) -> ExposureDecision: ...
   ```

   - 分组来源：`PluginContribution` 新增 `exposure: ExposureRule`，缺省 `ambient`（将来的插件不声明也能用，但预算不够时最先让位）；内置插件在 `plugins/builtin.py` 里声明：`clauses`、`hydraulics` 为 `core`，`materials`、`drawing` 为 `intent`；`McpContribution` 按 `servers` 逐台给出 `mcp:<server_id>` 组（`ambient`）。`skipped` 的插件与没有工具的 server 不成组；
   - `ToolAssembly` 新增 `exposure: ToolExposurePolicy`，由 `assemble_tools` 按插件顺序构造，持有每个工具的 schema 字符数（与 `registry.list_tools()` 同一序列化口径）与指引行长；
   - 关键词表放在 `builtin.py`，每组一个常量，注释写明它对应哪类问法；关键词与正则**只能**在第一个 commit 定稿数据集之后编写；
   - **`full` 策略**：`decide` 返回全部工具、`reasons` 全为 `"full"`、不做预算裁决（与 S3 行为一致，包括 MCP 时 schema 超 4500）。
4. **优先级与预算（A03）**：
   - 入选顺序：core → 本轮 query 命中的 intent 组 → 上一轮实际调用过工具的组（sticky）→ ambient；同一类内按插件 / server 顺序；
   - 两项预算同时约束：本轮开放工具的 schema 总长 ≤ `EXPOSED_SCHEMA_BUDGET = 4500`；按意图格式渲染的工具指引（含"本轮未开放"行与段标题）≤ `SEGMENT_BUDGETS["tool_guide"]`（800，不改）；
   - 超预算时按入选顺序的**逆序整组丢弃**，记进 `dropped`；core 永不丢弃——core 自身就超任一预算时 `assemble_tools` 抛 `ValueError`（编程错误，不降级）；
   - 预算数字只引用现有常量，不新增可调参数，不上调任何上限。
5. **sticky（A04）**：`previous_turn_tools` = 上一轮（本 thread 里最后一个真实用户问题之前的那一轮）实际产生过 `ToolMessage` 的工具名；只回看一轮。轮次切分复用 `prompting/history.py` 的口径（运行时上下文前言不算用户问题），不另写一套。
6. **接线（A05）**：
   - `build_agentic_app(..., exposure: ToolExposurePolicy | None = None)`；`None` 表示全量（直接调用 `build_agentic_app` 的既有测试行为不变）。生产两条路径（`AgenticSession`、runner real-agentic）都传 `assembly.exposure`（新不变量 P7-I08）；
   - `agent_loop` 入口：state 里本轮还没有暴露决定时算一次，写进新的 state 键 `tool_exposure`（`exposed` 工具名列表 + `dropped` / `reasons` 的可序列化形式），并在 `PER_TURN_RESET` 里归零，引用校验回流再进 `agent_loop` 时复用同一个决定；
   - 模型侧：`_BudgetChatModel` 在每次调用前把 `tools` 参数过滤成本轮开放的工具（流式与非流式两条路径都要）；`create_react_agent` 的 `tools`（ToolNode）仍是全部已注册工具；
   - 提示侧：`PromptAssembler` 从 state 的 `tool_exposure` 渲染按意图格式的指引；state 里没有它时走现行格式（回退逐字不变，`tests/test_answer_cache.py` 的 `< 1950` 因此不变）。指引行格式由一个函数产生，策略算 `guide_chars` 与 assembler 渲染共用它；
   - trace：每轮一条 `node="agent", event="tool_exposure"`，内容为 `strategy`、开放工具名、`reasons`、`dropped`、两个字符数；**不写 query 原文**。
7. **未开放工具被调用（A06）**：照常执行（经 `ToolRegistry.invoke`，护栏与主体授权不变）；trace 在该次工具事件上标 `unexposed_call: true`（或单独一条事件，二选一，写进决策记录）。**不得**为此改 `tools/**` 或 `ToolRegistry`。
8. **回滚开关（A07）**：`WATER_AGENT_TOOL_EXPOSURE` 在 `plugins/exposure.py` 里解析一次（`intent` / `full`，缺省 `intent`，其他值 `ValueError`），会话与 runner 共用；把它加进 `tool_assembly_snapshot.PINNED_ENV_KEYS`。`full` 下：模型收到全部工具，指引是现行格式，和 S3 逐字相同。
9. **装配快照（A10、A11）**：
   - surface 新增 `exposure`：对 harness 里写死的 4 条探针问题（规范问答 / 计算 / 材料统计 / 读图 + 材料统计）各记一次 `decide(...)` 的 `exposed`、`reasons`、`dropped`、`schema_chars`、`guide_chars` 与按意图指引的 sha256 / 字符数；
   - 递进 `TOOL_POLICY_VERSION`（`inventory.py`）与 `PROMPT_TEMPLATE_VERSION`（`assembler.py`；`tests/test_upload_retrieval.py` 锁着它的字面值，同步改）；
   - `--write --reason` 重写基线，`--diff` 原文贴进决策记录。**只允许**出现：`/versions/TOOL_POLICY_VERSION`、`/versions/PROMPT_TEMPLATE_VERSION`、各 profile 新增的 `surface/exposure`、`lifecycle/app/app_kwargs`（多了 `exposure`）；其余片段由范围块 `frozen_json` 冻结；
   - `tests/test_p7_known_figures.py`：`test_tool_guide_already_truncates_tools` 改写为 `test_exposed_tools_always_fit_the_tool_guide`（读基线 `surface/exposure`：每条探针的开放工具都出现在指引里、指引未截断、schema ≤ 4500；`session_mcp_fake` 的"读图 + 材料"探针里 MCP 组以 `schema_budget` 被丢弃），并保留一条断言：回退格式的 `surface/tool_guide` 仍截断（那是 `full` 模式的现状）。规格里 **P7-S0-A13** 改为 `untested`，在"未覆盖项"注明由 P7-S4-A11 取代。`test_local_tool_schema_headroom_is_three_characters` **不动**（F4 的全量数字仍成立）。
10. **门禁（A09、A12）**：`scripts/ci_gate.sh` 接入 `tool_exposure_p0`（intent 策略）+ `scripts/compare_tool_exposure_p0.py --head ... --base data/eval/tool_exposure_p0_main.json`。比较器纪律与其他套件相同：硬指标不降、基线用例在新报告里缺失或被跳过即违规、用例级回退即违规、拒绝人工编写的报告。`agentic_p0` 21/21 不变。

**不做**：

- 不新增、删除或改写任何工具的描述与 schema（`tools/**` 在 forbid 里）；不改 `ToolRegistry`；
- 不调模型做路由，不做 embedding / 语义相似度路由；
- 不给 MCP server 加关键词配置、不改 `mcp/**`（S6 一并设计）；
- 不改 Web（`webapp/**`）、CLI（`cli.py`）：开关只走环境变量；
- 不改 `FORMULA_SPECS` 与其他静态段（`calc_velocity` 在 core 里，静态段提到它不会失真）；
- **不声称真实模型的工具选择准确率**：`tool_exposure_p0` 测的是"按意图该开的工具确实开了、确实写进了指引、没超预算"，不测模型拿到工具后选得对不对。后者要真实模型，规格里记为 P7-S4-A13 `untested`（无 Key 即 blocked）。

## 2. 铁律与治理落点（不得违反）

- **铁律①**：计算工具每轮必开；暴露策略本身不做任何数值运算（只数字符）。
- **铁律②**：检索与引用同属 core，永远一起开放（`clauses` 插件不可拆，规格 §3.3）。
- **铁律③评测先行**：数据集与 harness 是第一个 commit，且在**未改动的**暴露代码上跑出"改之前"的报告；之后数据集冻结。
- **暴露 ≠ 授权**：本步不改任何能力、主体、护栏规则；`GRANTABLE_CAPABILITIES` 不变。
- **失败要响亮**：开关值非法、core 超预算、关键词正则非法，都在装配期抛错，不得静默回退成全量。
- **不上调任何预算**：4500、800、1950 一律不动。

## 3. 必须复用的现有组件（不得另写一份）

| 需要 | 用这个 | 位置 |
|---|---|---|
| 装配产物与插件贡献 | `assemble_tools` / `ToolAssembly` / `PluginContribution` / `McpContribution.servers` | `plugins/` |
| 工具 schema 序列化口径 | `ToolRegistry.list_tools()` / `ToolSpec.to_openai_tool()` | `tools/registry.py`、`tools/base.py` |
| 指引段预算与截断标记 | `SEGMENT_BUDGETS["tool_guide"]` / `_truncate_to_budget` | `prompting/assembler.py` |
| 轮次切分 | 运行时上下文前言识别与历史窗口的轮次口径 | `prompting/history.py` |
| 脚本模型 | `ScriptedChatModel` / `ScriptExhausted` | `harness/doubles.py` |
| 合成图纸 / fake MCP | `drawing/synth.py::build_synthetic_drawing`、`tests/fixtures/fake_mcp_server.py` | 同 `tool_assembly_snapshot` 的用法 |
| 快照与守卫 | `tool_assembly_snapshot --diff / --write --reason`、`check_step_scope.py` | `harness/`、`scripts/` |
| 比较器写法 | `scripts/compare_drawing_gen_p0.py`（结构与违规口径照抄） | `scripts/` |

## 4. 实施步骤（一步一个 commit；除 Step 5 外，每个 commit 结束时快照 `--check` 必须一致）

### Step 1：评测集 + harness，在现行代码上跑出"改之前"（A01）

- 新增 `tool_exposure_p0.jsonl`、`harness/tool_exposure_eval.py`、`tests/test_tool_exposure_eval.py`（harness 自身的单测：脚本恰好用完、指标分母为 0 记 `None`、缺 `must_expose` 的用例是加载问题而不是通过）；
- 跑 `python -m water_agent.harness.tool_exposure_eval data/eval/tool_exposure_p0.jsonl --report-path data/eval/reports/tool_exposure_p0_before.json`，预期**非零退出**：MCP 用例 schema 超 4500（F4）、读图与 MCP 工具不在指引里（F5）、无关工具被开放。把各指标数字记下，Step 6 写进决策记录；
- 本 commit 不接门禁、不写基线。

### Step 2：暴露契约与策略（A02、A03、A04、A07 的解析部分）

- `plugins/exposure.py`、`PluginContribution.exposure`、内置组声明、MCP 逐台分组、`ToolAssembly.exposure`；
- 新文件 `tests/test_tool_exposure.py`：确定性（同输入同输出、不受字典顺序影响）、优先级与两项预算、core 永不丢弃、core 超预算抛错、sticky 只回看一轮、`full` 策略等同全量、开关值非法抛错、AST 断言（不 import 模型 / HTTP / `random` / `time`）；
- 此时还没接线，快照 `--check` 必须一致。

### Step 3：接线（A05、A06、A08）

- `build_agentic_app(exposure=)`、`agent_loop` 决定一次并入 state、`PER_TURN_RESET`、`_BudgetChatModel` 过滤 `tools`、`PromptAssembler` 按 state 渲染、trace 事件、未开放调用标记；
- `AgenticSession` 与 runner real-agentic 都传 `assembly.exposure`；`tests/test_plugin_invariants.py` 加 P7-I08（两处生产 `build_agentic_app` 调用都带 `exposure=` 关键字，AST 判定）；
- 集成测试（放 `tests/test_tool_exposure.py`）：生产会话 + 记录型模型，断言模型第一次调用拿到的 `tools` 只含开放工具、ToolNode 能执行未开放工具且 trace 有标记、回流复用同一决定、第二轮按新问题重新决定；
- 快照此时会因 `app_kwargs` 多出 `exposure` 而变化——**不在本 commit 写基线**，留到 Step 5 一次性有意重写；本 commit 结束时用 `--diff` 确认变化只有 `lifecycle/app/app_kwargs`。

### Step 4：跑通评测集，接入门禁（A09）

- `--strategy intent` 下 `tool_exposure_p0` 必须 exit 0；`--strategy full` 下与 Step 1 的"改之前"报告硬指标一致（证明回滚开关确实回到原行为）；
- `scripts/compare_tool_exposure_p0.py` + 入仓基线 `data/eval/tool_exposure_p0_main.json`（intent 策略的报告）+ `ci_gate.sh` 接入；
- 关键词规则若要改，只能改 `builtin.py`，不得改数据集。

### Step 5：快照有意重写（A10、A11）

- 快照 surface 加 `exposure` 探针；递进两个版本号；`--write --reason`；`--diff` 原文留给决策记录；
- 按 §1 第 9 条改 `tests/test_p7_known_figures.py` 与 `tests/test_upload_retrieval.py` 的版本字面值。

### Step 6：收尾

- 规格 §2 更新 F4（补一句：每轮发送的 schema 由暴露策略约束，全量数字不变）、F5（按意图模式已修，`full` 模式仍截断）；§3 新增 3.6 暴露契约（照 §1 第 3–7 条）；§4 新增 P7-I08；§5 S4 行与 §7 S4 验收行翻成 delivered，验证项写成真实存在的测试节点；
- 写 `docs/design/p7-step-4-decision.md`（含 Step 1 与 Step 4 两份报告的指标对照、数据集冻结的 `git diff` 证据、Step 5 的 `--diff` 原文）；
- AGENTS.md 补一条 P7-S4，并把"工具 schema 实测 4497/4500……（加工具前先做按意图最小暴露，P7-S4）"那句改成现状。

## 5. 范围块

<!-- step-scope -->
```json
{
  "step": "P7-S4",
  "allow": [
    "src/water_agent/plugins/exposure.py",
    "src/water_agent/plugins/base.py",
    "src/water_agent/plugins/builtin.py",
    "src/water_agent/plugins/assembly.py",
    "src/water_agent/plugins/mcp_gateway.py",
    "src/water_agent/plugins/__init__.py",
    "src/water_agent/graphs/agent_rag.py",
    "src/water_agent/prompting/assembler.py",
    "src/water_agent/runtime/agentic.py",
    "src/water_agent/runtime/governance/inventory.py",
    "src/water_agent/harness/runner.py",
    "src/water_agent/harness/doubles.py",
    "src/water_agent/harness/tool_exposure_eval.py",
    "src/water_agent/harness/tool_assembly_snapshot.py",
    "data/eval/tool_exposure_p0.jsonl",
    "data/eval/tool_exposure_p0_main.json",
    "data/eval/tool_assembly_main.json",
    "scripts/compare_tool_exposure_p0.py",
    "scripts/ci_gate.sh",
    "tests/test_tool_exposure.py",
    "tests/test_tool_exposure_eval.py",
    "tests/test_tool_plugins.py",
    "tests/test_plugin_invariants.py",
    "tests/test_p7_known_figures.py",
    "tests/test_tool_assembly_snapshot.py",
    "tests/test_agent_rag.py",
    "tests/test_upload_retrieval.py",
    "docs/design/p7-step-4-decision.md",
    "docs/design/16-p7-plugin-mcp.md",
    "docs/execution-prompts/P7-S4-intent-tool-exposure.md",
    "AGENTS.md"
  ],
  "forbid": [
    "src/water_agent/tools/**",
    "src/water_agent/mcp/**",
    "src/water_agent/webapp/**",
    "src/water_agent/cli.py",
    "src/water_agent/runtime/governance/rules.py",
    "src/water_agent/runtime/governance/pipeline.py",
    "src/water_agent/runtime/governance/contracts.py",
    "src/water_agent/harness/spec_trace.py",
    "scripts/check_step_scope.py",
    ".github/**",
    "web/**",
    "tests/conftest.py",
    "tests/test_p7_traceability.py",
    "tests/test_check_step_scope.py",
    "tests/test_import_guard.py",
    "tests/test_answer_cache.py",
    "tests/test_governance_contracts.py",
    "tests/test_tool_policy.py",
    "tests/test_mcp_*.py",
    "data/mcp_servers.json",
    "data/eval/baseline.json",
    "data/eval/agentic_p0.jsonl",
    "data/eval/drawing_p0_main.json",
    "data/eval/drawing_gen_p0_main.json",
    "data/eval/memory_p0_main.json",
    "data/eval/multi_agent_p0_main.json"
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
      "node": "tests/test_p7_known_figures.py::test_local_tool_schema_headroom_is_three_characters",
      "contains": "assert surface[\"schema_chars_total\"] == 4497"
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
        "/versions/MCP_CONFIG_VERSION",
        "/profiles/*/surface/registration_order",
        "/profiles/*/surface/model_tool_order",
        "/profiles/*/surface/tools",
        "/profiles/*/surface/schema_chars_total",
        "/profiles/*/surface/tool_guide",
        "/profiles/*/surface/system_prompt",
        "/profiles/*/surface/principal",
        "/profiles/*/surface/registry_principal",
        "/profiles/*/lifecycle/mcp",
        "/profiles/*/lifecycle/exception",
        "/profiles/*/lifecycle/app/probes",
        "/profiles/*/lifecycle/app/chat_model_principal",
        "/profiles/*/lifecycle/app/cache_service",
        "/profiles/*/lifecycle/app/max_tool_turns",
        "/profiles/*/lifecycle/app/tools_bound_to_prompt_registry"
      ]
    }
  ]
}
```

说明：其他套件的入仓基线逐个列进 forbid（不用 `*_p0_main.json` 通配，否则会命中本步新建的 `tool_exposure_p0_main.json`——forbid 优先于 allow）。

## 6. 受保护断言

- 本地工具 schema `< 4500`、系统提示 `< 1950`、治理覆盖率 `{7, 7, 5, 5}`：本步不增减工具、不改回退格式的提示词。
- F4 的 `4497`：不得靠删减工具描述"制造余量"。
- S1 的两条确认门断言。

## 7. 停止条件

满足任一条就停下，写清现象后等确认：

- 快照出现 §1 第 9 条之外的差异（尤其是任何 `frozen_json` 片段）；
- 关键词规则写完仍有用例不过，且只能靠改数据集或放宽硬指标解决；
- core + 任一 intent 组的组合在 4500 / 800 下放不下（意味着要动工具描述或预算，都在本步之外）；
- 需要改 `tools/**`、`ToolRegistry`、护栏或 `mcp/**` 才能完成接线；
- `agentic_p0` 或其他门禁出现与本步无关的红灯。

## 8. 验证命令（完成前全部跑过，贴出输出）

```bash
export VENV=/Users/yaerken/Desktop/给排水agent/_venv PYTHONPATH=src
git fetch origin main
$VENV/bin/pytest -q tests/test_tool_exposure.py tests/test_tool_exposure_eval.py tests/test_tool_plugins.py tests/test_plugin_invariants.py tests/test_p7_known_figures.py tests/test_tool_assembly_snapshot.py tests/test_agent_rag.py tests/test_answer_cache.py tests/test_upload_retrieval.py tests/test_p7_traceability.py
$VENV/bin/python -m water_agent.harness.tool_exposure_eval data/eval/tool_exposure_p0.jsonl --report-path data/eval/reports/tool_exposure_p0_head.json
WATER_AGENT_TOOL_EXPOSURE=full $VENV/bin/python -m water_agent.harness.tool_exposure_eval data/eval/tool_exposure_p0.jsonl --report-path data/eval/reports/tool_exposure_p0_full.json
$VENV/bin/python scripts/compare_tool_exposure_p0.py --head data/eval/reports/tool_exposure_p0_head.json --base data/eval/tool_exposure_p0_main.json
$VENV/bin/python -m water_agent.harness.tool_assembly_snapshot --check
$VENV/bin/python scripts/check_step_scope.py docs/execution-prompts/P7-S4-intent-tool-exposure.md --base origin/main
COMPOSE_PROJECT_NAME=p7s4 VENV=$VENV bash scripts/ci_gate.sh
```

`full` 那一条预期非零退出（它就是"改之前"的行为），贴出输出即可。

## 9. 完成定义

- 满足规格 §8 第 5 条的全部项；
- PR 上的 `p7-scope` 与 `p5-ci` 通过；
- 决策记录贴出：Step 1（改之前）与 Step 4（intent / full）三份报告的硬指标对照、数据集冻结证据、Step 5 的 `--diff` 原文。

## 10. 汇报格式

- 改了哪些文件；
- 验证命令与实际输出（失败照贴）；
- 范围守卫的输出；
- 快照差异；
- 未验证项（含 P7-S4-A13 真实模型工具选择）；
- 残余风险。

## 11. 常见坑（本仓库实测过）

- `full` 模式与"state 里没有暴露决定"的回退必须逐字等于现行指引：`frozen_json` 冻结了 `/profiles/*/surface/tool_guide` 与 `system_prompt`（快照用 `assemble_prompt({})` 采集），`tests/test_answer_cache.py` 的 `< 1950` 也测这条路径。
- schema 字符数的口径是 `json.dumps(registry.list_tools(), ensure_ascii=False)`（快照与 `test_answer_cache` 同一口径）；单个工具的字符数相加比整体少列表分隔符，预算判定用整体口径。
- `_BudgetChatModel` 有 `_generate` 与 `_stream` 两条路径，过滤 `tools` 两处都要做；流式路径不测就会漏（Web 走流式）。
- 运行时 MCP 绑定登记只增不删：测试里用独立的 server id，或用 `clear_mcp_tool_bindings` 清理。
- 本机如果设了 `WATER_AGENT_MCP_OVERLAY` 或 `WATER_AGENT_TOOL_EXPOSURE`，跑测试和快照前先 `unset`。
- worktree 里不设 `PYTHONPATH=src` 时，pytest 会被 H0 拦下（exit 4）。
- 本机另一个 compose 栈在跑时，门禁的 memory_p0 备份用例会误报，用 `COMPOSE_PROJECT_NAME=<别名>` 隔离。
- 只含执行 Prompt 的文档 PR，分支名不能以 `YEK/p7-s<n>-` 开头（`p7-scope --auto` 会去 base 上找这一步的 Prompt）。
