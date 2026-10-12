# P7-S10 执行 Prompt：OpenAI 兼容端点可配置与关键词检索模式（Step G）

> **工作区**：仓库 `YEKYYY0312/water-SAD-agent`，基于最新 `main`（必须已包含本 Prompt）。新建分支 `YEK/p7-s10-openai-compatible-endpoints`。
> **必读**：
> - `AGENTS.md`（尤其"架构现状"的 P2.5 治理层、动态提示装配、`DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY` 三层解析、容器冒烟、P5 Step 46 本地模型兼容性探针几条）
> - `docs/design/16-p7-plugin-mcp.md`：§4 不变量、§5、§6 防漂移 harness、§7 验收表格式、§8 实施协议
> - `docs/design/12-p2.5-production-governance.md` 与 `docs/design/p2.5-provider-inventory.md`（出站清单、`ProviderGateway`、价格与预算口径）
> - `docs/design/08-ingestion.md`（入库与 `index_version`）、`docs/design/p5-step-46-decision.md`（本地模型兼容性探针与"没有证据就不启用"）
> - `docs/design/p2.5-threat-model.md` T3 / T5 / T7 / T11

---

## 0. 相对规格的修订与本步决定（以本文为准）

S10 是 2026-10-12 新增的一步。它不属于"工具与 MCP"，但用户决定归入 P7 以复用 `p7-scope` 范围守卫、追溯检查与装配快照（不另开一期、不改 CI）。用户的诉求：**对话模型改成任意 OpenAI 兼容端点；只填一个 LLM API 也能用 RAG**。用户的三个决定：**加关键词检索模式**、**embedding 端点也做成可配置（换模型必须重建索引）**、**换对话模型时兼容性探针是硬门槛**。

读码事实（写 Prompt 时实测，2026-10-12，main `932d5f6`）：

- 协议层本来就是 OpenAI 兼容：对话走 `ChatOpenAI(base_url=…)`（`generation/chat_model.py`）与 `urllib` → `/chat/completions`（`generation/llm.py`）；embedding 走百炼 `compatible-mode/v1/embeddings`；视觉审阅走 `compatible-mode/v1/chat/completions`。CLI 已有 `--model` / `--base-url` / `--api-key-env`。
- 写死的是配置：Web 路径固定 `MODEL_NAME` / `https://api.deepseek.com` / `DEEPSEEK_API_KEY`（`webapp/run_manager.py:284-286`）；"模型已配置"只看 `DEEPSEEK_API_KEY`（`runtime/agentic.py::real_llm_configured`）；embedding 固定 `MODEL_NAME` + `DASHSCOPE_API_KEY`，`BailianEmbeddingConfig.__post_init__` 把 `index_version` 锁成 `<model>__<dim>__curated-v1`；Web 预检在有真实索引时要求 embedding 凭据（`webapp/run_routes.py`，`WEB_EMBEDDING_NOT_CONFIGURED`）；`scripts/build_index.py` 建库必须调用百炼 embedding。
- **纯关键词检索的实测**（真实索引 `qwen3.7__512__curated.db` 的副本，零向量 + `search_hybrid(hybrid_alpha=0.0)`，`rag_p1` 58 条 gold）：Recall@1 84.48%、**Recall@5 98.28%**、MRR 90.80%（混合检索的入仓口径是 Recall@5 100%、MRR 92.24%）；唯一漏召回 `rag-p1-0002`"分流制与合流制分别指什么？"。两句手写改写里口语化的"雨污要不要分开排？新区怎么定"漏召回。**rag_p1 的问法与条文措辞高度同构，对关键词检索偏乐观**——不能拿 98% 当"关键词模式够用"的证据，见 A07 的口语化评测集。
- rerank 走百炼**原生**端点（`/api/v1/services/rerank/...`），OpenAI 没有 rerank 标准；视觉审阅依赖具体服务商的图片输入。

