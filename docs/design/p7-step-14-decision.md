# P7-S14 决策记录：MCP 发现（目录协议 v1 / 管理员带外搜索 / 已停用全拒提案 / 不连接条目地址）

> 日期：2026-10-23。分支 `YEK/p7-s14-impl`（由 `YEK/p7-docs-s14-prompt` 切出，Prompt 提交 `de67316`），基于 main `00982cf`（S0–S12、S15 已合入）。**S13 与本步并行、都基于同一个 main**，见 §0.3。
> 执行 Prompt `docs/execution-prompts/P7-S14-mcp-registry-discovery.md`，规格 `docs/design/17-p7-external-capabilities.md`（§7、§8 T17、§10 SDD17-I13、§11 的 P7-S14）与
> `17-p7-external-capabilities-harness.md` §6，16 号规格 §7 登记的 `P7-S14-A01…A10`，威胁模型 `p2.5-threat-model.md` T17。

## 2026-11-02 主线整合

- S13 执行说明 #55、S14 执行说明 #56、S13 实现 #57 与镜像／并发测试修复 #5 已先后合入 main。原 2026-10-23 验证记录保留为历史证据，CI 已恢复。
- 合并原 S14 分支与 S13 时解决 14 个文件的冲突，保留双方治理能力、ProviderKind、画像、环境变量、compose 服务／卷、文档与两套门禁。`skill_registry` 为第 10 个出口，`mcp_registry` 为第 11 个；`GOVERNANCE_CONFIG_VERSION` 递进为 `p7-s14-2026-11-02`。
- 保留 S13 新增的 `session_skills_installed`：21 个快照 profile 与基线一致。S14 没有新增 profile，三个装配版本号维持 S13 基线。
- 重算既有计数测试及 S14 自身的不变量、`mcp_registry_p0` 的 `site_count` 严格断言，均为 11。初次整合测试中 375 passed / 3 failed，三项失败均由后两处仍锁定 10 引起；修正后相关套件 147 passed，追溯／范围套件 39 passed。
- S13 的循环 YAML 别名与过深嵌套拒绝修复随 #57 保留；镜像字面量检查与基于线程信号的上传并发测试修复随 #5 保留。
- 目录仍只实现自定义协议 v1，未增加官方目录适配、条目连接或提案应用；真实目录／真实模型／CAD 的证据边界维持原文。

### 本轮验收记录

