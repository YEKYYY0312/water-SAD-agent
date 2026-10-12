# P7-S6 执行 Prompt：Web 扩展面板（Step E1）

> **工作区**：仓库 `YEKYYY0312/water-SAD-agent`，基于最新 `main`（必须已包含 P7-S7 与本 Prompt）。新建分支 `YEK/p7-s6-mcp-admin-panel`。
> **必读**：
> - `AGENTS.md`
> - `docs/design/16-p7-plugin-mcp.md`：§1 非目标（"Web 不能写 `command` / `args`"）、§3.4 MCP 来源与连接池、§3.7 确认门、§5 S6 约束、§7 的 P7-S6 验收、§8 实施协议
> - `docs/给排水agentP7插件化与MCP扩展任务书.md` §3（dsh 的面板与本项目的取舍）、§4 S6 行
> - `docs/design/14-p4-mcp-gateway.md` §3（配置契约、两层合并）
> - `docs/design/p7-step-3-decision.md`（分层配置）、`docs/design/p7-step-5-decision.md`（连接池与指纹）、`docs/design/p7-step-7-decision.md`（确认门）
> - 先例：`src/water_agent/webapp/settings_routes.py` + `src/water_agent/config/credentials.py`（凭据只写不读、admin + CSRF、卷不可写 503）、`src/water_agent/mcp/config.py::load_layered_gateway_config`（覆盖层合并）、`src/water_agent/webapp/auth/audit.py`（结构化审计）

---

## 0. 相对规格的修订与本步决定（以本文为准）

规格只写了"面板只能改启停 / 白名单 / 凭据引用；导入 `mcpServers` 只生成禁用的待审提案，由运维离线应用"。下面是 2026-10-07 读码后定下的做法，规格已在本 Prompt 的同一个 PR 里同步：

