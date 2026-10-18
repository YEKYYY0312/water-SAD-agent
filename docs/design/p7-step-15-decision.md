# P7-S15 决策记录：模型目录与按会话 / 按消息选择模型

> 日期：2026-10-17。分支 `YEK/p7-s15-model-catalog`，基于 main `432cd6d`（S0–S10、#45 设置页副栏、#46 外部能力 SDD、#47 多模型 SDD 已合入）。
> 执行 Prompt `docs/execution-prompts/P7-S15-model-catalog-and-selection.md`，规格 `docs/design/18-p7-multi-model.md`（§3、§6 不变量 I15 / I16 / I17 / I21、§7 P7-S15-A01…A13），
> 在 16 号规格 §7 登记的同名验收行见 `16-p7-plugin-mcp.md`，威胁模型 `p2.5-threat-model.md` T14 扩展。

**一句话结论**：管理员可以在同一对话端点、同一把 Key 下维护一份**模型白名单**（目录），用户在输入框旁按**消息**、或在会话里按**默认**选其中一个；
每个目录模型都要先过**逐对兼容性检查**才能被选，`thinking: off` 的条目探针与生产装配带同一份关闭思考的请求体；**模型从不被自动更换**
（失败、超时、预算、resume 都只会报错）。缺省行为逐字不变（不配目录 = 前端没有选择器、请求体没有 `model`、payload 没有 `model`）。
**没有任何真实 Key 的验证**：换模型后的作答质量、`thinking: off` 下函数调用与引用的真实表现、目录价格的真实性都是 blocked，**不得声称“换模型后质量不变”或“进阶档更准”**。

## 0. 偏离与修订

### 0.1 执行 Prompt §0 的修订

1. **范围块补遗（单独的文档 PR，先合入 main）**：执行 Prompt 的 `allow` 漏了两个已经改过的文件——新建的 `src/water_agent/webapp/model_choice.py`（选择校验放在一处，路由层只调用）与
   `src/water_agent/observability/json_trace.py`（trace 的 `cost_usd` 允许 `None`，否则未登记价格只能写成 0 或猜一个价）。按 S8 先例（#42）走独立文档 PR：分支名不以 `YEK/p7-s<n>-` 开头，
   只改执行 Prompt 的 `allow` 列表；合入后把 main 合进本分支再跑范围守卫。范围块从 merge-base 读，分支改不了自己的范围。
2. **18 号 SDD 的不变量不能登记成 16 号规格的表格行**：`tests/test_p7_traceability.py`（在 `forbid` 里，不能改）断言 16 号规格里不变量恰好 7 条（P7-I01…I07）。S15 的 I15 / I16 / I17 / I21
   留在 18 号 SDD，测试 docstring 用 `SDD18-I15` 这样不带 `P7-` 前缀的写法引用（带前缀会被“测试里出现了规格未定义的 ID”反向检查拦下）；验收行 P7-S15-A01…A14 登记进 16 号 §7，追溯检查才认。

### 0.2 实测后追加的偏离

- **D1 `thinking` 多了第三个取值 `default`**（什么都不传，且是缺省）。SDD 原设计只有 `off` / `roundtrip`：但有些模型根本没有思考开关，强传 `thinking` 字段会让严格服务商 400，
  管理员就没法“把它关掉”。`off` 才附加 `extra_body={"thinking":{"type":"disabled"}}`；`roundtrip` 只能保存、不能被选。锁定：`tests/test_model_catalog.py::test_thinking_profiles_map_to_the_request_body`。
- **D2 价格没有并入 `_default_prices`**。启动期去读凭据卷文件太脆弱（卷没挂、权限不对、目录损坏都会变成启动失败）。改为运行期 `generation/cost.py::estimate_model_cost` 查询目录价，
  只在目录里的模型上生效，**目录之外与此前逐字相同**；治理阈值没动，所以没有递进 `GOVERNANCE_CONFIG_VERSION`。未登记价格记 `None`（trace 的 `cost_usd` 允许 `None`）。
  锁定：`tests/test_model_governance.py::test_cost_is_attributed_per_catalog_model_and_unknown_prices_are_none`、`::test_llm_call_record_and_trace_keep_unknown_cost_as_none`。
