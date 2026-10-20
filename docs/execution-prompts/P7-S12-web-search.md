# P7-S12 执行 Prompt：联网搜索（`web_search` 工具 / provider 适配器 / 查询出口守卫 / 结果流水线 / 逐条消息勾选）

> **工作区**：仓库 `YEKYYY0312/water-SAD-agent`，基于最新 `main`（必须已包含本 Prompt、`docs/design/17-p7-external-capabilities.md` 与 S11 的全部交付——`external/envelope.py`、`external/http_get.py`、`external/taint.py`、`SourceTag.WEB_CONTENT`、`ToolSpec.result_source`）。新建分支 `YEK/p7-s12-web-search`。
> **必读**：
> - `AGENTS.md`
> - `docs/design/17-p7-external-capabilities.md`（SDD：§0 冲突清单、§3 事实表、§4 共同底座、**§5 联网搜索（本步全部）**、§8 威胁模型 T15、§10 不变量 P7-I09…I11、§11 的 P7-S12 验收、§12 实施协议、§14 待决事项 D1–D3 / D7 / D9 / D10）与 `17-p7-external-capabilities-harness.md`（§0 原则、§2 通用设施、**§4 `web_search_p0`**）
> - **`docs/design/p7-step-11-decision.md`（S11 的实测与偏离——本步建立在它之上，尤其 §0.2 的 D3 污点只认 artifact、D4 记忆写入是入口约束、D5 终答网址校验没有开关、D12 P7-I07 评测白名单）**
> - `docs/design/16-p7-plugin-mcp.md` §3.6（按意图最小暴露）、§3.10（`open_exchange` / 出站清单）、§6（防漂移 harness）、§8（实施协议）
> - `docs/design/p7-step-8-decision.md`（S8：唯一出站模块、出站清单 7 → 8、`ProviderKind.MCP_REMOTE` 画像、`GOVERNANCE_CONFIG_VERSION` 递进的写法）、`docs/design/p7-step-15-decision.md`（上一步的偏离格式）、`docs/design/p2.5-threat-model.md`、`docs/design/p2.5-provider-inventory.md`
> - 参考实现：`external/http_get.py::governed_get`、`external/envelope.py::wrap_web_content`（S11 的内核，**本步不改**）；`plugins/builtin.py`（`ClausesPlugin` / `SkillsPlugin`——插件怎么贡献工具与暴露规则）；`plugins/exposure.py`（`ExposureRule` / `ToolExposurePolicy.decide`）；`runtime/governance/config.py`（`ProviderProfile` / `MCP_REMOTE` 画像的写法）；`runtime/governance/inventory.py`（`PROVIDER_CALL_SITES` 的登记格式、`mcp_streamable_http` 站点）；`webapp/run_manager.py`（`WebAgenticSessionFactory` 的 `supports_*` 标志与"有值才传 kwarg"的写法、`confirmation=True` 入口标志）；`webapp/settings_routes.py`（管理员 + CSRF 路由、审计）；`config/credentials.py`（凭据白名单与三层解析）

---

## 0. 相对规格的修订与本步决定（以本文为准）

