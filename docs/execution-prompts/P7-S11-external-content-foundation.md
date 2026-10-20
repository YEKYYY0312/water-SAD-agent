# P7-S11 执行 Prompt：外部内容底座（来源标签 / 信封归一化 / 受治理 GET 内核 / 回合污点 / 调用方登记）

> **工作区**：仓库 `YEKYYY0312/water-SAD-agent`，基于最新 `main`（必须已包含本 Prompt 与 `docs/design/17-p7-external-capabilities.md`、`17-p7-external-capabilities-harness.md`）。新建分支 `YEK/p7-s11-external-content-foundation`。
> **必读**：
> - `AGENTS.md`
> - `docs/design/17-p7-external-capabilities.md`（SDD：§0 冲突清单、§3 事实表 F1–F12、§4 共同底座、§5.6 回答纪律、§8 威胁模型增量 T15、§10 不变量 P7-I08/I09/I14、§11 的 P7-S11 验收、§12 实施协议、§14 待决事项）与 `docs/design/17-p7-external-capabilities-harness.md`（§0 原则、§2 通用设施、§3 `external_content_p0`）
> - `docs/design/16-p7-plugin-mcp.md` §3.10（`open_exchange` 的语义）、§6（防漂移 harness）、§8（实施协议）
> - `docs/design/p7-step-8-decision.md`（S8：唯一出站模块、SSRF 校验、D13 假服务不起子进程）、`docs/design/p7-step-15-decision.md`（上一步的写法与偏离格式）、`docs/design/p2.5-threat-model.md`
> - 参考实现：`mcp/http_transport.py::open_exchange`（出站内核）、`generation/chat_model.py::list_models`（**已有的、经 `ProviderGateway.call` 包装 `open_exchange` 的读取原语——`governed_get` 的直接样板**）、`runtime/governance/inventory.py`（`_EGRESS_PATTERNS` / `_EGRESS_SCAN_EXEMPT` / 站点登记）、`tests/test_plugin_invariants.py`（AST 不变量写法与"白名单失效即红"）、`graphs/agent_rag.py`（`PER_TURN_RESET`、finalize、`cite_clauses` 可追溯集合）、`runtime/governance/cache.py`（写入资格的稳定拒绝原因）

---

## 0. 相对规格的修订与本步决定（以本文为准）