- **D3 `resume` 以 `runs.model` 为准，`NULL` 就是当时用了全局模型**。SDD 原有“读不到则落回会话默认 → 全局并告警”一句，实现时作废：一个已经在跑的 run 不该被会话默认的后续改动影响，
  也就没有“读不到”这种状态。锁定：`tests/test_web_model_selection.py::test_resume_after_a_restart_restores_the_persisted_run_model`。
- **D4 评测集在机制之后补写**，没有“机制未实现时全红”的基线（`mechanism=absent` 的基线不存在，比较器只认 `present`）。补偿办法是**每个硬指标一条反证**
  （`tests/test_model_choice_eval.py::test_each_hard_metric_goes_red_when_its_protection_is_removed`）：monkeypatch 拿掉对应保护、只跑相关用例，断言该指标离开目标值，不拿掉时同一批用例全在目标上。
- **D5 `PATCH /api/sessions/{id}` 放在 `run_routes.py`**（`workspace_routes` 与 `run_routes` 会循环导入）；`_session_payload` 仍在 `workspace_routes.py`。
- **D6 评测发现的 harness 盲点（已修）**：`local_model_override_leak_count` 的反证一开始没变红——泄漏检查发生在 `POST` 刚返回时，而会话工厂在 run 线程里异步建会话，202 路径上 `calls` 还是空的，
  检查等于恒真。改为等 run 收口后再看工厂收到过什么模型；基线报告因此仍逐字相同（正确实现下该计数本来就是 0）。这正是“反证”要抓的那类问题。
- **D7 全量测试发现的回归（已修）**：`POST /api/settings/llm-endpoint/compatibility` 的可选请求体写成 `CompatibilityRequest | None` 后，OpenAPI 里变成 `anyOf`，
  `tests/test_upload_body_limit.py::test_request_ceiling_leaves_room_for_the_largest_legitimate_body` 按 `$ref` 取模型而 `KeyError`。改成 `Body(default_factory=CompatibilityRequest)`：无请求体的旧调用照常（`model=None`）。
- **D8 前端与后端的上限对齐**：管理员目录编辑区的校验一度写成 64 条 / 名称 60 字，后端是 32 条 / 40 字（`MAX_ENTRIES` / `MAX_LABEL_LENGTH`）；已改为一致并有前端测试锁定。
- **D9 装配快照里 `compatibility` 的含义**：新 profile `session_pick_model_off` 的 `chat_model_endpoint.compatibility` 一开始是 `default_exempt`——因为 `_chat_model_endpoint` 按全局模型解析，
  验的不是所选模型。改为按所选模型解析后是 `verified`（证明逐对报告真的被采信）。

## 1. 交付

