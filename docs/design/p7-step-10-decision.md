# P7-S10 决策记录：OpenAI 兼容端点可配置与关键词检索模式

> 日期：2026-10-13。分支 `YEK/p7-s10-openai-compatible-endpoints`，基于 main `7f6bf9d`（S0–S9 已合入，S8 先于本步合入）。
> 执行 Prompt `docs/execution-prompts/P7-S10-openai-compatible-endpoints.md`，规格 `docs/design/16-p7-plugin-mcp.md` §3.11 / §7 P7-S10-A01…A12，
> 威胁模型 `p2.5-threat-model.md` T14。

**一句话结论**：对话模型与 embedding 都可以换成任意 OpenAI 兼容端点（缺省仍是 DeepSeek + 百炼，已有部署零变化）；非缺省对话端点必须先过
兼容性探针（硬门槛）；只配一个对话 Key 时 RAG 照常可用，检索走**关键词模式（BM25）**并在运行状态、trace、界面里如实标出——但
**口语化问法的召回率只有 46.30%**（`retrieval_colloquial_p0` text 模式 Recall@5；同一批条款在 rag_p1 的规范化问法上是 98.28%）。

## 0. 偏离与修订

### 0.1 执行 Prompt §0 的修订（S8 先合入带来的）

S8 先于本步合入，Prompt §5 末尾"若 S8 先合入"那一段成立：现有快照 profile 是 **17** 个（不是正文写的 16 个），全部冻结；
`provider_call_sites` 是 8、本步不改；`MCP_CONFIG_VERSION` 是 `p7-s8-2026-10-12`、本步冻结；`GOVERNANCE_CONFIG_VERSION` 由
`p7-s8-2026-10-12` 递进为 `p7-s10-2026-10-13`；`PINNED_ENV_KEYS` 里已有 `WEB_PUBLIC_BASE_URL`；威胁模型新增 **T14**（T13 已被 S8 用掉）。

### 0.2 实测后追加的偏离（均已在对应测试里锁定）

