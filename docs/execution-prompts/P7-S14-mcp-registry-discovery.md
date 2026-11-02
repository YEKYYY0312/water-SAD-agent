# P7-S14 执行 Prompt：MCP 发现（目录协议 v1 / 管理员带外搜索 / 已停用全拒提案 / 不连接条目地址）

> **工作区**：仓库 `YEKYYY0312/water-SAD-agent`，基于最新 `main`（必须已包含本 Prompt、`docs/design/17-p7-external-capabilities.md` 与 S11、S12 的全部交付——`external/http_get.py::governed_get`、`mcp/importer.py`、`webapp/mcp_admin_routes.py`）。新建分支 `YEK/p7-s14-mcp-registry-discovery`。
> **并行说明**：本步与 S13（Skill 下载）**都基于 main、互不依赖**，会在若干共享文件上文本冲突（见 §0 第二行与文末"并行冲突"）。先合入的那一步占用站点序号，后合的重算。
> **必读**：
> - `AGENTS.md`
> - `docs/design/17-p7-external-capabilities.md`（SDD：§0 冲突清单、§3 事实表（F12）、§4 共同底座、**§7 MCP 发现（本步全部）**、§8 威胁模型 T17、§10 不变量 P7-I13、§11 的 P7-S14 验收、§12 实施协议、§14 待决事项 D5）与 `17-p7-external-capabilities-harness.md`（§0 原则、§2 通用设施、**§6 `mcp_registry_p0`**）
> - `docs/design/p7-step-11-decision.md`（`governed_get` 的稳定码与 `resolver` / `ssl_context` 注入）、`docs/design/p7-step-12-decision.md`（第 9 个出站站点的登记写法）、`docs/design/p7-step-6-decision.md`（S6：导入预览的"只出禁用提案"、凭据槽位、`MAX_IMPORT_CHARACTERS`）、`docs/design/p7-step-8-decision.md`（S8：`streamable_http` 的 URL 校验与 `allow_private_network`）
> - `docs/design/16-p7-plugin-mcp.md` §3.8（Web 扩展面板）、§3.10（Streamable HTTP）、§6（防漂移 harness）、§8（实施协议）；`docs/design/p2.5-threat-model.md` T13、T15 与 `docs/design/p2.5-provider-inventory.md`
> - 参考实现：`mcp/importer.py`（`preview_import`、`ImportPreview`、`normalize_server_id`、`_unique_id`、`_remote_proposal`——**提案格式与"丢弃什么"的唯一真源，本步复用，不复制第二份**）、`mcp/http_transport.py::parse_server_url`（URL 校验）、`external/http_get.py::governed_get`、`external/envelope.py`（归一化与 `detect_injection` 的用法）、`webapp/mcp_admin_routes.py`（`require_admin` + CSRF + 请求体手工校验 + 错误信封的写法；`import_preview` 路由是最近的先例）、`webapp/auth/audit.py`（审计白名单的写法）、`harness/web_search_eval.py` 与 `harness/mcp_http_eval.py`（评测套路）、`web/app/src/McpAdminPanel.tsx` + `mcpAdminApi.ts` + `mcpAdminState.ts` + `web/app/tests/mcp_admin.test.mjs`（面板与 node 测试的写法）

---

## 0. 相对规格的修订与本步决定（以本文为准）