| 疑问 | 本步做法 | 原因 |
|---|---|---|
| **provider（待决 D1）** | 适配器接口 + **`searxng`**（自托管、无密钥、运维控制出口与引擎）为参考实现；`brave`（公网 API、需密钥）为第二个适配器。**两者都只对本地假服务验证**，不得声称接通任何真实搜索服务 | SDD 推荐；真实可用性取决于运维环境与数据出境评估，本机无网络沙箱与 Key |
| **`WEB_SEARCH` 能力（待决 D2）** | 新增 `Capability.WEB_SEARCH`，**不进** `DEFAULT_LOCAL_CAPABILITIES` 与 Web 成员能力集；`GRANTABLE_CAPABILITIES` 扩为 `{MCP_INVOKE, WEB_SEARCH}`，**同步修订 P7-I06**（`tests/test_plugin_invariants.py` 与 16 号规格 §4 的 I06 行）。`NETWORK_FETCH` 仍在 `DANGEROUS_CAPABILITIES`、仍被 `Principal` 构造期拒绝 | "只读外发、目的地固定"与"目的地由模型决定"的 `NETWORK_FETCH` 语义不同 |
| **站点 8 → 9** | 本步**新增** `ProviderKind.WEB_SEARCH` / `CallCategory.WEB_SEARCH` / 画像，`PROVIDER_CALL_SITES` 登记第 9 个站点 `web_search`：`module = water_agent/web_search/tool.py`，`egress_module = water_agent/mcp/http_transport.py`（与 `mcp_streamable_http`、四个 `deepseek_chat_*` 同构——出站原语在 `open_exchange` 所在模块）。三处 `== 8` 断言同步改 `== 9`（`tests/test_vision_review.py`、`tests/test_llm_endpoints.py`、`tests/test_governance_contracts.py`），`OPEN_EXCHANGE_CALLERS` 不变（`external/http_get.py` 已登记，`web_search/` 经 `governed_get`，**不直接调用 `open_exchange`**） | S11 决策记录 §0 / §4：登记一个没人走的站点没有意义，8 → 9 随第一个真实调用方 |
| **画像取值** | `ProviderKind.WEB_SEARCH` 画像：`timeout_seconds=15`、`RetryPolicy(max_attempts=2)`（**只对瞬时错误重试一次**，401/403 零重试且不计熔断，沿用 gateway 既有分类）、熔断按 provider **主机**（`model` 字段写 `web_search:<host>`）、限流 `RateLimitPolicy` 取失控保护量级（每主体每分钟 ≤ 20，注释写明"不是生产调优值，DAU 未知"）。`GOVERNANCE_CONFIG_VERSION` → `p7-s12-<日期>` | 沿用 `MCP_REMOTE` 的写法；改阈值必须改版本号 |
| **开关与暴露（两道门，待决 D10）** | ① 部署级开关 `WATER_AGENT_WEB_SEARCH=off\|on`（代码缺省 `off`，**Web compose 缺省也是 `off`**——外发默认关，与 skills 不同）；`on` 还需 `WATER_AGENT_WEB_SEARCH_PROVIDER` + `WATER_AGENT_WEB_SEARCH_BASE_URL`，有密钥的 provider 还需凭据槽位 `WATER_AGENT_WEB_SEARCH_API_KEY`；缺任一项 → 插件 `skipped`、不注册工具；取值非法 / provider id 拼错 → 装配期抛错。② **逐条消息勾选**：`MessageRequest.web_search: bool = False`；`AgenticSession(web_search=…)` 只在 ① 已满足时对**该条消息**把 `WEB_SEARCH` 追加给主体并开放工具（与 S7 的 `confirmation=True` 入口标志同构） | 隐私同意显式、免调关键词；代价是一个请求字段与一个前端开关 |
| **暴露规则** | `ExposureRule` 新增 `mode="request"`：**由入口标志决定**，不看 query；`ToolExposurePolicy.decide` 增加 `request_enabled: Collection[str]` 参数（缺省空）。`request` 组排在 core 之后、intent 之前入选；最紧组合下放不进预算 → 记入 `dropped`，**且必须向用户发 `WEB_SEARCH_NOT_EXPOSED` 警告**（`warnings` + trace），不得静默丢弃。`exposure.py` 在 allow 里仅为此 | 用户勾选了联网却因预算被让位，静默丢弃会让用户以为查过了 |
| **schema 预算** | `web_search` 的 `args_model` schema ≤ **500 字符**（Step 0 实测后写入决策记录）。**不得上调 4500**；`schema_chars_total == 4497`（本地工具总和）**不变**——`web_search` 是 `request` 组，不计入"本地工具"基线；最紧组合（读图 + 材料 = 4497）放不下它是**预期行为**，由 `WEB_SEARCH_NOT_EXPOSED` 兜住 | 保护断言 `assert cost < 4500` 与 `schema_chars_total == 4497` 不动 |
| **工具契约（SDD §5.1，P7-I09）** | `web_search(query: str, count: int = 3)`——**只有这两个参数**；`query` 1–128 字符、单行；`count` 1–5。无域名过滤、无时间过滤、无任何 URL 参数。`ToolSpec.result_source=SourceTag.WEB_CONTENT`、`timeout_seconds=15`。`urls` 字段必须是工具结果里的规范化网址列表（S11 的 `_with_web_artifact` 据此写 `web_refs`） | schema 越小越安全也越省预算 |
| **R2 终答网址校验的开关** | S11 的"本回合 `web_tainted` 或本 thread 已有 `web_refs` 才校验"**保持不变**（它已覆盖开关 off 的部署：off 时没有联网工具、没有污点、校验不触发）。SDD R2 写的"仅 `WATER_AGENT_WEB_SEARCH=on` 的装配生效"在 S11 D5 已决定不采用，本步**不再追加**一个开关 | 缺省路径逐字不变已由污点条件保证；多一个开关是第二个真源 |
| **S11 D12 的评测白名单** | 本步 `web_search_p0` 的会话用例**改走 `AgenticSession`**（它现在有生产联网工具可装配），并**删除** `tests/test_plugin_invariants.py` 的 `EVAL_GRAPH_CALLERS` 白名单条目 `harness/external_content_eval.py`——**前提**是 `external_content_p0` 的污点用例也改走 `AgenticSession(web_search=…)`（它们现在可以用生产 `web_search` 工具 + 假 provider）。若某条用例仍需夹具工具（例如专测 `result_source` 缺省行为），保留白名单条目并在决策记录里说明 | S11 决策记录 D12 明确写了"S12 接入真实工具后应收窄或删除" |
| **面板（D3）** | 设置页副栏**新增**"联网搜索"页（`SETTINGS_SECTIONS` 加一项，**修订** `settings_nav.test.mjs` 钉死的 `["models","skills","mcp"]`）：只读状态（开关 / provider id / **主机名** / 密钥是否已配置）+ 管理员写密钥槽位（只写不读，复用 `config/credentials.py` 的三层解析与 `PUT/DELETE /api/settings/credentials/{name}`）。**面板不能写地址、不能写 provider、不能开关**——这些是运维的 | 被盗的管理员账号不能把用户问题改道 |
| **trace / 审计（D9）** | trace 对 `query` 与用户问题同级处理（存，经 `_redact`）；审计事件 `web_search.performed` **只记查询哈希 + 长度、provider 主机名、条数、结果码**，不记查询原文、不记网址、不记密钥 | 审计是长期留存的，比 trace 更敏感 |
| **缓存** | 不做搜索结果缓存、不做语义缓存（D7）。`web_tainted` 回合的答案缓存拒绝已由 S11 完成，本步不改 `cache.py` | 网络内容未经核验、会过期 |
| **快照** | 现有 **19 个 profile 逐字不变**；新增 `session_web_search_fake`（`session_w` 开头——**注意**：S11 范围块把 `frozen_json` 写成 `session_*` 全冻结，本步范围块改为 `session_[!w]*` + 精确指针 `session_web_member` + `runner_*`，见范围块说明）。`TOOL_POLICY_VERSION` 递进（新增工具与授权）；`PROMPT_TEMPLATE_VERSION` **不递进**（`web_search` 走现有工具指引渲染，不改提示模板）；`MCP_CONFIG_VERSION` 不动。`PINNED_ENV_KEYS` 追加三个新环境变量 | 缺省不配 = 装配面逐字不变 |
| **评测** | `web_search_p0`（设计 ~52 条，Step 1 冻结）+ 假搜索服务 `tests/fixtures/fake_search_server.py`（**进程内线程，不起子进程**，S8 D13）接进 `ci_gate.sh` 与入仓基线 | 评测先行 |

