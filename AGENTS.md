# AGENTS.md

给排水领域 AI Agent 的构建约定。任何会话开工前先读本文件与 `docs/给排水agent改造任务书.md`。

## 项目是什么
面向给排水设计院的 Agent，覆盖规范问答、水力计算、计算书/设计说明生成、图纸审查、材料统计。Step 0–10 的工程机制已交付；P2 的模板引擎、材料聚合与导出、HITL、自检、专项评测已落地。2026-09-04 的受限 docgen 真实 LLM 润色样本为 4/4，但真实 RAG 质量、正式规范引用、检索 gold 与 P3 图纸自动抽取仍未收口。P2 权威口径见 `docs/design/11-docgen-materials.md`，P2.5（缓存/限流/成本/护栏/检测器）的权威规格见 `docs/design/12-p2.5-production-governance.md`，最新质量证据见 `docs/evals/p2-quality-2026-09-04.md`。P2.5 Step 11–16 已交付：Step 14（分层护栏）见 `docs/design/p2.5-step-14-decision.md`，Step 15（治理评测与失败检测器）见 `docs/design/p2.5-step-15-decision.md`，Step 16（治理组件端到端接入主链路）见 `docs/design/p2.5-step-16-wiring-decision.md`；护栏是**确定性正则规则集**，不是分类器，**必然存在未覆盖的改写变体**，不得声称"能拦住任意注入"。P3（图纸读取与审查）于 2026-09-07 启动，权威规格见 `docs/design/13-p3-drawing-review.md`，**解析基线是 DXF，DWG 直读 blocked**（转换需 `EXECUTE_SHELL`，该能力构造期即被拒绝）。

## 三条铁律（不得违反）
1. 计算类永远程序化校验，LLM 不参与数值运算。
2. 引用可追溯，禁止编造条款/规范号。
3. 评测先行，改动不得让关键指标下降。

## 验证命令（改动完成前必须执行）
```bash
bash scripts/ci_gate.sh  # pytest + ruff + mypy + 五套机制层 harness + baseline 对比
_venv/bin/pytest -q tests/test_governance_contracts.py tests/test_provider_gateway.py tests/test_tool_runtime_policy.py tests/test_answer_cache.py  # P2.5 Step 11–13 治理层
_venv/bin/pytest -q tests/test_guardrails.py tests/test_injection_defense.py tests/test_tool_policy.py tests/test_redteam_dataset.py  # P2.5 Step 14 护栏与红队数据集
_venv/bin/pytest -q tests/test_detectors.py tests/test_governance_observability.py tests/test_governance_dataset.py  # P2.5 Step 15 检测器与观测
_venv/bin/pytest -q tests/test_drawing_governance.py tests/test_drawing_root.py tests/test_dxf_reader.py tests/test_read_drawing_tool.py  # P3 Step 18–20 图纸链
_venv/bin/python -m water_agent.harness.runner data/eval/docgen_quality.jsonl
_venv/bin/python -m water_agent.harness.runner data/eval/material_quality.jsonl
_venv/bin/python scripts/evaluate_retrieval_matrix.py  # 真实检索矩阵，需百炼 Key/权限/网络
```
无 Key 属前置条件缺失时输出结构化 SKIP；已有 Key 但网络、权限或组件失败必须记为 blocked/非零退出，禁止包装成 SKIP 或通过。

