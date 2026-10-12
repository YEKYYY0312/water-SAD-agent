# P7-S8 执行 Prompt：MCP Streamable HTTP 传输与 OAuth 2.1 授权（Step D（二））

> **工作区**：仓库 `YEKYYY0312/water-SAD-agent`，基于最新 `main`（必须已包含 P7-S9 与本 Prompt）。新建分支 `YEK/p7-s8-streamable-http`。
> **必读**：
> - `AGENTS.md`
> - `docs/design/16-p7-plugin-mcp.md`：§1（插件不是权限主体）、§3.3–3.4 内置插件与 MCP 两种来源、§3.5 runner 语义、§3.6 按意图暴露（MCP 每台一组 ambient）、§3.7 确认门、§3.8 Web 扩展面板（Web 管理层、凭据槽位、探测）、§4 不变量、§5 S8 约束、§7 的 P7-S8 验收、§8 实施协议
> - `docs/design/14-p4-mcp-gateway.md` §2–§5（客户端协议面、"转换即授权"五道条件、`MCP_INVOKE` 权限模型；"不做 HTTP/SSE"与"不做 OAuth"两条非目标由本步解除）
> - `docs/design/p2.5-threat-model.md` §2 信任边界、T1 / T3 / T4 / T11 / T12、§5 残余风险——本步**新增出站网络面与令牌存储**，必须登记为 T13
> - `docs/design/p2.5-provider-inventory.md`（出站清单的人读版本）、`docs/design/p7-step-5-decision.md`（连接池与指纹）、`docs/design/p7-step-6-decision.md`（凭据槽位、面板写入边界）
> - MCP 规范原文（实现时以原文为准，不凭记忆）：`https://modelcontextprotocol.io/specification/2025-11-25/basic/transports`、`…/2025-11-25/basic/authorization`、`…/2025-11-25/basic/security_best_practices`；新版 `…/2026-07-28/basic/versioning` 与 `…/2026-07-28/basic/transports/streamable-http`（只做识别，见 §0）。原始 Markdown 在 `github.com/modelcontextprotocol/modelcontextprotocol` 的 `docs/specification/<版本>/` 下

---

## 0. 相对规格的修订与本步决定（以本文为准）

S8 是 2026-10-06 从 S5 拆出的一步（规格 §5 只写了"`NETWORK_FETCH` 能力决策 + 出站清单 7 → 8"）。2026-10-12 写本 Prompt 时用户定了三件事：**网络出口由运维白名单决定、不碰危险能力集**；**OAuth 2.1 授权一并做**；**协议以 2025-11-25（会话制）为主，2026-07-28（无状态）只做识别**。下面是读码与读规范后定下的做法，规格在本 Prompt 的同一个 PR 里同步：