| 疑问 | 本步做法 | 原因 |
|---|---|---|
| 对话端点怎么配 | 新增运维配置三项：`WATER_AGENT_LLM_BASE_URL`、`WATER_AGENT_LLM_MODEL`、密钥 `WATER_AGENT_LLM_API_KEY`（进 `config/credentials.py::CREDENTIALS` 白名单，env > `_FILE` > 凭据卷三层，与现有两把 Key 同一套）。**解析单一来源** `generation/endpoints.py::resolve_chat_endpoint()`：返回 `ChatEndpoint(base_url, model, api_key_name, source)`，CLI / Web / runner / docgen 润色 / judge / 兼容性探针**全部**经它取值（替换 `run_manager.py` 的写死值、`cli.py` 的默认值来源、`real_llm_configured` 的单 Key 判断）。缺省（三项都没设）= 现在的 DeepSeek，**已有部署零变化** | 用户要"只填一个 LLM API"；单一来源防止"预检认 A、真跑用 B"（AGENTS.md 里 chat / embedding 预检都踩过） |
| 旧变量兼容 | `WATER_AGENT_LLM_API_KEY` 未设时回落 `DEEPSEEK_API_KEY`（三层解析照旧）；`WATER_AGENT_LLM_BASE_URL` 未设时取 `https://api.deepseek.com`、`WATER_AGENT_LLM_MODEL` 未设时取 `MODEL_NAME`。**只要设了 `WATER_AGENT_LLM_BASE_URL` 指向别处而没设 `WATER_AGENT_LLM_API_KEY`**，不回落 `DEEPSEEK_API_KEY`（否则会把 DeepSeek 的 Key 发给别的服务商）→ 预检 424 `WEB_MODEL_NOT_CONFIGURED`，detail 写明缺哪一项 | 密钥只能发给它所属的服务商 |
| 本地模型路径 | `LOCAL_MODEL_*`（P5 Step 46）保留不删、语义不变；它与 `WATER_AGENT_LLM_*` **同时启用即配置错误**（启动期 `ValueError`，说明二选一）。兼容性门槛两条路径共用同一个实现（见下） | 不删旧路径（AGENTS.md"未完成功能用特性开关护住，不删旧路径"）；两套同时生效就有歧义 |
| 兼容性硬门槛 | 复用 `harness/local_model_compat.py` 的检查集与 `LocalModelCompatibilityReport`，泛化为 `generation/compatibility.py`：**非缺省端点**（`base_url` 或 `model` 与 DeepSeek 缺省值不同）必须有一份 `endpoint` + `model` 精确匹配、全部检查 `passed` 的报告（`WATER_AGENT_LLM_COMPATIBILITY_REPORT` 指向它）才允许装配；否则装配期拒绝、Web 预检 424 `WEB_MODEL_NOT_VERIFIED`（新错误码，detail 写"运行 `python -m water_agent.harness.endpoint_compat` 生成报告"）。**缺省 DeepSeek 端点免检**（现有质量证据就是在它上面取得的，rag_p1 real 84.48%）。检查项：`function_calling`、`structured_citations`、`programmatic_calculation`、`tool_schema_budget`（现有四项，真实调用）＋ `streaming_usage`（新：流式末块带 usage，否则预算按 unknown 结清）＋ `error_handling`（新：发一个故意错误的模型名，必须得到结构化失败而不是挂起）；`memory_p0` 是离线套件、与端点无关，从远程端点探针里**去掉**（本地模型路径照旧保留，不改它的检查集）；`cancellation_timeout` 远程端点改为真实测量（发一次流式请求、首块后取消，断言连接在 deadline 内关闭），不再恒 `blocked` | 用户选硬门槛；铁律②依赖模型真的用 function calling 调 `cite_clauses`，弱模型会在引用校验上静默失败 |
| 探针本身 | `python -m water_agent.harness.endpoint_compat --out <path>`：读 `resolve_chat_endpoint()`，**会产生少量计费调用**（命令行与面板都明示）；报告里不写 Key、不写请求原文，只写检查结果、端点、模型、探针版本、时间；报告有 `probe_version`，探针检查集变化时旧报告失效 | 证据可复现、不泄漏 |
| Web 面板 | 设置页"模型与密钥"新增"对话模型端点"卡片：`base_url`、`model`（管理员 + CSRF，写进凭据卷旁边的**非密钥**配置文件 `<credentials_dir>/llm_endpoint.json`，0600；运维 env 设了则面板只读并标"由部署配置"）、Key 槽位 `WATER_AGENT_LLM_API_KEY`（只写不读，同现有两把）、兼容性状态（已验证 / 未验证 / 缺省免检）与"运行兼容性检查"按钮（管理员，异步，结果写报告文件后刷新状态；同一时间只跑一个）。**base_url 只接受 `https://`**（`http://localhost` / `http://127.0.0.1` / compose 服务名 `http://local-model:` 例外），不得含 userinfo / fragment；改 base_url 或 model 即作废旧兼容报告 | 面板能写端点就等于能决定把用户问题与检索条文发到哪——与 S6"面板改不了 command"同一级风险，所以要求管理员、只接受 https、改了就必须重新验证 |
| 治理 | **不新增 `ProviderKind`**、不改名：`ProviderKind.DEEPSEEK_CHAT` 的语义扩为"OpenAI 兼容对话端点"（注释与 `p2.5-provider-inventory.md` 同步改写；限流 / 熔断 / 预算键里已含 `model`，换模型自然分桶）；出站清单数量**不变**（还是同一个出站模块、同一个调用点）；`ProviderProfile` 超时可由 `WATER_AGENT_LLM_TIMEOUT_SECONDS` 覆盖（缺省 60）。价格：未登记的模型成本记 `None`（不猜价，现状）；新增运维可选 `WATER_AGENT_LLM_PRICE`（`input_per_1m,output_per_1m[,cached_input_per_1m]`，`confirmed=False`，只用于显示与软预警，**不做硬拒绝**——与现有"未核对价格不做硬上限"同一口径）；`GOVERNANCE_CONFIG_VERSION` 递进 | 改 `ProviderKind` 名要动约 15 个源文件、39 个测试文件、多条锁定断言与快照，收益只是名字好看 |
| 缓存 | 模型名本来就是缓存键失效因子；`base_url` 也加进去（`CacheKeyFactors.model_endpoint_digest`，空串时与历史键同键——同 S9 的 `skill_library_digest` 做法）。缺省端点下键逐字不变 | 同名模型在不同服务商那里是不同的模型 |
| embedding 端点 | 对称三项：`WATER_AGENT_EMBEDDING_BASE_URL`、`WATER_AGENT_EMBEDDING_MODEL`、密钥 `WATER_AGENT_EMBEDDING_API_KEY`（未设回落 `DASHSCOPE_API_KEY`，规则同对话端点：base_url 改了就不回落）；`dimensions` 由索引决定。缺省 = 现在的百炼 `MODEL_NAME`。`BailianEmbeddingConfig` 的 `index_version` 校验改为 `<model>__<dim>__curated-v1` 对**任意 model** 成立（不再只认百炼模型名） | 用户选"可配置、换模型必须重建索引" |
| 索引与 embedding 模型绑定 | 建库时把 `embedding_model`、`embedding_base_url` 的规范化主机名、`dimensions`、`index_version`、`retrieval_modes`（见下）写进索引旁的 `<db>.index_meta.json`（新文件，随 `data/indexes/` 一起进镜像，`.dockerignore` 已整目录放行）。打开索引时比对当前 embedding 配置：**模型或维度不一致 → 拒绝向量检索**（`RETRIEVAL_EMBEDDING_MISMATCH`），按下面的模式规则降级到关键词模式**并显式标出**，或在 `WATER_AGENT_RETRIEVAL_MODE=hybrid` 强制时直接失败；没有 `index_meta.json` 的旧索引按现在的百炼缺省模型对待（向后兼容），并在运行状态里写 `index_meta=absent` | 换了 embedding 模型还用旧向量，检索会**静默**变差且没有任何报错——这是本步最危险的失败模式 |
| 检索模式 | 新增 `WATER_AGENT_RETRIEVAL_MODE=auto\|hybrid\|text`（缺省 `auto`）：`auto` = embedding 已配置且与索引匹配 → `hybrid`（现状，`hybrid_alpha=0.7`）；否则 → `text`（关键词 / BM25，`search_hybrid(零向量, hybrid_alpha=0.0)`，**不调用任何 embedding**）。`hybrid` = 强制混合，缺 embedding 或不匹配即失败；`text` = 强制关键词。`TriviumRetriever.runtime_status` 新增 `retrieval_mode`（`hybrid` / `text`）与 `retrieval_mode_reason`（`embedding_not_configured` / `embedding_mismatch` / `forced`），进 trace、进 Web run 结果投影（`_result_payload` 新增 `retrieval_mode`）、前端检查面板显示"检索：关键词模式（未配置向量模型）"。**text 模式下不调 rerank**（rerank 需要百炼 Key；有 Key 时 rerank 照常可用，与检索模式无关——rerank 本来就是可选项） | 用户要"只填一个 LLM API 也能用 RAG"；仓库纪律"不得把 BM25 回退宣称为语义检索成功"（`10-sdd-harness.md`）要求模式**可见** |
| Web 预检 | `WEB_EMBEDDING_NOT_CONFIGURED` 只在 `WATER_AGENT_RETRIEVAL_MODE=hybrid` 时出现；`auto` 下缺 embedding 不再 424，而是走 text 模式。`tests/test_web_runs.py::test_no_embedding_key_returns_its_own_precondition_failure` 的场景改为"强制 hybrid 时"（docstring 记一行原因），并新增"auto 时缺 embedding → 202 + `retrieval_mode=text`"用例 | 预检的语义从"必须有 embedding"变为"必须能检索" |
| 纯关键词建库 | `scripts/build_index.py --text-only`：不调 embedding、不要求任何 Key，向量写零向量（维度用 `--dimensions`，缺省 512），`index_meta.json` 写 `retrieval_modes: ["text"]`；这种索引在 `auto` 下永远走 text、`hybrid` 强制时失败（`RETRIEVAL_EMBEDDING_MISMATCH`）。现有"禁止本地 embedding 兜底"不变（text-only 不是"本地 embedding"，是不做 embedding） | 只有一个 LLM Key 的部署也能新增 / 更新规范库 |
| 入仓基线与默认行为 | harness / 单测的默认检索（fixture、`mock_embed`）**不变**；`baseline.json` 的 rag_p0 等不受影响。新增检索评测见 A07 | 基线漂移是硬红线 |
| 文案 | 设置面板"对话生成（DeepSeek）"改为"对话模型（OpenAI 兼容端点）"、"向量检索与重排（阿里云百炼）"改为"向量检索（OpenAI 兼容 embedding 端点）"＋"重排（阿里云百炼，可选）"；`DASHSCOPE_API_KEY` 的说明写明它同时服务重排与 PDF 视觉审阅 | 文案与能力一致 |