| 疑问 | 本步做法 | 原因 |
|---|---|---|
| **S11 不新增出站站点** | `PROVIDER_CALL_SITES` 仍是 **8**，`tests/test_vision_review.py` 与 `tests/test_llm_endpoints.py` 的 `== 8` 断言**不动**。`governed_get` 本步只有测试 / 评测调用方，没有任何生产调用方；**真正的出站站点（8 → 9）在 S12 随 `web_search` 一起登记** | 登记一个没有生产调用方的站点，等于清单里有一行永远不会被走到的"已治理"；SDD §0 的 8 → 9 → 10 → 11 本来就写在 S12–S14。**但 `governed_get` 内部走 `ProviderGateway.call` 需要一个 `ProviderKind` / `CallCategory`——本步用现有的 `ProviderKind.MCP_REMOTE` 之外的新值会触及 `contracts.py`（见下一行）** |
| `contracts.py` 的改动范围 | `contracts.py` 在本步**只允许两处**：① `SourceTag.WEB_CONTENT` 与 `SOURCE_TRUST_LEVEL[WEB_CONTENT] = 5`；② **不新增 `ProviderKind` / `CallCategory`**——`governed_get` 本步的网关调用方式由调用方传入 `ProviderCall`（`governed_get(…, call=ProviderCall(...))`），S11 的测试用现有的 `MCP_REMOTE` / `MCP_TOOL_CALL`；S12 再新增 `WEB_SEARCH` 的 kind / category / 画像 | 把"新 provider"留给第一个真正的调用方，S11 不预占枚举值；同时让 `governed_get` 不绑死任何一个调用方 |
| `Capability.WEB_SEARCH` | **本步不加**（SDD D2 的 `GRANTABLE_CAPABILITIES` 扩展属 S12）。`NETWORK_FETCH` 仍在危险集 | 本步没有任何模型可调用的联网工具，没有能力需要授予 |
| `INSTRUCTION_BEARING_SOURCES` | **不改**（受保护断言）。`WEB_CONTENT` 不可承载指令 | 铁律：只有 `SYSTEM_POLICY` 可作指令 |
| 回合污点的触发 | `ToolSpec` 新增可选字段 `result_source: SourceTag | None = None`（缺省 `None`，**现有工具一律不变**）；图在工具结果回填时若该工具声明了 `WEB_CONTENT`，置 state `web_tainted=True`。**本步没有任何生产工具声明它**——污点逻辑只靠测试用的夹具工具 + `external_content_p0` 验证 | 生产工具 `web_search` 是 S12 的；但污点效应（缓存 / 记忆 / 披露 / 网址追溯）必须先于它存在并被证明 |
| `ToolSpec` 的位置 | `ToolSpec` 定义在 `tools/base.py`，而 `tools/**` 在 S15 的 forbid 里——**本步 `tools/base.py` 在 allow 里**，但**只允许加 `result_source` 这一个字段**，不改任何现有字段、校验器与默认值；其他 `tools/*.py` 仍 forbid | 治理清单的静态扫描认 `tools/` 下的 `ToolSpec(` 声明；字段加在基类上最小 |
| 终答网址校验器 | 实现并接进 finalize，但**只在 `web_tainted` 或本 thread 已有 `web_refs` 时才起作用**（SDD R2 的"仅 `WATER_AGENT_WEB_SEARCH=on` 的装配生效"在 S11 没有该开关，改为"有污点才校验"）。没有污点的回合行为**逐字不变** | 缺省路径 0 变化；S12 再把开关接上 |
| 披露行 | 由程序在 finalize 追加固定文案 `【联网信息】本回答引用了联网搜索摘录，未经规范核验，不构成设计依据。`；文案是模块常量、有测试锁定；**不由模型生成** | SDD §4.3 |
| 静态扫描扩展 | 新增模式：`socket.create_connection` / `socket.socket(`、`urllib.request.Request` / `urllib.request.build_opener`、`aiohttp.ClientSession`；`subprocess` 调 `curl` / `wget` 改用 **AST**（首个参数字面量）。**不加** `ssl.create_default_context(`，**不把 `open_exchange(` 加进模式表**。实测命中（见 §1 做第 5 条）必须逐个裁决，不得留红灯 | SDD §4.4 |
| P7-I08 登记表 | `open_exchange(` 的调用方 = `mcp/http_client.py`、`mcp/oauth.py`、`generation/chat_model.py`（PR #45 已合入）、`external/http_get.py`（本步新建）；以**开工时 main 上的真实调用方为准**（Step 0 用 AST 重新枚举，写进决策记录） | SDD 写作时 #45 未合入 |
| 答案渲染 | `AnswerMarkdown` 用 `components.img` 覆盖，**不渲染任何外部图片**（输出纯文本 alt + 域名提示，或直接丢弃）；测试放 `web/app/tests/answer_markdown.test.mjs`（新建）。这是**现有缺口**（SDD F10），与是否有搜索无关 | `deploy/Caddyfile` 的 CSP 只保护经反代的部署，直连 8000 端口没有 |
| 快照 | **现有 19 个 profile 与三个版本号逐字不变**；S11 不新增 profile（没有新装配）。`TOOL_POLICY_VERSION` **不递进**（没有新工具、没有授权变化）；`GOVERNANCE_CONFIG_VERSION` 不递进（没有新画像）；`RULESET_VERSION` 不递进（护栏规则不变）。**唯一预期的版本变化是缓存键的格式版本是否因新增拒绝原因而变——拒绝原因是写入资格而不是键因子，不递进**；若实测发现必须递进，按 §5 停止 | 缺省路径零变化是本步的底线 |
| 评测 | `external_content_p0`（~32 条，Step 1 冻结）+ 敌意语料 `data/eval/hostile_web_corpus.jsonl`（~64 条，**先于代码冻结**）接进 `ci_gate.sh` 与入仓基线 | 评测先行 |
| 假服务 | `tests/fixtures/fake_search_server.py` **本步不建**（S12 的）。`governed_get` 的测试用一个最小的进程内 `http.server` 线程夹具（`tests/fixtures/fake_http_peer.py`，记录收到的请求数与路径）；**不起子进程**（S8 D13） | 本步只验证内核，不验证任何 provider |