| # | Prompt 写法 | 实际做法 | 原因 |
|---|---|---|---|
| D1 | 口语化集"≥ 40 条：每条 gold 条款配一句口语化 / 同义改写问法" | **54 条**：rag_p1 的全部 54 个 gold 条款各配一句，分两类——`colloquial` 38 条（不用术语的日常说法）、`paraphrase` 16 条（设计人员式改写，保留个别术语、句式与 rag_p1 不同）。"字面重合度低"定义为字符二元组重合率 ≤ 0.30（42/54 满足）；与 gold 条文的最长公共子串 ≤ 5 字（不抄关键短语） | 与 rag_p1 同一批条款、不同问法，两套可以直接对照；全集都用纯口语会失去代表性（设计院用户也会带术语提问），按类别分别报 Recall@5 |
| D2 | 评测输出 Recall@1/5、MRR | 每条用例记两条管线：`retriever`（检索器本身 `retrieve(top_k=5)`，即 Prompt 实测 98.28% 的口径）与 `local_rerank`（生产缺省：召回 20 条、本地重排取 5）；另记 Recall@3 | 生产 agentic 检索工具走的是后者；两条都看才知道重排能补回多少 |
| D3 | text 模式"对同一索引"跑 | 门禁里的 text 模式**从 `data/curated` 现建纯关键词索引**（`build_text_only_index`，0.5 s）；`--db-path` 显式给出时才用那份索引 | CI 干净克隆里没有 `data/indexes/`。实测现建索引与真实 `qwen3.7__512__curated.db` 的文本分量在 rag_p1 与口语化集上**指标逐项相同**（`.text` 侧文件字节不同、命中相同） |
| D4 | 比较器照 `compare_skill_p0.py` | 不比较检索到的条款列表，只比指标、逐条命中与 gold 名次 | **F21**：TriviumDB `search_hybrid` 对近乎同分的条目排序不固定（同一进程两次调用都可能互换，分数按名次换算且有浮点抖动）。两个数据集各连跑 10 次：指标、逐条 pass、gold 名次完全稳定，只有非 gold 的尾部条目变化 |
| D5 | 兼容性门槛"装配期拒绝" | 接在会话（`AgenticSession`）、CLI legacy（`build_app`）、runner 真实组件（`_build_real_chat_model` / `_build_real_generator`）、docgen 润色（`RealPolishTransport._ensure_llm`）；**judge 不设门槛**；探针本身不设门槛 | judge 是评测工具，它的输出被打分、不交给用户；探针就是用来产生证据的。docgen 润色的产物进交付文档，所以设门槛 |
| D6 | `ProviderKind.DEEPSEEK_CHAT` 的注释改写 | 枚举本身不动；语义说明写在 `inventory.py` 的调用点清单注释、`config.py` 与 `p2.5-provider-inventory.md` §1.1 / §1.2 | `runtime/governance/contracts.py` 在 forbid 里 |
| D7 | `LOCAL_MODEL_*` "语义不变" | 门槛判定与报告模型抽到 `compatibility.py` 后语义不变；端点匹配改用规范化比较（主机名不分大小写）。另：此前 **CLI** 在 `LOCAL_MODEL_ENABLED=true` 时仍用 DeepSeek 的参数构造模型（只有 Web 读本地设置），现在端点解析唯一来源后 CLI 也连本地端点 | 单一来源的直接结果；本地模型从未在任何部署里启用过（P5 Step 46 无本地 endpoint），实际影响为零 |
| D8 | mock 生成器的检索 | mock 生成器同一套模式规则：判定为 hybrid 时用伪向量（`fallback_kind=mock`），判定为 text 时**不用伪向量**（`fallback_kind=none`）。`auto` 且没有 embedding Key（冒烟容器、CI）→ text。`tests/test_container_smoke.py` 的 `fallback_kind == "mock"` 断言随之改为 `retrieval_mode == "text"` + `fallback_kind == "none"` | 伪向量 + 关键词混合只是"没有关键词模式时的权宜之计"；冒烟现在测的是生产在同样配置下会走的那条路 |
| D9 | runner 真实评测的前置条件 | embedding Key 只在**强制** `hybrid` 时是 SKIP 条件；`auto` 下没配 embedding 照常跑（text 模式），stderr 警告"真实评测的检索是关键词模式"，输出 JSON 带 `retrieval_mode` | 与 CLI / Web 同一规则，评测"只配对话 Key"这一场景才有路可走。代价：本机只有 `DEEPSEEK_API_KEY` 时 `--variant real` 不再 SKIP，而是计费运行 |
| D10 | Web 预检"只在强制 hybrid 时要求 embedding" | 另加两种 424：强制 hybrid 且 embedding 与索引不匹配 → `RETRIEVAL_EMBEDDING_MISMATCH`；`WATER_AGENT_RETRIEVAL_MODE` 取值不对 → `WEB_RETRIEVAL_MODE_INVALID` | 两者都会让装配必然失败、浏览器只看到通用的 "agent run failed"；独立错误码可自助排查（与 `WEB_EMBEDDING_NOT_CONFIGURED` 同一理由） |
| D11 | 面板写端点 | 面板配置文件 `llm_endpoint.json` 损坏或与本地模型冲突时（`PanelEndpointError`）**不阻止 Web 启动**，但解析 / 预检照样拒绝；运维 env 层面的配置错误仍在启动期失败 | 管理员要能在面板里改正它；不静默回到缺省端点 |
| D12 | compose 透传 `WATER_AGENT_LLM_*` | 透传端点、模型、兼容报告路径、超时、价格、embedding 端点 / 模型、检索模式；**两把专用 Key 不经环境变量传**，也不新增 Compose secret | 环境变量会出现在 `docker compose config` 的输出里；新增 secret 要求文件必须存在，会让已有部署起不来。Key 在设置面板的只写槽位填写（凭据卷） |
| D13 | 探针"真实调用" | 探针用专用 gateway 画像：不重试（失败如实记录，不放大调用量），写错模型名那一项的超时 30 s；预算账本与进程级默认 gateway 分开（与 embedding 入库同一种 `gateway_for_profile_overrides` 用法） | 探针是一次性证据采集；重试会把"不合格"掩盖成"偶尔慢" |
| D14 | 面板显示端点 | 成员只看到主机名与模型名，完整 base_url 只给管理员 | base_url 的路径部分可能有运维不想扩散的东西 |
| D15 | — | 兼容性检查进行中时改端点返回 409 `WEB_LLM_COMPATIBILITY_RUNNING` | 否则旧端点的检查结果会写在新端点之后（精确匹配会作废它，但状态显示会乱） |
| D16 | embedding 维度 | 缺省模型 MODEL_NAME 仍只收 256/512/768/1024；其余模型只要求 (0, 8192]；`build_index.py --dimensions` 去掉 choices 限制 | 任意 OpenAI 兼容 embedding 模型的维度由服务商决定（如 1536） |
| D17 | 缓存键只加 `model_endpoint_digest` | 另：真实索引上走关键词检索时，回答缓存键的语料 / 索引版本因子记为 `text-only`（与混合检索的回答互不命中）；混合检索时与历史键同键 | "检索参数变了必须失效"的同一原则：检索方式不同，答案依据就不同 |
| D18 | 检索模式进 trace | 每次 run 开 trace 时记一条 `retrieval_mode` 事件（模式、原因、不匹配说明、`index_meta`），不在装配期写 | 装配期 trace 尚未开始（F13 的教训）；装配快照因此不受影响 |
| D19 | `WATER_AGENT_LLM_PRICE` | 价格挂在**进程启动时**解析出的对话模型名上（`GovernanceConfig` 构造时读环境）；面板改了模型名后要重启才换价格 | 治理配置是进程级单例；价格只用于成本估算显示，不做硬拒绝，滞后不影响任何判定 |
| D20 | 凭据白名单 | 两把新 Key 追加在 `DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY` 之后；`tests/test_web_settings_api.py:110` 按 `list(CREDENTIALS)` 比较，无需改动 | 保持已有面板卡片顺序 |