---

## 1. 目标与边界

**做**（编号对应规格 §7 的 P7-S10-Axx）：

1. **评测先行（A07 前半）**：新建 `data/eval/retrieval_colloquial_p0.jsonl`（≥ 40 条：每条 gold 条款配一句**口语化 / 同义改写**问法，至少一半与条文措辞的字面重合度低；由与 rag_p1 不同的写法产出，**不得**从条文原文复制关键短语——写完用脚本计算每条与 gold 条文的字符二元组重合率并入报告）与 `harness/retrieval_mode_eval.py`：对同一索引分别跑 `hybrid` 与 `text` 两种模式，输出 Recall@1/5、MRR、逐条命中与重合率；**没有百炼 Key 时 hybrid 模式结构化 SKIP、text 模式照常跑**（text 不需要任何 Key）。在未改动的检索代码上用"零向量 + alpha=0.0"跑出 text 模式的"改之前"报告，数据集此后冻结。
2. **端点解析（A01）**：`generation/endpoints.py`（`ChatEndpoint` / `EmbeddingEndpoint` / `resolve_chat_endpoint` / `resolve_embedding_endpoint`），回落与"不跨服务商回落"规则；`config/credentials.py::CREDENTIALS` 加两把 Key；全部调用方改走它。
3. **兼容性门槛（A02）**：`generation/compatibility.py`（从 `local_model.py` 抽出报告模型与"精确匹配 + 全部 passed"判定，本地模型路径改为调用它，语义不变）；`harness/endpoint_compat.py`；装配期与 Web 预检的门槛；`WEB_MODEL_NOT_VERIFIED`。
4. **治理与缓存（A03）**：`ProviderKind.DEEPSEEK_CHAT` 注释与清单文档改写；超时与价格覆盖；`CacheKeyFactors.model_endpoint_digest`；`GOVERNANCE_CONFIG_VERSION` 递进。
5. **embedding 端点与索引绑定（A04）**：`embedding.py` 改为任意 OpenAI 兼容 embedding 端点；`index_meta.json` 写入与校验；`RETRIEVAL_EMBEDDING_MISMATCH`。
6. **检索模式（A05）**：`WATER_AGENT_RETRIEVAL_MODE`、`TriviumRetriever` 的 text 路径（不调 embedding）、`runtime_status.retrieval_mode` / `_reason`、trace、Web 结果投影、Web 预检改语义。
7. **纯关键词建库（A06）**：`scripts/build_index.py --text-only`。
8. **Web 面板（A08）**：`webapp/settings_routes.py` 新增端点配置的读写（`GET/PUT /api/settings/llm-endpoint`、`POST /api/settings/llm-endpoint/compatibility`）；前端 `SettingsPanel.tsx` / `settingsApi.ts` / `settingsState.ts` 与检查面板的检索模式显示；`web/app/tests/settings.test.mjs` 补用例；审计 `settings.llm_endpoint_updated` / `settings.llm_compatibility_checked`（只记端点主机名、模型名、结果码，**不记 Key**）。
9. **部署（A09）**：`compose.yml` 给 app 透传 `WATER_AGENT_LLM_*` / `WATER_AGENT_EMBEDDING_*` / `WATER_AGENT_RETRIEVAL_MODE`（缺省为空 = 现状）；冒烟 `webapp/smoke.py` 的 agentic 探针断言 `retrieval_mode` 字段存在且与配置一致（冒烟镜像独立，**两个镜像都要重建**，见 §11）。
10. **快照（A10）**：`PINNED_ENV_KEYS` 加本步全部新变量；现有 16 个 profile 的 surface 与 lifecycle **逐字不变**（缺省端点、auto 模式在 fixture 语料上不改变任何装配面），只允许 `/versions/...` 里本步递进的版本号变化（若有）；新增 profile `session_endpoint_custom`（非缺省端点 + 一份测试用兼容报告，断言 `chat_model` 指向配置的端点与模型）。

