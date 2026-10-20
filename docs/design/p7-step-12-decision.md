# P7-S12 决策记录：联网搜索（`web_search` 工具 / provider 适配器 / 查询出口守卫 / 结果流水线 / 逐条消息勾选）

> 日期：2026-10-20。分支 `YEK/p7-s12-web-search`，基于 main `47db0dd`（S0–S11、S15 已合入）+ 本步执行 Prompt（`219150b`，见 §0.1 第 1 条）。
> 执行 Prompt `docs/execution-prompts/P7-S12-web-search.md`，规格 `docs/design/17-p7-external-capabilities.md`（§5、§8 T15、§10 I09–I11、§11 的 P7-S12）与
> `17-p7-external-capabilities-harness.md` §4，16 号规格 §7 登记的 `P7-S12-A01…A14`，威胁模型 `p2.5-threat-model.md` T15 的 S12 增补。

**一句话结论**：模型可以经 `web_search(query, count)` 查询**运维配置的单一搜索服务**，得到有界、已标为不可信的标题 / 摘要 / 网址列表。部署开关（运维）与**逐条消息勾选**（用户）两道门缺一不可，缺省两道都关；
查询先过出口守卫，结果过 S11 的信封与归一化，回答受 R1–R5 约束，出口只经 `governed_get`。**只对本地假搜索服务验证**——`searxng` / `brave` 适配器的线上格式取自官方文档与源码原文，
标 `unverified_against_real_service`，**不得声称接通了任何真实搜索服务**；查询守卫是确定性正则 / 子串，**只能拦住标注的子串与正则可识别的泄露，不能说"能防住数据外泄"**。

## 0. 偏离与修订

### 0.1 执行 Prompt §0 的修订

1. **执行 Prompt 开工时不在 main 上**。Prompt 只在本地分支 `YEK/p7-docs-s12-prompt`（`219150b`，未推送）。它是 main `47db0dd` 的直接后代，本分支以它为起点（fast-forward），
   并把 worktree 默认的 `claude/…` 分支改名为 `YEK/p7-s12-web-search`。按 16 号规格 §8 的实施协议，Prompt 必须先合入 main，范围守卫才从 merge-base 读得到范围块——
   **合入实现之前要先单独合入这份 Prompt（含 §0.1 第 2 条的补遗）**。
2. **范围块补遗（需要单独的文档 PR，先合入 main）**：
   - `tests/test_dxf_upload.py::test_tool_schema_budget_and_governance_counts_are_untouched` 锁着 `provider_call_sites: 8` / `tools: 6`。站点 8 → 9、工具 6 → 7 必然改它，
     但它不在 `allow` 里（S8 D13 同一类疏漏）。范围守卫当前对它报 1 处越界——这是唯一的越界。
   - `tests/test_check_step_scope.py::test_repo_scope_blocks_are_usable` 要求**所有** P7 执行 Prompt 的 `protect` 断言在当前树上都成立；S15 与 S16 的 Prompt 都
     `protect` 了 `tests/test_vision_review.py` 的 `assert len(PROVIDER_CALL_SITES) == 8`，本步把它合法地改成 9（本 Prompt 明确要求）之后，那两份文档的 `protect` 失效，
     该测试在全量里变红（S9 / S8 踩过同类问题，当时在对应文档 PR 里删掉旧的 protect）。**补遗 PR 要把 S15 / S16 Prompt 里这条 protect 删掉**（S15 已交付、S16 未开工，
     两者都不再需要锁 8）。这两处都不在本分支能改的范围里。
3. **工具定义位置（Prompt 留给评审二选一，采纳推荐）**：`ToolSpec` 定义在 `web_search/tool.py`；`inventory.scan_tool_spec_names` 的扫描目录扩为 `tools/` + `web_search/`
   （`TOOLSPEC_SCAN_DIRS`，`plugins/` 仍不在内），并加反证 `test_a_toolspec_in_web_search_without_a_capability_goes_red`。扫描只认字面 `name="…"`，所以工具名在
   `ToolSpec(...)` 里写成字面量（不经常量），否则扫描看不见。