## 1. 目标与边界

**做**（编号对应 SDD §11 的 P7-S12-Axx）：

1. **能力与开关（A01）**：`Capability.WEB_SEARCH`；`GRANTABLE_CAPABILITIES` 扩展并修订 P7-I06；`web_search/config.py` 解析环境变量（取值非法 / provider id 拼错 → 装配期抛错；缺项 → `skipped`）；`NETWORK_FETCH` 仍被拒。
2. **Provider 适配器（A02）**：`web_search/adapters/`——`searxng.py`、`brave.py`，**纯函数** `build_request(query, count) -> (path, params, headers)` 与 `parse_response(bytes) -> list[RawHit]`；一律 GET，响应 ≤ 256 KiB、`Content-Type` 必须是 JSON；**不 import 任何网络原语**（P7-I10，AST 判定）；显式配置的 provider 失败**不回退**到别的 provider；无密钥的 provider 不因别的 provider 有密钥而被自动选用。
3. **目的地与传输（A03）**：基址由运维写死（`WATER_AGENT_WEB_SEARCH_BASE_URL`），经 `governed_get`；`allow_private_network` 仅在运维显式 `WATER_AGENT_WEB_SEARCH_ALLOW_PRIVATE=1` 时放行回环与私有（`searxng` 自托管指向内网的场景），**链路本地与云元数据永远拒**（`open_exchange` 既有语义）；禁止目的地零字节、3xx 不跟随、响应上限、Content-Type 校验。
4. **查询出口守卫（A04）**：`web_search/guard.py::QueryEgressGuard`，工具执行前依次检查（SDD §5.4）：长度 ≤ 128 / 单行 / 无控制与零宽字符 / 不含 `http(s)://` 形态 → 经现有工具参数护栏（PII、凭据、注入 → BLOCK）→ **私有上下文回显**（查询与本轮 `upload_context` / `memory_context` / 检索条文 / 图纸审查结果有 ≥ 16 字符公共连续子串 → `WEB_SEARCH_QUERY_REJECTED`；与用户自己消息的重合**不**拒绝）→ 每回合 ≤ 3 次。**良性查询不误拦**是显式指标（≥ 5 条良性查询）。守卫需要读到"本轮私有上下文"——通过工具的**装配期注入**（`WebSearchPlugin` 持有一个 `private_context_source: Callable[[], Sequence[str]]`，由 `AgenticSession` 提供，读 checkpoint 里本轮的 `*_context` 与检索结果），**不得**让模型经参数传入。
5. **结果流水线（A05）**：`web_search/results.py`——逐条：网址须解析为 http(s)、≤ 300 字符、无 userinfo、去 fragment、规范化；调用 S11 的 `wrap_web_content`（标题 ≤ 120、摘要 ≤ 300、注入命中整条丢弃、总量 ≤ 2500）；按规范化网址去重、至多 `count` 条；全部丢弃时返回固定文案"没有可用结果"，**不**透传被丢弃的内容；结果 dict 的 `urls` = 规范化网址列表。
6. **治理（A06）**：`ProviderKind.WEB_SEARCH` / `CallCategory.WEB_SEARCH` / 画像；站点 8 → 9；静态扫描（`web_search/tool.py` 不含原始网络原语，`egress_module` 指向 `mcp/http_transport.py`）；usage 未知；熔断按 provider 主机；`GOVERNANCE_CONFIG_VERSION` 递进。
7. **回答纪律（A07）**：R1–R5（SDD §5.6）——S11 已实现的 R1（网址不进 `cite_clauses` 可追溯集合）、R2、R4（披露行）、R5（污点效应）用**生产 `web_search` 工具**复测；**新增 R3**：只有网络证据的规范性回答不能 `succeeded`（零引用豁免不因搜索而放宽——`web_refs` 不是 `source_id`，回答含规范性结论而无语料引用 → 沿用既有回流一次再失败）。R6 不做。
8. **暴露（A08）**：`ExposureRule(mode="request")` + `decide(request_enabled=…)`；`AgenticSession(web_search=True)` 与 `WebAgenticSessionFactory.create(web_search=…)`（`supports_web_search` 标志，协议型替身不声明就不传该 kwarg——勾选了而工厂不支持直接报错，**不静默忽略**）；最紧组合让位时发 `WEB_SEARCH_NOT_EXPOSED`；schema ≤ 500。
9. **错误模板（A09）**：稳定码 `WEB_SEARCH_UNAVAILABLE` / `_AUTH` / `_RATE_LIMITED` / `_BAD_RESPONSE` / `_QUERY_REJECTED` / `_QUOTA` / `_NOT_EXPOSED`；文本只含码与一句话，**不含**上游正文 / 网址 / 密钥；熔断按 provider 主机；**不回退**。把 S11 的 `EXTERNAL_*` 码映射到 `WEB_SEARCH_*`（一一对应，不泄漏 `EXTERNAL_*`）。
10. **审计与 trace（A10）**：审计 `web_search.performed` 字段白名单；密钥在响应 / 审计 / trace / 日志 / 异常零出现。
11. **Web（A11）**：`MessageRequest.web_search`（`extra="forbid"` 下新增字段，缺省 false）；前端输入框旁的"联网"开关（**默认每条消息都关**、不持久、不随会话记忆）；设置页"联网搜索"子页（只读状态 + 密钥槽位）；`GET /api/system/capabilities` 加联网搜索状态计数（开关 / provider 是否就绪，**不含地址**）。
12. **部署（A12）**：`compose.yml` 透传三个环境变量，**缺省为空 = off**；`WATER_AGENT_WEB_SEARCH_API_KEY` 加入凭据白名单（env > `_FILE` > 凭据卷）。
13. **评测（A13）**：`web_search_p0` + 假服务 + 比较器 + 入仓基线 + `ci_gate.sh`；每个硬指标一条反证；快照 `session_web_search_fake`。
14. **文档**：决策记录 `docs/design/p7-step-12-decision.md`（结构见 `16-p7` §8 第 6 条）；`p2.5-threat-model.md` T15 增补；`p2.5-provider-inventory.md`（第 9 个站点）；16 号规格 §5 / §7 登记 `P7-S12-A01…A14`；17 号 SDD 状态翻转；AGENTS.md 追加条目。