| 疑问 | 本步做法 | 原因 |
|---|---|---|
| **官方注册表的真实形态（F12，硬事实）** | **没有任何真实样例**：官方 MCP 注册表的 API 字段本机读不到（`pypi.org` 与外网都不可达）。Step 0 **必须先尝试**取得真实样例（`WebFetch` 官方文档 / 注册表仓库）；**取不到就停在"自定义最小协议 + 假目录"**，在决策记录写明 `unverified_against_real_registry`，**不得声称兼容官方注册表或任何真实目录**。取到了也只能作为**适配器候选**记入决策记录，本步不做适配器（SDD §7.3） | 同 S13：协议越小越好验；不声称自己没验过的东西 |
| **站点数** | 本步**与 S13 并行，都基于 main**：起点是 **9 → 10**（与 S13 同一个数字）。`PROVIDER_CALL_SITES` 登记站点 `mcp_registry`：`module = water_agent/mcp/registry.py`，`egress_module = water_agent/mcp/http_transport.py`（与 `web_search` 同构：经 `governed_get`，**不直接调用 `open_exchange`**）。`ProviderKind.MCP_REGISTRY` / `CallCategory.MCP_REGISTRY` / 画像（`timeout_seconds=15`、`RetryPolicy(max_attempts=2)` 只对瞬时错误、每主体每分钟 ≤ 20 的失控保护量级、熔断键 `mcp_registry:<host>`）；`GOVERNANCE_CONFIG_VERSION` 递进。五处 `== 9` 断言同步改 `== 10`：`tests/test_vision_review.py`、`tests/test_llm_endpoints.py`、`tests/test_governance_contracts.py`（两处）、`tests/test_dxf_upload.py`（`provider_call_sites` / `provider_governed` 9 → 10，`tools` / `tools_guardrail_validated` 本步**不变**——目录搜索不是模型工具） | **S13 若先合入，站点已是 10，本步改为 10 → 11，上述断言里的 10 → 11**；这是并行方案的已知冲突点，合并时按"先合者占序号"重算，不要凭记忆写数字 |
| **`Capability`** | **复用 `Capability.NETWORK_FETCH`？不行**（危险集，`Principal` 构造期拒绝）。网关要求每次调用带能力。**本步新增 `Capability.MCP_REGISTRY_SEARCH`**（只读外发，目的地由运维写死），**不进** `DEFAULT_LOCAL_CAPABILITIES`、**不进** Web 成员能力集、**不进** `GRANTABLE_CAPABILITIES`；只有 `webapp/mcp_registry_routes.py` 在**管理员**请求内构造的主体持有（`Principal.local("mcp-registry", capabilities=frozenset({MCP_REGISTRY_SEARCH}))`——它是服务级主体，不是用户主体）。`GRANTABLE_CAPABILITIES == {MCP_INVOKE, WEB_SEARCH}` **不动**；它不对应任何 `ToolSpec`，所以 `inventory.py` 的"能力 → 工具"登记表与 `tools_guardrail_validated` 都不变 | 给"管理员触发的目录搜索"一个独立能力，比借用别的诚实。**这是需要评审裁决的设计点，见文末"待评审"第 1 条**（S13 的 `SKILL_FETCH` 是同一个设计的另一个实例） |
| **谁能做什么（SDD §7，P7-I13）** | Web 管理员：搜索 / 浏览目录、生成提案；**不能连接条目地址、不能应用提案**（应用 = 运维把提案文本贴进部署覆盖层）。模型：**没有任何入口**。`mcp/registry.py` **不得导入** `MCPStdioClient` / `MCPHttpClient` / `open_bridge`（AST）；`webapp/mcp_registry_routes.py` 同样不导入它们（目录路由不连接条目） | 条目 URL 是第三方目录写的；"目的地由运维写死"在这里变成"目的地由第三方目录写、运维批准"，所以**提案阶段必须零字节发往条目地址** |
| **目录协议 v1（我们自己定义）** | `GET {base}/servers?query=&limit=&cursor=` → `{"entries":[{"name","version","description","remotes":[{"type","url"}],"packages":[...]}],"next_cursor":null\|"..."}`。固定 `limit ≤ 20`；`cursor` 是不透明串（≤ 128 字符、`[A-Za-z0-9_\-.=]`）；仅 https（运维显式 `WATER_AGENT_MCP_REGISTRY_ALLOW_PRIVATE=1` 才放行回环 / 私网，链路本地与元数据永远拒）；基址 `WATER_AGENT_MCP_REGISTRY_URL` 由运维写死。上限：每页 ≤ 20 条、条目 ≤ 16 KiB、总量 ≤ 256 KiB、至多 5 页、查询 ≤ 64 字符（控制字符 / 换行拒绝）。**整页严格校验**：任何一条形状不对，整页被拒（与 S13 的 index 同一纪律） | 协议越小越好验 |
| **条目字段与不可信处理（SDD §7.2）** | 字段白名单：`name`（ASCII slug，`fullmatch`）、`version`（三段整数 `fullmatch`）、`description`（≤ 600 字符）、`remotes[].type`（`streamable-http` / `sse` 等**只作为展示字符串**）、`remotes[].url`、`packages[].{registry_type,identifier}`；其余键整条丢弃并计数。`description` 与所有展示字段走 `external/envelope.py` 的归一化（去不可见 / 控制字符、折叠空白、截断），再过 `detect_injection`：命中 → 该条标 `unsafe`、**不可生成提案**、描述不展示（只显示名字与 `unsafe` 标记）。**前端一律纯文本**（不渲染 Markdown / HTML / 链接）。**Step 0 必须对 `ensure_description_safe` 与归一化的 Tag 字符行为再实测一遍**（S13 发现 `mcp/schema.py::ensure_description_safe` 对 Unicode Tag 字符漏检）：本步的目录条目文本**不得**依赖它，要走 S13 同款的字符正向白名单（见下行） | 目录条目是第三方文本，会进管理员的浏览器与提案文本 |
| **字符白名单（复用 S13 的区间，但本步不依赖 S13 的代码）** | 本步与 S13 并行，**不能 import `skills/charset.py`**（S13 未合入时它不存在）。做法：`mcp/registry.py` 自带一个**同口径**的 `is_allowed_char`（ASCII 可打印 + 拉丁标点 + 希腊 + 常见符号 + CJK 与 CJK 标点 + 全角 ASCII，排除 `Cc Cf Co Cs Cn Zl Zp Zs Mn Mc Me`，NFKC 封闭，拒绝与拉丁字母同形的希腊大写 `ΑΒΕΖΗΙΚΜΝΟΡΤΥΧ`），**区间在决策记录里写明并说明与 S13 同源**；两份合并后的去重留给后续（交接文档记一条）。**不要为了去重而等 S13**。条目的 `name` / `description` / `remotes[].url` / `packages[].identifier` 每个字符都要过它；不通过 → 该条 `unsafe` | 并行分支不互相依赖；黑名单追不上 Tag / 变体选择符 / 同形字（S13 Step 0 实测） |
| **提案（SDD §7.2，复用 S6 的格式）** | `POST /api/admin/mcp/registry/proposal`，请求体只有 `{name, version}`（`extra="forbid"`）；服务端**重新**从目录取该条目（**不信客户端回传的任何内容**）；只接受 `remotes[type=streamable-http]` 且 URL 通过 `parse_server_url`（https、无 userinfo / fragment、规范点分十进制 IPv4；`allow_private_network` 一律 False）**并且**主机名不是 IP 字面量（IPv4 / IPv6 / 方括号形式）——**写 Prompt 时实测 `parse_server_url` 放行 `https://127.0.0.1/x`、`https://[::1]/x`、`https://10.0.0.5/x`，所以“非 IP 字面量”是本步自己要加并评测的一层，不是 `parse_server_url` 给的**；目录条目永远不能把提案指向 IP 地址（真正挡内网的连接期 SSRF 分类在 S8，发生在运维应用提案之后，提案阶段不能指望它）；产出覆盖层格式的**已停用**（`disabled: true`）、**不含 `allow`**（全拒）的 `streamable_http` 提案文本，复用 `importer` 的格式化与 `_unique_id`（冲突 id 改名）；`env` / 请求头 / 令牌 / URL 查询串一律丢弃（`secret_dropped` 计数）；附 `proposal_hash = sha256(规范化完整定义 + 目录主机 + 名称 + 版本)`（规范化 = `json.dumps(sort_keys=True, ensure_ascii=True, separators)`）。`stdio` / `npm` / `pypi` / `docker` 条目只展示"需要运维手写 command"，**不生成提案** | S6/S8 的闸门照常生效：提案只是文本，运维贴进覆盖层后才有 S6 探测与 S8 连接期 SSRF 校验 |
| **不探测、不连接（A04）** | 搜索与提案阶段**对条目 URL 零字节、对条目主机零 DNS 解析**。评测用 `NetworkSpy`（`harness/mcp_http_eval.py` 已有的连接间谍）+ 注入的 `resolver` 双重断言。`webapp/mcp_registry_routes.py` 与 `mcp/registry.py` 不 import 任何 MCP 客户端（P7-I13，AST，带反证） | 条目 URL 是第三方写的 |
| **API（SDD §7.2）** | `POST /api/admin/mcp/registry/search`（`{query, cursor?}`）与 `POST …/registry/proposal`；全部 `require_admin` + CSRF；请求体 `extra="forbid"`；每管理员每分钟限额（进程内，失控保护量级）；**审计** `mcp.registry_searched` / `mcp.registry_proposed` 只记 管理员 id、目录主机名、`name@version`（提案）、结果码、条数——**不记查询原文、不记条目 URL、不记提案文本**。`GET /api/admin/mcp/registry/status`（只读：是否配置 / 目录主机名 / 是否私网放行；**不含完整 URL**） | 管理员账号被盗最多拿到一份"已停用、全拒"的文本 |
| **部署** | `compose.yml` 给 `app` 传 `WATER_AGENT_MCP_REGISTRY_URL` / `WATER_AGENT_MCP_REGISTRY_ALLOW_PRIVATE`（缺省空 = 功能关闭，`status` 报未配置，两个 POST 返回 409 `WEB_MCP_REGISTRY_NOT_CONFIGURED`）；**不新增服务、不新增卷**（目录搜索是 app 内的只读外发，目的地是运维写死的）。两个变量钉进快照 `PINNED_ENV_KEYS` | 与联网搜索同构：外发默认关 |
| **快照** | 现有 **20 个 profile 逐字不变**（本步不改装配：目录搜索不是工具、不进插件）。**不新增快照 profile**；两个新环境变量只进 `PINNED_ENV_KEYS`。`TOOL_POLICY_VERSION` / `PROMPT_TEMPLATE_VERSION` / `MCP_CONFIG_VERSION` **都不递进** | 本步不改模型面 |
| **评测** | `mcp_registry_p0`（设计 ~36 条，Step 1 冻结）+ 假目录 `tests/fixtures/fake_mcp_registry.py`（**进程内线程，不起子进程**，S8 D13）接进 `ci_gate.sh` 与入仓基线（`scripts/compare_mcp_registry_p0.py`）。硬指标见 harness §6，**每个指标一条拿掉保护即变红的反证**；写完数据集后**必须对关键保护做手工变异**（S13 因此发现了两个评测盲点：只拿掉一层保护、另一层纵深仍把它挡住时指标不变红——反证补丁要同时拿掉两层） | 评测先行 |
| **R-registry-semantic（残余，不做）** | 目录条目与真实 server 的一致性、条目的 `description` 是否如实、运维启用并放行后该 server 的实际行为，任何确定性规则都检不出。本步不做、也不假装能做。决策记录与 T17 如实写 | 不得声称"目录搜索是安全的" |

