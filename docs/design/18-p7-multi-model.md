# P7 扩展规格：多模型选择与思考型模型（SDD，V0.1 草案）

> 本文档是 P7-S15（多模型选择）与 P7-S16（思考型模型）的**规格草案**，评审通过并合入 main 之前**不开工**。它继承并**不得放宽**：
> `docs/design/16-p7-plugin-mcp.md`（插件契约 / 不变量 / 实施协议，尤其 §3.11 端点与兼容性门槛）、
> `docs/design/p5-step-46-decision.md`、`docs/design/p7-step-10-decision.md`（S10：端点可配置，**多端点路由 / 按用户选模型当时列为"不做"**）、
> `docs/design/p2.5-threat-model.md`（T14：不得声称换模型后质量不变）。评测 harness 的设计见同目录 `18-p7-multi-model-harness.md`。
>
> 三条铁律在本规格的实例化：
> ① **数值程序化**：换模型不改变"计算只走程序化工具"；思考模式下 `temperature` 等采样参数在 provider 侧失效（§2 F3），所以**数值可靠性只能来自工具，不能来自采样设置**。
> ② **引用可追溯**：`cite_clauses` + 校验器回流对任何模型一视同仁；换模型不能绕过它。
> ③ **评测先行**：S15、S16 各自先冻结评测集（harness 文档）再写机制；**没有 Key 前，真实模型质量一律 blocked，绝不声称换模型后质量不变。**
>
> **编号说明**：`17-p7-external-capabilities.md` 已为外部能力获取预留 S11–S14（planned）；本规格用 S15/S16，执行顺序可与之任意交错。

---

## 0. 与既有规格的关系 / 本规格显式修订的地方

| 既有约束 | 出处 | 本规格怎么处理 |
|---|---|---|
| "多端点路由 / 按用户选模型"不做 | S10 决策记录 §4 | **有条件放开**：只做**用户显式选择** + 管理员目录白名单 + **逐对验证**；**自动选模型、失败换模型、预算降级仍禁止**（`BudgetConfig` 配置期拒绝 `DEGRADE`，`TestNoSilentFallback`）。首版**同端点、同 Key 槽**，不收 `base_url`。 |
| 兼容性报告是单文件、单 (端点, 模型) | `compatibility.py:120-173` | 增"每对一个文件"的存储；原单文件路径与 7 项检查 **S15 不变**（不使既有报告作废）。 |
| `ENDPOINT_PROBE_VERSION` | `compatibility.py:38` | S15 **不递进**；S16 因增加检查项**递进**（所有既有非缺省报告作废，部署影响须写进决策记录）。 |
| 免检缺省 = 字面 `MODEL_NAME` | `endpoints.py:194-202` | **不扩大**。不得为用 flash 改 `DEFAULT_CHAT_MODEL`（那会把免检转给没有质量证据的模型，违反铁律③）。 |
| 出站清单 8 个调用点 | `inventory.PROVIDER_CALL_SITES` | **不增**。思考子类放在已登记的 `generation/chat_model.py` 内。 |
| 威胁模型 T14（端点 / 模型可配置） | `p2.5-threat-model.md:383-394` | 在 S15 扩展为"逐条选择"；S16 新增 **T18 思考内容**（§4）。 |

---

## 1. 范围与目标

### Goal

1. **模型目录（S15）**：管理员在「模型与密钥」页维护一份服务端目录（同端点），成员只读；缺省不配目录 = 行为逐字不变。
2. **按会话 / 按消息选择（S15）**：优先级 **消息 > 会话默认 > 全局**；每个 (端点, 模型) **各自**通过兼容性检查后才可被选；会话默认与每次运行用的模型**落库**，进程重启后挂起的那一轮能恢复到当时选的模型（不静默换回默认）。
3. **新模型可用（S15）**：`MODEL_NAME` 这类默认开思考的模型，在目录里声明 `thinking: off` 时由我们显式关闭思考，**行为与今天的 `MODEL_NAME` 同构**（工具循环不需要回传）。
4. **思考型模型（S16）**：目录里声明 `thinking: roundtrip` 的模型走子类——抽取 `reasoning_content`、按 provider 规则在工具循环里回传、被拦截的回答清理其思考、折叠展示思考摘要（先过输出护栏）。

### 非目标