**不做**：任意网址抓取（`web_fetch` / 浏览器 / 截图，D6）；LLM 合成答案类搜索（托管联网、带 `answer` 的 provider）；搜索结果缓存与语义缓存（D7）；关键词 intent 暴露联网（D10 已定逐条勾选）；"计算入参必须出现在用户输入或非网络工具输出里"的来源校验（R-web-num，过于侵入）；面板写搜索服务地址 / provider / 开关；`NETWORK_FETCH` 授予任何主体；声称任何真实搜索服务可用；改 `external/envelope.py` / `external/http_get.py` / `external/taint.py` 的行为（S11 的内核，本步只调用）；改 `cache.py` / `pipeline.py` 的规则；改 `prompting/**`；上调任何预算数字。

## 2. 步骤序列（评测先行；一个逻辑面一个 commit）

0. **Step 0 读原文**（SDD §12 补充 1）：重新实测 `web_search` 的最小 schema 字符数（目标 ≤ 500）与"core + web_search"、"读图 + 材料 + web_search"两个组合的 schema / 指引字符数，写进决策记录 §0.2；用 AST 重新枚举 `open_exchange(` 的调用方确认仍是 4 项；读 S11 决策记录的 D3 / D5 / D12，确认 `web_refs` / `web_tainted` 的现状；**读 searxng 与 brave 的官方 API 文档原文**（响应 JSON 的字段名——**不得凭记忆写适配器**，SDD §12 补充 1），把读到的字段样例逐字记入决策记录；若本机读不到（无网络 / 被拦），适配器按**文档描述的最小子集**写并在决策记录里标 `unverified_against_real_service`；跑 `tool_assembly_snapshot --check` 确认起点干净。
1. **Step 1 冻结**：`data/eval/web_search_p0.jsonl`（~52 条，类别见 harness §4）、指标阈值、比较器骨架。**敌意语料沿用 S11 的 `hostile_web_corpus.jsonl`（不新增、不改）**；查询守卫需要自己的"应拦 / 应放"查询集，写进 `web_search_p0.jsonl` 的 `setup.queries`（应拦 ≥ 9 类、良性 ≥ 5 条）。
2. **Step 2 harness**：`harness/web_search_eval.py`、`tests/fixtures/fake_search_server.py`（模式见 harness §2.1：`ok` · `ok_many` · `empty` · `injection_snippets` · `forged_markers` · `invisible_unicode` · `md_image` · `bad_urls` · `dup_urls` · `oversize` · `wrong_ctype` · `junk_json` · `status_401/403/429/500/503` · `slow` · `redirect_to_loopback` · `redirect_chain` · `flood`；**记录收到的请求数与路径**）；此时机制未实现，红灯基线记入决策记录。
3. **Step 3 机制**（每块先写失败测试，docstring 写验收 ID）：`Capability` + `GRANTABLE` + I06 修订 → `web_search/config.py` → 适配器 → `results.py` → `guard.py` → `tool.py`（`ToolSpec` + `WebSearchPlugin`）→ 治理（kind / category / 画像 / 站点 / 版本）→ `exposure.py` 的 `request` 模式 → `AgenticSession` / 工厂 / `MessageRequest` / `RunManager` 接线 → 审计 → 前端 → 部署。
4. **Step 4 反证与基线**：补齐每个硬指标的反证；生成入仓基线；接 `scripts/ci_gate.sh`；快照 `--write --reason`（只新增 `session_web_search_fake` 一个 profile，其余 19 个 + 三个版本号中除 `TOOL_POLICY_VERSION` 外逐字不变）。
5. **Step 5 收口**：`external_content_p0` 的会话用例改走 `AgenticSession` 并删除 `EVAL_GRAPH_CALLERS` 条目（或写明保留原因）；追溯检查通过；决策记录与文档。

