# P7-S6 决策记录：Web 扩展面板（Step E1）

**日期**：2026-10-07
**状态**：已完成（浏览器手动验收 P7-S6-A11 只做了面板段，见 §2.6 / §5）
**规格来源**：`docs/design/16-p7-plugin-mcp.md` §3.8、§5 S6、§7 P7-S6；执行 Prompt `docs/execution-prompts/P7-S6-mcp-admin-panel.md`（其 §0 的决定以 Prompt 为准）
**基线**：main `9478b27`（S6 执行 Prompt `9b39740` 经 PR #34 合入后；实现开工时 main 是 `0d3a6f7`，见 §0.2 第 1 条）

---

## 0. 偏离与修订

### 0.1 执行 Prompt §0 的决定（照做）

| 决定 | 落点 |
|---|---|
| 面板的改动写进第三层配置 Web 管理层（`WATER_AGENT_MCP_WEB_LAYER`），合并在部署覆盖层之后 | `mcp/config.py::load_layered_gateway_config` / `merge_web_layer`；读写在 `mcp/web_layer.py` |
| Web 管理层只能写 `server_id` / `disabled` / `allow`，不能新增 server，不能写任何其他字段或顶层键 | `config.WEB_LAYER_SERVER_KEYS`；`web_layer._check_keys`（只列键名、不回显值）+ `WebLayerServer` / `WebLayerDocument`（`extra="forbid"`） |
| 面板可以放宽 `allow`、可以启用运维停用的 server | `with_override`；放行之后的工具照样过桥接五道条件与执行期护栏（本步没改 `bridge.py` / 治理层） |
| 孤立条目加载时忽略、列表接口列出并可清理 | `merge_web_layer` 返回孤立 id；`web_layer.orphan_entries`；`DELETE …/overrides` |
| 层文件损坏即 `MCP_CONFIG_INVALID`；读接口照常返回错误码；整层恢复可用 | `load_web_layer`；`_listing` 的 `config_error.layer`；`WebLayerStore.reset` |
| 凭据引用 = 运维声明的槽位 `credential_env`，面板只填值（只写不读，凭据卷 0600） | `MCPServerConfig.credential_env`；`mcp/credentials.py`；`child_env` 注入 |
| 槽位名约束：大写、不与内联 `env` 重名、拒绝加载器 / 解释器变量；注入顺序 `env_file` < 槽位 < 内联 `env` | `credentials.validate_slot_name`；`MCPServerConfig._credential_env_disjoint_from_env` |
| 槽位没填值 → `MCP_CREDENTIAL_MISSING`、不 spawn | `mcp/errors.py`；`wiring._credential_skips`（停用的 server 不查） |
| 导入是无状态预览：`disabled: true`、不写 `allow`、`env` 值丢弃、url / 非 stdio 不支持、名字归一与冲突、逐条模型层校验 | `mcp/importer.py::preview_import` |
| 探测经 Web 会话工厂的连接池借一次租约（`confirmation_ready=True`），只回工具名与跳过码，同台同时只一个 | `mcp_admin_routes._run_probe` / `_probe_result` / `McpAdminState` |
| 读接口返回的字段与不返回的字段 | `mcp_admin_routes._server_view`（命令只给文件名） |
| 写接口 `expected_revision` 乐观并发、文件锁、整份校验、原子替换；下一个 run 生效 | `WebLayerStore.update` + `check_candidate`；Web 每个 run 重新 `load_mcp_gateway()`，池按指纹换进程 |
| 全部 `require_admin`，写接口 CSRF；变更进结构化审计，凭据只记槽位名与动作；层文件条目带 `updated_at` / `updated_by` | `mcp_admin_routes`；`webapp/auth/audit.py` 的 `audit_mcp_*` |
| 能力端点只加计数，去掉过期文案 | `webapp/app.py` 的 `/api/system/capabilities` |
| compose 新卷 `app_mcp_admin` 挂在 `/var/lib/water-agent/mcp-admin`、Dockerfile 预建目录 | `compose.yml`、`Dockerfile` |
| 前端 `McpAdminPanel.tsx` + `mcpAdminApi.ts` + `mcpAdminState.ts`，读接口 403 时整块不渲染 | `web/app/src/`；`SettingsPanel.tsx` 末尾挂载 |
| 快照钉住新变量，只允许 `/versions/MCP_CONFIG_VERSION` 变化 | `harness/tool_assembly_snapshot.PINNED_ENV_KEYS`；§3 |