- **自动路由 / 按难度选模型 / 失败换模型 / 预算触顶降级**：仍禁止（需单独配置、评测、审批）。
- **多端点、跨服务商 Key**：首版不做（需新增 `CREDENTIALS` 槽，`test_web_settings_api.py:110` 按它比较）。
- **按模型分层的 `Capability`**：不新增（契约测试面太大）；限制强模型用目录项的 `allowed_roles`。
- **把 flash 设为免检缺省**；**收 `base_url`**；**"再调一次模型来总结思考"**（新增出站点与成本，还把思考发给第二次调用）。
- **声称高级模型更准**：界面文案、评测报告、决策记录都不得出现。
- **声称任何真实模型可用**：本机无 Key，真实质量层 blocked（§9）。

---

## 2. 事实表（2026-10-14，main `dc51201`）

**一手（本次直接读到的官方原文，`api-docs.deepseek.com/guides/thinking_mode`）**

| # | 事实 |
|---|---|
| F1 | 思考模式**默认开启**（默认 effort=`high`）；OpenAI SDK 下用 `extra_body={"thinking":{"type":"enabled\|disabled"}}` 开关；另有 `reasoning_effort`（low / high / max）。 |
| F2 | 请求带 `tools` 时，**之前所有轮**的 `reasoning_content` 都要回传（"even for turns where the model did not perform a tool call"），缺失则 `400`；不带 `tools` 时回传被忽略。 |
| F3 | 思考模式下 `temperature` / `presence_penalty` / `frequency_penalty` "will not trigger an error but will also have no effect"。 |
| F4 | 文档示例模型名是 `MODEL_NAME`。`reasoning_content` 与 `content` 同级：非流式在 `message.reasoning_content`，流式在 `delta.reasoning_content`（先于 `content` 到达）。 |

**二手（子代理转述，定价页读取被权限拦下，未复核——开工前须在 Step 0 实测）**

| # | 事实 |
|---|---|
| F5 | `MODEL_NAME` = MODEL_NAME（2026-09-10 发布）；`MODEL_NAME` 可能被悄悄路由到 V4.1-Flash；定价页只列这两个，均为 thinking 默认；V4.1 有峰谷价。 |
| F6 | 旧名 `MODEL_NAME` / `MODEL_NAME` 于 2026-07-24 停用，停用后的实际行为文档没写。 |

**代码事实（抽查 + 只读调研，`Rc` = main 检出）**