| 疑问 | 本步做法 | 原因 |
|---|---|---|
| 面板的改动写到哪里 | 第三层配置 **Web 管理层**：`WATER_AGENT_MCP_WEB_LAYER` 指向的 JSON 文件，合并顺序 仓库默认层 → 部署覆盖层 → Web 管理层；`load_layered_gateway_config` 读它，所有设了该变量的进程都生效。compose 给 app 新挂卷 `app_mcp_admin:/var/lib/water-agent/mcp-admin` 并设该变量 | 不改部署覆盖层：那是运维的文件（可能只读挂载、含 `command`），面板写它就把"运维的配置"和"管理员的操作"混在一起。不进数据库：不引入迁移，CLI / 评测 runner 不必连库也能得到同一份有效配置。卷挂在 `/var/lib/water-agent` 之下，`scripts/backup.sh` 原样就会打包它（层里没有密钥值） |
| Web 管理层能写什么 | 只有 `server_id`、`disabled`、`allow`（整体替换，与覆盖层同规则）。不能新增 server（只能改下面两层已定义的），不能写其他任何字段（`command` / `args` / `env` / `env_file` / `cwd` / `lifecycle` / `human_confirmation` / 超时）或顶层键（含 `confirmation_handler_ready`） | 任务书 §2.2 非目标；`human_confirmation` 是运维定的安全属性，面板既不能关也不扩大到别的语义 |
| 面板能不能放宽 `allow` | **能**：面板就是管理员逐个批准远端工具的地方（`MCP_NOT_IN_ALLOWLIST` 的原文就是"新工具需人工放行"）。放行之后的工具照样过 schema 子集、描述安检、确认门、`MCP_INVOKE` 与护栏 | 只许收窄的面板没有用处；放宽不绕过任何一道装配期 / 执行期检查 |
| 面板能不能启用运维停用的 server | **能**。要彻底下线一台 server，运维把它从覆盖层删掉——面板不能新增 server | "按实例启停"是面板的本职；能执行什么命令仍只由运维决定 |
| 已从下层删掉的 server 在 Web 管理层里还有条目 | 加载时**忽略**这类孤立条目（不报错），面板的列表接口如实列出并提供清理 | 孤立条目只能停用 / 放行一台不存在的 server，没有效果；为它让整个 MCP 配置响亮失败，等于运维删一台 server 就把 Web 的 MCP 全停了 |
| Web 管理层文件损坏 | 与覆盖层同语义：`MCP_CONFIG_INVALID` 响亮失败；面板的读接口在配置损坏时照常返回（带错误码），并提供"恢复为部署配置"（删除整个 Web 管理层） | 坏配置不能静默跳过；但面板必须仍能把自己写坏的东西撤回 |
| "凭据引用"是什么 | 运维在覆盖层给 server 声明**凭据槽位** `credential_env: ["CAD_API_TOKEN", ...]`（环境变量名）；面板只能给已声明的槽位写值（只写不读，与 LLM 凭据同一套：返回 `configured / updated_at`，不回显），值存在凭据卷 `<credentials_dir>/mcp/<server_id>/<ENV_NAME>`（0600），连接时注入子进程 env | 让浏览器决定环境变量**名**就等于让它执行代码（`LD_PRELOAD`、`NODE_OPTIONS`、`PYTHONSTARTUP`……）。名字由运维声明、值由管理员填，密钥不进任何配置文件、不进备份 |
| 槽位名的约束 | 加载期校验：大写环境变量名；不与同一 server 的内联 `env` 重名；拒绝加载器 / 解释器会执行的变量（`PATH`、`HOME`、`BASH_ENV`、`ENV`、`JAVA_TOOL_OPTIONS`，以及前缀 `LD_`、`DYLD_`、`PYTHON`、`NODE_`、`PERL`、`RUBY`）。注入顺序：`env_file` < 凭据槽位 < 内联 `env`（重名在加载期已拒绝） | 值来自浏览器：即便运维误把这类变量声明成槽位，也不能让面板借它执行代码 |
| 槽位没填值 | 该 server 连接前跳过，新错误码 `MCP_CREDENTIAL_MISSING`（不 spawn），列表接口如实显示 | 不带 token 启动的 server 会以各种不可诊断的方式失败 |
| 导入 `mcpServers` | 无状态的预览接口：接受 Claude Code / Cursor 的 JSON（`{"mcpServers": {...}}`）与 Codex 的 TOML（`[mcp_servers.<name>]`，标准库 `tomllib`），产出**覆盖层格式**的提案：每台 `disabled: true`、不写 `allow`（即全拒）、`env` 的**值一律丢弃**、其键变成 `credential_env` 槽位；`url` / 非 stdio 条目标为不支持（S8）；名字归一成合法 `server_id`、与现有 id 冲突的标出。逐条用 `MCPServerConfig` 做模型层校验（不查文件系统），不合格的逐条给原因。**不落盘、不 spawn、不应用**；前端提供复制 / 下载，运维离线贴进覆盖层 | 任务书："导入只生成禁用的待审提案，由运维离线应用"。导入文件里的 `env` 常含真实密钥，提案里留着就等于把密钥写进运维会到处粘贴的文本 |
| 管理员怎么知道一台 server 有哪些工具 | 探测接口：对**启用的** server 经 Web 会话工厂的连接池借一次租约（`confirmation_ready=True`，与 Web run 同语义）、读暴露 / 跳过结果、立即释放；只返回工具名与跳过码（远端描述原文不返回）；同一台 server 同时只允许一个探测 | 不另写连接代码（复用池与桥接）；停用的 server 不探测——停用常常就是因为它跑起来有问题 |
| 读接口返回什么 | 每台 server：`server_id`、每个字段来自哪一层、`disabled`、`allow`、`human_confirmation`、`lifecycle`、`command` 的文件名（不返回完整路径与 `args`）、凭据槽位状态、最近一次探测结果、孤立条目、整体配置错误码、Web 管理层的 `revision`。不返回 `env` 的值、`env_file` 内容、凭据值 | `args` 里常有运维写进去的 token；面板不能改它们，也就不必展示 |
| 并发与生效时机 | 写接口带 `expected_revision`（乐观并发，不符 409）；写入时持文件锁（`fcntl.flock`）、先整份合并并完整校验（含路径存在性，与加载同一函数）、通过才原子替换（同目录临时文件 + `os.replace`），否则 422 且不落盘。**下一个 run 生效**：Web 每个 run 都重新 `load_mcp_gateway()`，连接池按单台 server 指纹（已含 `allow` 与子进程 env 摘要）自动换新进程；正在跑的 run 不受影响 | 与 S5 的重载语义一致，不需要新的通知机制 |
| 权限与审计 | 全部接口 `require_admin`（读接口也是：这是部署细节）；写接口另加 CSRF。每次变更一条结构化审计事件（`webapp/auth/audit.py`）：`user_id`、`server_id`、变更的字段名、`allow` 的增减工具名；凭据只记槽位名与动作，**不记值**。层文件每个条目带 `updated_at` / `updated_by`（user_id） | 与凭据面板、用户管理同一权限档；审计不新建表 |
| 普通成员看到什么 | `/api/system/capabilities` 的 `mcp` 只加计数（启用 / 停用 / 需确认的 server 数），文案去掉 S7 后已过期的"危险确认门仍按配置 fail closed" | S5 决策记录留下的待办（能力端点不区分 shared / per_run）在这里一并处理 |