4. **工具能力登记不能进 `TOOL_CAPABILITIES`**：`tests/test_tool_policy.py::test_every_inventoried_tool_capability_is_granted_locally`（不在 allow 里）断言清单里每个工具的能力都在
   默认能力集——而本步要求 `WEB_SEARCH` **不在**默认集。新增 `inventory.PLUGIN_GRANTED_TOOL_CAPABILITIES`（只有 `web_search`），`capability_for_tool` / `undeclared_tool_names` /
   `governance_coverage` 同时查两张表；未登记仍 fail closed。那条既有断言对"默认就该可用的工具"仍然成立，没有放宽它。
5. **暴露顺序与 Prompt 字面相反（Prompt 内部自相矛盾）**。Prompt §0 同时写了"request 组排在 core 之后、intent 之前入选"与"最紧组合（读图 + 材料 = 4497）放不下它是预期，
   由 `WEB_SEARCH_NOT_EXPOSED` 兜住"。若 request 在 intent 之前，web_search（594 字符）总能放进去，被挤掉的会是 `read_drawing`——让位告警成了死代码，而且用户明确要读图却读不了。
   实现为 **core → intent → request → sticky → ambient**，`request` 组**不参与 sticky**（勾选只对那一条消息有效）。锁定：
   `tests/test_web_search_exposure.py::test_request_group_yields_after_intent_under_the_tightest_budget`（同一用例在"没有 intent 竞争"时断言 web_search 放得下，从而区分顺序）。
6. **授予能力走插件贡献，不改 `assemble_tools`**。Prompt 写"`assemble_tools` 要把 request_enabled 交给策略、并在 web_search=True 时追加能力"。实现为：`WebSearchPlugin(requested=…)`
   只在部署就绪**且**本条消息勾选时贡献工具与 `grants={WEB_SEARCH}`（与 MCP 插件追加 `MCP_INVOKE` 同一机制，`assemble_tools` 原有的 GRANTABLE 校验照常生效）；没勾选就不注册工具——
   于是"勾选只对这一条消息有效"由**装配**保证，而不只是暴露策略。`assemble_tools` 一行未改；`request_enabled` 由外层图的 `agent_loop` 从 state 的 `request_groups` 交给 `decide`。
7. **基址不在装配期校验**。Prompt 写"取值非法 / provider id 拼错 → 装配期抛错"。开关取值与 provider id 照此（`WEB_SEARCH_CONFIG_INVALID`）；但基址的形状与目的地**只在请求期**由
   `governed_get`（`parse_server_url` + `open_exchange`）判定：那是唯一的目的地真源，禁止即零字节、返回 `WEB_SEARCH_UNAVAILABLE`。装配期再校验一遍是第二个真源，而且
   一个配错的搜索 URL 会让**所有**会话（包括没勾选联网的）装配失败。锁定：`tests/test_web_search_tool.py::test_base_url_shape_is_judged_at_request_time_not_here`。
8. **勾选了而部署没就绪**：入口（`run_routes.py`）在建 run 之前 422 `WEB_SEARCH_UNAVAILABLE`；会话层若仍被这样调用（CLI / 评测），不抛错——插件 skipped、本轮不联网，
   外层图告警 `WEB_SEARCH_NOT_EXPOSED`（不静默丢弃勾选）。工厂不声明 `supports_web_search` 时勾选 → 422 `WEB_SEARCH_UNSUPPORTED`（与 S15 的 `supports_model_choice` 同构）。
9. **`/api/system/capabilities` 没有加联网状态**：该路由在 `webapp/app.py`，不在 allow 里。前端改读新的只读接口 `GET /api/settings/web-search`（所有已改密用户可读，
   只给开关 / 就绪 / provider / 主机名 / 密钥是否已配置 / 缺项原因）。A11 的"能力端点计数"由这个接口承担。
10. **`plugins/__init__.py` 不在 allow 里**：`WebSearchContribution` / `WebSearchPlugin` 只从 `plugins.builtin` 导入，不进包级 `__all__`。

### 0.2 实测后追加的偏离与发现

