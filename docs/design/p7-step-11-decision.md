# P7-S11 决策记录：外部内容底座（来源标签 / 信封归一化 / 受治理 GET 内核 / 回合污点 / 调用方登记）

> 日期：2026-10-19。分支 `YEK/p7-s11-external-content-foundation`，基于 main `267bc3f`（S0–S10、S15 与本步执行 Prompt 已合入）。
> 执行 Prompt `docs/execution-prompts/P7-S11-external-content-foundation.md`，规格 `docs/design/17-p7-external-capabilities.md`（§3 事实表、§4 共同底座、
> §5.6 回答纪律、§8 T15、§10 不变量 I08 / I09 / I14、§11 的 P7-S11 验收）与 `17-p7-external-capabilities-harness.md`（§2 通用设施、§3 `external_content_p0`），
> 16 号规格 §7 登记的 `P7-S11-A01…A09`，威胁模型 `p2.5-threat-model.md` T15 底座部分。

**一句话结论**：交付外部内容获取的**共同底座**——联网内容有了自己的来源标签（信任 5、不可承载指令）、有了每个文本字段都过的确定性归一化与随机 id 信封、
有了唯一的受治理 GET 内核、有了回合污点及其效应（缓存恒拒绝、披露行、终答网址须可追溯、记忆写入 fail closed），并把"谁能调用出站内核"和更多种原始网络写法
纳入机器检查，顺手兜住了一个与搜索无关的既有缺口（回答里的外部图片）。**没有任何模型可调用的联网工具、没有新的出站站点（仍是 8）**——`web_search` 是 S12。
**不得声称能拦住任意注入**：归一化是确定性正则，`external_content_p0` 的 1.0 只说明语料里的写法被覆盖。

## 0. 偏离与修订

### 0.1 执行 Prompt §0 的修订

1. **范围块补遗（单独的文档 PR，先合入 main）**：执行 Prompt 的范围块有两处与实现冲突，范围守卫对 `origin/main` 报 2 处越界：
   - `src/water_agent/tools/ds_adapters.py`：Prompt 的 `allow` 只放行了 `tools/base.py`，同时 `forbid` 了 `tools/[!b]*`。但污点只认 `ToolMessage.artifact`（D3）要求联网工具的结果在适配层
     拆成 `content` + `artifact`，而 `ds_adapters.py` 是 `ToolSpec` → LangChain 工具的**唯一**适配层，没有别的位置可以写。这是 Prompt 的疏漏，不是实现可以绕开的。
     改动限定为三处：`_with_web_artifact`、`WEB_CONTENT_ARTIFACT` 常量、以及 `result_source is WEB_CONTENT` 时才走新分支（其余工具的返回值与此前逐字相同）。
   - `web/app/src/answerImages.ts`（新建）：前端测试用 node strip-types 直跑，不能 import 带 JSX 的组件文件，所以把纯函数 `imageAsText` 拆到独立的 `.ts`。
   按 S15 先例（#48）走独立文档 PR：分支名不以 `YEK/p7-s<n>-` 开头，只改执行 Prompt 的 `allow` / `forbid`；合入 main 后把 main 合进本分支，再跑范围守卫。范围块从 merge-base 读，分支改不了自己的范围。
   补遗时把 `forbid` 里的 `tools/[!b]*` 收窄为 `tools/[!bd]*`（让 `base.py` 与 `ds_adapters.py` 之外的 `tools/*.py` 仍然禁止），其余 `forbid` 不动。
2. **18 / 17 号 SDD 的不变量引用**：评审已裁决照 S15 做法（PR #50）：测试 docstring 用 `SDD17-Ixx` 引用不变量，验收行登记进 16 号 §7，`tests/test_p7_traceability.py` 未改。

### 0.2 实测后追加的偏离

- **D1 评测集 44 条而不是设计目标 ~32 条**。harness 文档 §3 的 ~32 条是各类别条数的"设计目标"，Step 1 冻结时按 14 项硬指标各需要的覆盖面（尤其 `governed_get` 内核 5 条、
  `default` 对照 2 条、`citation_integrity` 1 条）扩到 44 条；敌意语料 64 条与设计一致。冻结之后没有改动过任何一条用例。