## 1. 目标与边界

**做**（编号对应 SDD §11 的 P7-S14-Axx）：

1. **目录协议 v1 客户端（A01）**：`mcp/registry.py`——`RegistryCatalogClient.search(query, cursor)` 只经 `governed_get`（`require_media_type="application/json"`、`max_bytes`、`timeout` 显式给）；分页 / 字节 / 条目上限；整页严格校验；任何失败抛带稳定码的 `McpRegistryError`，不重试到别的地址。
2. **条目归一与不可信处理（A02）**：字段白名单、字符白名单、归一化、`detect_injection` → `unsafe`；纯文本；重名 / 同形名处理。
3. **提案（A03、A05）**：只接受 `streamable-http` + 合法 https URL；恒停用且无 `allow`；`env` / 头 / 令牌 / 查询串丢弃；冲突改名；`proposal_hash` 确定性且对 URL / 版本 / 目录主机敏感；`stdio` / 包类条目只展示不提案。复用 `mcp/importer.py` 的格式化，**不复制第二份**。
4. **不连接（A04）**：条目 URL 零字节、条目主机零 DNS；P7-I13（AST）。
5. **API（A06）**：`webapp/mcp_registry_routes.py`——管理员 + CSRF、请求体白名单、服务端重取条目、限额、审计；在 `webapp/app.py` 挂载（只加一行 import 与一行 `include_router`）。
6. **前端（A07）**：MCP 页"从目录发现"区——搜索框、结果列表（纯文本、`unsafe` 标记）、"生成提案"按钮、复制提案文本；**没有**"连接 / 启用 / 应用"控件（node 测试断言）。
7. **治理（A08）**：`Capability.MCP_REGISTRY_SEARCH`（不在默认集 / 成员集 / `GRANTABLE`）、`ProviderKind` / `CallCategory.MCP_REGISTRY`、画像、站点 9 → 10、usage 未知、`GOVERNANCE_CONFIG_VERSION` 递进。
8. **评测（A09）**：`mcp_registry_p0` + 假目录 + 比较器 + 入仓基线 + `ci_gate.sh`；每个硬指标一条反证。
9. **部署**：`compose.yml` 传两个变量；`PINNED_ENV_KEYS` 追加。
10. **文档**：决策记录 `docs/design/p7-step-14-decision.md`（结构见 `16-p7` §8 第 6 条）；`p2.5-threat-model.md` T17；`p2.5-provider-inventory.md`（第 10 个站点，若 S13 先合则第 11 个）；16 号规格 §7 登记 `P7-S14-A01…A10`；17 号 SDD 状态翻转；AGENTS.md 追加条目。

