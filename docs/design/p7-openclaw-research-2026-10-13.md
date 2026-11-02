# OpenClaw 相关能力调研记录（联网搜索 / Skill 安装 / MCP 与插件信任）

日期：2026-10-13 · 用途：为 `docs/design/17-p7-external-capabilities.md`（SDD）与
`docs/design/17-p7-external-capabilities-harness.md`（评测 harness）提供外部参照。**这是二手调研，不是规格**：
规格里每一条"借鉴"都已改写成我们自己的约束并配了验收，不以本文为准。

## 0. 方法与局限（先读这一节）

- **对象**：公开仓库 `openclaw/openclaw`（MIT，2026-10-13 的 `main`，约 9 GB，另有独立的 ClawHub 服务端不在该仓库内）。
- **方法**：只用 `gh api repos/openclaw/openclaw/contents/<path> | base64 -d` 读单个文件，三个只读子代理分别负责
  ① 联网搜索与抓取 ② Skill 与 ClawHub ③ MCP 与插件信任。**没有克隆仓库（克隆被权限拦下，未绕过），没有写磁盘，没有执行其中任何代码。**
  `WebSearch` 对当前模型不可用、`WebFetch` 对 github.com 被拒；官方 MCP 注册表 API 的探测同样被权限拦下，所以其形态**未验证**。
- **读取深度不一**：测试文件多数只读了用例名，没读用例体（子代理在报告里标了"标题级"）；"未见某某"类结论只在已读范围内成立。
- **版权**：只借鉴设计思想，不复制代码；本仓库不引入其任何代码或依赖。
- **没复核**：下文的常量、阈值、文件路径来自子代理报告，我只抽查了与我们自己代码对接的部分。要把任何一条写进规格验收之前，必须在对应步骤的 Step 0 重新读原文。

## 1. 联网搜索与抓取

OpenClaw 的做法（路径相对其仓库根）：

| 面 | 做法 |
|---|---|
| 工具面 | 两个工具：`web_search(query, count≤10, freshness, 日期范围…)` 与 `web_fetch(url, extractMode, maxChars)`；都声明结果来源为 `network` 并带闭合 `outputSchema`。search 的输出是 `error / results / answer / raw` 四分支 union。 |
| provider | `src/plugins/web-provider-types.ts` 的插件接口；search 与 fetch 的 provider 分开；显式配置的 provider 失败**不回退**；未配置时只在"已有凭据"的 provider 里按顺序自动选；**无密钥 provider 永不自动选中**（避免查询被静默外送）；provider id 拼错在配置校验期失败；没有可用 provider 时工具不进工具列表。 |
| 不可信标记 | `src/security/external-content.ts`：随机 id 的 `<<<EXTERNAL_UNTRUSTED_CONTENT id=…>>>` 信封；正文里伪造的边界（全角、零宽拆分、超长 id、JSON 转义变体）统一替换；聊天模板特殊 token 替换；搜索输出先剥 provider 自带信封，再在核心边界逐字段统一包裹；URL 重新解析、长度与日期格式校验。 |
| 污点 | 网络工具结果令本回合"被污染"，之后 assistant 消息与记忆写入的来源记为不可信；工具异常文本被截断并去掉 cause。 |
| SSRF | 唯一收口 `fetchWithSsrFGuard`：主机名黑名单 + 全部 DNS 应答逐个分类 + 钉死已校验 IP + 手动跟随重定向（每跳复检、3 跳上限、环检测）+ 跨源重定向丢敏感头 + 非常规 IPv4 写法拒绝；每次出站前复核调用方授权仍有效。 |
| 失败 | 搜索失败给固定模板（状态码 + 提示），不含上游正文与密钥；授权类失败不回退；取消优先于回退。 |
| 成本 | 只有 count≤10、15 分钟进程内缓存、超时、字节上限、"连续 3 次相同错误终止回合"。**没有限流、预算、熔断**，缓存不分主体。 |
| 测试 | 伪造边界 / 模板 token / 零宽字符、闭合 schema、结果与总量预算、DNS 解析到内网、重定向到回环、审计日志不含 path / query、跨源重定向剥头、DNS rebinding 等（多数是用例名级阅读）。**没有对抗式注入评测**，QA 场景只验证调用与结果形状。 |