- **D2 `ToolSpec` 的 import 方向**。`tools/base.py` 要引用 `SourceTag`，而 `contracts.py` 不依赖 `tools`，所以没有循环导入；`ds_adapters.py` 引用 `external.taint.normalize_url`
  是单向的（`external/` 不依赖 `tools/`）。
- **D3 污点与网址集合只认 `ToolMessage.artifact`，不认正文**。执行 Prompt 只写了"图在工具结果回填时置 `web_tainted`"；实现时沿用 S7 `PENDING_CONFIRMATION_ARTIFACT` 的做法——
  artifact 由本地代码按 `ToolSpec.result_source` 写入、不发给模型，而正文由工具输出决定，任何来源都能写成同样的 JSON。声明了 `WEB_CONTENT` 的工具在 `ToolNode` 里以
  `response_format="content_and_artifact"` 的形式出现，工具失败（结构化错误）也算联网过——请求已发出，污点照记，网址集合为空。
- **D4 记忆写入拦截是入口约束，不是对现有路径的修复**。执行 Prompt §1 第 6 条写"该回合内由模型 / 系统发起的记忆写入走 `check_memory_write` 的来源检查并 BLOCK"。实测：
  **当前代码里根本没有模型 / 系统发起的记忆写入路径**（记忆只有面板里用户显式确认一条，来源是 `USER_MESSAGE`；`grep` 全 `src` 只有 `memory_routes.py` 经 `ExplicitMemoryWriteService`）。
  所以我让 `GuardrailPipeline.check_memory_write` 对 `WEB_CONTENT` 来源 fail closed（规则 `memory.web_content`），并在测试里如实写明：将来有人把记忆写入接到模型发起的路径上，
  联网内容也进不去；它不是对某个现有写入路径的修复。`pipeline.py` 在 allow 里仅为此一处。
- **D5 终答网址校验器没有开关**（对 SDD R2 的偏离，已写进 17 号 SDD）。SDD R2 写"仅当 `WATER_AGENT_WEB_SEARCH=on` 的装配生效"，S11 还没有这个开关，所以改为"本回合
  `web_tainted` 或本 thread 已有 `web_refs` 才校验"。没用过联网内容的回合不进这条分支，行为逐字不变（`ec-default-*` 两条与既有 326 条 agentic / 缓存 / 暴露测试锁定）。
- **D6 `__all__` 与 `PER_TURN_RESET` 的新键**。`PER_TURN_RESET` 新增 `web_tainted: False`、`web_url_reflows: 0`；`web_refs` **不在**复位表里（跨轮累积，测试锁定）。
  `AgenticRAGState` 新增三个 `NotRequired` 字段。只有确有联网工具结果时才把 `web_tainted` / `web_refs` 写进 agent_loop 的返回增量——缺省路径的 state 增量与此前逐字相同。
- **D7 截断缺陷（评测发现并修复）**。`ec-budget-no-surrogate-cut` 第一次跑就红了：`_truncate` 只对替换串的**首字符**匹配，`[[MARKER_SANITIZED]]` 会被切成 `[[MARK`。
  改为在原文里找出每个替换串的出现区间，截断点落进区间内部就退到区间起点；补了 `test_truncation_never_cuts_through_a_replacement_token`（30 个截断位置 × 2 种替换串）。
- **D8 全角标点不能整段 NFKC**。第一版对每个字段无条件做 NFKC，良性对照 `hc-benign-terms`、`hc-benign-zh-punct` 立刻红：NFKC 把中文工程文本里常见的全角逗号 / 分号 / 括号
  折成半角，违反"良性文本不被改写"。改为只有出现全角尖括号 / 竖线 / 方括号 / 感叹号或兼容连字时才折叠（这些是边界、模板 token、外发语法的全角变体必然包含的字符）。
  代价：不含这几类字符的全角写法不会被折叠——它们本来也不构成上述三类攻击。
- **D9 零宽字符只用 `\uXXXX` 转义**（AGENTS.md 既有纪律）。语料文件 `hostile_web_corpus.jsonl` 用 `ensure_ascii=True` 写盘（第一次用 `ensure_ascii=False` 写出了字面不可见字符，
  当场发现并重写；现在全文件 ASCII，测试断言），`envelope.py`、`external_content_eval.py`、测试文件里的不可见字符全部是转义，并用 `unicodedata` 扫描确认源文件里没有字面 `Cf` / `Cc` 字符。