## 1. 目标与边界

**做**（编号对应 SDD §11 的 P7-S11-Axx）：

1. **来源标签（A01）**：`SourceTag.WEB_CONTENT`，信任级别 5（`TOOL_OUTPUT` 10 之下、`GENERATED_OUTPUT` 0 之上）；`relabel` 只降不升；偏序测试更新；`INSTRUCTION_BEARING_SOURCES` 不动。
2. **信封与归一化（A02）**：`external/envelope.py`——
   - `wrap_web_content(items, *, source)`：随机 16 位十六进制 id 的信封 `<<<WEB_CONTENT id="…">>>…<<<END_WEB_CONTENT id="…">>>` + 固定声明行；
   - **核心边界归一化**对**每一个文本字段**（标题 / 摘要 / 描述 / 错误文本……，而不是某一条路径）依次：① 剥 provider 自带的任何信封 / 标记；② 正文里形似边界的文本（全角括号、零宽拆分、超长 id、JSON 转义变体、大小写 / 空白变体、只有结束标记、藏在网址里）→ `[[MARKER_SANITIZED]]`；③ 聊天模板特殊 token（ChatML / Llama `[INST]` / Gemma / harmony / Mistral…）→ `[REMOVED_SPECIAL_TOKEN]`；④ 去不可见 Unicode（零宽、bidi 控制、Tag 字符 U+E0000 区、软连字符）与控制字符（NUL / ESC…）；⑤ 去 Markdown 图片 / 引用式图片 / 自动链接 / 尖括号网址 / `<img>` 语法（只留纯文本网址）；⑥ 折叠空白、硬长度上限（字段与总量两级）；
   - 注入检测（复用 `detect_injection`）命中 → **丢弃该条**（不是脱敏放行）并计数 `dropped_suspicious`；
   - 归一化是**纯函数、确定性**，不调模型；**零宽字符只能写成 `\uXXXX` 转义**（AGENTS.md：字面字符会在编辑中静默消失）；替换后不得膨胀超过预算（模板 token 展开类用例）；截断不得切断替换串与代理对。