## 1. 交付

| 提交 | 验收 | 内容 |
|---|---|---|
| `b2bba8d` Step 1 | A07 前半 | `retrieval_colloquial_p0`（54 条）冻结 + `harness/retrieval_mode_eval.py`；在未改动的检索代码上跑出"改之前"报告（`mechanism=absent`，零向量 + `hybrid_alpha=0`） |
| `cead93c` Step 2 | A01 | `generation/endpoints.py`；凭据白名单两把新 Key；CLI / Web / runner / legacy / docgen / judge 全部改走它 |
| `44a82d4` Step 3 | A02 | `generation/compatibility.py`、`harness/endpoint_compat.py`、装配期与 Web 预检门槛（`WEB_MODEL_NOT_VERIFIED`）；本地模型路径改用共用判定 |
| `6cd3f54` Step 4 | A03 | 超时 / 价格覆盖、`model_endpoint_digest`、`GOVERNANCE_CONFIG_VERSION` → `p7-s10-2026-10-13`、清单文档 |
| `64cb903` Step 5 | A04 | embedding 端点可配置、`index_meta.json` 写入与校验、`RETRIEVAL_EMBEDDING_MISMATCH` |
| `5cbd249` Step 6 | A05 | `WATER_AGENT_RETRIEVAL_MODE`、text 路径、`build_index_retriever`（会话与 runner 共用）、运行状态 / trace / 结果投影、Web 预检改语义 |
| `4c99e0a` Step 7 | A06 | `build_index.py --text-only` |
| `3803611` Step 8 | A08 | 设置面板对话端点（后端路由 + 审计 + 前端卡片 + 检索方式显示） |
| `437b7ce` Step 9 | A09 | compose 透传、冒烟 `agentic_run_reports_retrieval_mode` |
| `5be43ae` Step 10 | A07 后半、A10 | 快照 `session_endpoint_custom`、`PINNED_ENV_KEYS`；`retrieval_colloquial_p0` text 模式接进 `ci_gate.sh` + 入仓基线 + 比较器 |
| Step 11 | — | 规格 §3.11 / §5 / §7 / F20·F21 / §10、威胁模型 T14、清单文档、入库文档、本记录、AGENTS.md |

A11（非 DeepSeek 端点上的真实作答质量、hybrid 在口语化集上的实测）与 A12（浏览器手动验收）按规格 `untested`，见 §5。

## 2. 实测

环境：本机 macOS，`_venv`（Python 3.12），worktree 内 `PYTHONPATH=src`，`WATER_AGENT_*` / `DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY` /
代理变量全部 unset。

### 2.1 两种检索模式的指标对照

text 模式（关键词，BM25，零 embedding 调用）。`retriever` = 检索器前 5；`local_rerank` = 召回 20、本地重排取 5（生产缺省）：

| 数据集 | 管线 | R@1 | R@3 | R@5 | MRR | 分类 R@5 |
|---|---|---|---|---|---|---|
| rag_p1（58 条） | retriever | 84.48% | 98.28% | **98.28%** | 90.80% | — |
| rag_p1 | local_rerank | 91.38% | 100% | 100% | 95.11% | — |
| retrieval_colloquial_p0（54 条） | retriever | 25.93% | 40.74% | **46.30%** | 32.78% | 口语 31.58%（38）/ 改写 81.25%（16） |
| retrieval_colloquial_p0 | local_rerank | 31.48% | 44.44% | 51.85% | 38.70% | 口语 36.84%（38）/ 改写 87.50%（16） |