| 疑问 | 本步做法 | 原因 |
|---|---|---|
| 远程 MCP 工具持什么能力 | **仍是 `Capability.MCP_INVOKE`**，与 stdio 一致；`NETWORK_FETCH` **留在** `DANGEROUS_CAPABILITIES`，`Principal` 构造期拒绝与护栏 `tool.dangerous_capability` 都不改 | `NETWORK_FETCH` 的语义是"模型决定去哪"；HTTP MCP 的出口是**运维写死的一个 URL**，模型只能选工具、填参数，改不了目标——这与 stdio 的 `command` 同一边界（装配期配置，模型不可达）。把出口当成配置面治理，比开一个"危险但被授予"的能力更窄 |
| 出站要不要进清单 | 进：`PROVIDER_CALL_SITES` 新增 `mcp_streamable_http`（`ProviderKind.MCP_REMOTE`、`CallCategory.MCP_TOOL_CALL`、`required_capability=MCP_INVOKE`、`data_class=user_query`），`provider_call_sites` / `provider_governed` **7 → 8**；出站模块 `water_agent/mcp/http_transport.py`；`_EGRESS_PATTERNS` 增加本步实际用到的连接原语（`http.client.HTTPSConnection(` / `HTTPConnection(`）——**扫描必须能命中新模块**，否则扫描形同虚设（AGENTS.md P2.5 条） | 规格 §5 S8 原话"出站清单 7 → 8"；不登记就是治理层可被绕过 |
| 远程调用走不走 `ProviderGateway` | **走**：每次 `tools/call` 与 OAuth 端点请求都经 `ProviderGateway.call`（限流 / 熔断 / 有界重试 / deadline），`ProviderProfile` 新增 `MCP_REMOTE` 画像（超时取 server 配置的 `call_timeout_seconds`，`max_attempts=1`——**工具调用不重试**：MCP 工具可能有副作用，重放即重复执行；熔断与错误分类**沿用 gateway 现有逻辑**（`gateway.py` 在 forbid 里，不改），`ProviderCall.model` 填 `mcp:<server_id>`，使熔断按单台 server 计）；usage 记 `usage_known=False`（MCP 没有 token 概念，**不估算**）；`GOVERNANCE_CONFIG_VERSION` 递进 | 出口统一治理是 P2.5 的硬约束；"401/403 零重试且不计熔断"沿用 gateway 现有语义 |
| HTTP 库 | **只用标准库**（`http.client` + `ssl` + `urllib.parse` + `json`），不新增依赖；**禁止**直接用 `httpx` / `requests`（它们是 openai 的传递依赖，但 `mcp/` 是纯标准库包，P4 决策 D1） | `mcp/` 包"纯标准库"有决策记录；`http.client` 让我们自己控制重定向（**不跟随**）、连接目标 IP 钉死（防 DNS 重绑定）、响应字节上限 |
| 协议版本 | 主路径实现 **2025-11-25** Streamable HTTP：POST 每条消息、`Accept: application/json, text/event-stream`、响应是单个 JSON **或** SSE 流（两种都必须支持）、`MCP-Session-Id`（大小写不敏感读，回发原值；只接受 0x21–0x7E）、之后每个请求带 `MCP-Protocol-Version`、404 → 重新 `initialize` 一次、关闭时 `DELETE`（405 视为正常）。stdio 客户端的 `MCP_PROTOCOL_VERSION`（`2025-06-18`）**不改**；HTTP 客户端在 `initialize` 里请求 `2025-11-25`，接受 server 回的 `2025-06-18` / `2025-03-26`（三者语义对客户端兼容），其余版本 → `MCP_PROTOCOL_ERROR` | 用户选"旧版为主"；2025-11-25 是当前多数 server 说的版本，与仓库现有 initialize 握手同构，改动面最小 |
| 2026-07-28（无状态）怎么"识别" | 不实现。`initialize` 被拒且响应体是可识别的新版错误（`UnsupportedProtocolVersionError`，或 `400` 且响应头 / 体表明需要 `_meta` 版本）时，记新错误码 **`MCP_PROTOCOL_UNSUPPORTED`**（detail 写明"server 只支持无状态协议 2026-07-28，S8 未实现"），不当成普通连接失败；**不**回落到已废弃的 HTTP+SSE（2024-11-05） | 能如实说"为什么连不上"，不把版本不匹配伪装成网络故障；HTTP+SSE 在新版里已正式 Deprecated |
| server 主动请求 / 通知 | SSE 流里出现的 server→client **请求**回 `MethodNotFound`（与 stdio 同）、**通知**忽略但可观测记录；**不**开 GET 监听流（规范 MAY）；**不**做 `Last-Event-ID` 续传（2025-11-25 是 SHOULD 级，且 2026-07-28 已删除）：流在收到响应前断开 → `MCP_CONNECT_FAILED`，工具调用**不自动重发**（可能有副作用） | 最小协议面；不重发是"有副作用的工具不能被网络抖动执行两次"的硬要求 |
| 配置形状 | `ServerTransport` 新增 `STREAMABLE_HTTP = "streamable_http"`；`MCPServerConfig` 新增 `url: str \| None`、`allow_private_network: bool = False`、`oauth: MCPOAuthConfig \| None`、`bearer_credential: str \| None`（引用一个 `credential_env` 槽位名）。`transport=stdio` 时 `command` 必填、`url` 必须为空；`streamable_http` 时 `url` 必填、`command` / `args` / `env` / `env_file` / `cwd` 必须为空（混写即 `MCP_CONFIG_INVALID`，不静默忽略）。`command` 由必填改为 `str \| None` 并由 model validator 按 transport 判定。**这些字段只能写在仓库默认层 / 部署覆盖层**；Web 管理层（S6）的可写键**不变**（`server_id` / `disabled` / `allow`），面板改不了 URL、改不了授权服务器 | URL 就是出口；能写 URL 的人等于能决定 agent 往哪发数据，与 `command` 同级，只能是运维 |
| URL 校验（SSRF） | 加载期：只接受 `https://`（`allow_private_network=true` 时额外接受 `http://`，**只**用于本机 / 局域网调试），不得含 userinfo（`user:pass@`）、fragment；主机名 IDNA 归一。**连接期**（每次建连，不只加载期）：解析出全部地址，任一地址落在回环 / 私有（RFC 1918、`fc00::/7`）/ 链路本地（`169.254.0.0/16`、`fe80::/10`，含云元数据 `169.254.169.254`）/ CGNAT（`100.64.0.0/10`）/ 未指定 / 组播 / 保留段 → `MCP_DESTINATION_FORBIDDEN`（`allow_private_network=true` 时放行回环与私有段，**链路本地与元数据地址永远拒绝**）；**连接钉死**到校验过的那个 IP（自定 `HTTPSConnection` 子类，TLS SNI 与证书校验仍按主机名），不再二次解析——防 DNS 重绑定；**重定向一律不跟随**（3xx → `MCP_PROTOCOL_ERROR`，detail 只写状态码） | 出口是运维写的，但解析结果不是：一个公网域名可以解析到内网；跟随重定向等于让远端决定下一跳 |
| 响应边界 | 单条 JSON 响应与 SSE 单事件都受 `max_response_bytes` 约束（超出即 `MCP_RESPONSE_TOO_LARGE` 并断开）；整个 SSE 流另有总字节上限（`max_response_bytes × 4`）与 deadline；`Content-Type` 不是 `application/json` / `text/event-stream` → `MCP_PROTOCOL_ERROR`；响应头里的 `MCP-Session-Id` 不合法 → `MCP_PROTOCOL_ERROR` | 与 stdio 客户端同一套"远端是不可信的"口径 |
| 远端内容 | 工具描述照旧过 `schema.ensure_description_safe`（不安全即 `MCP_DESCRIPTION_UNSAFE` 不暴露）、schema 子集照旧、结果照旧 `SourceTag.TOOL_OUTPUT`；**远端返回的 URL / 重定向 / `WWW-Authenticate` 里的地址都不是指令**——授权服务器地址只能来自运维配置或运维配置的 server 返回的元数据，并且同样过 SSRF 校验 | 远端不可信是 P4 的前提 |
| OAuth 2.1：谁授权、用谁的身份 | **服务级授权**：运维在覆盖层声明 `oauth`，**管理员**在 Web 扩展面板点"授权"走一次授权码 + PKCE 流程，拿到的令牌属于**这台 server 的部署**（所有用户的 run 共用）；**不做**按终端用户的委托授权（每个用户自己授权一次）。面板在授权前明确提示"所有用户都会以该授权身份调用此 server" | 我们是多用户服务，按用户授权需要用户级令牌存储、登录态与 run 主体绑定、撤销传播，是另一个量级的设计；服务级授权与 S6 凭据槽位同一信任模型（管理员给部署填凭据） |
| OAuth 2.1：客户端注册 | 按 2025-11-25 的优先级只实现两种：① **预注册**（运维在 `oauth.client_id` 写死；有 `client_secret` 的走凭据槽位，`token_endpoint_auth_method` 支持 `none` / `client_secret_basic` / `client_secret_post`）；② **动态客户端注册**（RFC 7591，授权服务器元数据有 `registration_endpoint` 且运维设 `oauth.dynamic_registration: true` 时；注册结果按 **issuer** 持久化在凭据卷，issuer 变了即作废重注册）。**不做** Client ID Metadata Documents（需要我们对外托管一个 HTTPS 元数据文档，部署前提不满足）；元数据声明 `client_id_metadata_document_supported` 但没有预注册也没开动态注册 → `MCP_OAUTH_UNAVAILABLE`（detail 说明缺什么） | CIMD 要求客户端有公网可达的 HTTPS 身份 URL；自托管部署不一定有，做了也无法离线测 |
| OAuth 2.1：发现与流程 | 严格按规范：未授权请求收到 `401` → 解析 `WWW-Authenticate` 的 `resource_metadata`（没有则依次试 `/.well-known/oauth-protected-resource/<path>` 与根路径）→ 取 `authorization_servers`（多个时取**第一个**、并且必须在运维配置的 `oauth.allowed_issuers` 里，没配即只接受第一个且记录）→ 授权服务器元数据按规范顺序尝试 RFC 8414 与 OIDC 两种 well-known（含 path 插入）→ **`code_challenge_methods_supported` 缺失或不含 `S256` → 拒绝继续**（`MCP_OAUTH_UNAVAILABLE`）→ 授权请求带 `response_type=code`、`client_id`、`redirect_uri`、`code_challenge`（S256）、`state`（每次新生成、一次性、10 分钟过期）、`resource`（server 的**规范 URI**：小写 scheme 与 host、无 fragment、无尾斜杠）、`scope`（`WWW-Authenticate` 的 `scope` 优先，其次 `scopes_supported`，都没有就不带）→ 回调校验 `state`、有 `iss` 时校验等于记录的 issuer（RFC 9207）→ 令牌请求带 `code_verifier` 与同一个 `resource` → 存令牌。所有 OAuth 端点必须 `https://`（规范 MUST），同样过 SSRF 校验与不跟随重定向 | 规范里客户端侧 MUST 项逐条落地；`allowed_issuers` 防"被攻陷的 MCP server 把令牌请求引到攻击者的授权服务器" |
| OAuth 2.1：回调地址 | `redirect_uri = <WEB_PUBLIC_BASE_URL>/api/admin/mcp/oauth/callback`，新环境变量 `WEB_PUBLIC_BASE_URL`（必须 `https://`，本机开发允许 `http://localhost` / `http://127.0.0.1`——规范允许 localhost 回调）；没配就**不提供**授权按钮（列表显示 `oauth_unavailable_reason`），不猜主机名、**不信任请求里的 Host 头** | 回调地址必须预先注册、精确匹配；从 Host 头推导等于让攻击者决定回调去哪 |
| OAuth 2.1：令牌存储与使用 | 令牌（access / refresh / 过期时间 / scope / issuer / 规范 resource）存凭据卷 `<credentials_dir>/mcp/<server_id>/oauth.json`（0600，目录 0700，原子替换），与 S6 槽位同卷、**不进备份**、**只写不读**（任何接口只回 `authorized` / `expires_at` / `scope` / `issuer`，不回令牌）；只经 `Authorization: Bearer` 头发送，**永不进 URL 查询串**；**只发给配置的这一个 MCP endpoint**（与签发它的 issuer 绑定，规范"不得把别处的令牌发给 MCP server"）；过期前 60 秒或收到 `401 invalid_token` 时用 refresh token 刷新**一次**（刷新响应若轮换了 refresh token 必须替换），刷新失败 → 清除令牌、`MCP_OAUTH_REQUIRED`（需管理员重新授权）。`403 insufficient_scope` → `MCP_OAUTH_INSUFFICIENT_SCOPE`（列出所需 scope，**不自动**发起升级授权——服务级令牌的 scope 变化必须管理员确认） | 令牌是长期凭据；与 S6 槽位同一保管纪律 |
| 令牌进不进 env 摘要 / 指纹 | 进：S5 连接池的 shared server 指纹里加入 `oauth.json` 的**摘要**（不是令牌本身），令牌刷新**不**换指纹（同一授权），重新授权 / 清除换指纹 | shared HTTP 连接（会话）复用的前提是凭据没变 |
| 静态 Bearer | `bearer_credential: "<槽位名>"`：运维在 `credential_env` 里声明该槽位、面板填值，作为 `Authorization: Bearer` 发出；与 `oauth` 互斥（同时配即 `MCP_CONFIG_INVALID`）；槽位没填值 → `MCP_CREDENTIAL_MISSING`（S6 同一语义）。stdio 的槽位仍注入子进程 env，HTTP 的槽位**不**进任何 env | 很多企业库 MCP 只发静态 API key；复用 S6 槽位不另起一套 |
| `lifecycle` 与连接池 | HTTP server 同样支持 `per_run` / `shared`：`shared` 复用的是 **MCP 会话**（`MCP-Session-Id` + HTTP keep-alive 连接），语义与 S5 相同——声明 shared 即声明这台 server 不持有按用户区分的会话状态；会话被 server 终止（404）时池内透明重建一次 | 与 S5 同构，不另写一套池 |
| 确认门 | `human_confirmation=true` 的 HTTP server 与 stdio 完全相同：只在 Web 入口暴露为延迟工具，CLI / runner 整台 `MCP_CONFIRMATION_UNAVAILABLE` | S7 的入口语义不因传输改变 |
| 导入预览 | S6 导入里 `url` / `type: http` / `streamableHttp` 条目由"标为 S8 不支持"改为生成 `transport: streamable_http` 的禁用提案（`disabled: true`、不写 `allow`、`headers` 里的值一律丢弃——形似 `Authorization` 的头变成提示"请改用 `bearer_credential` 槽位或 `oauth`"），`type: sse`（旧 HTTP+SSE）仍标为不支持 | 用户已有的 Claude Code / Cursor 配置大量是 HTTP server |
| 面板 | MCP 扩展区的 server 卡片对 HTTP server 显示：传输方式、URL 的**主机名**（不显示路径与查询串——可能含租户 id）、`allow_private_network` 标记、授权状态（未配置 / 未授权 / 已授权至 X / 授权失效）、"授权 / 重新授权 / 撤销授权"按钮（管理员 + CSRF；撤销 = 删本地令牌，若授权服务器有 revocation endpoint 则尽力调用一次）。**面板仍改不了 URL / oauth 配置**。成员看不到任何 HTTP server 细节（S6 已是管理员专属） | 与 S6"面板只改启停 / 白名单 / 凭据值"的边界一致 |
| 评测 | 新增 `mcp_http_p0`（≥ 24 条合成用例，Step 1 冻结）：经生产装配（会话 + runner + Web 会话工厂三条路径各若干条）对接**本地假 HTTP MCP server**（`tests/fixtures/fake_mcp_http_server.py`，标准库 `http.server` + 自签证书，覆盖 JSON / SSE 两种响应、会话 404、坏 Content-Type、超大响应、重定向、server→client 请求、新版协议拒绝、401 + OAuth 元数据、PKCE 缺失、`iss` 不匹配、令牌过期刷新、`insufficient_scope`、DNS 指向回环 / 元数据地址），断言暴露 / 跳过码 / 调用结果 / 出站计数 / 令牌不外泄。接进 `ci_gate.sh` 与入仓基线对比 | 评测先行；**没有真实 HTTP MCP server 与真实授权服务器**，全部离线（见未覆盖项） |