### 0.2 实测后追加的偏离

| 执行 Prompt 原文 | 本步做法 | 原因 |
|---|---|---|
| 开工前执行 Prompt 必须已在 main 上 | 开工时 `origin/main` = `0d3a6f7`，**没有** S6 执行 Prompt：草稿与规格修订在 S7 会话的 worktree（`funny-jennings-26fa75`，分支 `YEK/p7-docs-s6-prompt`）里未提交。实现分支先以 `9b39740` 为第一个 commit 原样收录那份草稿（Prompt + 规格 S6 行 + 任务书 S6 行），并在范围块 `allow` 补了一行 `tests/test_mcp_pool.py`（下一条）；`9b39740` 单独经文档 PR #34 合入 main（`9478b27`），实现分支随后 rebase 到它上面（树与 rebase 前逐字节相同），范围守卫改以 `--base origin/main` 跑 | 实现代码不依赖 Prompt 是否已合入；但 PR 上的 `p7-scope` 从 merge-base 读范围块 |
| 范围块 `allow` 不含 `tests/test_mcp_pool.py` | 加上 | `tests/test_mcp_pool.py:112`（P7-S5-A02）写死 `assert MCP_CONFIG_VERSION == "p7-s5-2026-10-06"`；Prompt 要求本步递进版本号，不改这一行测试必红。只改了这一个字面量 |
| 导入"输入上限 64 KiB" | `preview_import` 自身仍按 64 KiB UTF-8 拒绝；**经 HTTP 的上限是 16 000 字符**（`mcp_admin_routes.MAX_IMPORT_CHARACTERS`），前端先挡 | 首次全量门禁红在 `tests/test_upload_body_limit.py::test_request_ceiling_leaves_room_for_the_largest_legitimate_body`：它按最坏每字符 12 字节估算每个 JSON 请求体，要求最大的仍是消息请求（192 227 B）且 4 倍不超过 1 MiB 的请求体上限。64 Ki 字符最坏 786 KB；16 000 字符最坏 192 012 B。那条测试不在本步范围内、也不该为它放宽；收紧是更保守的方向。真实的 mcpServers 配置通常只有几 KB |
| （未提及）合并后"含路径存在性"的完整校验 | 设了 Web 管理层时，运维两层只做**模型**校验（报错仍指向运维的文件），路径存在性在三层合并后的**最终**配置上查；未设变量时与 S3 逐字相同（两层合并后查路径） | 否则面板救不了"运维配的 `cwd` 被删了"——那时整个 MCP 配置 `MCP_CONFIG_INVALID`，每个 Web run 都失败，而面板停用这台 server 的写入本身又过不了校验。`_check_paths_exist` 拆出 `missing_paths`，报错文本不变 |
| 不改 `pool.py` / `bridge.py` | `wiring.SkippedServer` 新增可选字段 `remote_tool`，`session_skips` 把 `SkippedTool.remote_tool` 带上；`to_dict` / `trace_summary` 不变 | 一台 server 一个工具都没放行时（初始 `allow=None` 的常态），`open_bridge` 收掉连接、池把它记成 server 级跳过，原先 `session_skips` 丢掉了远端工具名——探测就列不出"有哪些工具可以放行"。快照 lifecycle 只记 `[server_id, code]`，不受影响 |
| 探测"借一次租约" | 租约用的配置视图只含**目标 server + 池里已在用的 shared server** | 用全量配置会顺带 spawn 所有 per_run server；只给目标 server，池会把其余在用的 shared 连接当成"已从配置删除"而退役（`lease` 的 `_retire_missing`）。有测试锁住"探测之后另一台 shared server 仍是同一进程"。会话工厂没有池（测试替身）时临时建一个、用完关掉 |
| 槽位名拒绝 `PATH`、`HOME`、`BASH_ENV`、`ENV`、`JAVA_TOOL_OPTIONS` 与前缀 `LD_` / `DYLD_` / `PYTHON` / `NODE_` / `PERL` / `RUBY` | 另加 `JDK_JAVA_OPTIONS`、`SHELLOPTS`、`BASHOPTS`、`PS4`、`PROMPT_COMMAND`、`IFS`、`GCONV_PATH`；名字须匹配 `^[A-Z][A-Z0-9_]{0,127}$`（`_JAVA_OPTIONS` 这类下划线开头的因此也拒绝） | 同属 shell / 动态链接器 / JVM 启动时会读来执行代码或改变加载行为的变量；只收紧不放宽 |
| 导入"与现有 id 冲突的标出" | 标出并**改名**为 `<id>_2`（`_3`…），同批里归一成同一个 id 的同样处理 | 同名 id 贴进覆盖层会**改写**那台已有 server（覆盖层按 `server_id` 逐字段合并），而提案还带着 `disabled: true`——等于一份"导入"把现有 server 停掉并换掉命令 |
| PATCH 请求体 `{"disabled"?, "allow"?, "expected_revision"}` | 手工解析（`await request.json()`），写了别的键一律 422 `WEB_MCP_CONFIG_REJECTED`，固定文案不回显值；`allow: null` = 全拒（与覆盖层同规则），去掉改动走 `DELETE …/overrides` | 用 pydantic 模型的话多余键走全局 `RequestValidationError` → `WEB_INVALID_REQUEST`，拿不到"命令只能由运维修改"这样的可操作原因 |
| `DELETE …/{id}/overrides` | 带 `?expected_revision=`（写接口都做乐观并发）；对没有改动的已知 server 幂等 200、不递进 revision、不记审计；孤立条目的删除不跑整份校验（不改变有效配置）；`DELETE /web-layer` 不带 revision（损坏时读不出 revision） | — |
| 审计只记变更 | 另记 `mcp.server_probed` | 探测会按需 spawn 运维配置的命令：不是配置变更，但应留痕 |
| 能力端点加计数（启用 / 停用 / 需确认） | 另加 `shared_server_count` | S5 决策记录留下的待办（能力端点不区分 shared / per_run） |
| （测试） | fake server `--mode stateful` 新增只读工具 `env_present`（只回报变量是否存在）；`config.check_allow_items` 从 `MCPServerConfig._validate_allow` 拆出供 Web 管理层模型复用（规则与报错文本不变）；"目录不可写"用"父路径是普通文件"构造而不是 `chmod` + root 时 skip | 追溯检查不允许验证项里出现 `pytest.skip` |