**不做**：官方注册表适配器；条目 URL 的任何探测或连接；**应用提案**（写覆盖层 / 写 Web 管理层）；stdio / npm / pypi / docker 条目的提案；模型可调用的目录工具；声称兼容任何真实目录；"可疑仍可提案"的覆盖开关；改 `mcp/schema.py` 的既有行为（`ensure_description_safe` 的 Tag 字符缺口**只记录**，已在 S13 决策记录登记）；改 `mcp/importer.py` 的既有行为（只**调用**它）；改 `gateway.py` / `pipeline.py` / `rules.py` / `cache.py`；改 `external/**`；上调任何预算数字；声称"目录搜索是安全的"。

## 2. 步骤序列（评测先行；一个逻辑面一个 commit）

0. **Step 0 读原文与实测**（SDD §12 补充 1）：
   - **先尝试取真实样例**：`WebFetch` 官方 MCP 注册表的文档 / 仓库 README（`modelcontextprotocol/registry`），读 `servers` 列表接口的响应字段。取到 → 写进决策记录当"适配器候选"，**本步仍只实现自定义协议 v1**；取不到 → 记 `unverified_against_real_registry`。
   - 用真实 `mcp/importer.py` 重做：① 它对 `streamable_http` 提案的输出格式；② `_unique_id` 与 `normalize_server_id` 的行为；③ 它丢弃 `env` / 头 / 令牌 / 查询串的路径。**提案必须逐字复用这些**，不能另写一份。
   - **实测** `mcp/schema.py::ensure_description_safe` 与 `external/envelope.py` 对 Tag 字符 / 变体选择符 / 西里尔同形字的行为（S13 已发现前者漏检），把输出写进决策记录；据此确认本步必须自带字符白名单。
   - 用 AST 重新枚举 `open_exchange(` 的调用方确认仍是 4 项；跑 `tool_assembly_snapshot --check` 确认起点是 20 个 profile 且干净。
   - 实测 `parse_server_url` 对 `https://127.0.0.1/x`、`https://[::1]/x`、`https://10.0.0.5/x`、`https://2130706433/x`、`https://user:pw@h/x`、`https://h/x#f`、`http://h/x`、同形字域名（`https://exаmple.com/x`，会被 IDNA 成 `xn--…`）的行为。**写 Prompt 时的实测**：IP 字面量（含 `127.0.0.1`、`[::1]`、`10.0.0.5`）被放行；十进制 / 八进制整数主机名、userinfo、fragment、明文 http、非 https scheme、超长标签被拒；同形字域名不拒而是转成 punycode——提案里应当展示转换后的 `xn--` 形式并标出“含非 ASCII 域名”。