| 面 | 内容 | 锁定 |
|---|---|---|
| 目录 | `generation/model_catalog.py`：凭据卷 `llm_models.json`（0600 原子写）、≤ 32 条、`{id, label ≤ 40, tier, thinking, enabled, allowed_roles?, price?}`、不收 `base_url` / `api_key`、读不出即失败关闭 | `tests/test_model_catalog.py` |
| 逐对验证 | `compatibility.py`：`pair_report_path`（`llm_compatibility_reports/<sha256(端点\|模型)[:16]>.json`）、`endpoint_verification` 先逐对后原单文件、`matches()` 复核；原 7 项检查与 `ENDPOINT_PROBE_VERSION` 不变；免检仍只属于缺省端点 + 字面 `MODEL_NAME` | `tests/test_model_verification.py` |
| 探针 | `harness/endpoint_compat.py::run_endpoint_compatibility(model=…)`：被测模型带与生产相同的 `extra_body`；CLI `--model` | 同上 |
| 构造 | `generation/chat_model.py::build_chat_model(extra_body=)` 仅设置时才传；`runtime/agentic.py::chat_model_problem(model=)`；`LOCAL_MODEL_ENABLED` 时拒绝覆盖 | `tests/test_chat_model.py::test_extra_body_is_forwarded_only_when_given` |
| 选择 | `webapp/model_choice.py`（锁定 → 目录 → `roundtrip` → 角色 → 真实工厂下 `chat_model_problem`；403 先于 424）、`run_routes.py`（`MessageRequest.model`、`PATCH /api/sessions/{id}`）、`run_manager.py`（`supports_model_choice` 才传 kwarg，选了而不支持直接 `ValueError`） | `tests/test_web_model_selection.py`、`tests/test_model_governance.py` |
| 持久化 | `web_sessions.model`、`runs.model`（可空 `VARCHAR(200)`）；迁移 `0008_step15_model_choice`；`MIGRATION_HEAD` 三处同步（`storage/database.py`、`compose.yml` 的 `SMOKE_EXPECTED_MIGRATION_HEAD`、`tests/test_memory_store.py`） | `tests/test_p5_step38_storage.py::test_step15_migration_adds_and_removes_the_nullable_model_columns`、`tests/test_web_deployment_overlay.py` |
| 设置接口 | `GET/PUT /api/settings/llm-models`（成员只读且只看身份字段；管理员 + CSRF 写；部署锁定时只读 409）；兼容性检查可带 `model`；审计 `settings.llm_models_updated` 只记 id 与条数 | `tests/test_web_model_selection.py` |
| 治理 / 可观测 | `estimate_model_cost`（未登记 = `None`）；`model_selected` trace 事件；finalize payload 有值才带 `model` | `tests/test_model_governance.py` |
| 前端 | 输入框旁选择器、会话默认模型、Inspector「本次模型」、模型页里的管理员目录管理区（不新增副栏子页） | `web/app/tests/model_choice.test.mjs` |
| 评测 | `model_choice_p0`（50 条合成，11 项硬指标）+ 比较器 + 入仓基线 + `ci_gate.sh` | `tests/test_model_choice_eval.py` |
| 快照 | 新增 `session_pick_model_off`（见 §3） | `tests/test_tool_assembly_snapshot.py` |

## 2. 实测

`model_choice_p0`（`python -m water_agent.harness.model_choice_eval`，两次运行报告逐字相同、不含临时路径；入仓基线与 HEAD 逐字相同）：

```text
model_choice_p0 mechanism=present passed=50/50
  selection_validation_correct_rate: 1.0
  unauthorized_model_use_count: 0
  base_url_accepted_count: 0
  silent_fallback_count: 0
  default_behavior_identical_rate: 1.0
  per_pair_verification_isolation_rate: 1.0
  cost_attribution_correct_rate: 1.0
  local_model_override_leak_count: 0
  key_leak_count: 0
  benign_selection_rejected_rate: 0.0
  harness_error_count: 0
```

反证（拿掉保护 → 指标变红）全部成立：选择校验放在响应之后（`unauthorized_model_use_count`）、目录解析放过 `base_url`（`base_url_accepted_count`）、`resume` 丢掉 `model`（`silent_fallback_count`）、
`_session_payload` 恒带 `model`（`default_behavior_identical_rate`）、逐对报告共用同一路径（`per_pair_verification_isolation_rate`）、成本恒按 DeepSeek 价（`cost_attribution_correct_rate`）、
锁定与 `chat_model_problem` 同时放行（`local_model_override_leak_count`）、审计事件写进 Key（`key_leak_count`）、所有选择都被拒（`benign_selection_rejected_rate`）、未知用例类型（`harness_error_count`）。

验证命令与结果见 §2.1。