---

## 1. 交付

| commit | 产物 | 要点 |
|---|---|---|
| `9b39740`（PR #34，已在 main） | S6 执行 Prompt、规格 S6 验收行（planned）、任务书 S6 行 | 草稿原样 + `allow` 补 `tests/test_mcp_pool.py`（§0.2） |
| `3cc9392` | Web 管理层（A01、A10） | `OperatorLayers` / `read_operator_layers` 把"读两层"与"校验"拆开（报错口径不变）；`WebLayerStore`（锁 → 比对 revision → 改 → 校验 → 临时文件 + `os.replace`）；`MCP_CONFIG_VERSION` → `p7-s6-2026-10-07`，入仓骨架 / 示例同步；`PINNED_ENV_KEYS` 加 `WATER_AGENT_MCP_WEB_LAYER` |
| `e36c186` | 凭据槽位（A02） | `mcp/credentials.py` 是叶子模块（只依赖 `config/credentials.py`），`mcp/config.py` 顶层导入它；`child_env` 竞态下拿不到值同样报 `MCP_CREDENTIAL_MISSING`，不带空 token 启动 |
| `e79ea97` | 导入预览（A03） | 合成夹具 `tests/fixtures/mcp_import/`（Claude Code / Cursor JSON、Codex TOML，env 里放了形如密钥的值） |
| `796ade7` | 管理 API、探测、审计、能力端点计数（A03–A07） | `SkippedServer.remote_tool`（§0.2）；探测视图（§0.2） |
| `2cdc751` | 部署（A08） | `app_mcp_admin` 卷 + 环境变量 + 镜像预建目录 |
| `a2de216` | 前端（A09） | 设置页末尾挂 `McpAdminPanel`；样式块只用现有 token（有源码断言） |
| `2d4219d` | 导入 HTTP 字符上限 16 000（§0.2）、审计用例补日志 / 层文件断言 | 首次全量门禁的唯一红灯 |
| 本 commit | 规格 §3.8 / §5 / §7 / §10、P4 规格 §2.3 / §3、本记录、AGENTS.md | — |

---

## 2. 实测

### 2.1 先红后绿与反证（每条都在实现之后把关键一行改回去重跑）