**不做**：

- 改 `ProviderKind` / `CallCategory` 名或新增出站模块；改 rerank / 视觉审阅的端点（仍是百炼）。
- 本地 embedding 模型（bge 等）；自动 query 改写模块（agentic 模式下检索词由模型自己写，**是否需要额外的改写由 A07 的评测结果决定，不在本步做**）。
- 多端点路由 / 负载均衡 / 按用户选模型；面板写 rerank 或视觉审阅端点。
- 放宽兼容性门槛（缺省 DeepSeek 免检是唯一例外，不得扩大）。
- 删除 `LOCAL_MODEL_*` 路径；把 text 模式宣称为语义检索。

## 2. 铁律与治理落点（不得违反）

- **铁律②不被换模型绕过**：非缺省端点没有通过兼容性报告就不能装配；`cite_clauses` + `validate_citations` 原样生效。
- **检索模式可见**：任何时候 `retrieval_mode` 都在运行状态、trace 与 Web 结果里如实出现；text 模式不得出现"语义检索"字样；embedding 与索引不匹配不得静默继续向量检索。
- **密钥只发给所属服务商**：端点改了就不回落旧 Key；Key 只写不读、不进日志 / trace / 审计 / 报告。
- **出口受控**：面板写端点只接受 `https://`（及列明的本机例外），管理员 + CSRF，改了即作废兼容报告；所有对话 / embedding 调用仍经 `ProviderGateway`。
- **评测先行**：`retrieval_colloquial_p0` 在 Step 1 冻结；每条验收先写失败测试（docstring 写验收 ID）。
- **基线不漂**：缺省配置下现有全部机制层评测与 `baseline.json` 逐字不变。

## 3. 必须复用的现有组件（不得另写一份）