## 3. 验证命令

```bash
export PYTHONPATH=src   # worktree 里必须；并 unset WATER_AGENT_* / *_API_KEY / HTTPS_PROXY / LOCAL_MODEL_*
_venv/bin/pytest -q tests/test_web_search_tool.py tests/test_web_search_adapters.py tests/test_web_search_transport.py tests/test_web_query_guard.py tests/test_web_search_results.py tests/test_web_search_discipline.py tests/test_web_search_exposure.py tests/test_web_search_errors.py tests/test_web_search_audit.py tests/test_web_search_web.py tests/test_web_search_eval.py tests/test_governance_contracts.py tests/test_plugin_invariants.py tests/test_external_content_eval.py tests/test_web_taint.py
_venv/bin/python -m water_agent.harness.web_search_eval data/eval/web_search_p0.jsonl --report-path data/eval/reports/web_search_p0_head.json
_venv/bin/python scripts/compare_web_search_p0.py --head data/eval/reports/web_search_p0_head.json --base data/eval/web_search_p0_main.json
_venv/bin/python -m water_agent.harness.external_content_eval data/eval/external_content_p0.jsonl --report-path data/eval/reports/external_content_p0_head.json   # S11 套件不得回退
_venv/bin/python scripts/compare_external_content_p0.py --head data/eval/reports/external_content_p0_head.json --base data/eval/external_content_p0_main.json
_venv/bin/python -m water_agent.harness.tool_assembly_snapshot --check
(cd web/app && npm test && npm run build)
COMPOSE_PROJECT_NAME=p7s12 bash scripts/ci_gate.sh      # 本机有别的 water-agent 栈在跑时必须隔离
_venv/bin/python scripts/check_step_scope.py docs/execution-prompts/P7-S12-web-search.md --base origin/main
```