---

## 1. 目标与边界

**做**（编号对应规格 §7 的 P7-S8-Axx）：

1. **评测集先行（A12 前半）**：`data/eval/mcp_http_p0.jsonl` + `harness/mcp_http_eval.py` + `tests/fixtures/fake_mcp_http_server.py`（标准库 `http.server`，`--mode` 与 stdio 假 server 同一套命名；TLS 用测试运行时生成的自签证书——**本机 venv 没有 `cryptography`**，用 `openssl req -x509 …` 子进程生成，或预生成一对**仅测试用**的证书放进 `tests/fixtures/tls/` 并在文件名与注释里写明"测试私钥，非机密"，`scripts/check_image.sh` 不受影响因为 `tests/` 不进镜像）。在未改动的代码上跑一次，报告如实记 `mechanism=absent`，数据集此后冻结。
2. **配置（A01）**：`ServerTransport.STREAMABLE_HTTP`、`url` / `allow_private_network` / `oauth` / `bearer_credential` 与按 transport 的互斥校验；URL 加载期校验；`MCP_CONFIG_VERSION` 递进；Web 管理层可写键不变（有测试）。
3. **出口与 SSRF（A02）**：`mcp/http_transport.py`（唯一出站模块）：地址解析 + 分类拒绝 + 连接钉死 IP + 不跟随重定向 + 响应字节上限 + Content-Type 校验；新错误码 `MCP_DESTINATION_FORBIDDEN`。
4. **协议客户端（A03）**：`mcp/http_client.py::MCPHttpClient`，与 `MCPStdioClient` **同一个外部接口**（`connect` / `list_tools` / `call_tool` / `close` / `alive` / `server_info`），`bridge.open_bridge` 按 transport 选客户端，桥接其余逻辑一行不改；会话、协议版本头、JSON 与 SSE 两种响应、404 重建、DELETE、server→client 请求回 MethodNotFound；新版协议识别 `MCP_PROTOCOL_UNSUPPORTED`；同一连接上的请求串行（S5 的 F17 教训同样适用）。
5. **治理接线（A04）**：`ProviderKind.MCP_REMOTE`、`CallCategory.MCP_TOOL_CALL`、`MCP_REMOTE` 画像（工具调用不重试）、`PROVIDER_CALL_SITES` 7 → 8、`_EGRESS_PATTERNS` 补连接原语、`docs/design/p2.5-provider-inventory.md` 同步；`GOVERNANCE_CONFIG_VERSION` 递进。每次远程调用经 `ProviderGateway.call`，主体来自装配时的 `RequestContext`（会话 / runner / Web 的现有主体，**不新建主体**）。
6. **静态 Bearer（A05）**：`bearer_credential` 引用 S6 槽位；值只进 `Authorization` 头；缺值 `MCP_CREDENTIAL_MISSING`。
7. **OAuth 2.1（A06–A08）**：`mcp/oauth.py`（发现、PKCE、state、`resource` 规范化、`iss` 校验、令牌请求、刷新、撤销；纯标准库，所有出站经 `http_transport`）、`mcp/oauth_store.py`（凭据卷 `oauth.json`，只写不读）；新错误码 `MCP_OAUTH_REQUIRED` / `MCP_OAUTH_UNAVAILABLE` / `MCP_OAUTH_INSUFFICIENT_SCOPE`；Web 路由 `POST /api/admin/mcp/servers/{id}/oauth/start`（返回授权 URL，前端跳转）、`GET /api/admin/mcp/oauth/callback`（只认服务端存的 state，state 一次性）、`DELETE /api/admin/mcp/servers/{id}/oauth`（撤销）；`WEB_PUBLIC_BASE_URL`；审计 `mcp.oauth_authorized` / `mcp.oauth_revoked` / `mcp.oauth_refresh_failed`（只记 server_id、issuer、scope、结果码，**永不记令牌、code、verifier、state 值**）。
8. **连接池与生命周期（A09）**：`McpConnectionPool` 支持 HTTP 客户端；指纹加入令牌文件摘要；会话 404 透明重建一次；`close()` 发 DELETE。
9. **导入与面板（A10、A11）**：`mcp/importer.py` 支持 HTTP 条目；`webapp/mcp_admin_routes.py` 列表字段（transport、url 主机名、授权状态）；前端 `McpAdminPanel.tsx` / `mcpAdminApi.ts` / `mcpAdminState.ts` 的 HTTP 卡片与授权按钮、`web/app/tests/mcp_admin.test.mjs` 补用例。
10. **评测接线（A12 后半）**：`scripts/compare_mcp_http_p0.py` + 入仓基线 `data/eval/mcp_http_p0_main.json` + `scripts/ci_gate.sh`。
11. **快照（A13）**：`PINNED_ENV_KEYS` 加 `WEB_PUBLIC_BASE_URL`；新增 profile `session_http_mcp_fake`（经本地假 HTTP server 暴露两个工具）。现有 16 个 profile **逐字不变**，只允许 `/versions/MCP_CONFIG_VERSION` 变化（`GOVERNANCE_CONFIG_VERSION` 不在快照里）。