| # | 事实 | 来源 |
|---|---|---|
| F7 | 换模型的缝已存在：每个 run 新建 `AgenticSession` 与 `ChatOpenAI`；`WebAgenticSessionFactory._args()` 的 `model/base_url/api_key_env` 恒为 `None`，就是显式覆盖入口 | `runtime/agentic.py:650-679`、`webapp/run_manager.py:268-292` |
| F8 | 预检 `chat_model_problem()` **无参**，只看全局模型；被 `create_message` 的预检块调用，该块在会话归属 / 写权限**之后**、附件**之前** | `agentic.py:119-155`、`webapp/run_routes.py:224-237` |
| F9 | `MessageRequest` 是 `extra="forbid"`，只有 `message` / `attachments`；`web_sessions`、`runs` 表没有任何模型列；Alembic head = `0007_step51_dwg_uploads` | `run_routes.py:47-59`、`storage/models.py:86-116` |
| F10 | 免检 = `ChatEndpoint.is_default`（端点是 DeepSeek 缺省 URL 且 model 恰为 `MODEL_NAME`）；其余需报告精确匹配 (端点, 模型) + 探针版本一致 + 检查集恰好 7 项 + schema < 4500 | `endpoints.py:194-202`、`compatibility.py:143-173` |
| F11 | **七项探针只做单步调用**：没有"工具结果回灌后的第二次请求"、没有"流式 + 工具"、没有合成 assistant 形态。它会**放行一个在 agent 循环第 2 次请求就 400 的思考模型**（假通过） | `harness/endpoint_compat.py`、`local_model_compat.py:83-131` |
| F12 | `langchain-openai 1.6.0` 的 `ChatOpenAI` **明确不抽取也不保留** `reasoning_content`（响应两条路径与请求序列化都丢）；`langchain-deepseek` 未安装且只读不回传 | `langchain_openai/chat_models/base.py:3-11,215,402,484` |
| F13 | 最小子类覆写点：`_create_chat_result`（1946）、`_convert_chunk_to_generation_chunk`（1510）、`_get_request_payload`（1915）；`additional_kwargs` 经 checkpoint serde 往返无损 | 同上 |
| F14 | `window_messages` 把旧轮重建为纯文本（丢 `additional_kwargs`、丢带 tool_calls 的 AI 消息）——按 F2 这很可能让思考模式的第 2 个用户轮起 `400` | `prompting/history.py:52-56` |
| F15 | 引用回流的合成 assistant（`content=""`、只有 tool_calls）天生没有 reasoning | `graphs/agent_rag.py:991-1011` |
| F16 | `finalize` 的脱敏 / 拦截只改 `content`（`_rewrite`），`additional_kwargs` 原样保留 → 被拦截回答的 reasoning 会留在 checkpoint 并被回传 | `agent_rag.py:851-855,1249-1304` |
| F17 | 网关把 `400` 判为永久错误零重试，对用户只给 `PROVIDER_UNAVAILABLE`，provider 原文只在 `__cause__`——**思考模型的 400 会被误诊成"上游不可用"** | `gateway.py:70-72,161-189,763-767` |
| F18 | 价格 `_default_prices()` 只有 `MODEL_NAME` / `MODEL_NAME`（均 `confirmed=False`）；其他 id 的 `price_for` 为 `None`；trace 侧成本写死 MODEL_NAME 价 | `runtime/governance/config.py:235-243`、`agent_rag.py:257,528` |
| F19 | 限流桶键含 model，每个新 id 一个满桶且不回收；预算键与模型无关；熔断键 `provider\|model` 但每个会话新建 gateway 实例 | `gateway.py:290-301`、`stores.py` |
| F20 | **数据陷阱**：`LOCAL_MODEL_ENABLED` 时，只带 `model` 的显式覆盖会跳过本地分支，落到 DeepSeek 缺省端点 + `DEEPSEEK_API_KEY` | `endpoints.py:299-318` |
| F21 | 确认恢复路径：进程重启后 `resume()` 用空 handle 重建，没有持久化的模型就静默落回全局模型 | `run_manager.py:828-837` |
| F22 | 唯一构造 `ChatOpenAI(` 的是 `generation/chat_model.py`；出站扫描正则 `\bChatOpenAI\s*\(` 靠它识别该模块，对 `XxxChatOpenAI(` 因 `\b` 不匹配而**盲** | `inventory.py:411`、`test_governance_contracts.py:465-481,525-527` |
| F23 | 测试替身：至少 10 处严格签名 `create(*, run_id, thread_id, user_id, principal, project_id, stack)`；无条件新增 kwarg 会让它们全部 `TypeError` | `run_manager.py:628-637` 注释、各 `tests/test_web_*.py` |
| F24 | **运维实测（2026-10-14）**：经一个 OpenAI 兼容代理配置 `MODEL_NAME` 后，面板的兼容性检查 **7 项里 6 项未通过，仅 `error_handling` 通过**，整次运行状态 `blocked`。按探针代码：`_status` 在没有任何 `failed` 时才是 `blocked`，所以这 6 项**全是 `blocked`（请求抛了异常），不是模型答错**；前四项共用一次 try，第一次真实调用抛异常就一起 `blocked`；`error_handling` 故意发错误模型名、期望得到结构化错误，端点整体不通时它也会通过。因此失败发生在**请求层面**（路径 / 鉴权 / 模型 ID / 超时 / 网络之一），**与思考是否开启的关系未证实**；具体异常类别在报告 evidence 的 `model_facing_error` / `streaming_usage_error` / `cancellation_timeout_error` 里，待回填 | 运维反馈 + `endpoint_compat.py` |

---

## 3. 设计（S15：多模型选择）

### 3.1 模型目录

- 存放：凭据卷 `llm_models.json`（0600、原子写，仿 `generation/endpoints.py::write_panel_endpoint`），**不动** `llm_endpoint.json`。读写在新模块 `generation/model_catalog.py`，模型名复用 `validate_model_name`。
- 条目：`{id, label, tier: "fast"|"advanced", thinking: "off"|"roundtrip", enabled, allowed_roles?, price?}`。`label` ≤ 40 字符；`tier` 只是标签，**不参与任何路由**；`allowed_roles` 缺省 = 所有可写角色；`price` 可选，进 `ModelPrice(confirmed=False)`。
- 条数上限 32；`id` 唯一；`thinking: roundtrip` 在 S15 里**只能保存、不能被选**（S16 才放行，见 §5）。
- 谁能改：`require_admin` + CSRF；env 锁定（`_locked_reason()` 为 `deployment` / `local_model`）时目录**只读**；变更进审计（只记 id / 动作 / 结果，不记 Key）。
- 缺省：没有目录文件 = 只有全局模型可用，请求、响应、提示、快照**逐字不变**。

### 3.2 优先级与会话默认