## 1. 目标与边界

**做**（编号对应规格 §7 的 P7-S6-Axx）：

1. **Web 管理层（A01）**：`mcp/config.py` 新增第三层合并（`ENV_MCP_WEB_LAYER = "WATER_AGENT_MCP_WEB_LAYER"`）；新模块 `mcp/web_layer.py`：读 / 写 / 校验 / 孤立条目、`revision`、文件锁与原子替换。层文件形态：
   ```json
   {"version": "<MCP_CONFIG_VERSION>", "revision": 3,
    "servers": [{"server_id": "cad", "disabled": false, "allow": ["read_dwg"],
                 "updated_at": "2026-10-07T09:00:00+00:00", "updated_by": "<user_id>"}]}
   ```
   未设变量 = 没有这一层；设了但文件不存在 = 空层（面板首次写入时创建）；文件损坏 / 写了不允许的键 = `MCP_CONFIG_INVALID`。
2. **凭据槽位（A02）**：`MCPServerConfig.credential_env: tuple[str, ...] = ()`（只能写在仓库默认层或部署覆盖层，Web 管理层不能写）；新模块 `mcp/credentials.py`（复用 `config/credentials.py` 的 `credentials_dir()` 与值校验）：`status / write / delete`，路径只由已校验的 `server_id` 与已声明的槽位名拼出；`child_env` 注入；连接前缺值 → `MCP_CREDENTIAL_MISSING`（`mcp/errors.py` 新增，`pre_connection_skips` 判定，停用的 server 不查）。`MCP_CONFIG_VERSION` 递进，入仓骨架与示例同步。
3. **导入预览（A03）**：新模块 `mcp/importer.py`：`preview_import(text, *, existing_ids) -> ImportPreview`（提案 JSON 文本 + 逐条结果），输入上限 64 KiB，JSON / TOML 自动识别。
4. **管理 API（A04–A07）**：新路由文件 `webapp/mcp_admin_routes.py`，在 `webapp/app.py` 注册：
   - `GET /api/admin/mcp/servers`
   - `PATCH /api/admin/mcp/servers/{server_id}`（`{"disabled"?: bool, "allow"?: list[str] | null, "expected_revision": int}`）
   - `DELETE /api/admin/mcp/servers/{server_id}/overrides`（去掉该 server 在 Web 管理层的条目；孤立条目也用它清理）
   - `DELETE /api/admin/mcp/web-layer`（恢复为部署配置；配置损坏时也可用）
   - `PUT` / `DELETE /api/admin/mcp/servers/{server_id}/credentials/{env_name}`（只写；未声明的槽位与不存在的 server 同答 404，不回显名字）
   - `POST /api/admin/mcp/servers/{server_id}/probe`
   - `POST /api/admin/mcp/import-preview`
   - 审计函数加在 `webapp/auth/audit.py`；能力端点文案与计数改在 `webapp/app.py`。
   - 错误码：`WEB_MCP_SERVER_UNKNOWN`(404)、`WEB_MCP_REVISION_CONFLICT`(409)、`WEB_MCP_CONFIG_REJECTED`(422，带校验原因，不回显请求值)、`WEB_MCP_SERVER_DISABLED`(409，探测停用的 server)、`WEB_MCP_PROBE_BUSY`(409)、`WEB_MCP_ADMIN_STORE_UNAVAILABLE`(503，层文件目录不可写)、凭据沿用 `WEB_CREDENTIAL_INVALID` / `WEB_CREDENTIAL_STORE_UNAVAILABLE`。