- **D1 Step 0 实测**：`web_search` 参数模式 342 字符（含 pydantic 自动的 `title` 字段），整条工具定义（`bound_tool_definition` 口径）594 字符。组合（与 `tool_assembly_main.json`
  的 `session_drawing_arg` 同一口径）：core 2125 → core + web_search 2719；core + 读图 + web_search ≈ 3669；core + 材料 + web_search ≈ 4125；**读图 + 材料 + web_search ≈ 5081 > 4500**——
  最紧组合放不下，是预期，由让位告警兜住，4500 未上调。`open_exchange(` 的 AST 调用方仍是 4 项（`mcp/http_client.py`、`mcp/oauth.py`、`generation/chat_model.py`、
  `external/http_get.py`），`web_search/` 不直接调用它（有 AST 断言）。起点快照 `--check` 19 个 profile 一致。
- **D2 类 docstring 会进发给模型的 schema**：第一版 `WebSearchArgs` 带类 docstring，pydantic 把它放进 JSON schema 的 `description`，参数模式涨到 456 字符。改为注释后 342。
- **D3 适配器字段来源（Step 0 读原文）**：
  - **searxng**：`docs.searxng.org/dev/search_api.html`——端点 `GET /search`，参数 `q` 必填、`format` 可选 `json|csv|rss` 且 "Must be activated in `search:`"（未启用返回 403 Forbidden）；
    `searx/settings.yml` 的 `search.formats` 缺省只有 `- html`；`searx/webutils.py::get_json_response` 的顶层键为 `query` / `results` / `answers` / `corrections` / `infoboxes` /
    `suggestions` / `unresponsive_engines`（**没有** `number_of_results`，与常见描述不同）；条目由 `MainResult.as_dict()` 产生，字段含 `url` / `title` / `content` / `engine` /
    `engines` / `positions` / `score` / `category` / `template`…（`searx/result_types/_base.py`）。适配器只取 `results[].title / url / content`，`answers` / `infoboxes`（实例合成）不取。
  - **brave**：`api-dashboard.search.brave.com` 的 Web Search 文档——端点 `https://api.search.brave.com/res/v1/web/search`，查询在 `q`，令牌在请求头 `X-Subscription-Token`，
    `count` "max 20, default 20"、`offset` 最大 9；示例响应 `query.original` / `query.more_results_available`、`web.results[]` 的 `title` / `url` / `description`（`extra_snippets` 仅在请求时出现）。
    文档页**没有**给出 `type` / `age` / `page_age` 等字段的权威定义（指向另一个 API reference，本机读不到），所以只取官方示例里逐字出现的 `title` / `url` / `description`。
  - 两者都 **`unverified_against_real_service`**：本机无网络沙箱、无实例、无 Key。
- **D4 私有上下文在工具执行时还不在 checkpoint 里**（实测）：内层工具循环进行中读 checkpoint，只能看到上一次落盘的状态（本回合的检索结果还不在）。所以守卫的私有上下文来源是
  会话级的 `web_search/runtime.py::PrivateContext`：召回结果在 `aask` 入口登记；本回合 `retrieve_clauses` / `read_drawing` 的输出由 LangChain 回调（随 config 继承进内层 react 子图）
  在产生时捕获；回流 / 确认后恢复时再补读 checkpoint。工具输出是 JSON，中文可能被转义成 `\uXXXX`，比对前取 JSON 的全部字符串叶子。回调只在**勾选了且工具已装配**时挂上，
  缺省路径的 config 与此前逐字相同。用户本人的消息另存，作为豁免对象（与它重合不拒绝）。
- **D5 网络总预算**：工具的协作式 deadline 15 s；`governed_get` 的总预算设为 14 s（`NETWORK_BUDGET_SECONDS`，经 `RequestContext.deadline_seconds` 交给网关），慢的时候网关停止重试、
  返回稳定码，而不是撞上工具层 `TOOL_TIMEOUT`。单次交换超时取网关画像（单一来源）。
- **D6 评测发现的 harness 盲点（两处，均已修）**：
  1. `destination_zero_byte_rate` 的第一版反证没变红：拿掉目的地分类后，云元数据 / 私网地址本来就不是假服务，"假服务零请求"照样成立。加了**连接探针**
     `ConnectSpy`（记录每次 `socket.connect`、非回环地址一律拒绝），被禁止目的地的用例另断言**零连接尝试**。
  2. `off_identical_rate` 的第一版反证没变红：off 用例连 provider 都没配，拿掉开关后插件仍因"缺 provider"跳过。改为 off 用例除开关外**全部配齐**，证明单靠开关就关得住。
  另：反证补丁一开始只改了定义处的名字，`from x import f` 在导入方另存的绑定没被替换（`citation_integrity_violations` 的反证因此没咬到）——反证一律走 `_patch_everywhere`。