| 需要 | 用这个 | 位置 |
|---|---|---|
| 密钥三层解析 | `resolve_secret` / `CREDENTIALS` 白名单 / 凭据卷 | `config/credentials.py` |
| 对话模型构造 | `build_chat_model`（只改它的参数来源，不另写构造器） | `generation/chat_model.py` |
| 兼容性检查与报告 | `local_model_compat._real_checks` / `LocalModelCompatibilityReport`（抽出共用，不复制） | `harness/local_model_compat.py`、`generation/local_model.py` |
| 远程出口治理 | `ProviderGateway` / `ProviderProfile` / `PROVIDER_CALL_SITES` | `runtime/governance/` |
| 缓存键 | `CacheKeyFactors`（加字段，空串不进键） | `runtime/governance/cache.py` |
| 检索 | `TriviumRetriever` / `resolve_index_path` / `search_hybrid` | `retrieval/trivium.py` |
| 入库 | `ingest_to_trivium` / `build_index.py` | `ingestion/pipeline.py`、`scripts/` |
| Web 设置面板 | `settings_routes.py` 的鉴权与"只写不读"写法 | `webapp/settings_routes.py` |
| 审计 | `_emit` 风格事件 | `webapp/auth/audit.py` |
| 检索指标 | `harness/retrieval_metrics.py`（Recall@k / MRR，**不得**另写一份） | `harness/` |

## 4. 实施步骤（一步一个 commit；每个 commit 结束时快照 `--check` 必须一致或按本节写明的变化 `--write --reason`）

### Step 1：评测集冻结（A07 前半）
`data/eval/retrieval_colloquial_p0.jsonl` + `harness/retrieval_mode_eval.py`；改之前的 text 模式报告（零向量 + alpha=0.0 在未改动代码上跑）与 hybrid 模式报告（有 Key 时跑，没有就结构化 SKIP 并在决策记录写明）。数据集冻结。

### Step 2：端点解析（A01）
`generation/endpoints.py` + 凭据白名单两把新 Key；CLI / Web / runner / docgen / judge 改走它；缺省行为逐字不变（快照 `--check` 一致）。`tests/test_web_settings_api.py::test_unknown_credential_name_is_rejected` 用的是 `OPENAI_API_KEY`，与新白名单不冲突，不改。

### Step 3：兼容性门槛（A02）
`generation/compatibility.py` + `harness/endpoint_compat.py` + 装配期与预检门槛 + `WEB_MODEL_NOT_VERIFIED`；本地模型路径改用共用实现，`tests/test_step46_local_model.py` 照旧通过。

### Step 4：治理与缓存（A03）
注释与清单文档、超时 / 价格覆盖、`model_endpoint_digest`、`GOVERNANCE_CONFIG_VERSION`。

### Step 5：embedding 端点与索引绑定（A04）
`embedding.py`、`index_meta.json`（入库写、打开时校验）、`RETRIEVAL_EMBEDDING_MISMATCH`；`data/indexes/` 整目录不入库（`.gitignore` 第 36 行），真实索引的 `index_meta.json` 由 `build_index.py` 与索引一起产出、随镜像分发；本机已有的两份索引用一次性命令补写（命令写进决策记录），代码对**缺失** `index_meta.json` 的旧索引按百炼缺省模型处理（向后兼容）。入仓的只有测试用 fixture 小库现建时产出的 meta（在 `tmp_path` 里，不落仓库）。

### Step 6：检索模式（A05）
`WATER_AGENT_RETRIEVAL_MODE`、text 路径、运行状态与投影、Web 预检改语义、`test_web_runs.py` 两条用例按 §0 调整。

### Step 7：纯关键词建库（A06）
`build_index.py --text-only` + 测试（建一个 fixture 小库、断言零 embedding 调用、`auto` 下走 text、`hybrid` 强制失败）。

### Step 8：Web 面板（A08）
后端路由、前端、审计、前端单测。

### Step 9：部署与冒烟（A09）
compose 透传、冒烟断言、生产 overlay 解析。

### Step 10：快照与评测接线（A07 后半、A10）
新 profile、`PINNED_ENV_KEYS`；`retrieval_colloquial_p0` 的 text 模式接进 `ci_gate.sh`（离线可跑），入仓基线 `data/eval/retrieval_colloquial_p0_main.json` + `scripts/compare_retrieval_colloquial_p0.py`（照 `compare_skill_p0.py`：拒手改、数据集摘要、PASS_TO_PASS、用例消失、指标不降）。hybrid 模式不进门禁（需要 Key），在决策记录里贴有 Key 时的实测。

### Step 11：收尾
规格 §3 新增 3.11 端点与检索模式、§5 S10 行与 §7 S10 验收翻成 delivered；`p2.5-provider-inventory.md` 同步；威胁模型补 T14（面板可写对话端点 = 决定数据发往何处；换模型的能力退化；embedding 与索引不匹配的静默劣化）；`docs/design/p7-step-10-decision.md`（含两种模式在 `rag_p1` 与 `retrieval_colloquial_p0` 上的指标对照、兼容性报告样例、快照 `--diff` 原文）；AGENTS.md 补一条（明确"只配一个 LLM Key 时检索是关键词模式、质量证据见决策记录"）。

## 5. 范围块