另跑 `pytest -o addopts= -q`（ci_gate 的 pytest 是 `-qq`，没有汇总行）取全量计数；`ruff check src`、`mypy src/water_agent` 各自单独看退出码。**全量里才暴露的失败要单独追**——S11 就有 4 个只在全量下才红的真实回归（结构不变量与子进程扫描），单跑通过不等于没问题。

## 4. 受保护断言

- 预算：`assert cost < 4500`、`schema_chars_total == 4497`、系统提示 `< 1950`（本步不改本地工具与提示模板）。
- 信任：`INSTRUCTION_BEARING_SOURCES == frozenset({SourceTag.SYSTEM_POLICY})`。
- 危险能力：`NETWORK_FETCH` 仍在 `DANGEROUS_CAPABILITIES`，`Principal` 构造期仍拒绝授予。
- 缺省不变：不配任何新环境变量时，装配面、系统提示、工具清单、`PER_TURN_RESET` 之外的 state 增量**逐字不变**（现有 19 个快照 profile 逐字相同）。
- **会变的锁定数字 / 语义**（本步唯一允许改的）：`len(PROVIDER_CALL_SITES) == 8` → `== 9`（三处）、`GRANTABLE_CAPABILITIES`（P7-I06）、`settings_nav.test.mjs` 的子页 id 列表、`TOOL_POLICY_VERSION`、`GOVERNANCE_CONFIG_VERSION`、`PINNED_ENV_KEYS`；这些变化都要在决策记录里写明原因。

## 5. 停止条件

满足任一条就停下，写清现象后等确认：

- 现有 19 个快照 profile 或 `MCP_CONFIG_VERSION` / `PROMPT_TEMPLATE_VERSION` 有任何变化；缺省配置（不配 `web_search` 任何环境变量）下任何机制层评测或 `baseline.json` 对比出现差异；
- 需要改 `gateway.py` / `pipeline.py` / `rules.py` / `cache.py` 的规则语义，或改 `external/envelope.py` / `external/http_get.py` / `external/taint.py` 的行为（S11 的内核；发现缺陷也先停下说明，不顺手改）；
- 需要授予 `NETWORK_FETCH`、放宽 `DANGEROUS_CAPABILITIES`、让面板写地址 / provider / 开关，或让模型控制目的地（任何 URL 类参数）；
- 需要上调任何预算上限，或 `web_search` 的 schema 在 Step 0 实测 > 500 且无法在不丢必要语义的前提下压到 500；
- `web_search` 在 core + 1 个 intent 组的常见组合下放不进 4500（说明暴露设计要改，不是调阈值）；
- 适配器的响应字段无法从官方文档原文确认，且无法用"最小子集 + `unverified_against_real_service`"如实表达；
- 全量测试里出现与本步无关的红灯，或本步新增的结构不变量放宽（如再次增加 `EVAL_GRAPH_CALLERS` 之类白名单）而理由不成立；
- 门禁出现与本步无关的红灯。

## 6. 汇报格式

改了哪些文件；跑了哪些验证命令以及实际输出（失败照贴，含全量里才暴露的失败）；范围守卫输出；快照 `--diff`（应只有一个新 profile + `TOOL_POLICY_VERSION`）；Step 0 的 schema 字符数实测；适配器字段的官方文档来源与是否 `unverified_against_real_service`；**未验证项与原因**（真实搜索服务、真实模型是否滥用搜索 / 遵循披露、查询守卫对改写式泄露的有效性、R-web-num）；遗留风险。**不得**写"能拦住任意注入"，**不得**声称任何真实搜索服务可用，**不得**写"查询守卫能防住数据外泄"（只能说"能拦住标注的子串 / 正则可识别的泄露"）。

---