`消息.model` > `web_sessions.model` > 全局模型。会话默认由 `PATCH /api/sessions/{thread_id}` 修改（归属 → 项目写权限 → 目录校验）。**用户显式选择不是路由**：模型从不因失败、成本或问题难度被自动更换。

### 3.3 逐对验证

- 存储：`<dir>/<sha256(规范化端点|模型)[:16]>.json`，目录缺省为凭据卷下 `llm_compatibility_reports/`；**读出后仍调 `CompatibilityReport.matches()`**（文件名只是索引，不是信任根）。
- `endpoint_verification` 先走原单文件逻辑（缺省免检、`WATER_AGENT_LLM_COMPATIBILITY_REPORT`、面板单文件），再回落逐对文件。
- 面板「运行兼容性检查」接受可选 `model`（目录内才可）；同一时间只跑一个检查的互斥不变。
- **探针按目录项的 `thinking` 配置构造被测模型**：`run_endpoint_compatibility(model=…)` 必须用与生产**相同**的 `build_chat_model(extra_body=…)`（`thinking: off` 的条目探针也关闭思考）。否则探针开着思考通过、上线却关着思考（或反过来），验证的就不是实际要用的东西；思考阶段首块更慢，也可能让流式 / 取消 / 超时类检查的结论与生产不一致。这是**独立成立的正确性要求**，与 F24 那次失败的具体原因无关。
- 约 5 次计费请求 / 模型，面板照旧明示并二次确认。

### 3.4 选择链路与校验顺序

`create_message` 的顺序变为：会话归属 → 项目写权限 → **模型校验（新）** → 附件 → 互斥 / 挂起 → 建 run。模型校验依次要求：目录内且已启用 → 角色允许 → 该对已验证 → `chat_model_problem(model=…)` 通过（缺 Key 等）→ **非** `LOCAL_MODEL` 且**非** env 锁定（F20）。失败码：`WEB_MODEL_UNKNOWN`（422）、`WEB_MODEL_FORBIDDEN`（403）、`WEB_MODEL_NOT_VERIFIED`（424）、`WEB_MODEL_NOT_CONFIGURED`（424）、`WEB_MODEL_LOCKED`（409）、`WEB_MODEL_THINKING_UNSUPPORTED`（422，S15 里目录项 `thinking: roundtrip` 被选时返回；S16 的新版探针通过后放行）。**请求体不接受 `base_url`**。

下传：`manager.start(model=)` → `_RunHandle.model` → 工厂**声明 `supports_model_choice` 才传 kwarg**（F23）→ `WebAgenticSessionFactory.create(model=None)` 内 `args.model = model`。`_args()` 的默认值保持 `None`（`test_defaults_are_unchanged`）。

### 3.5 持久化（迁移 `0008`）

`web_sessions.model`、`runs.model`：nullable、`VARCHAR(200)`、revision ≤ 32 字符。同步 `storage/database.py:21`、`compose.yml` 的 `SMOKE_EXPECTED_MIGRATION_HEAD`、`tests/test_memory_store.py:103`。`resume` 优先读 `runs.model`（修 F21）；读不到则落回会话默认 → 全局，并发 `warning`（**绝不静默**）。会话 payload 新增 `model` 键（"有值才出现"，保住 `test_web_authorization.py:656` 的键集合断言需同步更新）。

### 3.6 治理与可观测

- 价格：目录项的 `price` 并入 `_default_prices`（`confirmed=False`，**不做硬拒绝**）；trace 的成本改走 `price_for(model)`，**未登记 = `None`**（不估算）。S15 只改 agentic 路径（`agent_rag.py` + `generation/cost.py`），legacy / docgen / judge 的写死价留作已知限制。
- `start_trace` 追加 `model_selected` 事件（model、端点主机名、来源 = message|session|global）；`_result_payload` 带 `model`（只放模型 id，不放 `base_url`）。
- 限流桶键仍含 model，靠目录白名单**有界**（自由文本 id 会无界增长，也能靠轮换名字绕过 RPM——所以请求体不收未登记 id）。
- 递进 `GOVERNANCE_CONFIG_VERSION`。

### 3.7 `thinking: off` 的构造

`build_chat_model` 增可选 `extra_body`（**仅设置时传**，不破坏 `test_chat_model.py` 对构造参数的断言）；目录项 `thinking: off` → `extra_body={"thinking":{"type":"disabled"}}`。默认路径保留字面量 `ChatOpenAI(`。**前提**：非思考模式下函数调用与 `cite_clauses` 的质量须经评测（铁律③）——真实质量层需要 Key，blocked。