**不做**：

- 已废弃的 HTTP+SSE 传输（2024-11-05）与回落探测；2026-07-28 无状态协议（只识别）；GET 监听流、`Last-Event-ID` 续传、工具调用自动重发。
- 按终端用户的 OAuth 委托授权；Client ID Metadata Documents；`client_credentials` 授权模式；自动 scope 升级；令牌内省。
- 面板写 URL / oauth / transport；面板新增 server（仍只能运维写覆盖层）。
- 本步不实现 MCP **server** 端（把本项目工具对外暴露为 HTTP MCP）。
- 不改 `NETWORK_FETCH` / `DANGEROUS_CAPABILITIES` / `INSTRUCTION_BEARING_SOURCES` / `GRANTABLE_CAPABILITIES`。
- 不引入新依赖（不装官方 `mcp` SDK、不用 `httpx` / `requests` / `authlib`）。

## 2. 铁律与治理落点（不得违反）

- **出口只由运维决定**：URL、授权服务器白名单、`allow_private_network` 只能来自仓库默认层 / 部署覆盖层；模型输入、远端响应、Web 管理层、HTTP 请求头都改变不了连接目标。连接期 SSRF 校验对**每一次**建连生效（含 OAuth 端点），不是只在加载期。
- **出站统一治理**：`mcp/http_transport.py` 是 MCP 远程出口的唯一模块，登记进 `PROVIDER_CALL_SITES`，静态扫描能命中它；每次远程请求经 `ProviderGateway`。工具调用**零重试**。
- **令牌只写不读**：令牌、refresh token、授权码、PKCE verifier、state 值永不出现在任何 HTTP 响应、日志、trace、审计、层文件、备份、快照、评测报告里（评测 harness 有专门的泄漏扫描指标）；只经 `Authorization` 头发给签发它的那台 server。
- **远端不可信**：描述安检、schema 子集、`TOOL_OUTPUT` 来源标签原样适用；远端返回的任何地址都要过同一套 SSRF 校验与白名单。
- **危险能力不放宽**：`NETWORK_FETCH` 仍在危险集；P2.5 的两层拒绝测试原样通过。
- **评测先行**：`mcp_http_p0` 在 Step 1 冻结；每条验收先写失败测试（docstring 写验收 ID）。
- 三条铁律：MCP 远程工具的结果是 observation，不是计算结果、不是条文来源；不得因为"远端算过了"就跳过 `calc_velocity` 或引用校验。