对我们的含义：

- **借**：单一出站收口；出站前复核授权；核心边界统一剥壳再包壳；固定模板错误；无密钥 provider 不自动选；配置拼写错误装配期失败；AST 静态扫描 + 登记；隐藏内容与不可见 Unicode 剥离。
- **不借**：把 `NETWORK_FETCH` 做成"默认关 + 分层放行"（我们要它在 Principal 里无法授予）；缓存不分主体；无限流预算熔断；只靠提示词层的标记（我们要结构化来源标签 + 确定性强制）；LLM 合成答案类 provider（`kind:"answer"`）与托管原生搜索；第三方抓取兜底（把 URL 外送并绕过反爬）；伪装浏览器 UA；DuckDuckGo 抓 HTML；`raw` 透传。
- **它的缺口，我们要补**：不可见 Unicode 只在 HTML 路径剥离，markdown / JSON / 搜索摘要路径没有；注入检测正则只用于监控；没有 robots / ToS 处理；静态扫描按 `file:line` 登记且只抓 `fetch(`。

## 2. Skill 发现、安装与 ClawHub

| 面 | 做法 |
|---|---|
| 格式 | `<root>/**/SKILL.md`（最深 6 层），必填 `name` / `description`，frontmatter **无白名单**（未知键保留不执行）；门控写在 `metadata.openclaw.{requires, os, install[]…}`；允许 `scripts/ assets/ …` 并由模型经 exec 执行。 |
| 优先级 | 工作区 > 用户 > 状态目录（ClawHub 落点）> workshop（模型自学）> 内置 > 额外目录；同名高者胜，仅记日志。 |
| 锁文件 | `.clawhub/lock.json` + 每个 skill 的 `origin.json`（registry、版本、制品 sha256、`fileTreeSha256`、verification 快照），二者必须互相一致，单侧被改即无效；GitHub 来源必须是 40 位 commit SHA。 |
| 安装顺序 | 解析确切版本 → **下载前**取安全判定并核对回显的 slug / 发布者 / 版本（缺失、不一致、不可达都失败关闭）→ 下载（≤256 MiB）→ 比对"同意时绑定的字节摘要" → 受限解压（拦 zip-slip / 符号链接）→ 必须含 SKILL.md → 过外部 `installPolicy`（allow / warn / block，异常一律 block，warn 需 TTY 键入名字确认）→ 暂存、备份、原子替换、失败回滚。 |
| 注入到提示 | prompt 里只有 `<available_skills>` 目录；正文由模型按需 `read`，是**工具结果**（权威低）。 |
| 威胁模型 | 自认没有 skill 级沙箱，恶意 skill 与更新投毒残余风险 High。 |

对我们的含义：

- **借**：下载前失败关闭 + 响应身份回显核对；"批准绑定字节摘要"；暂存→备份→原子替换→回滚；TTY 键入名字确认；锁文件 + 来源文件互校；内置 skill 在 CI 里过同一扫描器；`skill_changed` 式审计。
- **不借**：脚本 / `install[]` / 命令分发；**加载期不重算哈希**（哈希只在 update / uninstall 用）；"可疑 / 待扫描"状态只警告仍可装；官方发布者免检；自动安装通道没有确认；`update --all` 与 `latest`；热加载；未扫描源；把 registry 侧的扫描或签名当安全边界；workspace 同名覆盖内置（我们反过来：同名即拒）。
- **关键差异**：我们的 skill 正文是 `SYSTEM_POLICY`（权威远高于它的工具结果），所以**正则安检不是安全边界，真正的控制只能是"人审 + 哈希钉死 + 加载期重算"**。

## 3. MCP 与插件信任

**关键澄清**：OpenClaw **没有**"远程 MCP 注册表搜索 / 安装"。`openclaw mcp *` 只管本地 `mcp.servers`；`mcporter` 是经 exec 调用的外部 CLI；`src/mcp/*` 是它自己**作为** MCP 服务端暴露工具。最接近的是 ClawHub 插件安装（manifest 可带 `mcpServers`）与实验性的 `claws add --dry-run`。**所以我们的 MCP 发现没有现成参照，设计是我们自己的。**