3. **受治理 GET 内核（A03）**：`external/http_get.py::governed_get(url, *, headers, max_bytes, timeout, allow_private_network, call, resolver=None, ssl_context=None, gateway=None)`——包装 `open_exchange`（解析后整体分类目的地址、钉死 IP、**3xx 不跟随**、字节与时间上限）并经 `ProviderGateway.call`（限流 / 熔断 / 对账；usage 记**未知**）。错误统一映射成本规格的稳定码（`EXTERNAL_DESTINATION_FORBIDDEN` / `EXTERNAL_TIMEOUT` / `EXTERNAL_UNAVAILABLE` / `EXTERNAL_AUTH` / `EXTERNAL_RATE_LIMITED` / `EXTERNAL_BAD_RESPONSE` / `EXTERNAL_TOO_LARGE`），**不泄漏 `MCP_*` 命名空间**，错误文本只含码与一句话、不含上游正文 / 网址 / 密钥。`resolver` 与 `ssl_context` 可注入（供 DNS 重绑定与自签证书类用例）。**无任何原始网络原语**（`socket` / `http.client` / `urllib` / `requests`……）——只能经 `open_exchange`。
4. **P7-I08（A04）**：`open_exchange(` 的调用方 ∈ 登记表，AST 判定；登记项不再对应真实代码同样判失败；反证：在未登记模块调用即红。
5. **静态扫描扩展（A05）**：见 §0。**实测命中的四项必须逐个裁决并写进决策记录**（2026-10-13 对 main grep）：`harness/mcp_http_eval.py`（`socket.create_connection`，评测假服务的回环客户端侧 → 豁免并写理由）、`webapp/smoke.py`（`urllib.request.Request` / `build_opener`，**现有扫描漏掉的真实出站**，冒烟只连 app 自身 → 登记为出站模块（回环目的地）或豁免并写理由，二选一，写进决策记录）、`runtime/governance/rules.py`（`curl` / `wget` 字面量，护栏检测对象 → AST 判定后不再命中）、`mcp/http_transport.py`（`socket.socket(`，已登记）。每个新模式都配"放进未登记模块即红"的反证。
6. **回合污点（A06）**：`ToolSpec.result_source` → state `web_tainted`（`PER_TURN_RESET` 复位）；效应——① 答案缓存写入资格新增稳定拒绝原因 `web_content_used`（`REASON_WEB_CONTENT_USED`，拒绝原因集合 +1，锁定测试同步）；② 该回合内由模型 / 系统发起的记忆写入走 `check_memory_write` 的来源检查并 BLOCK（用户在记忆面板显式确认的写入不受影响）；③ finalize 由程序追加披露行；④ 本 thread 的 `web_refs`（工具返回的**规范化网址**集合）供下一条使用。**P7-I14**：`web_tainted` 回合的缓存写入资格恒为 false（`tests/test_answer_cache.py`）。
7. **终答网址追溯（A07）**：终答里出现的 http(s) 网址 ⊆ 本 thread 的 `web_refs` ∪ 用户消息里的网址，否则 `unverified_url` → 回流一次（复用引用校验的回流机制）、再失败即 `failed`；`web_refs` 跨轮累积（读 checkpoint，与 `cite_clauses` 可追溯集合同源思路，**不依赖进程内状态**）；**无污点且无 `web_refs` 的回合不校验、行为逐字不变**。**R1**：`web_refs` 永不进"本 thread 检索到的 source_id 集合"（`cite_clauses` 只接受语料 `source_id`）。
8. **答案渲染（A08）**：`AnswerMarkdown` 不渲染外部图片；链接保持 `rel="noopener noreferrer nofollow"`；前端 node 测试。
9. **评测（A09）**：`external_content_p0` + `hostile_web_corpus.jsonl` + 比较器 + 入仓基线 + `ci_gate.sh`；每个硬指标一条反证。
10. **追溯**：`P7-S11-A01…A09` 验收行登记进 16 号 §7（追溯检查只认 16 号规格）；17 号 SDD 的不变量在测试里用 `SDD17-I08` 写法引用，**不改** `tests/test_p7_traceability.py`。
11. **文档**：决策记录 `docs/design/p7-step-11-decision.md`（结构见 `16-p7` §8 第 6 条）、`p2.5-threat-model.md` 登记 T15 的底座部分、`p2.5-provider-inventory.md`（注明 `governed_get` 与 P7-I08）、`17` 号 SDD 把 S11 验收行翻成 `delivered`、AGENTS.md 追加条目。

**不做**：`web_search` 工具与任何 provider 适配器（S12）；`Capability.WEB_SEARCH`、`GRANTABLE_CAPABILITIES` 扩展、新 `ProviderKind` / `CallCategory` / 画像（S12）；出站站点 8 → 9（S12）；`QueryEgressGuard`（S12）；skill 下载、MCP 目录（S13 / S14）；搜索结果缓存、语义缓存；任何网址抓取（`web_fetch` / 浏览器）；改 `gateway.py` / `pipeline.py` 的规则语义；改 `DANGEROUS_CAPABILITIES`、`NETWORK_FETCH`；改 `INSTRUCTION_BEARING_SOURCES`；改预算数字（`cost < 4500`、`schema_chars_total == 4497`、系统提示 `< 1950`）；改 `plugins/**`、`mcp/**`（`open_exchange` 本身不动）、`prompting/**`；声称任何真实外部服务可用。