1. **Step 1 冻结**：`data/eval/mcp_registry_p0.jsonl`（~36–60 条，类别见 harness §6；**多配良性邻近用例**：一条合法的 streamable-http 条目必须能生成提案）、指标阈值、比较器骨架。敌意条目由假目录在运行期程序化生成（Tag 走私 / 同形字 / 超长字段 / 伪造边界 / 控制字符等不写进 JSONL）；JSONL 全文件 ASCII。
2. **Step 2 harness**：`harness/mcp_registry_eval.py`、`tests/fixtures/fake_mcp_registry.py`（模式：`ok` · `paging_many` · `oversize_entry` · `oversize_total` · `junk` · `injection_desc` · `forged_marker_desc` · `stdio_only` · `npm_only` · `ip_url` · `http_url` · `userinfo_url` · `query_secret_url` · `redirect` · `dup_names` · `homoglyph_names` · `tag_smuggle_desc` · `wrong_ctype` · `status_401|403|429|500|503`；**记录收到的请求数与路径**，并另起一个"条目地址"假端点专门记录是否有人连过来）。
3. **Step 3 机制**：`errors` 稳定码 → `Capability` + 治理（kind / category / 画像 / 站点 / 版本）→ `mcp/registry.py`（客户端 + 条目归一 + 提案）→ `webapp/mcp_registry_routes.py` + 审计 → 前端 → 部署。
4. **Step 4 反证与基线**：补齐每个硬指标的反证；**对关键保护做手工变异并把结果写进决策记录**；生成入仓基线；接 `scripts/ci_gate.sh`。
5. **Step 5 收口**：追溯检查通过；P7-I13 的 AST 测试；决策记录与文档。

## 3. 验证命令

```bash
export PYTHONPATH=src   # worktree 里必须；并 unset WATER_AGENT_* / *_API_KEY / HTTPS_PROXY / LOCAL_MODEL_*
_venv/bin/pytest -q tests/test_mcp_registry_client.py tests/test_mcp_registry_entries.py tests/test_mcp_registry_proposal.py tests/test_mcp_registry_invariants.py tests/test_web_mcp_registry.py tests/test_mcp_registry_eval.py tests/test_mcp_import.py tests/test_web_mcp_admin.py tests/test_governance_contracts.py tests/test_plugin_invariants.py tests/test_web_deployment.py
_venv/bin/python -m water_agent.harness.mcp_registry_eval data/eval/mcp_registry_p0.jsonl --report-path data/eval/reports/mcp_registry_p0_head.json
_venv/bin/python scripts/compare_mcp_registry_p0.py --head data/eval/reports/mcp_registry_p0_head.json --base data/eval/mcp_registry_p0_main.json
_venv/bin/python -m water_agent.harness.tool_assembly_snapshot --check
(cd web/app && npm test && npm run build)
COMPOSE_PROJECT_NAME=p7s14 bash scripts/ci_gate.sh      # 本机有别的 water-agent 栈在跑时必须隔离
_venv/bin/python scripts/check_step_scope.py docs/execution-prompts/P7-S14-mcp-registry-discovery.md --base origin/main
```