### 3.8 前端

composer 内模型选择器（目录非空才渲染）、会话默认模型控件、Inspector「本次模型」卡（抄 `RetrievalMode`）、「模型与密钥」页内的目录管理区（**不新增副栏子页**，`settings_nav.test.mjs:18` 钉死 id）。`sendMessage(..., model?)` 只有选了才进请求体。文案不得暗示"高级 = 更准"。

---

## 4. 设计（S16：思考型模型）与威胁模型增量

### 4.1 子类与回传

在 `generation/chat_model.py` 内写 `ChatOpenAI` 子类，覆写 F13 的三个方法：把 `reasoning_content` 写进 `AIMessage.additional_kwargs`（流式靠 `merge_dicts` 对同名字符串键自动拼接）；出站时**只把 `reasoning_content` 一个键加回 assistant 消息**（`test_the_status_stamp_never_reaches_the_provider` 要求其余未知键不外发）。**仅 `thinking: roundtrip` 的条目使用子类**；`extra_body` 开 thinking；默认路径不变。出站扫描同步扩展以识别子类构造（F22）并留反证。

### 4.2 回传策略（以 Step 0 实测为准）

| 形态 | 策略 | 依据 |
|---|---|---|
| 本轮内的 assistant | 全部回传 | F2 |
| 旧轮的终稿 | 先按 F2 字面回传（`history.py` 对 `roundtrip` 条目保留旧轮终稿的 reasoning），**实测若证明纯文本旧轮不 400，再收窄** | F14 |
| 引用回流的合成 assistant | 先尝试不带；若实测 400 则带空串；仍 400 则改为不合成该形态 | F15 |
| 目标模型不支持（跨模型串历史） | 剥离 | 严格校验会拒绝陌生字段 |

### 4.3 清理

`finalize` / `_rewrite` 在回答被拦截或脱敏时**同时清除** reasoning（F16）；收口时清除本轮中间 AIMessage 的 reasoning；当前轮 reasoning 设字符上限（`history.py` 对当前轮不裁剪）；内联 `<think>…</think>`（vLLM / 部分代理）剥离或拒绝该模型，**绝不让它进入答案**。

### 4.4 探针扩展

`endpoint_compat.py` 新增检查：**工具往返**（assistant(tool_calls) + tool 结果 → 再请求）、**流式 + 工具**、**合成 assistant 形态**；evidence 记录**响应里的实际服务模型**（识别 `MODEL_NAME` 被改道）；**递进 `ENDPOINT_PROBE_VERSION`**；复用 `local_model_compat.probe_tools()`（P7-I02）。

### 4.5 展示：`ThinkingPump`

仿 `DraftPump`：按完整句切分，**每句放行前对累计文本调 `check_output`**，命中即整段撤回并关闭本轮思考通道，原文**不进任何事件 / trace**；事件 `thinking_delta`（带 `offset`）/ `thinking_closed`；每 run 字符上限；`AgenticSession.aask/aresume` 加 `on_thinking`，由工厂标志 `supports_thinking_stream` 保护协议兼容。前端 `<details>` 折叠块，**纯文本、`white-space: pre-wrap`、不渲染 Markdown / 链接 / 图片**，文案"思考摘要 · 未经校验 · 不构成设计依据"，位置在草稿之前；`finalize` 清空；`offset` 不衔接的 delta 一律丢弃。

### 4.6 治理

思考模型的超时画像与"重试放大"防护（非流式思考响应可能超 60s 并被网关重试 3 次，成本近似翻倍——思考模型不重试非幂等的超时）；`ProviderUsage` / `UsageRecord` 增 reasoning token 字段（契约变更，`extra="forbid"` 测试同步）；输入估算计入 reasoning；`GOVERNANCE_CONFIG_VERSION` 递进。

### 4.7 威胁模型增量（拟登记，随各步合入 `p2.5-threat-model.md`）

| # | 威胁 | 主要缓解 | 残余 |
|---|---|---|---|
| T14 扩展：逐条选择 | 用户选到未验证 / 被改道的模型；本地模型模式下数据翻向云端；自由文本 id 绕过限流；资源滥用 | 目录白名单 + 逐对验证 + 角色限制；LOCAL / env 锁定拒绝；请求体不收 `base_url`；限流桶有界；evidence 记实际服务模型 | 真实质量未验证；峰谷价与别名改道无法完全避免 |
| T18 思考内容 | 思考文本绕过输出护栏；被拦截回答的思考留在 checkpoint 并被回传；思考膨胀上下文 / 成本；思考被当成答案 / 引用依据；`<think>` 内联混入答案 | `ThinkingPump` 逐句 `check_output`；收口清理；字符上限；通道隔离不变量 P7-I18；内联剥离 | 护栏是确定性正则，必有未覆盖变体；思考可能含用户私有内容（它本就来自同一对话）|