## 2. 步骤序列（评测先行；一个逻辑面一个 commit）

0. **Step 0 读原文**（SDD §12 补充 1）：用 AST 重新枚举 `open_exchange(` 的真实调用方；对 main 重跑 §1 第 5 条的四项 grep；读 `list_models` 的 `ProviderGateway.call` 包装方式；跑 `tool_assembly_snapshot --check` 确认起点干净。结果写进决策记录 §0.2。
1. **Step 1 冻结**：`data/eval/hostile_web_corpus.jsonl`（~64 条，类别与条数见 harness §2.2）、`data/eval/external_content_p0.jsonl`（~32 条，harness §3）、指标阈值、比较器骨架。**敌意语料先于被测代码写成**，每行带 `expect`（归一化后必须消失 / 必须保留什么）。
2. **Step 2 harness**：`harness/external_content_eval.py`、`tests/fixtures/fake_http_peer.py`（进程内线程、记录收到的请求数与路径）；此时机制未实现，红灯基线记入决策记录。
3. **Step 3 机制**（每块先写失败测试，docstring 写验收 ID）：`SourceTag.WEB_CONTENT` → `envelope.py` → `http_get.py` → `ToolSpec.result_source` 与污点 → 缓存拒绝原因 / 记忆拦截 / 披露行 → 终答网址校验器 → P7-I08 → 静态扫描扩展 → 前端。
4. **Step 4 反证与基线**：补齐每个硬指标的反证；生成入仓基线；接 `scripts/ci_gate.sh`。
5. **Step 5 快照与追溯**：快照 `--check` 必须仍一致（19 个 profile 不变）；`P7-S11-A01…A09` 登记进 16 号 §7，`test_p7_traceability.py` 通过。

## 3. 验证命令

```bash
export PYTHONPATH=src   # worktree 里必须；并 unset WATER_AGENT_* / *_API_KEY / HTTPS_PROXY / LOCAL_MODEL_*
_venv/bin/pytest -q tests/test_governance_contracts.py tests/test_external_envelope.py tests/test_external_http_get.py tests/test_plugin_invariants.py tests/test_web_taint.py tests/test_answer_cache.py tests/test_external_content_eval.py
_venv/bin/python -m water_agent.harness.external_content_eval data/eval/external_content_p0.jsonl --report-path data/eval/reports/external_content_p0_head.json
_venv/bin/python scripts/compare_external_content_p0.py --head data/eval/reports/external_content_p0_head.json --base data/eval/external_content_p0_main.json
_venv/bin/python -m water_agent.harness.tool_assembly_snapshot --check       # 必须仍是 19 个 profile 一致，本步不 --write
(cd web/app && npm test && npm run build)
COMPOSE_PROJECT_NAME=p7s11 bash scripts/ci_gate.sh      # 本机有别的 water-agent 栈在跑时必须隔离
_venv/bin/python scripts/check_step_scope.py docs/execution-prompts/P7-S11-external-content-foundation.md --base origin/main
```

另跑 `pytest -o addopts= -q`（ci_gate 的 pytest 是 `-qq`，没有汇总行）取全量计数；`ruff check src`、`mypy src/water_agent` 各自单独看退出码。

## 4. 受保护断言

- 预算：`assert cost < 4500`、`schema_chars_total == 4497`、系统提示 `< 1950`（本步不碰工具与提示）。
- 信任：`INSTRUCTION_BEARING_SOURCES == frozenset({SourceTag.SYSTEM_POLICY})`。
- 出站：`len(PROVIDER_CALL_SITES) == 8`（本步**不新增**出站站点）。
- 缺省不变：`test_defaults_are_unchanged`、`temperature == 0.0`、`max_retries == 0`、`stream_usage is True`。
- 危险能力：`NETWORK_FETCH` 仍在 `DANGEROUS_CAPABILITIES`，`Principal` 构造期仍拒绝授予（本步不改）。
- **会变的锁定数字 / 语义**（本步唯一允许改的）：`SourceTag` 枚举成员数与偏序表（`test_governance_contracts.py`）、缓存拒绝原因集合（`test_answer_cache.py`）、静态扫描的模式数；这些变化都要在决策记录里写明。