hybrid 模式（混合检索，需百炼 Key）：**未在本步实测**，见 §2.6。入仓口径（2026-09-15，rag_p1 real）是 Recall@5 100%、MRR 92.24%。

字面重合度（问法的字符二元组中出现在 gold 条文里的比例）：

| 数据集 | 中位数 | 均值 | 最小 | 最大 | ≤ 0.30 |
|---|---|---|---|---|---|
| rag_p1 | 0.634 | 0.627 | 0.375 | 0.909 | 0 / 58 |
| retrieval_colloquial_p0 | 0.172 | 0.198 | 0.000 | 0.500 | 42 / 54 |

rag_p1 唯一漏召回是 `rag-p1-0002`"分流制与合流制分别指什么？"（Prompt 写作时的实测同此）。口语化集 retriever 前 5 未命中 29 条，集中在不带任何
规范术语的说法（例："新开发的片区，下雨的水和家里排的脏水要不要分两套管子走？"→ gold GB50014-2021 3.1.2 未进前 5）。

**结论（按 Prompt §7 如实写明）：只配一个对话 Key 时（检索为关键词模式），口语化问法的 Recall@5 为 46.30%（重排后 51.85%），其中完全不带术语的
口语问法只有 31.58%。** 不阻断本步交付；面板在关键词模式下提示"未配置向量模型时，口语化问法可能检索不到"。是否需要额外的 query 改写由这组数据
决定，**不在本步做**（Prompt §1"不做"）。

"改之前 / 改之后"：Step 1 的 text 模式报告（零向量 + `hybrid_alpha=0`，在未改动代码上）与 Step 6 之后走生产 text 路径的报告，两个数据集上**指标逐项
相同**；用例明细只在 2 条用例的第 5 名非 gold 条目上不同（F21 的近同分排序，见 D4）。

```text
$ python -m water_agent.harness.retrieval_mode_eval data/eval/retrieval_colloquial_p0.jsonl --mode text
retrieval_colloquial_p0 mode=text mechanism=present status=ok index=curated_text_only_build embedding_calls=0
  retriever: recall_at_1=0.2593 recall_at_3=0.4074 recall_at_5=0.463 mrr=0.3278 | colloquial:R@5=0.3158(38) paraphrase:R@5=0.8125(16)
  local_rerank: recall_at_1=0.3148 recall_at_3=0.4444 recall_at_5=0.5185 mrr=0.387 | colloquial:R@5=0.3684(38) paraphrase:R@5=0.875(16)
  overlap: {'median': 0.1716, 'mean': 0.1979, 'min': 0.0, 'max': 0.5, 'low_count': 42, 'low_share': 0.7778} max_common_run=5

$ python -m water_agent.harness.retrieval_mode_eval data/eval/rag_p1.jsonl --mode text
rag_p1 mode=text mechanism=present status=ok index=curated_text_only_build embedding_calls=0
  retriever: recall_at_1=0.8448 recall_at_3=0.9828 recall_at_5=0.9828 mrr=0.908 | unspecified:R@5=0.9828(58)
  local_rerank: recall_at_1=0.9138 recall_at_3=1.0 recall_at_5=1.0 mrr=0.9511 | unspecified:R@5=1.0(58)
  overlap: {'median': 0.634, 'mean': 0.6269, 'min': 0.375, 'max': 0.9091, 'low_count': 0, 'low_share': 0.0} max_common_run=15
```

稳定性（F21）：两个数据集各连跑 10 次（每次独立进程、现建索引），`distinct metrics: 1`、`distinct passed sets: 1`、gold 名次不稳定的用例 0 条。

### 2.2 兼容性报告样例（假端点，离线，不含 Key）

`tests/fixtures/endpoint_compat/fake_openai_server.py` 上跑 `run_endpoint_compatibility`（真实报告格式；`checked_at` 为运行时间）：