另跑 `pytest -o addopts= -q`（ci_gate 的 pytest 是 `-qq`，没有汇总行）取全量计数；`ruff check src`、`mypy src/water_agent` 各自单独看退出码。`docker compose config -q` 与 `docker compose -f compose.yml -f compose.production.yml config -q` 都要能解析。

## 4. 受保护断言

- 预算：`assert cost < 4500`、`schema_chars_total == 4497`、系统提示 `< 1950`。
- 信任：`INSTRUCTION_BEARING_SOURCES == frozenset({SourceTag.SYSTEM_POLICY})`。
- 危险能力：`NETWORK_FETCH` 仍在 `DANGEROUS_CAPABILITIES`；`GRANTABLE_CAPABILITIES == {MCP_INVOKE, WEB_SEARCH}`（**`MCP_REGISTRY_SEARCH` 不在其中**）。
- 缺省不变：不配 `WATER_AGENT_MCP_REGISTRY_URL` 时，装配面、系统提示、面板既有输出**逐字不变**（现有 20 个快照 profile 逐字相同）。
- **会变的锁定数字 / 语义**（本步唯一允许改的）：`len(PROVIDER_CALL_SITES) == 9` → `== 10`（**五处**，见 §0 第二行；S13 先合入则 10 → 11）、`GOVERNANCE_CONFIG_VERSION` 字面量、`PINNED_ENV_KEYS`、`ProviderKind` 枚举列表断言（`test_llm_endpoints.py`）；这些变化都要在决策记录里写明原因。

## 5. 停止条件

满足任一条就停下，写清现象后等确认：

- 现有 20 个快照 profile 或 `TOOL_POLICY_VERSION` / `PROMPT_TEMPLATE_VERSION` / `MCP_CONFIG_VERSION` 有任何变化；
- 需要改 `mcp/importer.py` 的既有行为、`mcp/schema.py`、`external/**`、`gateway.py` / `pipeline.py` / `rules.py` / `cache.py`；
- 需要改 `webapp/app.py` 里除一行 import 与一行 `include_router` 之外的任何东西（中间件、`RunManager` 装配、静态目录）；
- 需要授予 `NETWORK_FETCH`、把 `MCP_REGISTRY_SEARCH` 放进默认能力集 / 成员集 / `GRANTABLE`、让任何路径连接条目 URL、或加"可疑仍可提案"开关；
- 评测里任一"条目地址"假端点收到了连接，或任一条目主机被 DNS 解析；
- 需要上调任何预算上限；
- 全量测试里出现与本步无关的红灯。

## 6. 汇报格式

改了哪些文件；跑了哪些验证命令以及实际输出（失败照贴）；范围守卫输出；Step 0 的真实样例取得情况（取到 / 没取到 + 原因）；字符白名单区间与 S13 的一致性说明；**手工变异清单与结果**（哪个保护被拿掉、哪个指标变红、发现的评测盲点）；**未验证项与原因**（真实注册表、条目与真实 server 的一致性、运维启用后该 server 的行为、浏览器验收）；遗留风险。**不得**写"目录搜索是安全的"，**不得**声称兼容任何真实目录。

---