## 5. 停止条件

满足任一条就停下，写清现象后等确认：

- 现有 19 个快照 profile 或三个版本号有任何变化；缺省配置（无污点、无外部工具）下任何机制层评测或 `baseline.json` 对比出现差异；
- 需要改 `gateway.py` / `pipeline.py` / `inventory.py` 的**站点登记**（`PROVIDER_CALL_SITES`）、`plugins/**`、`mcp/**`、`prompting/**`，或需要新增 `Capability` / `ProviderKind` / `CallCategory` / 出站站点；
- 需要改 `DANGEROUS_CAPABILITIES`、`INSTRUCTION_BEARING_SOURCES`，或上调任何预算上限；
- 追溯检查在不改 `tests/test_p7_traceability.py` 的前提下无法通过（说明 `SDD17-Ixx` 写法或 16 号 §7 登记有误）——先说明现象，不得改该文件；
- 静态扫描新增模式在 main 上命中了 §1 第 5 条之外的模块，且无法在"登记 / 豁免并写理由"之间裁决；
- 归一化的某条敌意语料无法被中和而不误伤良性对照（说明规则要改设计，不是调阈值）；
- 门禁出现与本步无关的红灯。

## 6. 汇报格式

改了哪些文件；跑了哪些验证命令以及实际输出（失败照贴）；范围守卫输出；快照 `--check` 输出（应为"一致"，无 `--diff`）；`open_exchange` 调用方的 AST 枚举结果与静态扫描四项命中的裁决；**未验证项与原因**（真实外部服务、真实模型是否被"中和后的"文本影响——归一化是确定性正则，必有语料之外的变体）；遗留风险。**不得**写"能拦住任意注入"，**不得**声称任何真实搜索服务 / 注册表可用。

---

<!-- step-scope -->
```json
{
  "step": "P7-S11",
  "allow": [
    "src/water_agent/external/**",
    "src/water_agent/runtime/governance/contracts.py",
    "src/water_agent/runtime/governance/inventory.py",
    "src/water_agent/runtime/governance/cache.py",
    "src/water_agent/runtime/governance/cache_service.py",
    "src/water_agent/runtime/governance/pipeline.py",
    "src/water_agent/tools/base.py",
    "src/water_agent/tools/ds_adapters.py",
    "src/water_agent/graphs/agent_rag.py",
    "src/water_agent/runtime/agentic.py",
    "src/water_agent/harness/external_content_eval.py",
    "scripts/compare_external_content_p0.py",
    "scripts/ci_gate.sh",
    "data/eval/external_content_p0.jsonl",
    "data/eval/external_content_p0_main.json",
    "data/eval/hostile_web_corpus.jsonl",
    "web/app/src/AnswerMarkdown.tsx",
    "web/app/src/answerImages.ts",
    "web/app/src/styles.css",
    "web/app/tests/answer_markdown.test.mjs",
    "tests/fixtures/fake_http_peer.py",
    "tests/test_external_envelope.py",
    "tests/test_external_http_get.py",
    "tests/test_web_taint.py",
    "tests/test_external_content_eval.py",
    "tests/test_governance_contracts.py",
    "tests/test_plugin_invariants.py",
    "tests/test_answer_cache.py",
    "docs/design/p7-step-11-decision.md",
    "docs/design/17-p7-external-capabilities.md",
    "docs/design/17-p7-external-capabilities-harness.md",
    "docs/design/16-p7-plugin-mcp.md",
    "docs/design/p2.5-threat-model.md",
    "docs/design/p2.5-provider-inventory.md",
    "docs/execution-prompts/P7-S11-external-content-foundation.md",
    "AGENTS.md"
  ],
  "forbid": [
    "src/water_agent/runtime/governance/gateway.py",
    "src/water_agent/runtime/governance/rules.py",
    "src/water_agent/runtime/governance/config.py",
    "src/water_agent/plugins/**",
    "src/water_agent/mcp/**",
    "src/water_agent/prompting/**",
    "src/water_agent/graphs/confirmation.py",
    "src/water_agent/generation/**",
    "src/water_agent/tools/[!bd]*",
    "src/water_agent/multi_agent/**",
    "src/water_agent/webapp/**",
    "src/water_agent/storage/**",
    "alembic/**",
    "compose.yml",
    "compose.production.yml",
    ".github/**",
    "tests/conftest.py",
    "tests/test_p7_traceability.py",
    "tests/test_check_step_scope.py",
    "tests/test_import_guard.py",
    "tests/test_tool_exposure_eval.py",
    "tests/test_vision_review.py",
    "tests/test_llm_endpoints.py",
    "tests/test_tool_assembly_snapshot.py",
    "data/eval/baseline.json",
    "data/eval/tool_assembly_main.json",
    "data/eval/tool_exposure_p0.jsonl",
    "data/eval/tool_exposure_p0_main.json",
    "data/eval/skill_p0_main.json",
    "data/eval/mcp_http_p0_main.json",
    "data/eval/model_choice_p0_main.json",
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
        "/versions/TOOL_POLICY_VERSION",
        "/versions/PROMPT_TEMPLATE_VERSION",
        "/profiles/session_*",
        "/profiles/runner_*"
      ]
    }
  ]
}
```
<!-- /step-scope -->