- **D10 一个自己写错的测试被反证抓出来**。`test_local_tools_take_no_destination_argument` 第一版通过得太容易——我怀疑它根本没遍历到工具（`contribution.tools` 字段名是猜的）。
  实测字段名对，但仍补了两处：断言确实遍历到了 `retrieve_clauses` / `cite_clauses` / `calc_velocity`，并加一条"塞进带 `url` 参数的假工具必须被报出"的反证。
- **D12 放宽了结构不变量 P7-I07 的评测白名单（需评审知悉）**。`external_content_p0` 的回合污点用例要经**生产图**验证夹具联网工具，而 `AgenticSession` 没有注入额外插件的通道
  （S11 的 `forbid` 里有 `plugins/**`，`runtime/agentic.py` 的 allow 也只限于 `web_refs` 接线），所以评测直接调用 `build_agentic_app`——这违反 P7-I07（"只有 `runtime/agentic.py` 与
  `harness/runner.py` 调用 `build_agentic_app`"）。做法：评测的**工具装配仍走生产的 `assemble_tools`**（不自己构造 `ToolRegistry` / 转换 `BaseTool`，P7-I01 / I02 不需要开白名单），
  只在 `tests/test_plugin_invariants.py` 里给 P7-I07 增加一个 `EVAL_GRAPH_CALLERS` 评测白名单（带理由、过期即失败），不影响装配快照的捕获点前提（快照只采两条生产路径）。
  这是**有意识的放宽，不是中性改动**：S12 的 `web_search` 接入生产装配后，评测应改走 `AgenticSession`，这条白名单随之删除。第一版评测曾自己构造 `ToolRegistry` 与 `tool_spec_to_base_tool`，
  被全量测试里的 P7-I01 / I02 抓出来，改成上述做法。
- **D11 R1 的第二道防线**。联网网址当作 `cite_clauses` 的 `source_id` 时，工具层的参数护栏会先把它当成"域名不在 allowlist"拦下（第一道）。`ec-spec-impersonation-not-citable`
  刻意绕开工具层、直接走**校验器层**（`_extract_validation_signal` + `validate_citations`）证明第二道：即使护栏被拿掉，校验器依然把网址判无效。反证把 `web_refs` 并入检索集合即变红。

## 1. 交付

| 面 | 内容 | 锁定 |
|---|---|---|
| 来源标签 | `SourceTag.WEB_CONTENT`，信任 5；`INSTRUCTION_BEARING_SOURCES` 不变；`relabel` 只降不升 | `tests/test_governance_contracts.py::TestSourceTagNotDowngradable::test_web_content_sits_between_tool_output_and_generated_output` |
| 信封与归一化 | `external/envelope.py`：`neutralize_text`（逐字段）、`wrap_web_content`（随机 16 位 id、固定声明行、注入命中整条丢弃、字段与总量上限、不切替换串）；纯函数，不 import 模型 / 网络 | `tests/test_external_envelope.py`（71 项，含 64 条语料逐条） |
| 受治理 GET 内核 | `external/http_get.py::governed_get`：`parse_server_url` → `open_exchange` → `ProviderGateway.call`；7 个稳定错误码（不泄漏 `MCP_*`），错误文本不含上游正文 / 网址 / 密钥；usage 未知；`resolver` / `ssl_context` 可注入；模块无原始网络原语 | `tests/test_external_http_get.py`（23 项）、`tests/fixtures/fake_http_peer.py`（进程内线程，不起子进程） |
| 回合污点 | `ToolSpec.result_source`（缺省 `None`）；`ds_adapters` 在声明了 `WEB_CONTENT` 的工具结果上写 artifact；`web_signal` 读 thread；`web_tainted`（逐轮复位）、`web_refs`（跨轮）；`REASON_WEB_CONTENT_USED`；披露行由 finalize 程序追加；`check_memory_write` 对 `WEB_CONTENT` fail closed | `tests/test_web_taint.py`（12 项）、`tests/test_answer_cache.py`（拒绝原因进参数化表） |
| 终答网址追溯 | `external/taint.py`（`normalize_url` / `extract_urls` / `unverified_urls`）；`citation_validator` 在引用校验通过后追加网址校验，回流一次，再失败 `failed`；反馈文本稳定且不含网址 | `tests/test_web_taint.py` |
| 调用方登记 | SDD17-I08：`open_exchange(` 的调用方 ∈ 登记表（4 项：`mcp/http_client.py`、`mcp/oauth.py`、`generation/chat_model.py`、`external/http_get.py`），AST 判定，登记项过期同样判失败；SDD17-I09：本地工具无目的地参数 | `tests/test_plugin_invariants.py`（各带反证） |
| 静态出站扫描 | 新增 `socket.create_connection` / `socket.socket(` / `urllib.request.Request`·`build_opener` / `aiohttp.ClientSession` 与子进程 `curl`·`wget`（AST，首个参数最左字符串字面量）；**不加** `ssl.create_default_context(` 与 `open_exchange(`；豁免恰为三项 | `tests/test_governance_contracts.py::TestInventoryCoverage`（8 种写法各一条反证 + 4 条良性对照 + 注释行为 + 豁免集合 + 站点数 8） |
| 答案渲染 | `AnswerMarkdown` 覆盖 `img`：不渲染任何图片，纯文本占位（alt + 主机名，不含路径 / 查询串，不是链接）；`imageAsText` 在独立 `.ts`（node strip-types 测试不能 import JSX） | `web/app/tests/answer_markdown.test.mjs` |
| 评测 | `external_content_p0`（44 条，14 项硬指标）+ `hostile_web_corpus.jsonl`（64 条）+ 比较器 + 入仓基线 + `ci_gate.sh`；每个硬指标一条反证（另有"每个指标都有反证"的完备性断言） | `tests/test_external_content_eval.py`（17 项） |
| 文档 | 16 号规格 §5 / §7 登记，17 号 SDD 状态翻转与 §4.4 实际裁决，威胁模型 T15 底座部分，provider inventory 注记，AGENTS.md | `tests/test_p7_traceability.py` |