## 3. 必须复用的现有组件（不得另写一份）

| 需要 | 用这个 | 位置 |
|---|---|---|
| JSON-RPC 编解码 | `encode_message` / `decode_message` / `make_request` / `make_notification` / `make_error_response` / `parse_tool_call_result` | `mcp/protocol.py` |
| 远端工具形状 | `RemoteTool` | `mcp/client.py` |
| 桥接与五道条件 | `bridge_server_tools` / `open_bridge`（只加"按 transport 选客户端"） | `mcp/bridge.py` |
| 描述安检 / schema 子集 | `ensure_description_safe` / schema 子集 | `mcp/schema.py` |
| 分层配置与面板层 | `load_layered_gateway_config` / `WebLayerStore` / `check_candidate` | `mcp/config.py`、`mcp/web_layer.py` |
| 凭据槽位与凭据卷 | `validate_slot_name` / `read_slot` / `write_slot` / `credentials_dir` | `mcp/credentials.py`、`config/credentials.py` |
| 连接池与指纹 | `McpConnectionPool` | `mcp/pool.py` |
| 远程出口治理 | `ProviderGateway.call` / `ProviderProfile` / `PROVIDER_CALL_SITES` / `_EGRESS_PATTERNS` | `runtime/governance/` |
| 取消下传 | `raise_if_cancelled`（SSE 读循环每个事件之间检查） | `runtime/cancellation.py` |
| 审计 | `_emit` 风格事件 | `webapp/auth/audit.py` |
| 管理端鉴权 | `require_admin` + `validate_csrf` | `webapp/auth/` |
| 评测运行 | 生产 `AgenticSession` / runner / `WebAgenticSessionFactory` + `ScriptedChatModel`，`hermetic_environment` | `harness/` |
| 比较器 | 照 `compare_tool_exposure_p0.py` / `compare_skill_p0.py` | `scripts/` |

## 4. 实施步骤（一步一个 commit；每个 commit 结束时快照 `--check` 必须一致或按本节写明的变化 `--write --reason`）

### Step 1：评测集冻结（A12 前半）
`tests/fixtures/fake_mcp_http_server.py`（标准库，自签 TLS，`--mode`：`json` / `sse` / `session_expire` / `bad_content_type` / `flood` / `redirect` / `server_request` / `modern_only` / `oauth` / `oauth_no_pkce` / `oauth_bad_iss` / `oauth_expire` / `insufficient_scope` / `slow`；OAuth 模式同时起一个假授权服务器，元数据 / 授权 / 令牌 / 注册 / 撤销端点俱全，授权端点**不需要浏览器**：测试直接带 state 调回调）、`data/eval/mcp_http_p0.jsonl`（≥ 24 条：transport 与暴露 / 协议健壮性 / SSRF / 静态 Bearer / OAuth 发现与流程 / 令牌刷新与失效 / 确认门 / 三条入口路径）、`harness/mcp_http_eval.py`（硬指标：`expected_exposure_rate`、`skip_code_match_rate`、`call_result_match_rate`、`forbidden_destination_block_rate`（应拦的目标全部拦下且**零字节**发出）、`token_leak_count`（报告 / trace / 审计 / 日志里出现令牌、code、verifier、state 原文的次数）、`tool_call_replay_count`（同一工具调用被发出超过一次的次数）、`egress_ungoverned_count`、`harness_error_count`）。在未改动代码上跑，报告记 `mechanism=absent`。

### Step 2：配置（A01）
`mcp/config.py`：`ServerTransport.STREAMABLE_HTTP`、新字段与互斥校验、URL 加载期校验、`MCPOAuthConfig`（`client_id` / `client_secret_credential`（槽位名）/ `token_endpoint_auth_method` / `dynamic_registration` / `allowed_issuers` / `scopes`，`extra="forbid"`）；`enabled_servers()` 不再只返回 stdio；`MCP_CONFIG_VERSION` 递进。`tests/test_mcp_pool.py` 里钉死的 `MCP_CONFIG_VERSION` 字面量随之更新（S6 同样做过）。快照：只有 `/versions/MCP_CONFIG_VERSION` 变化。

### Step 3：出口与 SSRF（A02）
`mcp/http_transport.py` + `MCP_DESTINATION_FORBIDDEN`。单测覆盖：每类保留地址、IPv4-mapped IPv6、十进制 / 八进制 / 短写 IPv4 主机名（`2130706433`、`0177.0.0.1`、`127.1`）、解析到多个地址其中一个私有、DNS 在校验后变更（用可替换的解析函数模拟重绑定，断言连接用的是校验过的 IP）、3xx 不跟随、userinfo、超大响应。

### Step 4：协议客户端（A03）
`mcp/http_client.py::MCPHttpClient` + `MCP_PROTOCOL_UNSUPPORTED`；`bridge.open_bridge` 按 transport 选客户端。对接假 server 的 `json` / `sse` / `session_expire` / `bad_content_type` / `flood` / `server_request` / `modern_only` 模式。并发调用串行（替身须乱序应答，S5 教训）。

### Step 5：治理接线（A04）
`ProviderKind.MCP_REMOTE` / `CallCategory.MCP_TOOL_CALL` / 画像 / 清单 7 → 8 / `_EGRESS_PATTERNS` / 人读清单文档；`GOVERNANCE_CONFIG_VERSION` 递进。反证：把出站代码挪到一个未登记模块，`test_no_ungoverned_remote_egress` 必须红。

### Step 6：静态 Bearer（A05）
`bearer_credential` 校验（必须是本 server 声明的 `credential_env` 槽位、与 `oauth` 互斥）与发送；缺值跳过。

### Step 7：OAuth 2.1（A06–A08）
`mcp/oauth.py` + `mcp/oauth_store.py` + 三个错误码 + Web 路由 + `WEB_PUBLIC_BASE_URL` + 审计。逐条对照 2025-11-25 authorization 的客户端 MUST 项写测试（清单见 §6 受保护断言下方的"规范逐条"）。

### Step 8：连接池（A09）
池支持 HTTP 客户端；指纹含令牌摘要；会话 404 重建；关闭发 DELETE。

### Step 9：导入与面板（A10、A11）
导入 HTTP 条目；管理 API 字段；前端卡片与授权按钮；前端单测。

### Step 10：快照与评测接线（A12 后半、A13）
新 profile `session_http_mcp_fake`；`PINNED_ENV_KEYS` 加 `WEB_PUBLIC_BASE_URL`；`compare_mcp_http_p0.py` + 基线 + `ci_gate.sh`。