5. **部署（A08）**：`compose.yml` 新卷 `app_mcp_admin:/var/lib/water-agent/mcp-admin` 与 `WATER_AGENT_MCP_WEB_LAYER=/var/lib/water-agent/mcp-admin/web_layer.json`；`Dockerfile` 预建该目录、属主 `wateragent`。凭据值仍只在 `app_credentials` 卷（不进备份）。
6. **前端（A09，TypeScript）**：设置页新增"MCP 扩展"区（`McpAdminPanel.tsx` + `mcpAdminApi.ts` + `mcpAdminState.ts`；读接口 403 时整块不渲染）：server 列表（来源层、启停开关、需确认 / 共享标记、命令文件名）、`allow` 编辑（勾选探测到的工具 + 手工添加）、凭据槽位（只写输入框，提交后立即清空）、"刷新工具清单"（探测）、"恢复为部署配置"、孤立条目清理、导入区（粘贴 → 预览 → 复制 / 下载提案）。固定说明："改动从下一个 run 生效；命令、参数、环境变量与工作目录只能由运维在部署覆盖层修改"。样式只用 `styles.css` 现有 token；测试 `web/app/tests/mcp_admin.test.mjs`；`npm run build` 的 `tsc` 必须通过。
7. **快照（A10）**：`harness/tool_assembly_snapshot.py` 的 `PINNED_ENV_KEYS` 加 `WATER_AGENT_MCP_WEB_LAYER`；快照只允许 `/versions/MCP_CONFIG_VERSION` 变化（`--write --reason`），surface、其余版本号、全部 lifecycle 由 `frozen_json` 冻结。

**不做**：

- 面板写 `command` / `args` / `env` / `env_file` / `cwd` / `lifecycle` / `human_confirmation` / 超时；面板新增 server；应用导入提案；
- 面板决定环境变量名（槽位只能运维声明）；读回任何凭据值或其片段；
- 数据库表或迁移；跨主机的分布式锁（文件锁只覆盖同一台主机）；
- Streamable HTTP（S8）、entry point 发现、按工具声明需确认；
- 不改桥接 / 协议 / schema / 客户端、工具暴露（S4）、确认门（S7）、提示词、治理层。

## 2. 铁律与治理落点（不得违反）

- **面板不能让 agent 执行新的东西**：能被 spawn 的命令、参数、环境变量名、工作目录只来自运维的两层配置；面板只能启停、放行工具名、给已声明的槽位填值。
- **密钥只写不读**：凭据值不进响应、日志、审计、trace、层文件、备份；错误信息不回显值。
- **坏配置响亮失败**：层文件损坏即 `MCP_CONFIG_INVALID`；写入前整份校验，不让面板写出一份加载不了的配置。
- **放行不是授权**：被放行的工具照样过 schema 子集、描述安检、确认门（`human_confirmation`）、`MCP_INVOKE` 与执行期护栏。
- **导入不应用**：预览接口不落盘、不 spawn、不改任何配置。
- **评测先行**：每条验收先写失败测试（docstring 写验收 ID）。

## 3. 必须复用的现有组件（不得另写一份）