- **D7 冻结后改动过的用例（Step 1 之后，写明原因）**：数据集在 commit `1eefe3b` 冻结（79 条）；机制实现后改了 8 条，**都是用例自身的期望写错，不是为了让实现通过**：
  - `ws-q-too-long` / `ws-q-url-shaped` / `ws-q-injection` / `ws-q-zero-width`：这四类在到达查询守卫之前就被更早的一层拦下（参数模式 `maxLength=128` → `INVALID_ARGUMENTS`；
    工具参数护栏的域名白名单 / 注入 / 不可见字符 → `GUARDRAIL_BLOCKED`）。期望码改成实际拦下它的那一层；"假服务零请求"不变；`query_block_recall` 度量的是"泄露查询没有出站"，与哪一层拦下无关。
  - `ws-q-private-retrieved-overlap`：原用例用 `private_context.retrieved` 声明检索条文，但那不是生产里检索条文的来源。改为本轮**真的先调用 `retrieve_clauses`**，再拿条文原句当搜索词
    （生产回调捕获检索输出）。
  - `ws-r-all-dropped`：原用 `injection_snippets`（三条里有一条正常），全部丢弃不成立；假服务新增 `injection_only` 模式。
  - `ws-toggle-not-sticky`：第一轮真的发出了搜索，回环假服务需要 `allow_private`（原用例漏了，导致工具失败、终答网址不可追溯而回流）。
  - `ws-off-no-tool`：见 D6 第 2 条。
  - `ws-a-audit-whitelist`：期望字段里的 `actor_user_id` 改为 `principal_hash`——工具层拿不到 Web 用户 id，审计按主体哈希记（同样不可逆）。
  - `ws-web-capabilities-counts`：见 §0.1 第 9 条，改测 `GET /api/settings/web-search`。
  - `ws-web-field-true-reaches-factory`：补上部署就绪的环境（勾选 + 部署 off 是另一条用例 `ws-web-unsupported-factory-refuses` 之外的拒绝路径）。
  入仓基线是在这些修改之后生成的；之后再改任何一条都要重新固化并写明原因。
- **D8 S11 评测白名单已删除**：`external_content_p0` 的会话用例改走 `AgenticSession`（生产 `web_search` + 进程内假搜索服务 + 逐条消费同一组回复的离线模型），
  **报告与 S11 入仓基线逐字相同**（`cmp` 通过）；`tests/test_plugin_invariants.py` 的 `EVAL_GRAPH_CALLERS` 清空。没有任何用例还需要夹具工具。
- **D9 前端的取舍**（由前端实现时提出，已审）：`/api/settings/credentials` 多出的 `WATER_AGENT_WEB_SEARCH_API_KEY` 一行在"模型与密钥"页过滤掉，只在"联网搜索"子页填写（避免同一把
  密钥两个表单、且在那页写入后不刷新"联网"开关）；`styles.css` 里顺带让 320px 宽时模型选择器先收缩（与开关有无无关的既有溢出，开关多占约 66px 使其更明显）。
  后端契约以 `run_routes.py` / `settings_routes.py` 为准，前端测试 132/132（新增 19 条在 `web/app/tests/web_search.test.mjs`）。

## 1. 交付