<!-- step-scope -->
```json
{
  "step": "P7-S12",
  "allow": [
    "src/water_agent/web_search/**",
    "src/water_agent/runtime/governance/contracts.py",
    "src/water_agent/runtime/governance/inventory.py",
    "src/water_agent/runtime/governance/config.py",
    "src/water_agent/plugins/base.py",
    "src/water_agent/plugins/builtin.py",
    "src/water_agent/plugins/exposure.py",
    "src/water_agent/plugins/assembly.py",
    "src/water_agent/graphs/agent_rag.py",
    "src/water_agent/runtime/agentic.py",
    "src/water_agent/config/credentials.py",
    "src/water_agent/webapp/run_manager.py",
    "src/water_agent/webapp/run_routes.py",
    "src/water_agent/webapp/settings_routes.py",
    "src/water_agent/webapp/workspace_routes.py",
    "src/water_agent/webapp/auth/audit.py",
    "src/water_agent/harness/web_search_eval.py",
    "src/water_agent/harness/external_content_eval.py",
    "src/water_agent/harness/tool_assembly_snapshot.py",
    "scripts/compare_web_search_p0.py",
    "scripts/ci_gate.sh",
    "compose.yml",
    "data/eval/web_search_p0.jsonl",
    "data/eval/web_search_p0_main.json",
    "data/eval/tool_assembly_main.json",
    "web/app/src/**",
    "web/app/tests/**",
    "tests/fixtures/fake_search_server.py",
    "tests/test_web_search_tool.py",
    "tests/test_web_search_adapters.py",
    "tests/test_web_search_transport.py",
    "tests/test_web_query_guard.py",
    "tests/test_web_search_results.py",
    "tests/test_web_search_discipline.py",
    "tests/test_web_search_exposure.py",
    "tests/test_web_search_errors.py",
    "tests/test_web_search_audit.py",
    "tests/test_web_search_web.py",
    "tests/test_web_search_eval.py",
    "tests/test_governance_contracts.py",
    "tests/test_plugin_invariants.py",
    "tests/test_external_content_eval.py",
    "tests/test_web_taint.py",
    "tests/test_vision_review.py",
    "tests/test_llm_endpoints.py",
    "tests/test_tool_assembly_snapshot.py",
    "tests/test_p7_known_figures.py",
    "tests/test_web_deployment.py",
    "tests/test_web_authorization.py",
    "docs/design/p7-step-12-decision.md",
    "docs/design/17-p7-external-capabilities.md",
    "docs/design/17-p7-external-capabilities-harness.md",
    "docs/design/16-p7-plugin-mcp.md",
    "docs/design/p2.5-threat-model.md",
    "docs/design/p2.5-provider-inventory.md",
    "docs/execution-prompts/P7-S12-web-search.md",
    "AGENTS.md"
  ],
  "forbid": [
    "src/water_agent/runtime/governance/gateway.py",
    "src/water_agent/runtime/governance/pipeline.py",
    "src/water_agent/runtime/governance/rules.py",
    "src/water_agent/runtime/governance/cache.py",
    "src/water_agent/runtime/governance/cache_service.py",
    "src/water_agent/external/**",
    "src/water_agent/mcp/**",
    "src/water_agent/prompting/**",
    "src/water_agent/graphs/confirmation.py",
    "src/water_agent/generation/**",
    "src/water_agent/tools/**",
    "src/water_agent/multi_agent/**",
    "src/water_agent/storage/**",
    "alembic/**",
    "compose.production.yml",
    ".github/**",
    "tests/conftest.py",
    "tests/test_p7_traceability.py",
    "tests/test_check_step_scope.py",
    "tests/test_import_guard.py",
    "tests/test_tool_exposure_eval.py",
    "data/eval/baseline.json",
    "data/eval/tool_exposure_p0.jsonl",
    "data/eval/tool_exposure_p0_main.json",
    "data/eval/skill_p0_main.json",
    "data/eval/mcp_http_p0_main.json",
    "data/eval/model_choice_p0_main.json",
    "data/eval/external_content_p0_main.json",
    "data/eval/hostile_web_corpus.jsonl",
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
      "node": "tests/test_governance_contracts.py::TestSourceTagNotDowngradable::test_only_system_policy_bears_instructions",
      "contains": "assert INSTRUCTION_BEARING_SOURCES == frozenset({SourceTag.SYSTEM_POLICY})"
    },
    {
      "node": "tests/test_p7_known_figures.py::test_local_tool_schema_headroom_is_three_characters",
      "contains": "assert surface[\"schema_chars_total\"] == 4497"
    }
  ],
  "frozen_json": [
    {
      "path": "data/eval/tool_assembly_main.json",
      "pointers": [
        "/versions/MCP_CONFIG_VERSION",
        "/versions/PROMPT_TEMPLATE_VERSION",
        "/profiles/session_[!w]*",
        "/profiles/session_web_member",
        "/profiles/runner_*"
      ]
    }
  ]
}
```
<!-- /step-scope -->