| 需要 | 用这个 | 位置 |
|---|---|---|
| 分层合并与整体替换 | `merge_overlay` 的规则（Web 管理层另加字段白名单） | `mcp/config.py` |
| 合并后校验（含路径存在性） | `MCPGatewayConfig.model_validate` + `_check_paths_exist` | `mcp/config.py` |
| 凭据卷位置与值校验 | `credentials_dir()`、`validate_credential_value` | `config/credentials.py` |
| 子进程 env 构造 | `child_env`（槽位注入加在这里） | `mcp/config.py` |
| 连接前跳过 | `pre_connection_skips` | `mcp/wiring.py` |
| 探测用的连接 | `McpConnectionPool.lease(config, confirmation_ready=True)` | `mcp/pool.py`（Web 会话工厂持有的那个池） |
| admin / CSRF | `require_admin`、`validate_csrf` | `webapp/auth/` |
| 审计 | `_emit` 风格的结构化事件 | `webapp/auth/audit.py` |
| 前端凭据输入的交互 | `SettingsPanel.tsx` 的 `CredentialCard`（提交后清空、只显示状态） | `web/app/src/` |

## 4. 实施步骤（一步一个 commit；每个 commit 结束时快照 `--check` 必须一致，Step 1 的版本号变化在同一 commit 内 `--write --reason`）

### Step 1：Web 管理层（A01、A10）

- 第三层合并、`mcp/web_layer.py`、孤立条目、`PINNED_ENV_KEYS`、`MCP_CONFIG_VERSION` 递进（本步唯一一次递进；Step 2 的 `credential_env` 同属这一版，不再递进），入仓骨架 `version` 同步；新文件 `tests/test_mcp_web_layer.py`：只能写三个键、不能新增 server、`confirmation_handler_ready` / 未知键 / 坏 JSON → `MCP_CONFIG_INVALID`、`allow` 整体替换、合并顺序（Web 管理层最后）、孤立条目被忽略且可列出、未设变量与文件缺失的语义、写入的原子性（写到一半失败不留半个文件）与 revision 冲突。

### Step 2：凭据槽位（A02）

- `credential_env`、`mcp/credentials.py`、`child_env` 注入、`MCP_CREDENTIAL_MISSING`；新文件 `tests/test_mcp_credentials.py`：槽位名约束（含拒绝 `LD_PRELOAD` / `PYTHONPATH` / `NODE_OPTIONS` 与内联 `env` 重名）、Web 管理层写 `credential_env` 被拒、值只写不读（状态里没有值、文件 0600）、注入到子进程（fake server 回报 env 是否存在，不回报值）、缺值 → `MCP_CREDENTIAL_MISSING` 且不 spawn、改值后 shared server 指纹变化。fake server 若需新增"回报某个环境变量是否存在"的只读工具，写在 `--mode stateful` 里，**只回报存在与否，不回报值**。

### Step 3：导入预览（A03）

- `mcp/importer.py`；新文件 `tests/test_mcp_import.py`：Claude Code / Cursor JSON 与 Codex TOML 各一份夹具；提案里每台 `disabled: true`、没有 `allow`、没有 `env` 值（输入里的值一个都不出现在输出里）、`env` 键成为 `credential_env`；`url` 条目、含 `/` 的相对 `command`、非法名字逐条给原因；与现有 `server_id` 冲突标出；超过 64 KiB 拒绝；不调用任何文件系统写入 / 子进程（用替身断言）。

### Step 4：管理 API、探测与审计（A04–A07）