## 架构现状
- `graphs/rag.py`：legacy 单次 RAG 图（router→retrieve→generate→citation_check→finalize），保留不删；citation_check 已接 Step 5 确定性校验器 `validate_citations`（state 层 `citations` 统一为 `list[CitationClaim]`）。
- 已完成：工具注册表（`ToolSpec`/`ToolRegistry`，执行唯一入口 `ToolRegistry.invoke`）、会话记忆（checkpointer + thread_id）、agentic RAG 外层循环（`graphs/agent_rag.py` = agent_loop→citation_validator→finalize）、DeepSeek function calling 与流式。无效引用有界回流；有检索/有回答但零引用时强制回流一次，再次零引用结构化失败。`--mode agentic` 默认真实模型，`--mock-generator` 走离线脚本模型。
- 动态提示装配（Step 8）：`prompting/assembler.py`（分段系统提示 + 段级预算 + create_react_agent callable 签名）、`prompting/runtime_context.py`（RuntimeContextProjection 变化才注入，省 token）、`ingestion/version_registry.py` + `data/versions.json`（只录有来源事实：非 unknown 条目缺 source/verified_at 加载时降级 unknown；未录入显式返回 status=unknown；四本入库规范现行状态尚未人工核实，当前全部 unknown）；校验器 `validate_citations(version_registry=)` 已接进 legacy/agentic 两条图，版本状态写回 `CitationClaim.spec_version/version_status`，未知只标记进 `warnings` 不进 `errors`（不是模型能重试纠正的事）；`--mode agentic` 走 `prompt=PromptAssembler(tool_registry=registry)`。
- Human-in-the-Loop（Step 9 + P2）：`graphs/docgen.py` = gather→draft→self_check→approval(interrupt)→finalize；自检失败有界重试，拒绝后带反馈重拟，总稿数上限 `MAX_DRAFT_ROUNDS=2`。审批只接受严格 bool，`failed` 不进入 finalize，`approval_log` 由 checkpointer 持久化，SqliteSaver 跨进程 resume 已测。
- 双轨评测（Step 10）：`harness/runner.py --variant real` 默认走 **agentic 图**（模型经 cite_clauses 声明引用、校验器真判；`--real-graph legacy` 引用系统代填、citation_ok 恒真，仅供改造前后对比）；退出码 0 通过/全 SKIP、1 用例失败、2 加载问题、3 有 Key 但真实组件建不起来（`RealComponentsError`，绝不包成 SKIP）；观测 `llm_call` 事件已接进 legacy generate 节点与 agentic `_BudgetChatModel`（token/cost/延迟/摘要，trace 落盘）；PR 门禁 `scripts/ci_gate.sh` + `compare_baseline.py`（THRESHOLDS 单一来源）。
- CLI 收口：`--interactive` 真正作答（`--mode agentic` 用同一 `AgenticSession`/thread 跨轮，`--resume` 恢复；其他模式每行 `run_once`）；`--flow docgen` = gather（计算/RAG）→ draft → self_check → 人审 y/n+原因 → finalize，EOF 挂起 exit 0、`--resume` 恢复，审批记录写 trace；`--doc-type auto|calculation_book|design_note`（P2，默认 auto：计算意图→计算书，其余→设计说明）；检索装配统一 `_build_retriever/_build_reranker`，agentic 检索工具经 `RerankingRetriever` 套 rerank。`--mode legacy|agentic` 双轨共存。
- P2 文档链：`docgen/templates.py` 负责计算书/设计说明的结构化渲染，数值只来自输入 `calc_values`，引用只来自输入 `source_id`；`graphs/docgen.py` 草稿/终稿同源。`docgen/quality.py` 增加受限 prose 润色与确定性自检（结构、逐行数字、引用白名单、非有限值），真实调用失败或结构破坏均返回 blocked，不静默回退为通过。
- P2 材料统计：`materials/core.py` 管段表（段号/系统/管径/长度/材质）→ 按 `(材质, 规格)` 聚合（数量=长度和，round 3 位单一来源），每行带 `segments`+`detail` 溯源；`materials/export.py` 导出 `.xlsx`（材料清单 + 统计说明两个 sheet，openpyxl>=3.1,<4 已入 pyproject 依赖）与 CSV；`tools/material_stats.py`（ToolSpec，注册进 CLI agentic 会话与 runner real-agentic registry）。
- P2 评测：`docgen_p0` 11/11、`material_p0` 10/10，均进入 `scripts/ci_gate.sh` 与 `data/eval/baseline.json`；默认 baseline 四套为 rag_p0/calc_p0/docgen_p0/material_p0。专项质量集为 `docgen_quality` 4 条（离线 4/4；2026-09-04 真实 LLM 4/4）和 `material_quality` 7/7；专项质量集尚未纳入默认 baseline。
- 计算永远走程序化工具，缺参→`missing_inputs`，越界→`assumptions`。
- P3 图纸链（Step 19–20）：`drawing/` 包是图纸读取的唯一实现。`drawing_root.py` 四态契约（`NOT_CONFIGURED`/`NOT_FOUND`/`NO_DRAWING_FILES`/`OK`，**不递归子目录**）；`dxf_reader.py` 路径三层纵深防御（登记号正则让模型无法表达路径 → `resolve()+relative_to` 防符号链接 → `ToolPolicy.read_dir_allowed` 裁定）+ 四道护栏（类型/大小/路径/内容）；`models.py` 6 个稳定错误码；`synth.py` 程序化合成测试图纸。`tools/read_drawing.py` 是**第一个碰文件系统**的模型可调用工具：`root`/`policy` 装配期注入，参数只有 `drawing_id`/`system`/`max_entities`（schema 里不出现任何路径概念，有测试断言）；业务失败走返回值不走 `TOOL_ERROR`（保住结构化错误码语义）。`maybe_build_read_drawing_tool()` 是 CLI 与 harness **共用**的条件注册策略——`DRAWING_ROOT` 不可用就不注册（能力不可用就不暴露，恒失败的工具会诱导模型重试并白占 958 tokens）。**内容护栏的确切语义**：注入命中**只标记 `suspicious` 不改写文本**（改写会破坏正常的规范引用标注），`redact_text` 只对 PII/凭据改写；隔离的实质保证是 `ContentEnvelope` + `RETRIEVED_FILE` 不在 `INSTRUCTION_BEARING_SOURCES` 内。**工具 schema token 实测 4370/4500，headroom 130，`< 4500` 断言不得上调**（Step 21 加工具前先做按意图最小暴露）。决策记录见 `docs/design/p3-step-20-decision.md`。
- P2.5 治理层（Step 11–14）：`runtime/governance/` 是运行时边界的唯一定义，**不依赖 langchain/langgraph**（有测试锁死）。
  - `contracts.py`：`Principal`（只能 `local:<thread_id>`，subject 不进 trace，只暴露 `principal_hash`；4 个危险能力构造期即拒绝授予）、`SourceTag`（信任级别是代码定义的偏序，`relabel` 只能降级；只有 `SYSTEM_POLICY` 可承载指令）、`ContentEnvelope`、`Capability`、`PolicyDecision`、`ProviderCall`、`UsageRecord`（`usage_known=False` 时禁止填 token/成本——估算不得当账单）、`CacheDisposition`（`status` 与 `eligible_for_store` 独立，使「MISS 且不可存」可表达）与 13 个稳定错误码。
  - `gateway.py`：`ProviderGateway` 是**所有远程模型调用的唯一入口**。顺序为能力校验→熔断→预发送限流→预算预留→有界重试（受 deadline 约束）→usage 对账/回滚→落账。只对瞬时错误重试（408/429/5xx/超时/连接错误）；**401/403 零重试且不计熔断**（换 Key 才能解决，403 是 rerank 已知阻塞码）；未知错误默认永久（不确定就不放大故障）；429 遵守 `Retry-After`。**永不换模型/永不回退本地/永不伪造 usage**。
  - `config.py`：版本化配置（改阈值须改 `GOVERNANCE_CONFIG_VERSION`）。**成本硬上限默认不启用**（`ModelPrice.confirmed=False`，用未核对价格做硬拒绝等于用估算当账单）；只有 `daily_token_cap` 做硬限制；`BudgetPolicy` 默认 `WARN`，`DEGRADE` 配置期即拒绝。限流额度定位是**失控保护**而非生产调优值（DAU/账单仍 unknown）；`INGEST_RATE_LIMIT` 单独放宽，否则批量入库会被自己的限流打断。
  - `stores.py`：`RateLimitStore`/`BudgetLedger` 可替换；内存实现 `supports_multi_instance` **恒为 False**（不得用单进程 dict 谎称支持多实例）。预算是**预留→真实 usage 对账→失败回滚**三段语义。
  - `inventory.py`：6 个远程调用点 + 4 个工具 capability 的机器可读真源，`governed=6/6`、`tools_guardrail_validated=4/4`（有测试锁死这两个数）。**静态扫描断言「代码里的出站模块 == 清单登记」**，新增未登记出口或未声明 capability 的工具立即红灯（反例已实测）。新增别的 SDK 入口必须同时扩展 `_EGRESS_PATTERNS`，否则扫描形同虚设；扫描不含 `scripts/`。
  - 出站模块经 Step 12 收敛为 4 个：`docgen/quality.py` 与 `harness/llm_judge.py` 不再自建连接，统一走 `generation/chat_model.py::build_chat_model` → `GovernedChatModel`。**`ChatOpenAI` 自带重试必须关（`max_retries=0`）**，否则与 gateway 重试叠乘。
  - `ToolSpec.max_retries` **已删除**（从未被读取的死字段，含静态测试保护）；`timeout_seconds` 接入 `ToolRegistry.invoke` 的**协作式 deadline**（超时返回 `TOOL_TIMEOUT`，**不杀线程**——当前 4 个工具均只读/纯计算，加入写入类工具需另设协议）。
  - `cache.py` / `cache_service.py` / `corpus_version.py`（Step 13）：**只做精确缓存，不做语义缓存**（`cache.py` 有测试断言不含 `embed`/`similarity`/`cosine`/`vector`；规范化只做 NFKC + 空白折叠，不做大小写折叠或同义处理）。缓存键有 **10 个失效因子**（规范化 query、`principal_scope`=主体+授权域、模型、温度、`PROMPT_TEMPLATE_VERSION`、`TOOL_POLICY_VERSION`、语料+索引版本、规范版本状态摘要、检索参数、条目格式版本），每个都有独立 miss 测试；`CacheKeyFactors` 为 `extra="forbid"`+`frozen`，**新增失效维度必须改契约**。写入资格有 **10 种稳定拒绝原因**；`output_guardrails_passed` **无宽松默认**（忘传即报错）。TTL/容量必须为正；`no_cache` 不读但可写（返回 `BYPASS` 且**不计 miss**），`no_store` 不写。条目只存最终回答 + 已验证 source_id，**不存**条文全文/系统提示/用户原文。
  - **⚠️ 缓存当前命中率结构性为 0**：`data/versions.json` 4 本规范全为 `unknown`，按规格 §5「不缓存版本未知的结果」写入资格恒为 false（原因 `spec_version_unknown`）。这是刻意的 fail closed，有 3 条测试断言（含直读 `data/versions.json` 双保险）。**人工核实版本状态即解锁缓存**，届时相关测试会失败并提示更新。**不得为提高命中率放宽版本校验**。缓存也因此**尚未接进 RAG 主路径**（`graphs/rag.py`/`run_once`），留到版本核实后一并做。
  - 缓存键的规范版本摘要用**注册表全量快照**而非本次引用：`lookup` 发生在检索之前，用"本次引用"会导致查写键永不相同、缓存永远命中不了。
  - provider prompt cache 观测（`observe_prompt_cache`）恒返回 `DOCUMENTED_UNVERIFIED`，**不得声称 `VERIFIED`**（字段路径依官方文档编写，无 Key 环境未用真实响应证实；有测试锁死）。`cached_tokens=None`（字段缺失）与 `=0`（有字段未命中）是**不同事实**。
  - trace 的 `record_cache_event` **只写 `cache_key_hash`，永不写 key 原文**；`saved_*` 仅在 usage 可得时填写（不估算）。
  - `rules.py` / `pipeline.py`（Step 14）：`GuardrailPipeline` 有五个检查面，**动作不同**——用户输入/工具参数/记忆写入命中即 **BLOCK**；检索内容命中是 **REDACT+标记**（丢弃整条会让投毒语料把正常检索废掉，真正防线是"内容不能成为指令"）；输出面与既有 docgen 校验组合而非替代。规则集是**确定性正则**（`RULESET_VERSION`，进缓存键），不是分类器，**必然存在未覆盖的改写变体**。检测前先规范化（NFKC + 剥不可见字符 + 有界 Base64 解码），但**放行时送模型的内容是原文**（规范化只用于检测，不用于改写）；零宽字符测试/规则**只能用 `\uXXXX` 转义**，字面字符会在编辑中静默消失。身份证用 GB 11643 校验位+出生日期验证（不是"18 位数字"，工程长数字曾被误判）；`pii_mode=allow` 下**凭据仍强制脱敏**（授权的是 PII 不是密钥）；trace 落盘的 `_redact` **无条件脱敏、不看 `pii_mode`**。`ToolRegistry.invoke` 护栏在 `args_model` 校验**之前**（顺序是安全属性），**未在治理清单登记且未装配期声明能力的工具 fail closed**；`declared_capabilities` 是**装配期声明**（测试夹具用），不放松任何检查，也不得在生产工具上使用。`GuardrailPipeline`/`principal` 缺省回落**进程级默认单例**，不是跳过治理（无绕过后门）。`data/eval/redteam_p0.jsonl`（33 条合成用例）由 `tests/test_redteam_dataset.py` 逐条断言"命中哪条规则"，不只断言"被拦了"。
  - `scenarios.py` / `detectors.py` / `observability.py`（Step 15）：32 条离线治理场景（`data/eval/governance_p0.jsonl`，覆盖限流/预算/可靠性/护栏/trace/检测器/缓存十个面）；8 个基于 trace 的失败模式检测器（`guardrail_bypass`/`tool_misuse`/`cascading_errors`/`context_loss`/`hallucinated_action`/`success_hallucination`/`unverified_citation_after_cache`/`budget_bypass`，每个有 PAGE/TICKET 严重度与运维 runbook）；聚合指标与本地结构化告警（`delivery="local_event_only"`，**不假装已发送 PagerDuty/Slack**）。检测器**不得调用 LLM**（有 AST 静态断言），**不得只在崩溃时打标**（多数 Agent 故障产出看起来正常的输出）。未知值记 `None` 不记 `0.0`（同 `UsageRecord.usage_known` 纪律）。工具执行事件已接入 `ToolRegistry.invoke`，护栏事件已接入 `record_guardrail_event`，两者通过 `invocation_id` 配对供 `guardrail_bypass` 检测器判定。**残余缺口**：检测器尚未接进 CLI/runner 生产调用站点（trace 参数未传递）；告警路由未接外部投递通道；`check_memory_write` 未接 checkpointer；`materials/export.py` 未接护栏路径校验。