<!-- step-scope -->
```json
{
  "step": "P7-S10",
  "allow": [
    "src/water_agent/generation/endpoints.py",
    "src/water_agent/generation/compatibility.py",
    "src/water_agent/generation/chat_model.py",
    "src/water_agent/generation/llm.py",
    "src/water_agent/generation/local_model.py",
    "src/water_agent/generation/cost.py",
    "src/water_agent/generation/governed_chat.py",
    "src/water_agent/embedding.py",
    "src/water_agent/retrieval/trivium.py",
    "src/water_agent/ingestion/pipeline.py",
    "src/water_agent/config/credentials.py",
    "src/water_agent/runtime/agentic.py",
    "src/water_agent/runtime/governance/config.py",
    "src/water_agent/runtime/governance/cache.py",
    "src/water_agent/runtime/governance/cache_service.py",
    "src/water_agent/runtime/governance/inventory.py",
    "src/water_agent/cli.py",
    "src/water_agent/docgen/quality.py",
    "src/water_agent/harness/llm_judge.py",
    "src/water_agent/harness/runner.py",
    "src/water_agent/harness/local_model_compat.py",
    "src/water_agent/harness/endpoint_compat.py",
    "src/water_agent/harness/retrieval_mode_eval.py",
    "src/water_agent/harness/tool_assembly_snapshot.py",
    "src/water_agent/webapp/run_manager.py",
    "src/water_agent/webapp/run_routes.py",
    "src/water_agent/webapp/settings_routes.py",
    "src/water_agent/webapp/app.py",
    "src/water_agent/webapp/smoke.py",
    "src/water_agent/webapp/auth/audit.py",
    "scripts/build_index.py",
    "scripts/compare_retrieval_colloquial_p0.py",
    "scripts/ci_gate.sh",
    "compose.yml",
    "data/eval/retrieval_colloquial_p0.jsonl",
    "data/eval/retrieval_colloquial_p0_main.json",
    "data/eval/tool_assembly_main.json",
    "web/app/src/**",
    "web/app/tests/**",
    "tests/fixtures/endpoint_compat/**",
    "tests/test_llm_endpoints.py",
    "tests/test_endpoint_compatibility.py",
    "tests/test_embedding_endpoint.py",
    "tests/test_retrieval_mode.py",
    "tests/test_build_index_text_only.py",
    "tests/test_retrieval_mode_eval.py",
    "tests/test_web_llm_endpoint.py",
    "tests/test_web_runs.py",
    "tests/test_web_settings_api.py",
    "tests/test_credentials_resolution.py",
    "tests/test_embedding_provider.py",
    "tests/test_answer_cache.py",
    "tests/test_step46_local_model.py",
    "tests/test_retrieval_metrics.py",
    "tests/test_container_smoke.py",
    "tests/test_web_deployment.py",
    "tests/test_index_selection.py",
    "tests/test_p7_known_figures.py",
    "tests/test_tool_assembly_snapshot.py",
    "docs/design/p7-step-10-decision.md",
    "docs/design/16-p7-plugin-mcp.md",
    "docs/design/p2.5-threat-model.md",
    "docs/design/p2.5-provider-inventory.md",
    "docs/design/08-ingestion.md",
    "docs/execution-prompts/P7-S10-openai-compatible-endpoints.md",
    "AGENTS.md"
  ],
  "forbid": [
    "src/water_agent/runtime/governance/gateway.py",
    "src/water_agent/runtime/governance/pipeline.py",
    "src/water_agent/runtime/governance/rules.py",
    "src/water_agent/runtime/governance/contracts.py",
    "src/water_agent/retrieval/rerank.py",
    "src/water_agent/vision.py",
    "src/water_agent/graphs/**",
    "src/water_agent/prompting/**",
    "src/water_agent/plugins/**",
    "src/water_agent/tools/**",
    "src/water_agent/skills/**",
    "src/water_agent/mcp/**",
    "src/water_agent/multi_agent/**",
    "src/water_agent/storage/**",
    "alembic/**",
    "alembic.ini",
    "pyproject.toml",
    "uv.lock",
    ".github/**",
    "Dockerfile",
    "compose.production.yml",
    "tests/conftest.py",
    "tests/test_p7_traceability.py",
    "tests/test_check_step_scope.py",
    "tests/test_import_guard.py",
    "tests/test_tool_policy.py",
    "tests/test_governance_contracts.py",
    "data/eval/baseline.json",
    "data/eval/rag_p0.jsonl",
    "data/eval/rag_p1.jsonl",
    "data/eval/tool_exposure_p0.jsonl",
    "data/eval/tool_exposure_p0_main.json",
    "data/eval/skill_p0.jsonl",
    "data/eval/skill_p0_main.json",
    "data/eval/memory_p0_main.json",
    "data/indexes/*.db",
    "data/indexes/*.vec",
    "data/indexes/*.text",
    "data/curated/**"
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
      "node": "tests/test_governance_contracts.py::TestInventoryCoverage::test_no_ungoverned_remote_egress",
      "contains": "assert missing == frozenset()"
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
      "node": "tests/test_web_settings_api.py::test_unknown_credential_name_is_rejected",
      "contains": "assert response.json()[\"error\"][\"code\"] == \"WEB_CREDENTIAL_UNKNOWN\""
    }
  ],
  "frozen_json": [
    {
      "path": "data/eval/tool_assembly_main.json",
      "pointers": [
        "/versions/MCP_CONFIG_VERSION",
        "/versions/TOOL_POLICY_VERSION",
        "/versions/PROMPT_TEMPLATE_VERSION",
        "/profiles/session_[!e]*",
        "/profiles/runner_*"
      ]
    }
  ]
}
```
<!-- /step-scope -->