- A01：`load_layered_gateway_config` 里 Web 管理层路径恒取空 → `test_web_layer_can_only_toggle_and_allow`、`…merges_last…`、`…orphan_entries…` 3 条失败；`merge_web_layer` 不查键 → `test_web_layer_can_only_toggle_and_allow` 失败。
- A02：`child_env` 不注入槽位值 → `test_credential_values_are_write_only_and_reach_the_child` 失败；去掉 `_credential_skips` → `test_missing_credential_skips_the_server` 失败；`credential_env` 不校验名字 → `test_credential_slots_are_declared_by_the_operator` 失败。
- A03：提案带上 `env`、不生成槽位 → 两条导入用例都失败；`disabled` 改成 `False` → `test_import_produces_disabled_proposals_only` 失败。
- A04：PATCH 不拒绝多余键 → `test_server_update_is_validated_and_versioned` 失败。
- A06：`session_skips` 不带 `remote_tool` → 探测用例失败（全拒时列不出工具名）；探测视图只给目标 server → 同一用例失败（另一台 shared server 被退役、pid 变了）。
- A07：去掉凭据写入的审计 → `test_admin_changes_are_audited_without_secrets` 失败。
- A08：compose 去掉卷挂载 → `test_mcp_admin_layer_volume_is_backed_up` 失败。
- A10：`PINNED_ENV_KEYS` 去掉 `WATER_AGENT_MCP_WEB_LAYER` → `test_snapshot_is_hermetic_and_deterministic`（污染环境里放了一份损坏的层文件）失败。

### 2.2 面板写不了 command / 新 server / 环境变量名（反证）

- 层文件里写 `command` / `args` / `env` / `env_file` / `cwd` / `lifecycle` / `human_confirmation` / `transport` / 三个超时 / `credential_env` 任一键，或顶层写 `confirmation_handler_ready`（true / false 都算）/ 未知键：加载即 `MCP_CONFIG_INVALID`，报错只列键名（`test_web_layer_can_only_toggle_and_allow`）。
- 层文件里给下层不存在的 `gamma` 写合法形状的条目：有效配置里的 server 仍只有 `alpha`、`beta`（同上）。
- `PATCH /api/admin/mcp/servers/adm_cad` 带 `command` / `args` / `env` / `cwd` / `lifecycle` / `human_confirmation` / `credential_env` / `server_id` 任一键：422 `WEB_MCP_CONFIG_REJECTED`，响应里没有 `/bin/sh`、`/tmp/evil.so`、`NODE_OPTIONS`，层文件**没有被创建**（`test_server_update_is_validated_and_versioned`）。
- 未声明的槽位 `OTHER_TOKEN` 与不存在的 server：`PUT …/credentials/…` 都是 404 `WEB_MCP_SERVER_UNKNOWN`，响应不回显名字，不记审计（`test_admin_changes_are_audited_without_secrets`）。
- 前端写接口的请求体类型只有 `disabled` / `allow` / `expected_revision` 三个键，传进多余键也不会被发出去（`web/app/tests/mcp_admin.test.mjs`）。

### 2.3 凭据值不出现在任何响应 / 日志 / 审计 / 层文件里

`tests/test_web_mcp_admin.py` 的覆盖层里一台停用的 server 把 `args` 写成 `["--token", "args-secret-value-0001"]`、内联 `env` 写 `inline-secret-value-0002`、`env_file` 内容是 `file-secret-value-0003`，槽位经 API 填 `slot-secret-value-0004`，之后再轮换成 `…0005`：

- 列表接口、PATCH 回执的全文里都没有这 5 个值，也没有 `/opt/operator-only-dir` 与 `sys.executable` 的完整路径，只有命令文件名 `library-server` / `python`（`test_admin_api_authorization`）；
- 审计事件（`_get_events()` 全部）、root / `water_agent` / 审计 / uvicorn / httpx logger 在整段操作里收到的每一行、层文件全文：都没有（`test_admin_changes_are_audited_without_secrets`）；
- 槽位文件 0600、目录 0700，值只在凭据卷；清除后凭据卷里不留这个值（同上，`test_credential_values_are_write_only_and_reach_the_child`）；
- 子进程只回报 `CAD_API_TOKEN` 存在、`DEEPSEEK_API_KEY` 不存在，从不回报值（`env_present`）。

### 2.4 "下一个 run 生效"