- `webapp/mcp_admin_routes.py`、`app.py` 注册与能力端点、`audit.py`；新文件 `tests/test_web_mcp_admin.py`（沿用 `tests/test_web_runs.py` 的 SQLite 夹具、`tests/test_web_mcp_pool.py` 的工厂与 fake server）：
  - 未登录 401、成员 403、临时密码管理员 403、写接口缺 CSRF 403；
  - 读接口不含 `args`、`env` 值、凭据值、完整命令路径；
  - PATCH：未知 server 404、`expected_revision` 过期 409、合并后不合法（例如启用一台 `cwd` 不存在的 server）422 且层文件不变、成功后 revision + 1；
  - 恢复部署配置（单台与整层），配置损坏时读接口照常返回错误码、整层恢复可用；
  - 下一个 run 生效：停用 / 改 `allow` / 改凭据后，下一次会话工厂装配随之变化，shared server 换新进程；
  - 探测：返回暴露工具与跳过码、不含远端描述原文；停用的 server 409；并发第二个探测 409；
  - 审计：每次变更一条事件，`allow` 增减工具名正确，任何事件里都没有凭据值。

### Step 5：部署（A08）

- `compose.yml`、`Dockerfile`；测试补在 `tests/test_web_deployment.py`：卷挂在 `/var/lib/water-agent/` 之下（在备份范围内）、顶层 volumes 注册、环境变量指向卷内文件、镜像预建目录且属主 `wateragent`、凭据卷仍不在 `/var/lib/water-agent/` 之下。生产 overlay 解析（`docker compose -f compose.yml -f compose.production.yml config -q`）照常通过。

### Step 6：前端（A09）

- 面板 + API + 状态 + 测试；`npm test` 与 `npm run build` 通过。

### Step 7：收尾

- 规格 §2（若有新事实）、§3 新增 3.8 Web 扩展面板、§5 S6 行与 §7 S6 验收行翻成 delivered；`14-p4-mcp-gateway.md` §3 补 Web 管理层、`credential_env` 与 `MCP_CREDENTIAL_MISSING`；
- 写 `docs/design/p7-step-6-decision.md`；AGENTS.md 补一条，并更新 P7 交付状态（S0–S7 除 S8）。

## 5. 范围块