> `frozen_json` 说明：现有 16 个 profile（11 个 `session_*` + 5 个 `runner_*`）**整段**冻结、三个版本号都冻结；本步新增的 `session_endpoint_custom` 以 `session_e` 开头，不被 `session_[!e]*` 匹配（现有会话侧 profile 没有以 `session_e` 开头的——写 Prompt 时已用范围守卫的展开函数核对：`session_[!e]*` 匹配 11 个、`runner_*` 匹配 5 个）。**本步不得改变任何现有 profile**：缺省端点 + auto 模式在 fixture 语料上不应改变装配面；若实测必须变化（例如快照记录的 chat model 参数），先停下说明。
>
> `tests/test_governance_contracts.py` 在 forbid：出站清单数量与工具数都不变（本步不新增出站模块、不新增工具）。
>
> **与 S8（Streamable HTTP）的先后**：两步互不依赖，谁先合入都行，但会在 `compose.yml`、`data/eval/tool_assembly_main.json`、`harness/tool_assembly_snapshot.py::PINNED_ENV_KEYS`、`runtime/governance/config.py::GOVERNANCE_CONFIG_VERSION`、`scripts/ci_gate.sh`、`webapp/auth/audit.py`、威胁模型（S8 是 T13、本步是 T14）、AGENTS.md 上产生文本冲突——后合的那一步 merge main 解决冲突即可（不 rebase、不 force-push）。若 S8 先合入：现有 profile 是 17 个（多 `session_http_mcp_fake`，以 `session_h` 开头，被本步的 `session_[!e]*` 冻结——正确，本步不得改它）；`provider_call_sites` 是 8、本步仍不改它；`MCP_CONFIG_VERSION` 是 S8 递进后的值、本步冻结不改。S8 的冻结规则 `session_[!h]*` 同样会冻结本步新增的 `session_endpoint_custom`（若本步先合入），两边都成立。

## 6. 受保护断言

- 预算：`assert cost < 4500`、`schema_chars_total == 4497`、系统提示 `< 1950`（本步不碰工具与提示）。
- 信任：`INSTRUCTION_BEARING_SOURCES == {SYSTEM_POLICY}`。
- 出站扫描：`test_no_ungoverned_remote_egress` 不变；`provider_call_sites` 仍是 7（S8 合入后是 8，本步不改它）。
- 确认门无凭证拒绝（S1 / S7）。
- 凭据白名单：未登记的名字（如 `OPENAI_API_KEY`）仍 404 `WEB_CREDENTIAL_UNKNOWN`。
- **会变的锁定数字 / 语义**（本步唯一允许改的）：`tests/test_web_runs.py::test_no_embedding_key_returns_its_own_precondition_failure` 的前提改为"强制 hybrid 模式时"（docstring 记一行原因）；`tests/test_embedding_provider.py` 里锁"只认百炼模型名"的断言（若有）改为"index_version 与模型、维度一致"；设置面板文案的前端测试字面量。

## 7. 停止条件

满足任一条就停下，写清现象后等确认：

- 现有 16 个 profile 有任何变化；缺省配置下任何机制层评测或 `baseline.json` 对比出现差异；
- 需要改 `gateway.py` / `pipeline.py` / `rules.py` / `contracts.py` / `graphs/` / `prompting/` / `tools/` / `plugins/` / `mcp/`，或需要新依赖、数据库迁移；
- 需要新增 `ProviderKind` 或出站模块；
- 兼容性探针在缺省 DeepSeek 端点上（有 Key 时）不能全部 `passed`——说明探针本身有问题，不得通过放宽检查解决；
- `retrieval_colloquial_p0` 的 text 模式 Recall@5 低于 80%：**不阻断本步交付**，但必须在决策记录与 AGENTS.md 里如实写明"只配 LLM Key 时口语化问法召回率为 X%"，并在面板的检索模式提示里写"未配置向量模型时，口语化问法可能检索不到"；
- 门禁出现与本步无关的红灯。

## 8. 验证命令（完成前全部跑过，贴出输出）