---

## 5. 分步交付

| 步 | 内容 | 执行 Prompt | 状态 |
|---|---|---|---|
| S15 | 模型目录 + 逐对验证 + 按会话 / 按消息选择 + 迁移 0008 + `thinking: off`（flash 关闭思考）+ 治理与可观测 + 前端 + `model_choice_p0`。**目录里 `thinking: roundtrip` 的条目只能保存，不能被选**（返回 `WEB_MODEL_THINKING_UNSUPPORTED`） | `docs/execution-prompts/P7-S15-model-catalog-and-selection.md` | planned |
| S16 | 思考子类与回传策略 + 清理 + 探针扩展（版本递进）+ `ThinkingPump` 与折叠展示 + 治理 + `thinking_p0`；放行 `roundtrip` 条目 | `docs/execution-prompts/P7-S16-thinking-models.md` | planned |

**Step 0（两步之前，不写代码）**：用户填 DeepSeek / 百炼 Key → 设置页「获取模型」得到**真实模型 ID** → 对 `MODEL_NAME` 跑现有兼容性检查（**预期可能假通过**，F11）→ **做一次真实带工具的问答**，观察默认思考下是否 400 → 回答 §10 的四个实测问题 → 结果写回本文 §2。

依赖：S16 依赖 S15。每一步的执行 Prompt 必须**先合入 main** 再开工。

---

## 6. 新增不变量

| # | 不变量 | 强制位置（拟）| 状态 |
|---|---|---|---|
| P7-I15 | 逐条选择只接受目录内的 model id；`MessageRequest` / 会话 PATCH 不接受 `base_url`、`api_key` 等端点字段 | `tests/test_web_model_selection.py` | planned |
| P7-I16 | `LOCAL_MODEL_ENABLED` 或 env 锁定时，任何逐条 / 会话级模型覆盖一律拒绝 | `tests/test_web_model_selection.py` | planned |
| P7-I17 | 免检仍仅限字面 `MODEL_NAME` + 缺省端点（`ChatEndpoint.is_default` 定义不变）；目录项永远走逐对验证 | `tests/test_llm_endpoints.py`、`tests/test_model_verification.py` | planned |
| P7-I18 | `reasoning_content` 永不进入 `answer`、历史投影、导出、答案缓存、trace、`UsageRecord`，也不走旧 `token` / `draft_*` 通道；只经 `thinking_*` 事件，且先过 `check_output` | `tests/test_thinking_isolation.py` | planned |
| P7-I19 | 出站消息只允许在 assistant 消息上加 `reasoning_content` 一个非标准键 | `tests/test_reasoning_chat_model.py` | planned |
| P7-I20 | 子类构造点唯一在 `generation/chat_model.py`，且出站扫描能识别它（放进未登记模块即红）| `tests/test_governance_contracts.py::TestInventoryCoverage` | planned |
| P7-I21 | 模型从不被自动更换：失败、超时、预算、resume 缺记录都只能报错或落回**用户已选的更低优先级默认并告警**，不得换成目录里的另一个模型 | `tests/test_model_governance.py` | planned |

---

## 7. 验收 → 测试映射（拟；节点名在各步 Step 1 冻结时落定，状态均为 `planned`）

### S15：多模型选择