### 2.1 测试与门禁

（见本文件末尾“验证记录”，在最终验证后回填。）

## 3. 快照变化

`--diff`（`--write` 之前）：只有新增一个 profile；现有 18 个 profile 与三个版本号逐字不变，没有新增环境变量（`PINNED_ENV_KEYS` 不动）：

```text
/profiles/session_pick_model_off: "<absent>" -> {"description": "缺省端点上从目录选 thinking=off 的模型 + 逐对兼容性报告：chat model 指向所选模型并附带关闭思考的 extra_body", "lifecycle": {"app": {"a...
```

新 profile 的要点：`exception: null`；`chat_model_endpoint = {"wrapper": "GovernedChatModel", "base_url": "https://api.deepseek.com", "model": "snapshot-picked-model",
"compatibility": "verified", "extra_body": {"thinking": {"type": "disabled"}}}`；模型可见面（注册顺序、schema、工具指引、系统提示、主体、暴露探针）与
`session_mock_default` **完全相同**——选模型不改变工具与提示。已知数字 52 → 56（14 个装配成功的 profile × 4 条暴露探针）。

## 4. 不做

- **思考型模型**（`reasoning_content` 回传、清理、`ThinkingPump`、折叠展示）：S16。它要先用真实 Key 实测 18 号 SDD §10 的 D4–D8；本步 `thinking: roundtrip` 的条目只能保存、不能被选，前端显示“暂不支持”。
- **不同端点 / 不同 Key 的模型**：首版同端点、同 Key 槽；目录与请求体都不接受 `base_url` / `api_key`。
- **自动选模型、按难度路由、失败换模型、预算降级**：仍禁止。
- 不新增 `Capability` / `ProviderKind` / 出站点（`PROVIDER_CALL_SITES` 仍是 8）；不改 `gateway.py` / `pipeline.py` / `contracts.py` / `inventory.py` / `plugins` / `mcp` / `prompting`；不上调任何预算数字（`cost < 4500`、`schema_chars_total == 4497`、系统提示 `< 1950`）；不改 `DEFAULT_CHAT_MODEL`。
- legacy / docgen / judge 路径的成本仍是写死价（已知限制，S15 只改 agentic 路径）。

## 5. 残余风险与未验证项

- **真实质量 blocked（P7-S15-A13）**：没有真实 Key，没跑过 `rag_p1 --variant real`。`thinking: off` 下函数调用与 `cite_clauses` 的真实表现、目录模型在真实端点上是否存在、价格是否真实都没验证。
  **不得声称“换模型后质量不变”，也不得声称“进阶档更准”**——`tier` 只是标签，界面文案已写明“档位只表示响应速度与费用倾向，不代表回答更准确”。
- 兼容性探针只覆盖七类能力，**通过 ≠ 作答质量合格**；经中转站的 `MODEL_NAME` 多步工具循环未 400，但**第二个用户轮（旧轮是纯文本）是否 400 尚未测**，那是 S16 的前置，不属于本步。
- 被盗的管理员账号能把目录扩到端点上的任何模型并标成“快速档”；`allowed_roles` 只按全局角色判断，不看项目内角色。
- 会话默认模型被停用 / 删除后，该会话的下一条消息 422 `WEB_MODEL_UNKNOWN`（失败关闭，不换模型）；用户需在会话里改选或清除默认——界面会提示，但需要用户动手。
- 浏览器手动验收（P7-S15-A14）：自动化覆盖在 HTTP 往返与前端 node 测试；真实浏览器里选择器与目录管理区的交互未做。
- 前端测试是 node 测试（`web/app/tests/model_choice.test.mjs`），追溯检查认不出 `.mjs`，A10 在 16 号规格里登记为 `untested` 并写明原因。
- 升级后需要重建镜像并让 `migrate` 服务执行 0008；运行中的旧栈用的是 S15 之前的镜像，在重建之前不会有选择器。