`test_changes_take_effect_on_the_next_run`（真实 `WebAgenticSessionFactory`，PG checkpointer 换成内存实现，fake server `--mode stateful`，`lifecycle: shared`）：

1. 槽位没填：会话装配里 `adm_cad` 是 `MCP_CREDENTIAL_MISSING`，没有进程；
2. 填值 → 下一个会话暴露 `mcp_adm_cad_whoami`、拿到 pid₁；
3. **这个 run 还开着**时 PATCH `allow` 加 `env_present` → 下一个会话 `connections == {"adm_cad": "shared_new"}`、pid₂ ≠ pid₁、`env_present` 可用并回报槽位变量存在；旧 run 里 pid₁ 照常可调用；旧 run 结束后 pid₁ 退出；
4. 轮换凭据值 → 下一个会话又是 `shared_new`、新 pid；
5. 停用 → 下一个会话没有任何 `mcp_adm_cad_*` 工具、记 `MCP_SERVER_DISABLED`，池里不再有它。

### 2.5 验证命令

```
export VENV=/Users/yaerken/Desktop/给排水agent/_venv PYTHONPATH=src
$VENV/bin/pytest -q tests/test_mcp_web_layer.py tests/test_mcp_credentials.py tests/test_mcp_import.py tests/test_web_mcp_admin.py tests/test_mcp_layered_config.py tests/test_mcp_wiring.py tests/test_mcp_gateway.py tests/test_mcp_pool.py tests/test_web_mcp_pool.py tests/test_web_settings_api.py tests/test_web_deployment.py tests/test_p7_known_figures.py tests/test_tool_assembly_snapshot.py tests/test_p7_traceability.py
(cd web/app && npm test && npm run build)
$VENV/bin/python -m water_agent.harness.tool_assembly_snapshot --check
$VENV/bin/python scripts/check_step_scope.py docs/execution-prompts/P7-S6-mcp-admin-panel.md --base origin/main
docker compose -f compose.yml -f compose.production.yml config -q
COMPOSE_PROJECT_NAME=p7s6 VENV=$VENV bash scripts/ci_gate.sh
```

实际输出见 §2.7。

### 2.6 浏览器检查（P7-S6-A11 的面板段）

本机起了一个**不入库**的开发服务器（`/tmp` 下的脚本：SQLite 上的真实鉴权 + 三层配置 + fake server `--mode stateful`，前端是 `npm run build` 的产物），在浏览器里：

- 管理员：填 `CAD_API_TOKEN` → 输入框立即清空、显示"已填写"；"刷新工具清单"在白名单全拒时列出 `whoami` / `bump` / `peek` / `env_present` / `slow_echo`（全部"未放行"）；勾选 `env_present` 保存 → revision 1、白名单来源显示 Web 管理层、卡片标"Web 管理层已改动"；
- 导入一份 env 里有 `ghp_secretvalue123456` 的 `mcpServers`：提案里没有这个值，`GITHUB_PERSONAL_ACCESS_TOKEN` 变成槽位，`dev-cad` 与现有 `dev_cad` 冲突改名 `dev_cad_2`，url 条目标"不支持"，页面全文不含该值；
- 成员账号：设置页里没有"MCP 扩展"区，能力端点只有计数（`configured 2 / enabled 1 / disabled 1 / confirmation 0 / shared 0`）。

**没做**："发一条会用到它的问题、看到工具卡片；停用后再问一次"——Web run 需要 PostgreSQL（checkpointer）与真实模型，fake server 也不在镜像里；自动化覆盖在 §2.4（会话工厂装配逐步变化）。

### 2.7 实测输出（2026-10-07，本机 macOS；rebase 前后树相同）