| 面 | 内容 | 锁定 |
|---|---|---|
| 能力与开关 | `Capability.WEB_SEARCH`（不在默认集 / Web 成员集）；`GRANTABLE_CAPABILITIES = {MCP_INVOKE, WEB_SEARCH}`（P7-I06 修订）；`web_search/config.py`：开关非法 / provider 拼错 → `WEB_SEARCH_CONFIG_INVALID`，缺项 → skipped | `tests/test_web_search_tool.py`、`tests/test_plugin_invariants.py::test_invariants_hold[P7-I06]` |
| 适配器 | `web_search/adapters/{searxng,brave}.py`：纯函数，不 import 网络原语（SDD17-I10，AST）；不回退；无密钥 provider 不读密钥 | `tests/test_web_search_adapters.py` |
| 目的地 | 运维写死的基址，经 `governed_get`；`ALLOW_PRIVATE=1` 才放行回环 / 私网；禁止目的地零字节（含 DNS 重绑定）、3xx 不跟随、256 KiB、必须 JSON | `tests/test_web_search_transport.py` |
| 查询出口守卫 | `web_search/guard.py`：长度 / 单行 / 不可见与控制字符 / 网址形态 / PII / 凭据 / 注入 / 私有上下文回显（≥ 16，用户本人消息豁免）/ 每回合 3 次 | `tests/test_web_query_guard.py` |
| 结果流水线 | `web_search/results.py`：网址校验与规范化、去重、注入命中丢弃且不占名额、≤ count、S11 信封、`urls` 只列保留条目 | `tests/test_web_search_results.py` |
| 工具与插件 | `web_search/tool.py`（`ToolSpec`、`result_source=WEB_CONTENT`、15 s）；`plugins/builtin.py::WebSearchPlugin`（两道门都过才贡献工具与 `WEB_SEARCH`）；`BUILTIN_PLUGIN_ORDER` 在 `mcp_gateway` 与 `skills` 之间插入 `web_search`（P7-I04 修订） | `tests/test_web_search_exposure.py`、`tests/test_plugin_invariants.py` |
| 暴露 | `ExposureRule(mode="request")` + `decide(request_enabled=…)`；顺序 core → intent → request；不参与 sticky；`agent_loop` 在勾选了却没开放时告警 `WEB_SEARCH_NOT_EXPOSED`（warnings + trace） | `tests/test_web_search_exposure.py`、`tests/test_web_search_discipline.py` |
| 治理 | `ProviderKind` / `CallCategory.WEB_SEARCH`；画像 15 s / `max_attempts=2` / 每分钟 20；熔断键 `web_search:<host>`；站点 8 → 9；`PLUGIN_GRANTED_TOOL_CAPABILITIES`；扫描目录 `tools/` + `web_search/`；`GOVERNANCE_CONFIG_VERSION` → `p7-s12-2026-10-20`；`TOOL_POLICY_VERSION` → `p7-step12-2026-10-20` | `tests/test_governance_contracts.py` |
| 错误模板 | `web_search/errors.py`：7 个稳定码，`EXTERNAL_*` 一一映射，文本不含上游正文 / 网址 / 密钥 | `tests/test_web_search_errors.py` |
| 审计与 trace | `web_search.performed`（主体哈希、查询哈希 + 长度、provider、主机名、条数、结果码）；trace `web_search` 事件不记网址；密钥只在请求头 | `tests/test_web_search_audit.py` |
| Web | `MessageRequest.web_search: StrictBool = False`（不落库）；`supports_web_search` 标志；422 `WEB_SEARCH_UNSUPPORTED` / `WEB_SEARCH_UNAVAILABLE`；`GET /api/settings/web-search`（只读）；密钥槽位 `WATER_AGENT_WEB_SEARCH_API_KEY`（复用凭据接口）；前端"联网"开关（默认关、不持久）与"联网搜索"子页 | `tests/test_web_search_web.py`、`web/app/tests/web_search.test.mjs`、`web/app/tests/settings_nav.test.mjs` |
| 部署 | compose 透传四个变量，缺省为空 = off；密钥不经环境变量传 | `tests/test_web_deployment.py::test_web_search_is_off_by_default_and_its_key_never_travels_by_env` |
| 评测 | `web_search_p0`（79 条：会话 64 · Web 6 · unit 5 · runner 4；13 项硬指标）+ 进程内假搜索服务 + 比较器 + 入仓基线 + `ci_gate.sh`；每个硬指标一条反证 + 完备性断言 | `tests/test_web_search_eval.py` |

## 2. 实测

`web_search_p0`（两次运行报告逐字相同，入仓基线与 HEAD 逐字相同）：

```text
web_search_p0 mechanism=present passed=79/79
  exposure_correct_rate: 1.0
  off_identical_rate: 1.0
  destination_zero_byte_rate: 1.0
  hostile_neutralized_rate: 1.0
  instruction_bearing_leak_count: 0
  query_block_recall: 1.0
  query_false_block_rate: 0.0
  web_url_traceability_rate: 1.0
  citation_integrity_violations: 0
  taint_effect_rate: 1.0
  key_leak_count: 0
  error_leak_count: 0
  harness_error_count: 0
```