## 改动范围纪律
- 本任务书共 Step -1…15，每步一个逻辑面，一步一 commit。P3 在任务书之外续编为 Step 17…22（Step 16 已被 P2.5 接线步占用），权威规格见 `docs/design/13-p3-drawing-review.md` §8。**已交付 Step 17（规格）、18（`READ_DRAWING` 能力 + 读写白名单分离）、19（DXF 解析 + 中间格式 + 合成图纸）、20（`read_drawing` 工具 + CLI/harness 条件注册）**；Step 21（语义映射 + 规则引擎）、22（审查子图 + `drawing_p0` 评测集）未开始。
- P2.5 新增 Step 11…15；执行前必须读取 `docs/design/12-p2.5-production-governance.md` 中对应 Step 的 Skill/Prompt，并产出受任务书约束的决策记录。**已完成 Step 11（治理契约+威胁模型+接入清单）、Step 12（ProviderGateway：6/6 远程出口已接入限流/预算/超时/有界重试/熔断/usage 对账）、Step 13（精确回答缓存：机制完整，但当前语料版本全 unknown 故命中率为 0，且未接进 RAG 主路径）、Step 14（GuardrailPipeline：4/4 工具已接入，确定性正则规则集，红队数据集 33 条）与 Step 15（治理评测集 32 条，8 个失败检测器，聚合指标与本地告警）**；因此**仍不得声称项目具备生产级通用内容过滤或 P4 认证授权**。
- 未完成功能用特性开关护住，不删旧路径。
- 每步验收 = 该步"最小闭环验证"命令全部跑出预期且 `pytest` 不回退。