## 2. 实测

`external_content_p0`（`python -m water_agent.harness.external_content_eval`，两次运行报告逐字相同、不含临时路径；入仓基线与 HEAD 逐字相同）：

```text
external_content_p0 passed=44/44
  marker_forgery_neutralized_rate: 1.0
  template_token_residue_count: 0
  invisible_char_residue_count: 0
  exfil_syntax_residue_count: 0
  instruction_bearing_leak_count: 0
  taint_effect_rate: 1.0
  budget_violation_count: 0
  benign_alteration_rate: 0.0
  url_traceability_rate: 1.0
  citation_integrity_violation_count: 0
  zero_byte_rate: 1.0
  error_leak_count: 0
  default_unchanged_rate: 1.0
  harness_error_count: 0
```

残留检测用评测自己的字符表和正则，**不复用被测代码的正则**（被测规则漏了、评测不能跟着漏）。反证（拿掉保护 → 指标变红）14 条全部成立：边界归一化、模板 token 移除、不可见字符剥离、
外发语法剥离、注入不丢弃、污点不置位、长度上限、良性文本被改写、网址不校验、联网网址并入可引用集合、目的地分类放行、错误回显上游正文、缺省路径被置污点、未知用例类型。

**答案渲染的真实渲染验证**（不是源码字符串检查）：用 `vite build --ssr` 把 `AnswerMarkdown` 打成 Node 可跑模块，再用 `react-dom/server` 渲染含恶意图片的 Markdown——
行内图片、引用式图片、`<img onerror>`、`data:` 图片四种写法都没有 `<img>`、目标网址不在任何属性里；普通链接保持 `target=_blank rel="noopener noreferrer nofollow"`；
良性中文（`[0.6, 2.0]`、`**DN200**`）不变。该脚本是临时的、未入库（见 16 号规格"未覆盖项"）。

### 2.1 测试与门禁

各命令单独运行、单独看退出码（本机无 Key；30 个 skip 是 PG 门控等既有前置条件）：

```text
$ pytest -o addopts= -q                                  # 全量
3969 passed, 30 skipped, 788 warnings in 271.30s        exit=0
$ ruff check src                                          # ci_gate 的口径
All checks passed!                                        exit=0
$ mypy src/water_agent
Success: no issues found in 249 source files              exit=0
$ python -m water_agent.harness.tool_assembly_snapshot --check
装配快照与基线一致（19 个 profile）                        exit=0
$ (cd web/app && npm test && npm run build)
tests 113 / pass 113 / fail 0；tsc + vite build 通过        exit=0
$ COMPOSE_PROJECT_NAME=p7s11 bash scripts/ci_gate.sh      # 含 external_content_p0(44/44) 与基线对比
==> CI 门禁通过：… + model_choice_p0(50/50) + external_content_p0(44/44) + … + 基线对比   exit=0
```