```json
{
  "version": "p7-s10-2026-10-13",
  "status": "compatible",
  "model": "sample-model",
  "endpoint": "http://127.0.0.1:59043/v1",
  "checks": {
    "function_calling": "passed",
    "structured_citations": "passed",
    "programmatic_calculation": "passed",
    "tool_schema_budget": "passed",
    "streaming_usage": "passed",
    "error_handling": "passed",
    "cancellation_timeout": "passed"
  },
  "schema_tokens": 3083,
  "evidence": {
    "probe_version": "p7-s10-2026-10-13",
    "kind": "openai_compatible_endpoint",
    "endpoint_host": "127.0.0.1",
    "checked_at": "2026-10-13T04:29:51+00:00",
    "function_call_names": ["calc_velocity"],
    "citation_call_names": ["cite_clauses"],
    "calculation_result_keys": ["assumptions", "velocity_mps"],
    "stream_usage_reported": true,
    "error_code": "PROVIDER_UNAVAILABLE",
    "error_seconds": 0.001,
    "cancel_first_chunk": true,
    "cancel_close_seconds": 0.0
  }
}
```

报告文本里不含 Key（`contains key: False`）。不合格的端点（不会 function calling / 流式末块不带 usage / 不认识的模型名也回成功）分别记
`function_calling` / `streaming_usage` / `error_handling` 为 `failed`、`status=incomplete`（`tests/test_endpoint_compatibility.py::test_probe_report_has_no_secrets`）。

### 2.3 "不跨服务商回落"的反证

```text
$ WATER_AGENT_LLM_BASE_URL=https://llm.other-provider.example/v1 DEEPSEEK_API_KEY=sk-deepseek-only-0001 python -c ...
api_key_name = WATER_AGENT_LLM_API_KEY | missing = WATER_AGENT_LLM_API_KEY
preflight = ('WEB_MODEL_NOT_CONFIGURED', 'Real agent model is not configured: WATER_AGENT_LLM_API_KEY is required for llm.other-provider.example (keys of other providers are never sent to it)')
build_chat_model -> WATER_AGENT_LLM_API_KEY must not be empty（可设同名环境变量、WATER_AGENT_LLM_API_KEY_FILE，或在 Web 设置面板填写）
```

### 2.4 embedding 与索引不匹配的反证

```text
$ 索引用 bge-m3 建库、当前 embedding 配置是缺省 MODEL_NAME（auto / hybrid 各一次）
auto  -> text embedding_mismatch | 索引用 bge-m3 建库，当前配置的 embedding 模型是 MODEL_NAME（换模型必须重建索引）
hybrid -> EmbeddingMismatchError RETRIEVAL_EMBEDDING_MISMATCH: 索引用 bge-m3 建库，当前配置的 embedding 模型是 MODEL_NAME（换模型必须重建索引）
```

两种情况下 embedding 请求数都是 0（`tests/test_embedding_endpoint.py::test_mismatched_embedding_never_searches_vectors`）。

### 2.5 本机两份真实索引补写 `index_meta.json`（一次性命令）

`data/indexes/` 不入库，主检出里的两份真实索引是本步之前建的、没有元数据（代码按百炼缺省模型对待，`index_meta=absent`）。先只读打开核对维度
（256 / 512），再补写：

```bash
PYTHONPATH=src _venv/bin/python -c "
from water_agent.ingestion.pipeline import write_index_meta
for d in (256, 512):
    write_index_meta('/Users/yaerken/Desktop/给排水agent/data/indexes/qwen3.7__%d__curated.db' % d, embedding_model='MODEL_NAME', embedding_base_url='https://dashscope.aliyuncs.com/compatible-mode/v1', dimensions=d, index_version='MODEL_NAME-text-embedding__%d__curated-v1' % d, retrieval_modes=('hybrid', 'text'))
"
```

只新增两个 `*.db.index_meta.json` 文件，不改索引本身；删掉即回到 `absent`。

### 2.6 需要计费调用的三项

`harness.endpoint_compat`（缺省 DeepSeek 端点）、`retrieval_mode_eval --mode hybrid`、真实 `rag_p1`：本步在 Key 全部 unset 的环境里开发与验证，三项都
**没有发出计费请求**。在无 Key 环境里它们分别是结构化 SKIP（`status=skipped` + `requires`），不构成质量证据。是否在有 Key 的环境里补跑由用户决定
（见 PR 描述）；没补跑之前：**不得声称换模型后作答质量不变，也不得声称关键词模式与混合检索等效**（P7-S10-A11 blocked）。

### 2.7 测试与门禁