> **范围块说明（写 Prompt 时已逐条核对 main，开工前请再核一遍）**：
>
> - **`external/**` 在 forbid 里**：S11 的内核（`envelope.py` / `http_get.py` / `taint.py`）本步只调用不修改。若发现缺陷，按 §5 先停下说明——S11 的先例是评测发现缺陷后在**同一步**修，但 S12 的内核已合入 main 并被多条不变量锁定，改它需要评审知悉。`tools/**` 同理（`ds_adapters.py` 的 artifact 机制已在 S11 完成；`web_search` 的 `ToolSpec` 定义在 `web_search/tool.py`，**不在 `tools/` 下**）。
> - **`ToolSpec` 定义位置的冲突（需开工前裁决）**：`runtime/governance/inventory.py::scan_tool_spec_names` **只扫描 `tools/` 下的 `ToolSpec(`**（"静态扫描让新增未声明 capability 的工具立即红灯"的前提），而 `plugins/` 里禁止构造 `ToolSpec`（P7-I05）。`web_search/tool.py` 若构造 `ToolSpec`，要么治理清单扫描看不到它（`web_search` 工具没有登记 capability 也不会红），要么把 `scan_tool_spec_names` 的扫描范围扩到 `web_search/`。**推荐**：扩扫描范围（`inventory.py` 在 allow 里，改动限定为"扫描目录集合"一处，并配"在 `web_search/` 放一个未登记 capability 的 `ToolSpec` 即红"的反证）；备选是把 `ToolSpec` 定义放进 `tools/web_search.py`（需要把 `tools/**` 的 forbid 收窄为 `tools/[!w]*`，走补遗 PR）。开工前评审请二选一，Prompt 按推荐写。
> - **`frozen_json` 与 S11 不同**：S11 冻结 `session_*` 全部；本步新增 `session_web_search_fake` 以 `session_w` 开头，所以用 `session_[!w]*` 排除它——**但 `session_[!w]*` 同时会把既有的 `session_web_member` 也排除出冻结**（它也以 `session_w` 开头，写 Prompt 时用快照基线逐个匹配实测发现），所以另列一条精确指针 `/profiles/session_web_member` 把它冻回去；这样既有 19 个 profile 全部冻结，只有新的 `session_web_search_fake` 不在冻结范围内。**`TOOL_POLICY_VERSION` 没有出现在 `frozen_json` 里**——本步需要递进它（新增工具与授权）；`MCP_CONFIG_VERSION` 与 `PROMPT_TEMPLATE_VERSION` 冻结。
> - **`tests/test_vision_review.py` / `tests/test_llm_endpoints.py` 在 allow 里**：两处 `len(PROVIDER_CALL_SITES) == 8` 要改 9（S11 的 forbid 里它们是冻结的，本步不是）。`protect` 里**不再**包含 `== 8` 的断言（它在本步合法地变成 `== 9`）。
> - **`webapp/run_routes.py` / `run_manager.py` / `workspace_routes.py` 在 allow 里**：`MessageRequest.web_search`、`WebAgenticSessionFactory.create(web_search=…)` 与 `supports_web_search` 标志、能力端点计数。S11 的 forbid 里 `webapp/**` 本步解除。`storage/**` 与 `alembic/**` 仍 forbid——**`web_search` 不落库**（它是逐条消息的瞬时标志，不进 `runs` 表、不进会话默认；与 S15 的模型选择刻意不同）。
> - **`data/eval/external_content_p0_main.json` 在 forbid 里**：S11 的基线不得改。`external_content_p0` 的会话用例若改走 `AgenticSession`（见 §0 倒数第三行），**报告必须逐字不变**——评测改道不应改变任何用例的判定结果；若报告变了，说明改道改变了被测行为，按 §5 停下。`harness/external_content_eval.py` 在 allow 里仅为这一处改道。
> - **`config/credentials.py` 在 allow 里**：只为把 `WATER_AGENT_WEB_SEARCH_API_KEY` 加入凭据白名单（env > `_FILE` > 凭据卷，与 `WATER_AGENT_LLM_API_KEY` 同构）。
> - **`plugins/assembly.py` 在 allow 里**：`assemble_tools` 要把 `request_enabled` 交给 `ToolExposurePolicy`、并在 `web_search=True` 时追加 `WEB_SEARCH` 能力；`GRANTABLE_CAPABILITIES` 在 `plugins/base.py`。改动不得改变不传 `web_search` 时的任何行为。