全量里曾出现过 4 个只在全量下才暴露的失败（单跑都通过），都是真实回归，已修：
`test_dwg_upload::test_subprocess_only_in_registered_modules`（`inventory.py` 注释里写了 `subprocess.run(` 字样，被既有的子进程扫描误命中——改了注释措辞）；
`test_plugin_invariants::[P7-I01 / P7-I02 / P7-I07]`（评测与测试第一版自己构造 `ToolRegistry`、转换 `BaseTool`、直接构图，违反结构不变量——见 D12）。

范围守卫：对 `origin/main` 跑报 2 处越界（`tools/ds_adapters.py`、`web/app/src/answerImages.ts`），见 §0.1 第 1 条；**正式结论要在补遗 PR 合入、main 合进本分支之后再跑一次**。

未做：真实 Key / 真实搜索服务的任何调用（S11 没有 provider）；浏览器手动验收。

## 3. 快照变化

**无变化。** `tool_assembly_snapshot --check`：19 个 profile 与基线一致，三个版本号（`MCP_CONFIG_VERSION` / `TOOL_POLICY_VERSION` / `PROMPT_TEMPLATE_VERSION`）不变，
没有新增环境变量（`PINNED_ENV_KEYS` 不动）。S11 没有新装配、没有新工具、没有授权变化，因此不递进任何版本号；`GOVERNANCE_CONFIG_VERSION` 与 `RULESET_VERSION` 同样不递进
（没有新画像、护栏规则未改）。缓存拒绝原因是写入资格而不是键因子，不递进键格式版本。

## 4. 不做

- `web_search` 工具与任何 provider 适配器、`QueryEgressGuard`、`Capability.WEB_SEARCH`、`GRANTABLE_CAPABILITIES` 扩展（S12）；
- 新增 `ProviderKind` / `CallCategory` / 画像与出站站点（8 → 9 属 S12）；`governed_get` 本步只有测试 / 评测调用方；
- skill 下载（S13）、MCP 目录（S14）；
- 搜索结果缓存、语义缓存、任何网址抓取（`web_fetch` / 浏览器）；
- 改 `gateway.py` / `rules.py` / `config.py` / `plugins/**` / `mcp/**` / `prompting/**`；改 `DANGEROUS_CAPABILITIES`、`NETWORK_FETCH`、`INSTRUCTION_BEARING_SOURCES`；上调任何预算数字；
- "计算入参必须出现在用户输入或非网络工具输出里"的来源校验（R-web-num，过于侵入，留作残余风险）。

## 5. 残余风险与未验证项

- **归一化是确定性正则，必有语料之外的写法**。`external_content_p0` 全绿只说明 64 条语料里的写法被覆盖，**不得声称能拦住任意注入**。真正的隔离靠 `WEB_CONTENT` 不可承载指令。
- **网址抽取是正则**，会漏被刻意拆开 / 编码的写法；终答网址追溯防的是"凭空编网址"，不是任意外泄。
- **S11 没有生产调用方**：污点逻辑只经夹具工具验证，`governed_get` 只经进程内假对端验证。生产工具 `web_search` 接入后，污点 / 网址追溯 / 披露行要用生产工具复测（S12 的 `web_search_p0`）。
- **当前没有模型 / 系统发起的记忆写入路径**，所以记忆写入的 fail closed 是入口约束（见 D4）。
- **既有扫描豁免的两项**（`harness/mcp_http_eval.py`、`webapp/smoke.py`）不再被出站扫描看见；`tests/test_governance_contracts.py` 锁定豁免集合恰为评审过的三项，新增豁免必须改测试、进评审。
  子进程 `curl`·`wget` 的 AST 判定看不见运行时才拼出来的程序名（已知局限）。
- **真实搜索服务 / 真实模型是否被"中和后的"文本影响均未验证**（无 Key 无网络，S12 起也只对本地假服务验证）。
- 浏览器里的实际渲染未做手动验收（只做了 SSR 渲染验证）。