<!-- step-scope -->
```json
{
  "step": "P7-S14",
  "allow": [
    "src/water_agent/mcp/registry.py",
    "src/water_agent/mcp/errors.py",
    "src/water_agent/runtime/governance/contracts.py",
    "src/water_agent/runtime/governance/inventory.py",
    "src/water_agent/runtime/governance/config.py",
    "src/water_agent/webapp/mcp_registry_routes.py",
    "src/water_agent/webapp/app.py",
    "src/water_agent/webapp/auth/audit.py",
    "src/water_agent/harness/mcp_registry_eval.py",
    "src/water_agent/harness/tool_assembly_snapshot.py",
    "scripts/compare_mcp_registry_p0.py",
    "scripts/ci_gate.sh",
    "compose.yml",
    "data/eval/mcp_registry_p0.jsonl",
    "data/eval/mcp_registry_p0_main.json",
    "web/app/src/**",
    "web/app/tests/**",
    "tests/fixtures/fake_mcp_registry.py",
    "tests/test_mcp_registry_client.py",
    "tests/test_mcp_registry_entries.py",
    "tests/test_mcp_registry_proposal.py",
    "tests/test_mcp_registry_invariants.py",
    "tests/test_mcp_registry_eval.py",
    "tests/test_web_mcp_registry.py",
    "tests/test_governance_contracts.py",
    "tests/test_mcp_http_config.py",
    "tests/test_plugin_invariants.py",
    "tests/test_vision_review.py",
    "tests/test_llm_endpoints.py",
    "tests/test_dxf_upload.py",
    "tests/test_web_deployment.py",
    "docs/design/p7-step-14-decision.md",
    "docs/design/17-p7-external-capabilities.md",
    "docs/design/17-p7-external-capabilities-harness.md",
    "docs/design/16-p7-plugin-mcp.md",
    "docs/design/p2.5-threat-model.md",
    "docs/design/p2.5-provider-inventory.md",
    "docs/execution-prompts/P7-S14-mcp-registry-discovery.md",
    "AGENTS.md"
  ],
  "forbid": [
    "src/water_agent/mcp/importer.py",
    "src/water_agent/mcp/schema.py",
    "src/water_agent/mcp/client.py",
    "src/water_agent/mcp/http_client.py",
    "src/water_agent/mcp/http_transport.py",
    "src/water_agent/mcp/oauth.py",
    "src/water_agent/mcp/config.py",
    "src/water_agent/mcp/bridge.py",
    "src/water_agent/mcp/pool.py",
    "src/water_agent/mcp/web_layer.py",
    "src/water_agent/runtime/governance/gateway.py",
    "src/water_agent/runtime/governance/pipeline.py",
    "src/water_agent/runtime/governance/rules.py",
    "src/water_agent/runtime/governance/cache.py",
    "src/water_agent/runtime/governance/cache_service.py",
    "src/water_agent/external/**",
    "src/water_agent/prompting/**",
    "src/water_agent/plugins/**",
    "src/water_agent/graphs/**",
    "src/water_agent/runtime/agentic.py",
    "src/water_agent/generation/**",
    "src/water_agent/multi_agent/**",
    "src/water_agent/storage/**",
    "src/water_agent/skills/**",
    "src/water_agent/web_search/**",
    "src/water_agent/webapp/run_manager.py",
    "src/water_agent/webapp/run_routes.py",
    "src/water_agent/webapp/settings_routes.py",
    "src/water_agent/webapp/skill_routes.py",
    "src/water_agent/webapp/mcp_admin_routes.py",
    "src/water_agent/webapp/auth/dependencies.py",
    "src/water_agent/tools/base.py",
    "src/water_agent/tools/cite.py",
    "src/water_agent/tools/ds_adapters.py",
    "src/water_agent/tools/registry.py",
    "src/water_agent/tools/retrieve_clauses.py",
    "src/water_agent/tools/read_drawing.py",
    "src/water_agent/tools/material_stats.py",
    "src/water_agent/tools/calc_velocity.py",
    "src/water_agent/tools/hydraulics.py",
    "src/water_agent/tools/load_skill.py",
    "alembic/**",
    "Dockerfile",
    "compose.production.yml",
    ".github/**",
    "scripts/backup.sh",
    "scripts/restore.sh",
    "scripts/check_image.sh",
    "tests/conftest.py",
    "tests/test_p7_traceability.py",
    "tests/test_check_step_scope.py",
    "tests/test_import_guard.py",
    "tests/test_tool_exposure_eval.py",
    "data/eval/baseline.json",
    "data/eval/skill_p0.jsonl",
    "data/eval/skill_p0_main.json",
    "data/eval/tool_assembly_main.json",
    "data/eval/tool_exposure_p0.jsonl",
    "data/eval/tool_exposure_p0_main.json",
    "data/eval/mcp_http_p0_main.json",
    "data/eval/model_choice_p0_main.json",
    "data/eval/external_content_p0_main.json",
    "data/eval/web_search_p0_main.json",
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
    },
    {
      "node": "tests/test_web_search_tool.py::test_capability_surface",
      "contains": "assert GRANTABLE_CAPABILITIES == frozenset({Capability.MCP_INVOKE, Capability.WEB_SEARCH})"
    },
    {
      "node": "tests/test_web_search_tool.py::test_capability_surface",
      "contains": "assert Capability.NETWORK_FETCH in DANGEROUS_CAPABILITIES"
    }
  ],
  "frozen_json": [
    {
      "path": "data/eval/tool_assembly_main.json",
      "pointers": [
        "/versions/MCP_CONFIG_VERSION",
        "/versions/PROMPT_TEMPLATE_VERSION",
        "/versions/TOOL_POLICY_VERSION",
        "/profiles/*"
      ]
    }
  ]
}
```
<!-- /step-scope -->