| ID | 验收 | 验证（拟）| 状态 |
|---|---|---|---|
| P7-S15-A01 | 目录：格式 / 条数 / id 校验、原子写 0600、成员只读、管理员 + CSRF 写、env 锁定只读、审计只记 id；缺省不配目录 = 行为逐字不变 | `tests/test_model_catalog.py` | planned |
| P7-S15-A02 | 逐对验证：存储与回落、`matches()` 复核、原单文件路径不变、改一个模型只作废它自己、面板按模型跑检查；**探针用与生产相同的 `extra_body`（`thinking: off` 的条目探针也关思考）** | `tests/test_model_verification.py` | planned |
| P7-S15-A03 | 构造：`extra_body` 仅设置时传；`thinking: off` 传 disabled；默认构造参数与 `temperature=0.0` 逐字不变 | `tests/test_chat_model.py` | planned |
| P7-S15-A04 | 选择校验顺序与错误码；不收 `base_url`；`extra="forbid"`；403 先于 424；LOCAL / env 锁定拒绝；`roundtrip` 条目不可选 | `tests/test_web_model_selection.py` | planned |
| P7-S15-A05 | 下传与协议兼容：`supports_model_choice`；缺省不传 kwarg；`_args()` 默认全 `None` | `tests/test_web_model_selection.py`、`tests/test_llm_endpoints.py` | planned |
| P7-S15-A06 | 迁移 0008（SQLite 真跑）与三处版本同步 | `tests/test_p5_step38_storage.py`、`tests/test_web_deployment_overlay.py`、`tests/test_memory_store.py` | planned |
| P7-S15-A07 | 会话默认与优先级；`PATCH` 授权；`resume` 读 `runs.model`；读不到则降级并告警；payload 键集合 | `tests/test_web_model_selection.py` | planned |
| P7-S15-A08 | 治理：价格 `confirmed=False`；成本按模型、未登记为 `None`；`model_selected` 事件；`payload.model`；限流桶有界；从不自动换模型（P7-I21）| `tests/test_model_governance.py` | planned |
| P7-S15-A09 | 缺省行为不变：快照现有 profile 逐字不变（仅允许 `PINNED_ENV_KEYS` 与声明的新 env）| `tests/test_tool_assembly_snapshot.py` | planned |
| P7-S15-A10 | 前端：选择器仅目录非空渲染；请求体仅选了才带；Inspector 卡；目录管理区在模型页；文案不暗示更准（node 测试）| `web/app/tests/model_choice.test.mjs` | planned |
| P7-S15-A11 | `model_choice_p0` 门禁 + 基线 | `gate:model_choice_p0` | planned |
| P7-S15-A12 | 部署：compose / env / 卷；迁移服务先于 app | `tests/test_web_deployment.py` | planned |
| P7-S15-A13 | 真实 Key 实测：`MODEL_NAME`（`thinking:disabled`）的兼容性检查、真实带工具问答、`rag_p1 --variant real`（见 §9）| 无 | untested |

### S16：思考型模型

| ID | 验收 | 验证（拟）| 状态 |
|---|---|---|---|
| P7-S16-A01 | 子类：非流式 / 流式解析 `reasoning_content`；出站只加该键（P7-I19）；非 `roundtrip` 条目不用子类；默认路径不变 | `tests/test_reasoning_chat_model.py` | planned |
| P7-S16-A02 | 回传策略：本轮全回传、旧轮终稿、合成 assistant、跨模型剥离；对"缺回传即 400"的假端点能收口为可诊断的错误而不是"上游不可用" | `tests/test_reasoning_roundtrip.py` | planned |
| P7-S16-A03 | 清理：拦截 / 脱敏时清 reasoning；收口清中间消息；字符上限；内联 `<think>` 不进答案；checkpoint 无残留 | `tests/test_reasoning_cleanup.py` | planned |
| P7-S16-A04 | 探针：新检查项各有坏模式反证；版本递进使旧报告作废；evidence 记实际服务模型 | `tests/test_endpoint_compatibility.py` | planned |
| P7-S16-A05 | `ThinkingPump`：逐句 `check_output`、命中撤回并关闭通道、原文不进事件 / trace、`offset` 续接、字符上限、取消、失败撤回 | `tests/test_thinking_pump.py` | planned |
| P7-S16-A06 | 通道隔离（P7-I18）：不进答案 / 历史 / 导出 / 缓存 / `ConversationMessage` / 旧 `token` 通道 | `tests/test_thinking_isolation.py` | planned |
| P7-S16-A07 | 协议兼容：`on_thinking` + `supports_thinking_stream`；严格签名替身不破 | `tests/test_web_runs.py` 等既有替身 | planned |
| P7-S16-A08 | 治理：思考模型超时画像、重试放大防护、reasoning token 字段、输入估算、版本递进 | `tests/test_provider_gateway.py`、`tests/test_governance_contracts.py` | planned |
| P7-S16-A09 | 前端：`<details>` 纯文本折叠块、文案与位置、`finalize` 清空、`offset` 续接、不进 `archiveRun` / `mergeHistory`（node 测试）| `web/app/tests/thinking.test.mjs` | planned |
| P7-S16-A10 | `thinking_p0` 门禁 + 基线 | `gate:thinking_p0` | planned |
| P7-S16-A11 | 快照：现有 profile 逐字不变（除声明）；`PROMPT_TEMPLATE_VERSION` 是否变化按实现决定并走 `--write --reason` | `tests/test_tool_assembly_snapshot.py` | planned |
| P7-S16-A12 | 真实 `MODEL_NAME`（思考开）带工具往返实测、旧轮 / 合成形态是否 400（见 §10）| 无 | untested |