<!-- step-scope -->
```json
{
  "step": "P7-S6",
  "allow": [
    "src/water_agent/mcp/config.py",
    "src/water_agent/mcp/web_layer.py",
    "src/water_agent/mcp/credentials.py",
    "src/water_agent/mcp/importer.py",
    "src/water_agent/mcp/errors.py",
    "src/water_agent/mcp/wiring.py",
    "src/water_agent/mcp/__init__.py",
    "src/water_agent/config/credentials.py",
    "src/water_agent/webapp/mcp_admin_routes.py",
    "src/water_agent/webapp/app.py",
    "src/water_agent/webapp/auth/audit.py",
    "src/water_agent/harness/tool_assembly_snapshot.py",
    "compose.yml",
    "Dockerfile",
    "data/mcp_servers.json",
    "data/mcp_servers.example.json",
    "data/eval/tool_assembly_main.json",
    "web/app/src/**",
    "web/app/tests/**",
    "tests/fixtures/fake_mcp_server.py",
    "tests/fixtures/mcp_import/**",
    "tests/test_mcp_web_layer.py",
    "tests/test_mcp_credentials.py",
    "tests/test_mcp_import.py",
    "tests/test_web_mcp_admin.py",
    "tests/test_mcp_layered_config.py",
    "tests/test_mcp_wiring.py",
    "tests/test_mcp_pool.py",
    "tests/test_mcp_gateway.py",
    "tests/test_web_deployment.py",
    "tests/test_web_settings_api.py",
    "tests/test_p7_known_figures.py",
    "tests/test_tool_assembly_snapshot.py",
    "docs/design/p7-step-6-decision.md",
    "docs/design/16-p7-plugin-mcp.md",
    "docs/design/14-p4-mcp-gateway.md",
    "docs/execution-prompts/P7-S6-mcp-admin-panel.md",
    "AGENTS.md"
  ],
  "forbid": [
    "src/water_agent/mcp/bridge.py",
    "src/water_agent/mcp/protocol.py",
    "src/water_agent/mcp/schema.py",
    "src/water_agent/mcp/client.py",
    "src/water_agent/mcp/pool.py",
    "src/water_agent/plugins/**",
    "src/water_agent/graphs/**",
    "src/water_agent/prompting/**",
    "src/water_agent/runtime/**",
    "src/water_agent/tools/**",
    "src/water_agent/multi_agent/**",
    "src/water_agent/harness/runner.py",
    "src/water_agent/harness/spec_trace.py",
    "src/water_agent/harness/tool_exposure_eval.py",
    "src/water_agent/webapp/run_manager.py",
    "src/water_agent/webapp/run_routes.py",
    "src/water_agent/cli.py",
    "src/water_agent/storage/**",
    "alembic/**",
    "alembic.ini",
    "scripts/**",
    ".github/**",
    "compose.production.yml",
    "tests/conftest.py",
    "tests/test_p7_traceability.py",
    "tests/test_check_step_scope.py",
    "tests/test_import_guard.py",
    "tests/test_plugin_invariants.py",
    "tests/test_answer_cache.py",
    "tests/test_governance_contracts.py",
    "tests/test_tool_policy.py",
    "tests/test_tool_exposure.py",
    "tests/test_tool_exposure_eval.py",
    "tests/test_confirmation_grant.py",
    "tests/test_confirmation_gate.py",
    "tests/test_web_confirmation.py",
    "data/eval/baseline.json",
    "data/eval/*.jsonl",
    "data/eval/drawing_p0_main.json",
    "data/eval/drawing_gen_p0_main.json",
    "data/eval/memory_p0_main.json",
    "data/eval/multi_agent_p0_main.json",
    "data/eval/tool_exposure_p0_main.json"
  ],
  "protect": [
    {
      "node": "tests/test_answer_cache.py::TestContextBudgetReview::test_tool_definitions_token_cost_is_bounded",
      "contains": "assert cost < 4500"
    },
    {
      "node": "tests/test_governance_contracts.py::TestInventoryCoverage::test_all_provider_sites_governed_after_step12",
      "contains": "\"provider_call_sites\": 7"
    },
    {
      "node": "tests/test_tool_policy.py::TestConfirmationGate::test_confirmation_required_tool_blocked_at_call_time",
      "contains": "assert blocked.decision.rule_id == \"tool.confirmation_required\""
    },
    {
      "node": "tests/test_mcp_wiring.py::test_ready_flag_rejected_until_confirmation_gate_exists",
      "contains": "assert exc.value.code == \"MCP_CONFIG_INVALID\""
    },
    {
      "node": "tests/test_mcp_layered_config.py::test_overlay_cannot_widen_the_gate",
      "contains": "assert exc.value.code == \"MCP_CONFIG_INVALID\", bad"
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
        "/versions/PROMPT_TEMPLATE_VERSION",
        "/versions/TOOL_POLICY_VERSION",
        "/profiles/*/surface",
        "/profiles/*/lifecycle"
      ]
    }
  ]
}
```

## 6. 受保护断言

- S1：没有凭证的需确认调用仍被 `tool.confirmation_required` 拒绝；配置里 `confirmation_handler_ready=true` 仍 `MCP_CONFIG_INVALID`。
- S3：覆盖层不能写 `confirmation_handler_ready` 或未知键（Web 管理层的限制比覆盖层更窄，不得反过来放宽覆盖层）。
- 出站清单 `provider_call_sites: 7`、本地工具 schema `< 4500` 与 `4497`、`tools_guardrail_validated: 5`。

## 7. 停止条件

满足任一条就停下，写清现象后等确认：

- 快照里 `/versions/MCP_CONFIG_VERSION` 之外的任何片段变化（尤其 surface 或 lifecycle）；
- 需要改 `bridge.py`、`client.py`、`pool.py`、`exposure.py`、确认门、治理层、提示词，或需要数据库迁移；
- 槽位注入发现必须让面板决定环境变量名才能满足某个真实 server（说明槽位模型不够，需重新评审，不得放开名字）；
- 发现现有部署覆盖层的某个合法写法会被第三层合并改变语义（向后不兼容）；
- 需要放宽任何一条受保护断言；
- 门禁出现与本步无关的红灯。