| 面 | 做法 |
|---|---|
| 配置 | 传输 stdio / sse / streamable-http；schema 透传未知键；admin 可经 CLI 或控制台**直接新增 stdio command**；`toolFilter` include / exclude、`tools.deny` 整服剔除；工具名 `<server>__<tool>`（≤64、重名整体剔除）；描述只做 scrub（替换"ignore previous instructions"并截断 1200 字），**不拒绝**；子进程环境过滤 `NODE_OPTIONS` / `DYLD_*` / `LD_*`。 |
| 安装与信任 | 来源分级（内置 > 官方 > ClawHub > 任意 npm / git / 本地）；npm 仅精确版本 + `--ignore-scripts`；ClawHub 社区包先查 trust（不可用即失败关闭并核对回显）；`installPolicy` 外部策略程序 allow / warn / block；**能力同意令牌**：`reviewToken = sha256(声明面)`，让出后重读 staged 工件比对，仅"变宽"才再批——但 `mcpServers` 只哈希了名字，换 command / url 不触发。 |
| 审批 | `exec` 有 deny / allowlist / ask / auto / full 与"批准绑 cwd + argv + 可执行文件身份"；MCP 工具无风险分类，只有 Codex 路径逐次审批，且信任服务器自报的 `readOnlyHint`（可伪造）；"Allow Always"与参数无关。 |
| 其他 | `plugins` 工具的参数里自带 `reviewToken` / 确认字段（审批经模型参数携带）；`list_changed` 只使缓存失效，不触发再批。 |

对我们的含义：

- **借**：`--dry-run → planIntegrity → 应用时重建计划，漂移即拒`；同意令牌绑实际字节 + 让出后重验 + 仅变宽再批（**但摘要必须含完整服务器定义**）；严格清单作"导入 → 禁用提案"的 schema；环境变量阻断名单；URL 变更清凭据；注册表读不了必须告警；写入前复核授权仍有效；来源分类 `{kind, authority, mutable, network}`，非完整 commit 的 git ref 视为可变。
- **不借**：审批经模型参数携带 / admin 自动同意；声明面只含名字；CLI / 面板新增 stdio command；`sslVerify:false` 与"配置的 origin 豁免私网"这类逃生口；信任服务器自报 annotations；`npx -y` 运行时拉取；审批通道做成 MCP 工具；未知键透传；与参数无关的 Allow Always；描述只 scrub 不拒绝。

## 4. 汇总：我们的现有设计已经比它严的地方（不要为"对齐"而放宽）

| 项 | 我们 | OpenClaw |
|---|---|---|
| 网络能力 | `NETWORK_FETCH` 构造期即拒绝授予 | 默认关，配置可开 |
| 出站治理 | 全部经 `ProviderGateway`（限流 / 预算 / 熔断 / 对账）+ 静态扫描要求登记 | 无限流预算熔断，缓存不分主体 |
| MCP command | 只能运维写在部署覆盖层，面板永远写不了 | 控制台可直接新增 stdio |
| 工具描述 | 命中注入即拒绝暴露 | 只 scrub |
| 危险工具 | 声明式 `human_confirmation` + 一次性执行凭证 | 无风险分类（Codex 路径信自报） |
| skill | 严格 frontmatter 白名单、只允许文本、两级来源、同名内置优先 | 无白名单、可带脚本、工作区覆盖内置 |

## 5. 未读 / 未验证

- ClawHub 服务端（不在该仓库）：发布者门槛、扫描管线、徽记，仅来自客户端代码与威胁模型文档的转述。
- 官方 MCP 注册表（`registry.modelcontextprotocol.io`）的 API 形态：**未验证**（探测被拦）。
- OpenClaw 各测试文件的用例体；它的 QA 场景与评测只看到了一个 `web-fetch.yaml`。
- 任何真实搜索服务（Brave / SearXNG / 其他）在本机网络下的可用性与数据出境合规。