---

## 8. 实施协议

照 `16-p7-plugin-mcp.md` §8（读 `AGENTS.md` / 本规格 / 该步执行 Prompt → 确认执行 Prompt 已在 main → 拉 `YEK/p7-s<n>-<名>` → `tool_assembly_snapshot --check` 起点干净 → 先写失败测试 → 最小改动 → 一个逻辑面一个 commit；停止条件、快照有意变更、决策记录结构、汇报格式同该节）。补充：

1. **Step 0 实测先于 S16 的策略定稿**：§4.2 的回传策略表是"待实测的假设"，S16 开工前必须用真实 Key 把 §10 的四个问题回答清楚并回填 §2。
2. **评测集 Step 1 冻结**；冻结后改动任何一条用例，要在该步决策记录里写明原因。
3. **追溯**：`spec_trace.parse_spec(text, prefix="P7")` 可解析本文；S15 范围块允许把本文加入追溯检查。

## 9. 已知限制与 blocked 项

- **没有 Key，真实质量层全部 blocked**：flash（关思考）与 `MODEL_NAME` 在 `rag_p1 --variant real` 上的差异、思考型模型的真实工具往返、旧轮 / 合成形态是否 400、`MODEL_NAME` 停用后的实际路由。
- 现有 84.48% 的 RAG 质量证据只在缺省 `MODEL_NAME` 上取得，**不得迁移到任何其他模型**。
- 价格峰谷价没有时间维度，成本仍是估算；其余 legacy / docgen / judge 路径的成本写死价留作已知限制。
- 思考模式下 `temperature` 失效：数值可靠性只来自程序化工具；相关不变量在文档与评测里如实写明。
- 多端点、跨服务商、按角色细分的强模型授权不在本规格。

## 10. 待决事项 / 待实测

| # | 问题 | 现状 / 推荐 |
|---|---|---|
| D1 | 目录是否只允许"同端点"？ | 是（已定）。多端点另立步骤 |
| D2 | `allowed_roles` 缺省？ | 缺省所有可写角色；管理员可逐项收紧 |
| D3 | 会话默认谁能改？ | 项目会话需 owner / member；VIEWER 不可 |
| D4 | 旧轮终稿是否保留 reasoning？ | **待实测**（F14）；先按 F2 字面回传，实测后收窄 |
| D5 | 缺 `reasoning_content` 的合成 assistant / 旧轮纯文本是否 400？空串是否被接受？ | **待实测** |
| D6 | `thinking:disabled` 下 `MODEL_NAME` 的 `rag_p1 --variant real` 质量？ | **待实测**；不得先验声称 |
| D7 | `MODEL_NAME` 现在是否仍可用、被路由到哪个模型？ | **待实测**；影响"免检缺省"的质量依据 |
| D8 | 思考型模型的超时画像取值 | 待实测 p95 后定；初值沿用 60s 并禁用超时重试 |

---

## 11. 相关文件索引（拟新增 / 修改）

| 文件 | 作用 |
|---|---|
| `src/water_agent/generation/model_catalog.py` | 目录读写与校验（S15）|
| `src/water_agent/generation/compatibility.py` / `harness/endpoint_compat.py` | 逐对验证存储（S15）、新检查项与版本递进（S16）|
| `src/water_agent/generation/chat_model.py` | `extra_body`（S15）、`ChatOpenAI` 子类（S16）|
| `src/water_agent/webapp/{run_routes,run_manager,workspace_routes,settings_routes}.py` | 选择链路、会话 PATCH、目录路由 |
| `src/water_agent/storage/{models,database,repository}.py` + `alembic/versions/0008_*.py` | 迁移 0008 |
| `src/water_agent/graphs/agent_rag.py` / `runtime/agentic.py` | 成本按模型、`model_selected`、`ThinkingPump`（S16）|
| `src/water_agent/harness/{model_choice,thinking}_eval.py` + `data/eval/{model_choice,thinking}_p0.jsonl` + `*_main.json` | 评测（见 harness 文档）|
| `web/app/src/{workspaceComponents,useWorkspaceController,workspaceApi,workspaceInspector,SettingsPanel}.tsx` 等 | 选择器、Inspector 卡、目录管理区、思考折叠块 |
| `docs/design/18-p7-multi-model-harness.md` | 评测 harness 设计 |