> **范围块说明（写 Prompt 时已逐条核对 main，开工前请再核一遍）**：
>
> - **`webapp/mcp_admin_routes.py` 在 forbid 里，`webapp/app.py` 在 allow 里**：新路由写在新文件 `webapp/mcp_registry_routes.py`，在 `app.py` 里与其它 router 并列 `include_router`（写 Prompt 时核对过：`app.py` 逐个 `include_router`，所以挂载新路由**必然**要改它）。`app.py` 只许加一行 import 和一行 `include_router`，不许动中间件与 `RunManager` 装配。
> - **`data/eval/tool_assembly_main.json` 的 `frozen_json` 用 `/profiles/*`**：本步**不新增**快照 profile，所以全部 profile（含 S13 可能新增的）一律冻结，比 S12/S13 的逐项列举更严。范围守卫的 glob 只支持 `*` `?` `**`，不支持字符类（S11 的 `tools/[!bd]*` 是无效模式，实测）。`TOOL_POLICY_VERSION` 在这里也被冻结——因为本步不动模型面。
> - **`mcp/errors.py` 在 allow 里，`tests/test_mcp_http_config.py` 也在**：新增 `MCP_REGISTRY_*` 稳定码；`MCP_ERROR_CODES` 的集合被 `tests/test_mcp_http_config.py:215`（`len(MCP_ERROR_CODES) == 20`）锁死，新增码必然改它——**只许把计数改成新的、并把新码加进"只增不减"的断言，不许放宽成 `>=`**（写 Prompt 时用 `grep -rn MCP_ERROR_CODES tests/` 核对过，只有这一处）。
> - **`compose.yml` 在 allow 里，`Dockerfile` 在 forbid 里**：本步不新增卷，不需要预建目录。`compose.production.yml` 不改。
> - **与 S13 并行的共享文件**：`compose.yml`、`scripts/ci_gate.sh`、`runtime/governance/{contracts,inventory,config}.py`、五处 `== 9` 测试、`PINNED_ENV_KEYS`、`AGENTS.md`、`16/17` 号文档、`p2.5-*.md`。后合入者必然文本冲突，见文末。
> - **`tests/test_dxf_upload.py` 在 allow 里**（S8 D13 / S12 同例）：它锁着 `provider_call_sites` / `provider_governed`。

### 并行冲突（给合入者）

S13 与 S14 都基于 `00982cf`（S12 合入后的 main），**都把站点数从 9 改成 10**。先合入者保持 10；**后合入者必须**在合并后把自己所有的 `10` 重算为 `11`：

1. 五处断言（`test_vision_review.py`、`test_llm_endpoints.py`、`test_governance_contracts.py` ×2、`test_dxf_upload.py` 的两个数）：`10` → `11`，`ProviderKind` 枚举断言列表末尾追加两项（顺序以先合入者为准）。
2. `GOVERNANCE_CONFIG_VERSION`：取后合入者自己的新版本号（版本串带日期，不会相同，但必须再递进一次，不能两边各留一个）。
3. `PINNED_ENV_KEYS`：两边追加的键**取并集**。
4. `compose.yml`：两边各加自己的环境变量与（S13 的）卷 / 服务，**取并集**，不要覆盖。
5. `tool_assembly_main.json`：S13 新增了 `session_skills_installed`，S14 不新增。后合入 S14 的话，S14 的 `frozen_json` 里 `/profiles/*` 会把 S13 的新 profile 也冻结住——没有冲突；先合 S14 再合 S13 则 S13 范围块的 `frozen_json` 已按"现有 20 个"写，也不冲突。
6. `AGENTS.md`、`16` / `17` 号文档、`p2.5-*.md`：各自追加自己的小节，**取并集**；`17-p7-external-capabilities.md` 里 §0 那一行"站点数"两边都改了同一行，手工合成 `S13 9 → 10、S14 10 → 11`。
7. 合并后必须重跑：`tool_assembly_snapshot --check`、`test_p7_traceability`、`test_check_step_scope`、两个套件各自的 runner 与比较器。

### 待评审（开工前需要裁决）

1. **`Capability.MCP_REGISTRY_SEARCH` 要不要新增**：本 Prompt 决定新增（§0 第三行）。替代方案是复用 S13 的 `SKILL_FETCH` 改名为通用的"受治理注册表读取"能力——但两步并行，不能互相依赖，且语义不同（一个是运维 CLI 取 skill，一个是管理员面板搜目录）。推荐新增，合并后再看是否收敛。
2. **相邻缺口 `mcp/schema.py::ensure_description_safe` 对 Tag 字符的漏检**（S13 Step 0 实测）：本步 `mcp/schema.py` 在 forbid 里，目录条目文本走自带的字符白名单，**不依赖它**。是否另开一步修，由评审定。