- 全量 pytest（每个 Step 提交前各跑一次，最后一次在 Step 10）：`3745 passed, 30 skipped`。
- 前端：`npm test` 92 / 92 通过，`npm run build`（tsc + vite）通过。
- `docker compose -f compose.yml -f compose.production.yml config -q`：exit 0。
- 范围守卫：每个 Step 提交前跑，均"范围守卫通过"，没有越界文件。
- 全量门禁 `COMPOSE_PROJECT_NAME=p7s10 bash scripts/ci_gate.sh`：结果见 PR 描述（本记录写于门禁之前）。

## 3. 快照变化

`--diff`（Step 10，`--write` 之前）原文只有一行——新增 profile；现有 17 个 profile 与三个版本号逐字不变（范围块的 `frozen_json` 守住）：

```text
/profiles/session_endpoint_custom: "<absent>" -> {"description": "非缺省 OpenAI 兼容对话端点 + 精确匹配的测试兼容报告：chat model 指向配置的端点与模型", "lifecycle": {"app": {"app_kwargs": ["cache_...
```

新 profile 的要点：`exception: null`，`chat_model_endpoint = {"wrapper": "GovernedChatModel", "base_url": "https://llm.snapshot.invalid/v1",
"model": "snapshot-compatible-model", "compatibility": "verified"}`；模型可见面（注册顺序、schema、工具指引、系统提示、主体、暴露探针）与
`session_mock_default` **完全相同**——换端点不改变工具与提示。已知数字 48 → 52（13 个装配成功的 profile × 4 条暴露探针）。

```text
$ python -m water_agent.harness.tool_assembly_snapshot --check
装配快照与基线一致（18 个 profile）
```

## 4. 不做

- 改 `ProviderKind` / `CallCategory` 名或新增出站模块；改 rerank / 视觉审阅的端点（仍是百炼）。
- 本地 embedding 模型；自动 query 改写（口语化召回低，是否需要由 §2.1 的数据决定，单独评审）。
- 多端点路由 / 负载均衡 / 按用户选模型；面板写 rerank、视觉审阅或 embedding 端点（embedding 端点只由运维环境变量配置）。
- 放宽兼容性门槛（缺省 DeepSeek 免检是唯一例外）；删除 `LOCAL_MODEL_*`；把关键词模式宣称为语义检索。
- 经环境变量或新的 Compose secret 传两把专用 Key（D12）。

## 5. 残余风险与未验证项

- **关键词模式对口语化问法的召回缺口**：只配对话 Key 时口语化 Recall@5 46.30%（不带术语的口语 31.58%）。检索不到时 agent 按"无依据"诚实拒答，
  用户感知为"答不上来"。面板如实提示；改进（query 改写、向量模型）不在本步。
- **面板可写对话端点 = 管理员决定数据发往何处**：只收 https、管理员 + CSRF、审计只记主机名；但被盗的管理员账号可以把对话改到攻击者的端点并填它
  的 Key，再让那个端点"通过"兼容性检查（T14）。
- **兼容性探针只覆盖七类能力，不等于作答质量合格**：非 DeepSeek 端点上的真实作答质量（`rag_p1 --variant real`）未实测（A11 blocked）。
- **缺省 DeepSeek 免检是基于历史证据的例外**：DeepSeek 自身模型行为变化时不会重新检查。
- **价格未核对时成本不可见**：未登记模型的成本记 `None`；`WATER_AGENT_LLM_PRICE` 是 `confirmed=False` 的估算、只显示不拦截；面板改了模型后价格要
  重启才更新（D19）。
- **hybrid 模式在口语化集上的实测**：需要百炼 Key，未做（A11 blocked）；所以"混合检索在口语化问法上能补回多少"目前没有数字。
- **embedding 匹配只比模型名与维度**：同名模型在不同服务商那里向量空间不同时检不出来；没有元数据的旧索引按百炼缺省模型假设。
- **TriviumDB 近同分排序不固定**（F21）：关键词检索的尾部条目每次可能不同；指标稳定，但单条用例的前 5 列表不可逐字复现。
- **兼容性检查与面板状态在进程内**：检查的"进行中 / 最近一次结果"不跨 worker、重启即丢（报告文件本身在凭据卷里，重启后仍有效）。
- **浏览器手动验收未做**（A12）：①只配对话 Key 时带引用作答并显示关键词模式、②换端点未验证即 424、③改 embedding 模型未重建索引即显示不匹配——
  自动化覆盖在 A05 / A08（HTTP 往返）与前端 node 测试，没有在浏览器里点过。
- **runner 行为变化**（D9）：本机只有 `DEEPSEEK_API_KEY` 时 `--variant real` 不再 SKIP，而是以关键词模式计费运行。