> **范围块说明（写 Prompt 时已逐条核对 main，开工前请再核一遍）**：
>
> - **`tests/test_vision_review.py`、`tests/test_llm_endpoints.py`、`tests/test_tool_assembly_snapshot.py`、`data/eval/tool_assembly_main.json` 在 forbid 里**：S11 不新增出站站点、不新增快照 profile，这些文件的断言（`== 8`、19 个 profile）必须原样成立；`frozen_json` 因此冻结**全部** `session_*` 与 `runner_*`，没有"新 profile 不被匹配"的例外。若实测必须新增站点或 profile，按 §5 停止。
> - **`protect` 里没有 `len(PROVIDER_CALL_SITES) == 8`**：该断言在 `tests/test_vision_review.py` 里，而那个文件在 forbid 里（本身就守住了它）；`tests/test_llm_endpoints.py::test_defaults_are_unchanged` 同理。
> - **`tests/test_p7_traceability.py` 在 forbid 里**：它断言 16 号规格的不变量恰好 7 条，S15 因此把 18 号 SDD 的不变量改成了不带 `P7-` 前缀的引用（决策记录 `p7-step-15-decision.md` §0.1 第 2 条）。评审已裁决照 S15 做法（见下条），本步不需要改这个文件。
> - **`docs/design/16-p7-plugin-mcp.md` 已放进 allow**（采纳推荐方案）：本步在 16 号 §7 登记 `P7-S11-A01…A09` 验收行并把 S11 纳入 §5 分步表；S11 测试 docstring 引用 17 号 SDD 的不变量时用 `SDD17-I08` 这种不带 `P7-` 前缀的写法（带前缀会被 `test_p7_traceability.py` 的反向检查拦下），**不改** `tests/test_p7_traceability.py`。
> - `src/water_agent/generation/**` 在 forbid 里：`list_models` 已经是 `open_exchange` 的合规调用方，本步只读它、不改它。
> - `src/water_agent/runtime/governance/pipeline.py` 在 allow 里仅为 `check_memory_write` 的来源检查（污点 BLOCK）；不得改其他护栏规则（`rules.py` 在 forbid 里，`RULESET_VERSION` 不递进）。
> - `src/water_agent/runtime/agentic.py` 在 allow 里仅为把 `web_refs` 读出 checkpoint 接进可追溯集合的同款机制；不得改会话装配、工具暴露与提示。
> - `webapp/**` 在 forbid 里：本步没有 Web 路由、没有请求字段（逐条消息勾选 `web_search` 是 S12）。