- 本地完整 `ci_gate.sh` 退出码 0；pytest **4439 passed / 30 skipped / 0 failed**（JUnit 共 4469 条）。ruff、mypy、全部机制层套件与基线对比通过，包含 `skill_fetch_p0` 111/111 与 `mcp_registry_p0` 64/64。
- 前端 `npm test` **146/146**，`tsc -b && vite build` 通过。Skill 库 fallback 说明补齐三种来源；实际接口提示由 S13 文案补丁 [#58](https://github.com/YEKYYY0312/water-SAD-agent/pull/58) 同步。
- 独立非 root／只读集成镜像构建成功，`check_image.sh` 必需检查 PASS；外部漏洞扫描器因 docker-scout 未登录而 SKIP，未据此声称漏洞扫描通过。
- 独立 Compose 项目 `wa-p7-integration-20261102` 的 HTTP 冒烟 **32 passed / 0 failed**，生产 overlay 能解析。使用本次生成的测试口令与独立卷，未配置模型密钥；真实模型 HTTP 调用按预期拒绝，不计为真实质量通过。
- 真实浏览器验证：隔离测试账号登录、设置导航、4 个内置 Skill、MCP 目录未配置状态均正常，控制台 error／warn 均为 0。真实目录上的“搜索 → 生成提案 → 复制”流程仍未验证。
- 基于已包含 #55、#56、#57、#5 的 `origin/main` 跑正式范围守卫通过，未使用 `--bootstrap`；21 个装配 profile 与基线一致。

**一句话结论**：Web 管理员可以**带外**搜索 / 浏览运维写死的单一 MCP 目录，并为合适的条目生成**已停用、不含 `allow`（全拒）**的待审提案文本，由运维核对后离线贴进部署覆盖层。
**搜索与提案全程对条目里的任何地址零字节、对条目主机零 DNS**（`mcp/registry.py` 与路由不导入任何 MCP 客户端，AST 判定带反证）；提案不落盘、不应用。
**只对本地假目录验证**——目录协议 v1 是本项目自定义的最小协议，**不是官方 MCP 注册表的格式**（Step 0 取得了官方注册表的真实响应，形态完全不同，见 §0.1）；
**不得声称兼容任何真实目录，也不得声称"目录搜索是安全的"**：字符白名单 / 注入检测是确定性规则，条目与真实 server 的一致性、运维启用并放行后该 server 的行为，任何确定性规则都检不出。

## 0. 偏离与修订

### 0.1 Step 0 取得了官方注册表的真实样例（Prompt §0 第一行要求的尝试）

Prompt 写作时 `WebFetch` 只拿到页面标题。本步 Step 0 的结果不同：

- `WebFetch https://github.com/modelcontextprotocol/registry` 只有 README，指向 `https://registry.modelcontextprotocol.io/docs`（只有标题）与"API freeze (v0.1)，2025-10-24"；
- **直接 `GET https://registry.modelcontextprotocol.io/v0/servers?limit=2` 成功**（`HTTP 200`，`application/json`，1362 字节）。真实形态：

  ```text
  { "servers": [ { "server": { "$schema", "name", "description", "title", "version",
                               "remotes": [ { "type": "streamable-http", "url": "https://…" } ] },
                   "_meta": { "io.modelcontextprotocol.registry/official": { "status", "statusChangedAt", "publishedAt", "updatedAt", "isLatest" } } } ],
    "metadata": { "nextCursor": "ac.inference.sh/mcp:1.0.1", "count": 2 } }
  ```

- 与本步目录协议 v1（`GET /servers?query=&limit=&cursor=` → `{entries:[{name,version,description,remotes,packages}], next_cursor}`）**逐层不同**：路径（`/v0/servers`）、
  外壳（`servers[].server` + `_meta`）、分页字段（`metadata.nextCursor`）、`name` 的形状（`ac.inference.sh/mcp`，含 `/`，**不满足**本步的 `[a-z0-9][a-z0-9._-]{0,63}`）、
  多了 `title` 与 `$schema`，且一个 `name` 有多个 `version` 条目（`isLatest`）。
- **裁决**：照 Prompt 与 SDD §7.3，取到真实样例也**只作适配器候选**记入本文，**本步不做适配器**。本模块对官方注册表会**整页失败**（`name` 含 `/` → 形状不对 → `MCP_REGISTRY_BAD_RESPONSE`），
  这是 fail closed 的预期行为，不是缺陷。要接官方注册表需要另开一步：映射 `servers[].server.*`、`metadata.nextCursor`、`isLatest`，并重新评审 `name` 的字符集。
  样例只有 2 条，没有覆盖 `packages` / `remotes` 缺省 / `headers` / `variables` 等字段，适配器步还需要读官方 `server.schema.json`。
- 状态：`unverified_against_real_registry`（协议 v1 对真实目录**从未**验证）。样例是 2026-10-23 的单次快照，字段可能随官方演进变化。

### 0.2 Step 0 的其余实测

| 项 | 实测结果 | 对本步的影响 |
|---|---|---|
| `parse_server_url` 对 `https://127.0.0.1/x`、`https://[::1]/x`、`https://10.0.0.5/x` | **放行**（Prompt 更正已写明） | "非 IP 字面量"是本步自己加的一层（`_proposal_eligibility`），有 3 条 IP 用例 + 反证 |
| `https://2130706433/x`、`https://0177.0.0.1/x` | 拒（十进制 / 八进制 / 十六进制 / 短写） | 沿用 |
| `https://user:pw@h/x`、`https://h/x#f`、`http://h/x`、`ftp://h/x` | 拒 | 沿用 |
| `https://exаmple.com/x`（西里尔 а） | **不拒**，转成 `xn--exmple-4nf.com` | 提案写 punycode 并提示核对；字符白名单在条目解析阶段先拦下（`unsafe`） |
| `mcp/schema.py::ensure_description_safe` 对 Unicode Tag 字符（U+E0000 区）、变体选择符、西里尔同形字 | **全部放行**；`detect_injection` 同样 0 命中；`neutralize_text` 会静默删掉 Tag / 变体选择符但不报告 | 与 S13 发现的缺口同根。**目录条目文本不依赖它们**，走自带的字符正向白名单；`tests/test_mcp_registry_entries.py::test_tag_characters_are_caught_by_the_charset_not_by_the_blacklists` 钉住"为什么不能依赖旧黑名单"（`mcp/schema.py` 在 forbid 里，缺口**只记录不修**） |
| `open_exchange(` 的调用方（AST） | `external/http_get.py`、`generation/chat_model.py`、`mcp/http_client.py`、`mcp/oauth.py`，仍是 4 项 | `mcp/registry.py` 不直接调用它，SDD17-I08 的登记表不变 |
| `tool_assembly_snapshot --check` | 起点 20 个 profile 与基线一致；终点同 | 本步不改装配面 |
| `preview_import` 对 `streamable_http` 提案的输出 | `{"version", "servers":[{"server_id","transport":"streamable_http","url","disabled":true}]}`，查询串丢弃并写进 notes，冲突 id 改名 `_2` | 提案**逐字复用**它（`build_proposal` 把条目转成 `mcpServers` 输入再调用 `preview_import`），不另写第二份格式 |

### 0.3 站点数与并行冲突

Prompt 起点是 **9 → 10**（与 S13 同一个数字）。本分支上：`PROVIDER_CALL_SITES` 9 → 10，五处 `== 9` 断言改为 `== 10`（`test_vision_review.py`、`test_llm_endpoints.py`、
`test_governance_contracts.py` ×3、`test_dxf_upload.py` 的两个数），`ProviderKind` 列表末尾追加 `mcp_registry`，`GOVERNANCE_CONFIG_VERSION` → `p7-s14-2026-10-23`；
`tools` / `tools_guardrail_validated` **不变**（目录搜索不是模型工具）。**S13 先合入则站点已是 10，本步合并后须改 10 → 11**——清单见 Prompt 文末"并行冲突"与
`docs/HANDOFF-P7-S13-S14-merge-order.md` §4.5；不要凭记忆写数字，以合并后的实际计数为准。

`MCP_ERROR_CODES` 20 → 28（新增 8 个 `MCP_REGISTRY_*`），`tests/test_mcp_http_config.py::test_new_error_codes_are_registered_and_stable` 的计数按 Prompt 改为 28，**没有放宽成 `>=`**。

### 0.4 字符白名单与 S13 的一致性

`mcp/registry.py::ALLOWED_RANGES` 是 33 段闭区间，与 S13 的 `skills/charset.py::ALLOWED_RANGES` **逐项完全一致**（实测 `tuple(s13) == tuple(s14)`，从 `origin/YEK/p7-s13-skill-fetch` 读取）；
希腊同形字集合（`ΑΒΕΖΗΙΚΜΝΟΡΤΥΧοιυχ`）、被排除的码点类别（`Cc Cf Co Cs Cn Zl Zp Zs Mn Mc Me`）、NFKC 封闭规则也相同。**唯一差异**：S14 的字段都是单行文本，所以不放行 `\n` / `\t`
（`_ALWAYS_ALLOWED` 只有 `U+0020` 与 `U+3000`）；S13 的 skill 正文需要换行。区间同源是**手工保持的**——本步与 S13 并行，不能 `import skills/charset.py`（S13 未合入时它不存在）。
**合并后应去重为一份共享实现**（交接文档已记一条），否则两份会各自漂移。

### 0.5 实测后追加的偏离与发现

1. **冻结后改动 1 条用例**（S8 D13 / S12 D7 的先例）：`mr-prop-unsafe-refused` 增加 `expect.labelled_unsafe: true`。原因：`unsafe_entry_proposed_count` 原先只在"被测代码把该条目标成 unsafe"时才计数，
   而反证"拿掉 unsafe 判定"之后条目不再是 unsafe，指标退化成 `None`（**未测量**）而不是**变红**——保护一拿掉，评测就看不到它本该被拒。改为由**数据集标注**判定"这是敌意条目"，不看被测代码。
   这是反证发现的真实评测盲点，不是为了让指标好看。
2. **两个指标借用了别的分母**（反证发现）：`bytes_to_entry_urls` / `dns_lookups_for_entry_hosts` 最初用"提案速率的分母 > 0"判是否测量过，提案不跑的反证里变 `None`。改为独立的 `no_connect_checked` 标记。
3. **admin 反证最初没咬到**：`dependency_overrides` 的替身依赖必须显式声明 `Cookie(default=None)`，FastAPI 才会注入；否则替身永远拿到 `None`、全部 401，看起来像"保护还在"。已修并加注释。
4. **不可见字符不能字面写进测试**：写 `"a‮b"` 时我一度让编辑器吃进了字面 U+202E / U+FEFF，`ruff PLE2502` 当场报出。已全部改成 `\uXXXX` 转义并用脚本扫描所有新文件确认字面不可见字符为 0。
5. **审计里的 `registry_host` 在评测里就是 `127.0.0.1`**（假目录在回环上）。审计按设计记**目录主机名**，所以 `mr-api-failure-no-leak` 的审计检查排除了主机名这一项（响应检查仍含它）。
   这是用例本身的口径问题，不是泄漏。
6. **匿名 POST 返回 403 而不是 401**：CSRF 依赖排在会话解析之前，与既有 `/api/admin/mcp/*` 写接口一致（`import-preview` 匿名 POST 同为 403）。测试按既有行为断言，没有改依赖顺序。
7. **前端 `npm run build` 在 worktree 里最初失败**：`node_modules` 符号链接指向主检出，主检出的依赖缺 `react-markdown` / `remark-gfm`（`AnswerMarkdown.tsx` 报 TS2307，与本步无关）。
   改为在 worktree 里 `npm ci`（`package-lock.json` 未改动）后 `tsc -b && vite build` 通过。

## 1. 交付

| 面 | 内容 | 锁定 |
|---|---|---|
| 目录协议 v1 客户端 | `mcp/registry.py::RegistryCatalogClient`：`GET {base}/servers?query=&limit=20&cursor=`，只经 `governed_get`（`require_media_type=application/json`）；至多 5 页 / 单条 16 KiB / 累计 256 KiB；**整页严格校验**；搜索词 ≤ 64 字符且单行；游标 `[A-Za-z0-9_\-.=]{1,128}`；8 个 `MCP_REGISTRY_*` 稳定码，文案固定 | `tests/test_mcp_registry_client.py` |
| 条目不可信处理 | 字段白名单（其余丢弃并计数）→ 字符正向白名单 → 核心边界归一化不改写 → `detect_injection`（**对全文**，再截断展示）→ 任一不过即 `unsafe`（描述 / 地址 / 包不展示、不可提案）；重名与归一后同名全部 `unsafe`；展示 URL 去 userinfo 与查询串并转 punycode | `tests/test_mcp_registry_entries.py` |
| 提案 | `build_proposal`：复用 `preview_import`；只收 `streamable-http` + https + **非 IP 字面量 / 非 localhost / 非单标签**；恒 `disabled` 且无 `allow`、只含四个键；纵深复核 + 剥敏感键；`proposal_hash = sha256(规范化定义 + 目录主机 + 名称 + 版本)`；`stdio` / 包类 / sse 只展示 | `tests/test_mcp_registry_proposal.py` |
| 不连接 | 目录模块与路由不导入 `MCPStdioClient` / `MCPHttpClient` / `open_bridge` / 连接池 / 桥接 / OAuth / 装配，不直接调用 `open_exchange`，无原始网络原语（AST，带 7 种禁用写法的反证）；条目地址探针 + 网络间谍零连接零字节零 DNS | `tests/test_mcp_registry_invariants.py` |
| API | `webapp/mcp_registry_routes.py`：`GET status`、`POST search`、`POST proposal`；`require_admin` + CSRF；`extra=forbid`；提案只收 `{name, version}` 并**服务端重取**；每管理员每分钟 10 次，限流表有上限 1024（满了 fail closed）；错误 502 / 429 / 404 / 422 / 409；`Cache-Control: no-store` | `tests/test_web_mcp_registry.py` |
| 审计 | `mcp.registry_searched`（管理员 id、目录主机名、结果码、条数、页数）、`mcp.registry_proposed`（+ `name@version`、提案哈希）；**不记查询原文、条目网址、提案文本** | `tests/test_web_mcp_registry.py::test_audit_records_only_whitelisted_fields` |
| 治理 | `Capability.MCP_REGISTRY_SEARCH`（不在默认集 / Web 成员集 / `GRANTABLE`，不是危险能力，不对应任何 `ToolSpec`）；`ProviderKind` / `CallCategory.MCP_REGISTRY`；画像 15 s / `max_attempts=2` / 每主体每分钟 20；熔断键 `mcp_registry:<host>`；站点 9 → 10；usage 未知；`GOVERNANCE_CONFIG_VERSION` → `p7-s14-2026-10-23` | `tests/test_mcp_registry_invariants.py`、`tests/test_governance_contracts.py` |
| 前端 | `McpRegistrySection.tsx`（挂在 MCP 页导入区之前，不新增副栏子页）+ `mcpRegistryApi.ts`（**只有** `status` / `search` / `propose`）+ `mcpRegistryState.ts`；纯文本渲染；没有连接 / 启用 / 应用 / 保存控件；读状态失败（含 403）整块不渲染 | `web/app/tests/mcp_registry.test.mjs` |
| 部署 | `compose.yml` 给 app 透传 `WATER_AGENT_MCP_REGISTRY_URL` / `…_ALLOW_PRIVATE`（缺省为空 = 功能关闭）；**不新增服务、不新增卷**；两个变量钉进快照 `PINNED_ENV_KEYS` | `tests/test_web_deployment.py::test_mcp_registry_is_off_by_default_and_only_app_gets_it` |
| 评测 | `mcp_registry_p0`（64 条：registry 53 · api 8 · invariant 3；12 项硬指标）+ 进程内假目录 + 条目地址探针 + 网络间谍 + 比较器 + 入仓基线 + `ci_gate.sh`；每个硬指标一条反证 | `tests/test_mcp_registry_eval.py` |

## 2. 实测

`mcp_registry_p0`（两次运行报告逐字相同、不含临时路径，入仓基线与 HEAD 逐字相同）：

```text
mcp_registry_p0 mechanism=present passed=64/64
  remote_only_proposal_rate: 1.0
  proposal_disabled_no_allow_rate: 1.0
  bytes_to_entry_urls: 0
  dns_lookups_for_entry_hosts: 0
  secret_drop_rate: 1.0
  unsafe_entry_proposed_count: 0
  proposal_hash_determinism_rate: 1.0
  registry_failure_fail_closed_rate: 1.0
  non_admin_access_count: 0
  benign_entry_proposed_rate: 1.0
  benign_text_false_reject_count: 0
  harness_error_count: 0
```

### 2.1 反证（拿掉保护 → 指标离开目标值）

| 指标 | 干净 | 拿掉保护后 | 拿掉的保护 |
|---|---|---|---|
| `remote_only_proposal_rate` | 1.0 | 0.4 | 资格判定恒放行 + 兜底取第一个 remote（**两层**） |
| `proposal_disabled_no_allow_rate` | 1.0 | 0.0 | 提案改成启用 + 带 `allow` |
| `bytes_to_entry_urls` | 0 | 57 | 提案阶段对条目地址发 TCP 连接并写字节 |
| `dns_lookups_for_entry_hosts` | 0 | 4 | 提案阶段解析条目主机 |
| `secret_drop_rate` | 1.0 | 0.0 | 解析阶段保留非白名单字段 + 提案附带 URL 原文（**两层**） |
| `unsafe_entry_proposed_count` | 0 | 1 | unsafe 三重检查全放行 + `build_proposal` 不看 `status`（**两层**） |
| `proposal_hash_determinism_rate` | 1.0 | 0.6 | 哈希只含名称与版本 |
| `registry_failure_fail_closed_rate` | 1.0 | 0.25 | 整页校验改成跳过坏条目 + 去掉总量 / 页数 / 条目上限（**多层**） |
| `benign_entry_proposed_rate` | 1.0 | 0.0 | 资格判定恒拒（"全拒"不能拿满分） |
| `benign_text_false_reject_count` | 0 | 4 | 字符白名单"全拒非 ASCII" |
| `non_admin_access_count` | 0 | ≠ 0 | 路由依赖换成"任何已登录用户"（`dependency_overrides`，单独一条测试） |

另有"每个硬指标都有反证"的完备性断言、harness 出错计数、"未测量是 `None` 且判不达标"，以及比较器拒绝手改 / 数据集变化 / 用例消失 / PASS_TO_PASS 回退。

### 2.2 手工变异（单元层，17 + 4 项）

对 `mcp/registry.py` 手工拿掉单个保护，跑 4 个 pytest 文件：**17 项中 14 项直接被杀，3 项初始存活**，补测后 2 项被杀，1 项确认为真冗余：

| 存活项 | 原因 | 处理 |
|---|---|---|
| 提案不再强制 `disabled` / 允许 `allow` | `importer` 本身恒输出停用提案，防御性复核成了"永远不触发的代码" | 补 `test_the_defensive_recheck_refuses_a_proposal_that_is_not_disabled_deny_all`（让 `preview_import` 吐出启用 / 带 `allow` 的 server）与 `test_sensitive_keys_are_stripped_even_if_the_importer_were_to_emit_them` |
| 关掉 256 KiB 累计上限 | 单页字节上限（`MAX_RESPONSE_BYTES`）把它**冗余覆盖**，累计检查从没被单独测过 | 假目录加 `paging_heavy` 模式（每页 ~100 KiB、永远有下一页），补 `test_cumulative_size_across_pages_is_capped` |
| 只关掉搜索词的控制字符检查 | 后面的字符白名单也拒控制字符（**真冗余**）；同时关两层时 `test_bad_queries_are_refused_before_any_request` 变红 | 不补测——两层同时拿掉已被抓到，单层冗余是设计意图 |

前端手工变异 8 项（加"连接"按钮 / `dangerouslySetInnerHTML` / `propose` 多传 `url` / unsafe 分支展示描述 / 去掉 403 隐藏 / 渲染 `<a>` / 加 `apply()` 方法 / 改接口路径）全部被抓到，
其中 **`apply()` 一项初版存活**（断言只数了 `status|search|propose` 三个名字，多出来的方法看不见）——已收紧为"类里的方法恰好是 `constructor / request / status / search / propose`，且所有请求路径都在 `/api/admin/mcp/registry/` 下"。

## 3. 快照变化

`tool_assembly_snapshot --check`：**与基线一致（20 个 profile）**。本步不改装配面（目录搜索不是工具、不进插件）：现有 20 个 profile 与 `TOOL_POLICY_VERSION` / `PROMPT_TEMPLATE_VERSION` / `MCP_CONFIG_VERSION`
**逐字不变**；不新增 profile；两个新环境变量只进 `PINNED_ENV_KEYS`。随之更新的计数断言见 §0.3。

## 4. 不做

官方注册表适配器（§0.1）；条目 URL 的任何探测或连接；**应用提案**（写覆盖层 / 写 Web 管理层）；stdio / npm / pypi / docker 条目的提案；模型可调用的目录工具；声称兼容任何真实目录；
"可疑仍可提案"的覆盖开关；改 `mcp/schema.py`（`ensure_description_safe` 的 Tag 字符缺口只记录）、`mcp/importer.py`、`gateway.py` / `pipeline.py` / `rules.py` / `cache.py`、`external/**`；上调任何预算数字。

## 5. 残余风险与未验证项

- **没有任何真实目录被验证**（P7-S14-A10）：协议 v1 是自定义的；官方注册表真实形态不同且本步不做适配器（§0.1），对它会整页失败。
- **条目与真实 server 的一致性检不出**：目录里写的描述是否如实、地址背后是不是它声称的 server、运维启用并放行后该 server 的实际行为，任何确定性规则都不能判断。**不得声称"目录搜索是安全的"。**
- **字符白名单 / 注入检测 / 归一化都是确定性规则**，必有语料之外的写法。白名单放行 `ρ ν κ`（合法工程符号），它们能在注入句里替换 `p v k`；本步没有像 S13 那样对"希腊同形字折叠版"再跑一遍注入检测——
  目录描述会被展示给管理员、进提案备注，但**不进系统提示、不进任何模型输入**，所以这条残余的影响面比 S13 小，仍应记录。
- **提案哈希不是签名**：它只用来核对"贴进覆盖层的就是看到的这份"，不证明条目可信；面板上已有这句话。
- **被盗的管理员账号**：最多拿到一份"已停用、全拒"的文本，并能触发对运维目录的只读请求（限额 10 次 / 分钟 / 管理员）；改不了目录地址。
- **数据出境**：搜索词会发往运维配置的目录；运维写错基址就是数据外发；`ALLOW_PRIVATE=1` 打开后内网可达（云元数据地址永远拒）。
- **限流 / 熔断是进程内的**：多 worker 各一份（与既有网关相同）；管理员限额表是进程内、有上限（1024），满了 fail closed 到"限流"。
- **字符白名单手工同源于 S13**，合并后应去重（§0.4）。
- **浏览器手动验收未做**：只有组件级 node 测试与 `tsc -b && vite build`；没有在真实 Web 栈里点过"搜索 → 生成提案 → 复制"。
- **匿名 / 成员访问**的状态码以既有行为为准（匿名 POST 为 403，见 §0.5 第 6 条）。

## 验证记录

2026-10-23，本机（macOS，Python 3.12.14，Node 26.8.1），分支 `YEK/p7-s14-impl`，`PYTHONPATH=src`，已 `unset` 的变量：`HTTPS_PROXY HTTP_PROXY WATER_AGENT_LLM_BASE_URL WATER_AGENT_LLM_MODEL DEEPSEEK_API_KEY DASHSCOPE_API_KEY LOCAL_MODEL_ENABLED`。

| 项 | 命令 | 结果 |
|---|---|---|
| 本步新增测试 | `pytest tests/test_mcp_registry_*.py tests/test_web_mcp_registry.py` | 客户端 33 · 条目 36 · 提案 26 · 不变量 14 · Web 28 · 评测 18，全部通过 |
| 受影响的既有套件 | `pytest tests/test_vision_review.py tests/test_llm_endpoints.py tests/test_governance_contracts.py tests/test_dxf_upload.py tests/test_mcp_http_config.py tests/test_plugin_invariants.py tests/test_web_deployment.py` | 通过（计数 9 → 10、错误码 20 → 28、`GOVERNANCE_CONFIG_VERSION` 已更新） |
| 防漂移 harness | `pytest tests/test_p7_traceability.py tests/test_check_step_scope.py tests/test_import_guard.py tests/test_p7_known_figures.py tests/test_tool_assembly_snapshot.py` | 通过（规格 §7 登记 `P7-S14-A01…A10` 之后） |
| 全量 | `pytest -q -o addopts= -p no:cacheprovider` | **4278 passed, 30 skipped**（6 分 35 秒）；首次全量曾是 4277 passed、1 failed（见下） |
| 干净 main 对照 | 同一命令，`origin/main`（`00982cf`）的独立 worktree | 4122 passed, 30 skipped（本步净增 156 条测试） |
| ruff | `ruff check src` | All checks passed |
| mypy | `mypy src/water_agent` | no issues found in 265 source files |
| 快照 | `tool_assembly_snapshot --check` | 与基线一致（20 个 profile） |
| 范围守卫 | `check_step_scope.py … --base origin/main --bootstrap` | 范围守卫通过（Prompt 尚未合入 main，故用 `--bootstrap`） |
| compose | `docker compose config -q` 与 `-f compose.yml -f compose.production.yml config -q` | 都能解析 |
| 前端 | `cd web/app && npm test && npm run build` | 139 passed；`tsc -b && vite build` 通过（chunk 大小提示是既有的） |
| 门禁 | `COMPOSE_PROJECT_NAME=p7s14 bash scripts/ci_gate.sh` | **退出码 0**；`mcp_registry_p0 passed=64/64` 且基线对比通过 |

**一条间歇失败，如实记录**：首次全量里 `tests/test_web_model_selection.py::test_catalog_is_admin_managed_and_member_readable`（S15 的测试，本步没改过）失败了一次。
单独跑、与相邻 Web 测试一起跑、以及 `test_web_*.py + test_mcp_*.py`（834 条）整体跑都通过；之后在**同一个分支上**再跑两次全量（一次独立，一次在 `ci_gate.sh` 内）都全绿。
**我没有抓到失败时的断言输出**（首次全量用了 `tail` 只截了汇总行），所以**不知道**根因，也**不能**断言它与本步无关——只能说：它不在本步改动的任何文件里，本步新增的测试也没有改共享的审计缓冲或凭据卷；
该测试读 `_get_events()` 的进程内共享缓冲并断言 `len(events) == 1`，是已知的顺序敏感点。**建议合并前在 CI 上再观察**；若再现，应当用 `-rf` 抓完整断言输出再定位。

**未做**：真实 Web 栈（PG + 浏览器）上的手动验收；真实目录；`p5-compose` 冒烟与 `check_image.sh`（本机 `docker compose build` 因 buildx bake 的 gRPC 错误不可用，见 AGENTS.md）；
CI 上 `p7-scope` 的 `--auto` 运行（分支未推送，且 GitHub Actions 当前被计费拦住）。