### Step 11：收尾
规格 §3 新增 3.10 Streamable HTTP 与 OAuth、§5 S8 行与 §7 S8 验收翻成 delivered、§2 F 表补本步实测事实（若有）；P4 规格 §2–§3 的"不做 HTTP / 不做 OAuth"两条改为指向本步；威胁模型新增 **T13**（远程 MCP 出口与服务级令牌：SSRF / DNS 重绑定 / 令牌窃取 / 授权服务器混淆 / 服务级授权等于所有用户共用身份）；`docs/design/p7-step-8-decision.md`；AGENTS.md 补一条并更新 P7 交付状态（S0–S9 全部交付）。

## 5. 范围块

<!-- step-scope -->
```json
{
  "step": "P7-S8",
  "allow": [
    "src/water_agent/mcp/config.py",
    "src/water_agent/mcp/http_transport.py",
    "src/water_agent/mcp/http_client.py",
    "src/water_agent/mcp/oauth.py",
    "src/water_agent/mcp/oauth_store.py",
    "src/water_agent/mcp/bridge.py",
    "src/water_agent/mcp/pool.py",
    "src/water_agent/mcp/wiring.py",
    "src/water_agent/mcp/errors.py",
    "src/water_agent/mcp/importer.py",
    "src/water_agent/mcp/web_layer.py",
    "src/water_agent/mcp/credentials.py",
    "src/water_agent/mcp/__init__.py",
    "src/water_agent/runtime/governance/contracts.py",
    "src/water_agent/runtime/governance/inventory.py",
    "src/water_agent/runtime/governance/config.py",
    "src/water_agent/runtime/agentic.py",
    "src/water_agent/harness/runner.py",
    "src/water_agent/harness/mcp_http_eval.py",
    "src/water_agent/harness/tool_assembly_snapshot.py",
    "src/water_agent/webapp/mcp_admin_routes.py",
    "src/water_agent/webapp/run_manager.py",
    "src/water_agent/webapp/app.py",
    "src/water_agent/webapp/auth/audit.py",
    "scripts/compare_mcp_http_p0.py",
    "scripts/ci_gate.sh",
    "compose.yml",
    "data/mcp_servers.example.json",
    "data/eval/mcp_http_p0.jsonl",
    "data/eval/mcp_http_p0_main.json",
    "data/eval/tool_assembly_main.json",
    "web/app/src/**",
    "web/app/tests/**",
    "tests/fixtures/fake_mcp_http_server.py",
    "tests/fixtures/tls/**",
    "tests/fixtures/mcp_import/**",
    "tests/test_mcp_http_config.py",
    "tests/test_mcp_http_transport.py",
    "tests/test_mcp_http_client.py",
    "tests/test_mcp_oauth.py",
    "tests/test_web_mcp_oauth.py",
    "tests/test_mcp_http_eval.py",
    "tests/test_mcp_pool.py",
    "tests/test_web_mcp_pool.py",
    "tests/test_mcp_import.py",
    "tests/test_mcp_web_layer.py",
    "tests/test_web_mcp_admin.py",
    "tests/test_mcp_gateway.py",
    "tests/test_mcp_layered_config.py",
    "tests/test_mcp_wiring.py",
    "tests/test_mcp_credentials.py",
    "tests/test_governance_contracts.py",
    "tests/test_dxf_upload.py",
    "tests/test_vision_review.py",
    "tests/test_provider_gateway.py",
    "tests/test_web_deployment.py",
    "tests/test_p7_known_figures.py",
    "tests/test_tool_assembly_snapshot.py",
    "docs/design/p7-step-8-decision.md",
    "docs/design/16-p7-plugin-mcp.md",
    "docs/design/14-p4-mcp-gateway.md",
    "docs/design/p2.5-threat-model.md",
    "docs/design/p2.5-provider-inventory.md",
    "docs/execution-prompts/P7-S8-streamable-http.md",
    "AGENTS.md"
  ],
  "forbid": [
    "src/water_agent/mcp/client.py",
    "src/water_agent/mcp/protocol.py",
    "src/water_agent/mcp/schema.py",
    "src/water_agent/plugins/**",
    "src/water_agent/graphs/**",
    "src/water_agent/prompting/**",
    "src/water_agent/skills/**",
    "src/water_agent/tools/**",
    "src/water_agent/runtime/governance/pipeline.py",
    "src/water_agent/runtime/governance/rules.py",
    "src/water_agent/runtime/governance/gateway.py",
    "src/water_agent/multi_agent/**",
    "src/water_agent/storage/**",
    "alembic/**",
    "alembic.ini",
    "pyproject.toml",
    "uv.lock",
    ".github/**",
    "compose.production.yml",
    "Dockerfile",
    "tests/conftest.py",
    "tests/test_p7_traceability.py",
    "tests/test_check_step_scope.py",
    "tests/test_import_guard.py",
    "tests/test_tool_policy.py",
    "tests/test_confirmation_grant.py",
    "tests/test_confirmation_gate.py",
    "tests/test_web_confirmation.py",
    "tests/test_tool_exposure_eval.py",
    "tests/test_skill_eval.py",
    "data/eval/baseline.json",
    "data/eval/tool_exposure_p0.jsonl",
    "data/eval/tool_exposure_p0_main.json",
    "data/eval/skill_p0.jsonl",
    "data/eval/skill_p0_main.json",
    "data/eval/drawing_p0_main.json",
    "data/eval/drawing_gen_p0_main.json",
    "data/eval/memory_p0_main.json",
    "data/eval/multi_agent_p0_main.json",
    "data/mcp_servers.json"
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
      "node": "tests/test_governance_contracts.py::TestLocalPrincipalNaming::test_dangerous_capability_cannot_be_granted",
      "contains": "Principal.local(\"t\", capabilities=frozenset({capability}))"
    },
    {
      "node": "tests/test_governance_contracts.py::TestLocalPrincipalNaming::test_default_local_capabilities_exclude_dangerous",
      "contains": "assert not (DEFAULT_LOCAL_CAPABILITIES & DANGEROUS_CAPABILITIES)"
    },
    {
      "node": "tests/test_governance_contracts.py::TestInventoryCoverage::test_no_ungoverned_remote_egress",
      "contains": "assert missing == frozenset()"
    },
    {
      "node": "tests/test_tool_policy.py::TestCapabilityChecks::test_principal_cannot_be_granted_dangerous_capability",
      "contains": "Principal.local(\"x\", capabilities=frozenset({capability}))"
    },
    {
      "node": "tests/test_tool_policy.py::TestConfirmationGate::test_confirmation_required_tool_blocked_at_call_time",
      "contains": "assert blocked.decision.rule_id == \"tool.confirmation_required\""
    },
    {
      "node": "tests/test_p7_known_figures.py::test_local_tool_schema_headroom_is_three_characters",
      "contains": "assert surface[\"schema_chars_total\"] == 4497"
    },
    {
      "node": "tests/test_mcp_layered_config.py::test_overlay_cannot_widen_the_gate",
      "contains": "assert exc.value.code == \"MCP_CONFIG_INVALID\", bad"
    },
    {
      "node": "tests/test_mcp_web_layer.py::test_web_layer_can_only_toggle_and_allow",
      "contains": "assert alpha.command == sys.executable, \"命令只来自运维的两层\""
    }
  ],
  "frozen_json": [
    {
      "path": "data/eval/tool_assembly_main.json",
      "pointers": [
        "/versions/PROMPT_TEMPLATE_VERSION",
        "/versions/TOOL_POLICY_VERSION",
        "/profiles/session_[!h]*",
        "/profiles/runner_*"
      ]
    }
  ]
}
```
<!-- /step-scope -->