```bash
export VENV=/Users/yaerken/Desktop/给排水agent/_venv PYTHONPATH=src
unset WATER_AGENT_MCP_OVERLAY WATER_AGENT_MCP_CONFIG WATER_AGENT_MCP_WEB_LAYER WATER_AGENT_TOOL_EXPOSURE WATER_AGENT_SKILLS WATER_AGENT_SKILLS_DIR WATER_AGENT_LLM_BASE_URL WATER_AGENT_LLM_MODEL WATER_AGENT_LLM_API_KEY WATER_AGENT_EMBEDDING_BASE_URL WATER_AGENT_EMBEDDING_MODEL WATER_AGENT_EMBEDDING_API_KEY WATER_AGENT_RETRIEVAL_MODE
git fetch origin main
$VENV/bin/pytest -q tests/test_llm_endpoints.py tests/test_endpoint_compatibility.py tests/test_embedding_endpoint.py tests/test_retrieval_mode.py tests/test_build_index_text_only.py tests/test_retrieval_mode_eval.py tests/test_web_llm_endpoint.py tests/test_web_runs.py tests/test_web_settings_api.py tests/test_credentials_resolution.py tests/test_embedding_provider.py tests/test_answer_cache.py tests/test_container_smoke.py tests/test_web_deployment.py tests/test_index_selection.py tests/test_governance_contracts.py tests/test_p7_known_figures.py tests/test_tool_assembly_snapshot.py tests/test_p7_traceability.py tests/test_check_step_scope.py
$VENV/bin/python -m water_agent.harness.retrieval_mode_eval data/eval/retrieval_colloquial_p0.jsonl --mode text --report-path data/eval/reports/retrieval_colloquial_p0_head.json
$VENV/bin/python scripts/compare_retrieval_colloquial_p0.py --head data/eval/reports/retrieval_colloquial_p0_head.json --base data/eval/retrieval_colloquial_p0_main.json
$VENV/bin/python -m water_agent.harness.retrieval_mode_eval data/eval/rag_p1.jsonl --mode text   # 对照：rag_p1 上 text 模式应约为 Recall@5 98.28%
$VENV/bin/python -m water_agent.harness.retrieval_mode_eval data/eval/retrieval_colloquial_p0.jsonl --mode hybrid   # 需要百炼 Key；没有即结构化 SKIP
$VENV/bin/python -m water_agent.harness.endpoint_compat --out /tmp/compat.json   # 需要对话 Key；会产生少量计费调用；没有 Key 即结构化 SKIP
$VENV/bin/python -m water_agent.harness.tool_assembly_snapshot --check
(cd web/app && npm test && npm run build)
$VENV/bin/python scripts/check_step_scope.py docs/execution-prompts/P7-S10-openai-compatible-endpoints.md --base origin/main
docker compose -f compose.yml -f compose.production.yml config -q
COMPOSE_PROJECT_NAME=p7s10 VENV=$VENV bash scripts/ci_gate.sh
```

本机 Docker daemon 没起时 `tests/test_web_deployment_overlay.py::test_image_check_skips_structurally_instead_of_failing` 必然失败（main 上同样），先 `open -a Docker` 再跑门禁。

手动验收（有条件时做，没有就在决策记录写明未做）：本机 Web 栈里 ① 只配一个对话 Key、清掉百炼 Key：问一个规范问题能得到带引用的回答，检查面板显示"检索：关键词模式"；② 面板把对话端点改成另一个 OpenAI 兼容服务（或本机 Ollama）但不跑兼容性检查：发消息得到 424 `WEB_MODEL_NOT_VERIFIED`；跑检查通过后能正常作答；③ 面板改 embedding 模型名但不重建索引：检索显示"关键词模式（向量模型与索引不匹配）"。

## 9. 完成定义

- 满足规格 §8 第 5 条的全部项；
- PR 上的 `p7-scope` 与 `p5-ci` 通过；
- 决策记录贴出：两种检索模式在 `rag_p1` 与 `retrieval_colloquial_p0` 上的指标对照（含重合率分布）、兼容性报告样例（不含 Key）、"不跨服务商回落"的反证、embedding 不匹配的反证、快照 `--check` 输出。

## 10. 汇报格式

- 改了哪些文件；
- 验证命令与实际输出（失败照贴）；
- 范围守卫的输出；
- 快照差异；
- 未验证项（至少：非 DeepSeek 端点上的真实作答质量（rag_p1 real）、hybrid 模式在口语化集上的实测（无百炼 Key 即 blocked）、浏览器手动验收）；
- 残余风险（至少：关键词模式对口语化问法的召回缺口、面板可写对话端点 = 管理员决定数据发往何处、兼容性探针只覆盖四类能力不等于质量合格、价格未核对时成本不可见、缺省 DeepSeek 免检是基于历史证据的例外）。

## 11. 常见坑（本仓库实测过）

- worktree 里不设 `PYTHONPATH=src` 时 pytest 被 H0 拦下（exit 4）；web/app 新 worktree 先 `npm ci`（走代理）再 `npm run build`。
- 本机设了 `WATER_AGENT_*`、`DEEPSEEK_API_KEY`、`DASHSCOPE_API_KEY`、`HTTPS_PROXY` 时先 unset 再跑测试和快照；测试里用 `monkeypatch.setenv`，**不要**直接写 `os.environ`（S9 实测：泄漏的变量让后面的快照测试全红）。
- 本机 `data/indexes/` 有真实索引而 GitHub runner 的干净克隆里**没有**（不入库的构建产物）：依赖真实索引的用例要像 `test_no_embedding_key_returns_its_own_precondition_failure` 那样用 fixture 现建小索引并经 `WATER_AGENT_INDEX_PATH` 指定，否则本机绿、CI 红。
- `data/indexes/` 整目录被 `.gitignore` 忽略（索引是构建产物）：`index_meta.json` 不入库，由 `build_index.py` 与索引一起产出；`.dockerignore` 已整目录放行进镜像。CI 的干净克隆里没有真实索引，相关用例一律现建 fixture 小库。
- 冒烟镜像独立于 app 镜像（`water-agent-smoke:local`）：改 `webapp/smoke.py` 后**两个镜像都要重建**，否则新检查项无声缺席（P5-S47 实测）。
- 零宽字符只能用 `\uXXXX` 转义；JSON 请求体最坏每字符 12 字节（`tests/test_upload_body_limit.py`）——端点配置的 PUT 请求体只有短字符串，不要加长文本字段。
- 提交前单独确认测试退出码为 0，不要把 `git commit` 串在测试命令后面。
- 只含执行 Prompt 的文档 PR，分支名不能以 `YEK/p7-s<n>-` 开头。