反证 12 条全部成立（拿掉保护 → 指标离开目标值；不拿掉时同一批用例在目标上）：逐条消息勾选、部署开关、目的地分类、信封归一化 + 注入丢弃、联网摘录不进系统提示、查询守卫"全放"、
查询守卫"全拒"（误拦率必须变红）、终答网址追溯、联网网址并入可引用集合、回合污点、密钥进查询串、错误模板回显上游正文；另有"每个硬指标都有反证"的完备性断言与 harness 出错计数。

`external_content_p0`（S11 套件，会话用例已改走 `AgenticSession`）：`passed=44/44`，报告与 S11 入仓基线 `cmp` 逐字相同。

### 2.1 测试与门禁

见本文末尾"验证记录"（全量 / ruff / mypy / 快照 / 前端 / ci_gate / 范围守卫的实际输出）。

## 3. 快照变化

`--diff`（`--write --reason` 之前）原文：

```text
/profiles/session_web_search_fake: "<absent>" -> {"description": "联网搜索部署开关 on + 假 searxng（.invalid 基址）+ 本条消息勾选联网：多出 web_search（request 组）、主体多 web_search 能力", "lifecyc...
/versions/TOOL_POLICY_VERSION: "p7-step9-2026-10-09" -> "p7-step12-2026-10-20"
```

只新增 `session_web_search_fake` 一个 profile（注册顺序末尾多 `web_search`、主体多 `web_search` 能力、暴露策略多一个 `request` 组与一条"勾选了"的探针：exposed 为 core + web_search，
schema 2719，指引 311 字符，未截断）；现有 19 个 profile 逐字不变；`PROMPT_TEMPLATE_VERSION` / `MCP_CONFIG_VERSION` 不变；新环境变量 6 个钉进 `PINNED_ENV_KEYS`。
随之更新的计数断言：`tests/test_p7_known_figures.py::test_exposed_tools_always_fit_the_tool_guide`（探针数 56 → 61）、`tests/test_tool_assembly_snapshot.py` 两处"其余 profile"集合排除新 profile。

## 4. 不做

任意网址抓取（`web_fetch` / 浏览器 / 截图，D6）；LLM 合成答案类搜索；搜索结果缓存与语义缓存（D7）；关键词 intent 暴露联网；R-web-num 的计算入参来源校验；面板写搜索服务地址 / provider / 开关；
`NETWORK_FETCH` 授予任何主体；改 `external/**`、`mcp/**`、`prompting/**`、`tools/**`、`gateway.py` / `pipeline.py` / `rules.py` / `cache.py`；上调任何预算数字。

## 5. 残余风险与未验证项

- **没有任何真实搜索服务被验证**：searxng / brave 只对本地假服务验证（线上格式取自官方原文，`unverified_against_real_service`）；真实服务的响应形态、限流、可用性未知（P7-S12-A14，blocked）。
- **查询守卫是确定性正则 / 子串**：改写、拆分、同义复述式的泄露拦不住（`tests/test_web_query_guard.py::test_known_limit_paraphrased_leaks_pass` 断言它**被放行**）。
  只能说"拦住标注的子串与正则可识别的泄露"，**不得**说"能防住数据外泄"。
- **用户问题本身会发往第三方搜索服务**：数据出境与隐私合规由运维评估；运维写错基址即数据外发；`ALLOW_PRIVATE=1` 打开后内网可达（云元数据仍拒）。
- **归一化与网址抽取都是确定性正则**（S11 的局限照旧）：不得声称能拦住任意注入。
- **R-web-num**：模型把网络数字抄进计算入参没有来源校验。
- **真实模型行为未验证**：是否滥用搜索、是否遵循披露与"网络信息不是规范依据"、会不会被冒充条文的网页说服（需 Key，blocked）。
- **浏览器手动验收未做**（勾选 → 发送 → 切会话 → 设置页保存密钥）：只有组件级测试与 SSR 渲染核对。
- **熔断 / 限流是进程内的**：多 worker 各一份（与既有网关相同）。

## 验证记录

（实施完成后回填。）