> `frozen_json` 说明：现有 16 个 profile（11 个 `session_*` + 5 个 `runner_*`）**整段**冻结；本步新增的 `session_http_mcp_fake` 以 `session_h` 开头，不被 `session_[!h]*` 匹配（现有会话侧 profile 没有以 `session_h` 开头的——写 Prompt 时已用范围守卫的展开函数核对过：`session_[!h]*` 匹配 11 个、`runner_*` 匹配 5 个）。只有 `/versions/MCP_CONFIG_VERSION` 允许变化。

## 6. 受保护断言

- 预算：`assert cost < 4500`、`schema_chars_total == 4497`（5 个本地工具；HTTP 工具与 stdio MCP 工具同为 ambient 组，不进这个 profile）、系统提示 `< 1950`。
- 信任：`INSTRUCTION_BEARING_SOURCES == {SYSTEM_POLICY}`。
- 危险能力：`DANGEROUS_CAPABILITIES` 不变、`Principal` 构造期拒绝、护栏层拒绝（两层测试原样通过）；`DEFAULT_LOCAL_CAPABILITIES` 不含危险能力。
- 出站扫描：`test_no_ungoverned_remote_egress` 断言不变（新模块必须登记，而不是放宽断言）。
- S1 / S3 / S6 / S7：确认门无凭证拒绝；覆盖层不能写 `confirmation_handler_ready` 或未知键；Web 管理层可写键不变、命令只来自运维两层。
- **会变的锁定数字**（本步唯一允许改的）：`tests/test_governance_contracts.py::TestInventoryCoverage::test_all_provider_sites_governed_after_step12` 的 `"provider_call_sites": 7` / `"provider_governed": 7` → 8（docstring 记一行原因：P7-S8 新增 MCP 远程出口）；`tests/test_dxf_upload.py::test_tool_schema_budget_and_governance_counts_are_untouched` 同一组数字同样 7 → 8（`tools` / `tools_guardrail_validated` 仍是 6）；`tests/test_vision_review.py::test_vision_call_site_is_registered_and_governed` 的 `len(PROVIDER_CALL_SITES) == 7` 是同一个 7 → 8（写 Prompt 时漏列，S8 全量门禁实测发现，见 `p7-step-8-decision.md` D13）；`tests/test_mcp_pool.py` 的 `MCP_CONFIG_VERSION` 字面量；`test_scan_actually_finds_known_egress` 的已知出口列表加 `water_agent/mcp/http_transport.py`。**任务书与 S1–S9 执行 Prompt 的 `protect` 里锁着 `"provider_call_sites": 7`**——它们不在本步范围内，必须在**本 Prompt 的文档 PR 里**一并改掉（见 §12），否则 S8 实现合入后 `test_repo_scope_blocks_are_usable` 在 main 上变红（S9 实测踩过同类问题）。
- `MCP_ERROR_CODES` 只增不减（本步新增 5 个：`MCP_DESTINATION_FORBIDDEN`、`MCP_PROTOCOL_UNSUPPORTED`、`MCP_OAUTH_REQUIRED`、`MCP_OAUTH_UNAVAILABLE`、`MCP_OAUTH_INSUFFICIENT_SCOPE`）。

**规范逐条（OAuth 客户端 MUST 项，每条一个断言，A06–A08）**：

1. 401 时优先用 `WWW-Authenticate` 的 `resource_metadata`，没有则按顺序试路径插入与根路径两个 well-known；
2. 授权服务器元数据按规范顺序尝试 RFC 8414 与 OIDC（有 path 的 issuer 三个、无 path 的两个）；
3. `code_challenge_methods_supported` 缺失 → 拒绝继续；必须用 `S256`；
4. 授权请求与令牌请求都带 `resource`，值是规范 URI（小写 scheme / host、无 fragment、无尾斜杠），且授权服务器不支持时也照发；
5. `state` 每次新生成、一次性、不匹配即丢弃结果；回调带 `iss` 时必须等于记录的 issuer；
6. 令牌只经 `Authorization: Bearer` 头、永不进查询串；只发给签发它的 issuer 对应的那台 server；
7. 所有授权服务器端点必须 `https://`；回调地址是 `https://` 或 localhost；
8. 持久化的客户端注册信息按 issuer 存，issuer 变化即作废、不复用；
9. refresh token 被轮换时替换旧值；
10. 403 `insufficient_scope` 解析 `scope` 并如实报出，不自动升级。

## 7. 停止条件

满足任一条就停下，写清现象后等确认：

- 现有 16 个 profile 有任何变化；
- 需要改 `mcp/client.py`（stdio 客户端）/ `mcp/protocol.py` / `mcp/schema.py` / `plugins/` / `graphs/` / `prompting/` / `tools/` / 治理层 `pipeline.py` / `rules.py` / `gateway.py`，或需要数据库迁移；
- 需要新依赖（`pyproject.toml` / `uv.lock` 在 forbid 里）；
- 需要把 `NETWORK_FETCH` 授予任何主体或移出危险集；
- 需要放宽任何一条受保护断言；
- 规范原文与本 Prompt §0 的做法冲突（以规范为准，但先停下说明）；
- `tool_exposure_p0` / `skill_p0` 的任何用例回退；
- 门禁出现与本步无关的红灯。

## 8. 验证命令（完成前全部跑过，贴出输出）