## 8. 验证命令（完成前全部跑过，贴出输出）

```bash
export VENV=/Users/yaerken/Desktop/给排水agent/_venv PYTHONPATH=src
git fetch origin main
$VENV/bin/pytest -q tests/test_mcp_web_layer.py tests/test_mcp_credentials.py tests/test_mcp_import.py tests/test_web_mcp_admin.py tests/test_mcp_layered_config.py tests/test_mcp_wiring.py tests/test_mcp_gateway.py tests/test_mcp_pool.py tests/test_web_mcp_pool.py tests/test_web_settings_api.py tests/test_web_deployment.py tests/test_p7_known_figures.py tests/test_tool_assembly_snapshot.py tests/test_p7_traceability.py
(cd web/app && npm test && npm run build)
$VENV/bin/python -m water_agent.harness.tool_assembly_snapshot --check
$VENV/bin/python scripts/check_step_scope.py docs/execution-prompts/P7-S6-mcp-admin-panel.md --base origin/main
docker compose -f compose.yml -f compose.production.yml config -q
COMPOSE_PROJECT_NAME=p7s6 VENV=$VENV bash scripts/ci_gate.sh
```

手动验收（有条件时做，没有就在决策记录里写明未做）：本机 Web 栈 + 覆盖层里一台 fake server（声明一个凭据槽位），用管理员账号在浏览器里：探测 → 勾选一个工具放行 → 填凭据 → 发一条会用到它的问题、看到工具卡片；停用 → 再问一次、工具不再出现；导入一份含 `env` 密钥的 `mcpServers`，确认提案里没有密钥值。

## 9. 完成定义

- 满足规格 §8 第 5 条的全部项；
- PR 上的 `p7-scope` 与 `p5-ci` 通过；
- 决策记录贴出：面板写不了 `command` / 新 server / 环境变量名的反证、凭据值不出现在任何响应 / 日志 / 审计 / 层文件里的实测、"下一个 run 生效"的实测、Step 1 的 `--diff` 原文。

## 10. 汇报格式

- 改了哪些文件；
- 验证命令与实际输出（失败照贴）；
- 范围守卫的输出；
- 快照差异；
- 未验证项（含手动验收是否做了）；
- 残余风险（至少包括：管理员账号被盗时能放行任意已上报的远端工具、给槽位填任意值；文件锁只覆盖单台主机；探测按需 spawn 运维配置的命令）。

## 11. 常见坑（本仓库实测过）

- worktree 里不设 `PYTHONPATH=src` 时，pytest 会被 H0 拦下（exit 4）；`web/app` 在新 worktree 里要先 `npm ci`（走代理）才能 `npm run build`。
- 本机如果设了 `WATER_AGENT_MCP_OVERLAY`、`WATER_AGENT_MCP_CONFIG`、`WATER_AGENT_MCP_WEB_LAYER` 或 `WATER_AGENT_TOOL_EXPOSURE`，跑测试和快照前先 `unset`。
- `MCP_CONFIG_VERSION` 递进时，`data/mcp_servers.json` 的 `version` 要同步（`tests/test_mcp_wiring.py::TestConfigWiring::test_default_config_is_empty_skeleton` 断言相等）。
- 运行时 MCP 绑定登记只增不删：测试里用独立的 server id。
- 镜像根文件系统只读（`read_only: true`）：层文件只能落在挂载的卷里；新卷的挂载点必须在镜像里预建并 chown，否则 named volume 首次挂载继承 root 属主，uid 10001 写不进去。
- 前端测试用 node strip-types 直跑，被测模块间相对导入须带 `.ts` 后缀，且不能用 TS 参数属性；源码文本断言改了表达式要同步更新。
- 提交前单独确认测试退出码为 0，不要把 `git commit` 串在测试命令后面。
- 只含执行 Prompt 的文档 PR，分支名不能以 `YEK/p7-s<n>-` 开头。