## 数据资产策略
- 入库：源码、设计文档、评测集、`data/curated/clauses/` + `manifest.json`。
- 不入库（可重建）：`data/indexes/*.db/.vec/.wal/.lock`、`data/quality/reports/`、`data/eval/{reports,traces}/`。
- 重建索引：`scripts/build_index.py`（源自 `data/curated/`）。

## 工程基线入口
- `data/eval/baseline_preflight.json`：基线记录（含 Step -1 与 Step 0–10 post_state），改动前先对照。
- `data/eval/baseline.json`：2026-09-06 更新的 PR 机制层基线，包含 rag_p0 11/12、calc_p0 28/28、docgen_p0 11/11、material_p0 10/10、governance_p0 32/32。
- `docs/design/10-sdd-harness.md`：SDD+Harness 工程规范（规格模板/错误状态语义/完成定义）。

## 术语
- 机制层评测：mock/in-memory，离线确定性。质量层评测：P2 docgen real 只调用 DeepSeek 润色；RAG real 使用 DeepSeek + 百炼 embedding，并可选 judge。缺 Key 可结构化 SKIP。
- observation：工具结果/错误回填给模型，失败文本必须可见。

## 尚未完成（已知阻塞）
- **真实 RAG 质量层 blocked**：2026-09-04 的 `rag_p1 --variant real --judge` 为机制判分 56/58、独立质量判定 36/58（62.07%），退出码 1；失败与低分项必须继续保留，不能写成通过。2026-09-03 的连接错误是历史阻塞证据，已被本次可运行但未达标的结果更新。
- **真实检索矩阵 blocked**：2026-09-04 已重跑；none/local 四组虽为 Recall@1/5/10=1.0、MRR=1.0，但 70 条 query 与条文原文高度同构，属于泄漏 gold，不能建基线；两组百炼 rerank 为 HTTP 403 `AccessDenied`。
- **正式规范引用 blocked**：docgen 真实润色样本使用显式 `FIX` 引用，只证明受限润色与守卫，不证明真实规范检索/正式条文引用质量。`data/versions.json` 的 GB50015-2019、GB50013-2018、GB50014-2021、GB50318-2017 均为 unknown；公告号、生效日期与来源待人工核实，核实后同步更新 `tests/test_version_registry.py::test_status_from_prod_json`。
- **P3 依赖 blocked**：材料统计目前接收结构化管段表；从图纸自动抽取管段仍依赖 P3。P3 于 2026-09-07 启动，Step 17–20 已交付（图纸可读为结构化图元），接缝是 `materials.PipeRun`，由 **Step 21 语义映射**产出——该步未开始，故"图纸→材料清单"链路**尚未闭环**。
- **P3 DWG 直读 blocked**：本机无 ODA File Converter / libredwg（`ezdxf.addons.odafc.is_installed()` 实测 `False`），且转换需 `Capability.EXECUTE_SHELL` —— 该能力在 `Principal` 构造期即被拒绝，**故不可能做成模型可调用的工具**。`.dwg` 恒返回结构化 `DRAWING_UNSUPPORTED_FORMAT`，只能人工预先转 DXF。
- **P3 真实图纸未验证 blocked**：全仓 0 个样例图纸，测试图纸由 ezdxf 程序化合成。真实设计院 DXF 含 INSERT 块参照、XREF 外部参照、非标图层命名与图纸空间视口；当前只证明解析管线与护栏正确，**不得声称**能处理真实图纸。
- **P3 规则引擎条文支撑不足 blocked**：`GB50014-2021 5.2.10`（最小管径与最小设计坡度）、`5.2.13` **均不在语料库**；`GB50015-2019` 全库仅 9 条且 `4.7.1` 自述"非正式规范条文"。Step 21 的规则必须按规格 §6 分级标注（`formal_clause`/`informal_summary`/`engineering_practice`），缺正式条文者**不得声称"已按规范校验"**。
- **护栏路径面已知缺口**：`GuardrailPipeline._looks_like_path` **不识别相对路径**（实测 `plan1.dxf`、`drawings/a.dxf` 的 `validate_tool_call` 返回 `allowed=True`）。它刻意保守以免误伤 `DN200/DN150`，**不能靠收紧正则修复**。新增任何直接收路径参数的工具必须在工具层自行做绝对路径校验，不得依赖护栏路径面；`read_drawing` 就是这么做的（Step 20 实测：相对路径形态只有 pydantic schema 能拦，绝对路径与 `..` 则由护栏先 BLOCK，两组反例分开断言以侦测护栏顺序被改动）。详见 `docs/design/p2.5-threat-model.md` §5 第 23 条、`docs/design/p3-step-17-decision.md` D3 与 `p3-step-20-decision.md` D4。
- `DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY` 非持久配置；无 Key 时结构化 SKIP，有 Key但网络或权限失败时记 blocked。
- `src/water_agent/ingestion/semantic_audit.py` 的 OCR 符号集合已按同类 OCR 噪声字符集还原为 `[#|]{3,}|[?？]{1,}`（2026-09-04）；如需纳入其他乱码符号（如 ■▲◆…）在此扩展并同步测试与质检报告。