```bash
export VENV=/Users/yaerken/Desktop/给排水agent/_venv PYTHONPATH=src
unset WATER_AGENT_MCP_OVERLAY WATER_AGENT_MCP_CONFIG WATER_AGENT_MCP_WEB_LAYER WATER_AGENT_TOOL_EXPOSURE WATER_AGENT_SKILLS WATER_AGENT_SKILLS_DIR WEB_PUBLIC_BASE_URL
git fetch origin main
$VENV/bin/pytest -q tests/test_mcp_http_config.py tests/test_mcp_http_transport.py tests/test_mcp_http_client.py tests/test_mcp_oauth.py tests/test_web_mcp_oauth.py tests/test_mcp_http_eval.py tests/test_mcp_pool.py tests/test_web_mcp_pool.py tests/test_mcp_import.py tests/test_mcp_web_layer.py tests/test_web_mcp_admin.py tests/test_mcp_gateway.py tests/test_mcp_behaviors.py tests/test_mcp_layered_config.py tests/test_mcp_wiring.py tests/test_governance_contracts.py tests/test_provider_gateway.py tests/test_tool_policy.py tests/test_dxf_upload.py tests/test_web_deployment.py tests/test_p7_known_figures.py tests/test_tool_assembly_snapshot.py tests/test_p7_traceability.py tests/test_check_step_scope.py
$VENV/bin/python -m water_agent.harness.mcp_http_eval data/eval/mcp_http_p0.jsonl --report-path data/eval/reports/mcp_http_p0_head.json
$VENV/bin/python scripts/compare_mcp_http_p0.py --head data/eval/reports/mcp_http_p0_head.json --base data/eval/mcp_http_p0_main.json
$VENV/bin/python -m water_agent.harness.tool_exposure_eval data/eval/tool_exposure_p0.jsonl --report-path data/eval/reports/tool_exposure_p0_head.json
$VENV/bin/python scripts/compare_tool_exposure_p0.py --head data/eval/reports/tool_exposure_p0_head.json --base data/eval/tool_exposure_p0_main.json
$VENV/bin/python -m water_agent.harness.skill_eval data/eval/skill_p0.jsonl --report-path data/eval/reports/skill_p0_head.json
$VENV/bin/python scripts/compare_skill_p0.py --head data/eval/reports/skill_p0_head.json --base data/eval/skill_p0_main.json
(cd web/app && npm test && npm run build)
$VENV/bin/python -m water_agent.harness.tool_assembly_snapshot --check
$VENV/bin/python scripts/check_step_scope.py docs/execution-prompts/P7-S8-streamable-http.md --base origin/main
docker compose -f compose.yml -f compose.production.yml config -q
COMPOSE_PROJECT_NAME=p7s8 VENV=$VENV bash scripts/ci_gate.sh
```

本机 Docker daemon 没起时 `tests/test_web_deployment_overlay.py::test_image_check_skips_structurally_instead_of_failing` 必然失败（main 上同样），先 `open -a Docker` 再跑门禁；仍失败照贴并说明。

手动验收（有条件时做，没有就在决策记录里写明未做）：本机 Web 栈里，覆盖层配一台假 HTTP server（`allow_private_network: true` + 本机地址）→ 面板显示传输方式与主机名 → 授权（OAuth 模式下点"授权"跳到假授权服务器、回调成功、显示已授权）→ 放行一个工具 → 发问题看到工具卡片 → 撤销授权后再问，工具记 `MCP_OAUTH_REQUIRED`。

## 9. 完成定义

- 满足规格 §8 第 5 条的全部项；
- PR 上的 `p7-scope` 与 `p5-ci` 通过；
- 决策记录贴出：出站清单 7 → 8 的登记与扫描反证、SSRF 每类地址的拒绝结果、OAuth 规范逐条（§6）的测试名、`mcp_http_p0` 指标、令牌泄漏扫描结果、快照 `--diff` 原文。

## 10. 汇报格式

- 改了哪些文件；
- 验证命令与实际输出（失败照贴）；
- 范围守卫的输出；
- 快照差异；
- 未验证项（至少：真实 HTTP MCP server、真实授权服务器、TLS 证书链与企业代理、浏览器授权跳转——无条件即 blocked；手动验收是否做了）；
- 残余风险（至少：服务级授权让所有用户共用同一身份、运维写错 URL 就是数据外发、`allow_private_network` 打开后内网可达、DNS 重绑定只靠连接钉死 IP 防护、令牌存在凭据卷且卷被盗即令牌泄露、远端工具可能有副作用而网络失败时状态未知）。

## 11. 常见坑（本仓库实测过）

- worktree 里不设 `PYTHONPATH=src` 时 pytest 被 H0 拦下（exit 4）；web/app 新 worktree 先 `npm ci`（走代理）再 `npm run build`。
- 本机设了 `WATER_AGENT_MCP_*`、`WATER_AGENT_TOOL_EXPOSURE`、`WATER_AGENT_SKILLS*`、`WEB_PUBLIC_BASE_URL` 时先 unset 再跑测试和快照；测试里用 `monkeypatch.setenv`，**不要**直接写 `os.environ`（S9 实测：泄漏的变量让后面的快照测试全红）。
- 测试替身必须**乱序**应答，否则掩盖串行缺陷（S5 F17）。
- 运行时 MCP 绑定登记只增不删，测试之间用 `clear_mcp_tool_bindings`。
- 本机 venv **没有** `cryptography`；TLS 测试证书用 `openssl` 子进程生成，或入库一对标注"测试私钥"的证书。不要为了测试往 `pyproject.toml` 加依赖（在 forbid 里）。
- `http.client` 默认会读系统代理吗？不会——但 `urllib.request` 会读 `HTTPS_PROXY`。本机常设 `HTTPS_PROXY=http://127.0.0.1:7892`，测试与评测里必须 unset 或确认不经 `urllib`，否则连本地假 server 会绕到代理上。
- 零宽字符只能用 `\uXXXX` 转义；JSON 请求体最坏每字符 12 字节（`tests/test_upload_body_limit.py`）——OAuth 回调是 GET、启动授权是无体 POST，不要给 API 加长文本字段。
- 提交前单独确认测试退出码为 0，不要把 `git commit` 串在测试命令后面。
- 只含执行 Prompt 的文档 PR，分支名不能以 `YEK/p7-s<n>-` 开头。

## 12. 本 Prompt 文档 PR 里一并改的文件（不属于实现范围）

- `docs/design/16-p7-plugin-mcp.md`：§5 S8 行改写、§5 S8 约束段落吸收本 Prompt §0、§7 新增 P7-S8-A01…A15（planned）、§10 索引补本 Prompt。
- `docs/给排水agentP7插件化与MCP扩展任务书.md`：S8 行补执行 Prompt 路径与关键门槛。
- 任务书与 `docs/execution-prompts/P7-S1…S7, S9-*.md` 共 9 份文档的 `protect` 里 `"provider_call_sites": 7` 那一条：**删除**（同一节点的覆盖率由 S8 本步的"会变的锁定数字"说明；`test_no_ungoverned_remote_egress` 与本 Prompt 的 protect 继续守出站扫描）。删除前后都要用范围守卫的 `check_protected` 在 main 代码上核对 0 违规。