| 命令 | 结果 |
|---|---|
| §2.5 第一条 pytest（14 个文件） | exit 0，144 passed |
| `npm test` / `npm run build` | 79/79 pass（S7 后 74 条 + 本步 5 条）；`tsc -b` + vite 构建通过 |
| `tool_assembly_snapshot --check` | 装配快照与基线一致（15 个 profile） |
| `check_step_scope.py … --base origin/main`（PR #34 合入、rebase 之后） | exit 0，`merge-base=9478b27 commits=8 changed_files=37`，范围守卫通过；`--auto` 同样通过 |
| 同上，PR #34 合入之前 | exit 2：`执行 Prompt 不在 merge-base 上`（预期内）；当时以 `--base 9b39740` 跑，exit 0、`changed_files=37` |
| `docker compose -f compose.yml -f compose.production.yml config -q` | exit 0 |
| `COMPOSE_PROJECT_NAME=p7s6 bash scripts/ci_gate.sh`（第一次，`a2de216`） | exit 1：唯一失败 `tests/test_upload_body_limit.py::test_request_ceiling_leaves_room_for_the_largest_legitimate_body`（`assert 'ImportRequest' == 'MessageRequest'`），其余全部通过 → `2d4219d` 修（§0.2） |
| 同上（第二次，`2d4219d`） | exit 0：pytest 3489 passed / 30 skipped（S7 合入时 3469 / 30，本步新增 20 条）+ ruff + mypy（222 个源文件）+ rag_p0 12/12 + calc_p0 28/28 + docgen_p0 11/11 + material_p0 10/10 + governance_p0 32/32 + agentic_p0 21/21 + tool_exposure_p0 36/36 + drawing_p0 + drawing_gen_p0 + multi_agent_p0 + memory_p0（§13.2 十二项）+ 基线对比 |

---

## 3. 快照变化

Step 1 有意变更，`--write --reason "P7-S6：新增第三层 Web 管理层与 credential_env 凭据槽位，MCP_CONFIG_VERSION 递进；surface 与 lifecycle 不变"` 之前的 `--diff` 原文：

```
/versions/MCP_CONFIG_VERSION: "p7-s5-2026-10-06" -> "p7-s6-2026-10-07"
```

之后每个 commit `--check` 均为"装配快照与基线一致（15 个 profile）"。`credential_env` 进了 `model_dump`，manifest hash 的**值**随之变化（不比较），surface 与全部 lifecycle 不变（范围块 `frozen_json` 冻结）。

---

## 4. 不做

- 面板写 `command` / `args` / `env` / `env_file` / `cwd` / `lifecycle` / `human_confirmation` / 超时；面板新增 server；应用导入提案。
- 面板决定环境变量名；读回任何凭据值或其片段。
- 数据库表或迁移；跨主机的分布式锁。
- Streamable HTTP（S8）、entry point 发现、按工具声明需确认。
- 不改桥接 / 协议 / schema / 客户端 / 连接池、工具暴露（S4）、确认门（S7）、提示词、治理层。

---

## 5. 残余风险

- **管理员账号被盗**：能放行任意已上报的远端工具、给任意已声明的槽位填任意值、启用运维停用的 server。放行之后仍有 schema 子集、描述安检、确认门、`MCP_INVOKE` 与执行期护栏，但"这台 server 上的哪个工具能被模型调用"完全由管理员决定。能执行什么命令、带什么环境变量名仍只由运维决定。
- **文件锁只覆盖单台主机**：`fcntl.flock` 管不住跨主机共享卷上的并发写；`revision` 比对在锁内，单机多 worker 也安全。
- **探测按需 spawn 运维配置的命令**：管理员点一次就启动一次（per_run）或复用 / 新开一个 shared 进程；同台同时只一个探测，没有频率限制。探测会像 run 一样往运行时 MCP 绑定登记里追加条目（只增不删）。
- **探测结果存进程内**：重启后列表里的"最近一次探测"为空；多 worker 各一份。
- **层文件随备份流转**：里面只有启停、工具名与 `updated_by`（user_id），没有密钥；但恢复备份会把面板改动一起恢复。
- **孤立条目**：运维删掉一台 server 后条目被忽略；同名 server 将来重新出现在覆盖层时，旧的面板改动会重新生效（列表接口会显示来源为 Web 管理层，可清理）。
- **竞态**：连接前判定之后、spawn 之前槽位被清掉，`child_env` 报 `MCP_CREDENTIAL_MISSING`；在连接池的指纹计算里发生时，这个 run 的装配失败（窗口极小，不带空 token 启动）。
- **导入**：`args` 原样进提案（常含令牌，提案里有提示）；HTTP 上限 16 000 字符（§0.2）。
- **浏览器验收没做完**：§2.6 只覆盖面板；"看到工具卡片 / 停用后消失"在 PG 栈 + 真实模型上未验证（P7-S6-A11）。
- 真实 CAD/BIM、企业库 MCP server 仍未验证（P4 Step 26 blocked），本步不改变这一点。
