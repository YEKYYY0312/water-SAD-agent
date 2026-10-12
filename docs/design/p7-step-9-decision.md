# P7-S9 决策记录：Skill 库（Step F）

**日期**：2026-10-12
**状态**：已完成（真实模型的加载与遵循 P7-S9-A13 blocked，前端由 node 测试覆盖 P7-S9-A12，见 §5）
**规格来源**：`docs/design/16-p7-plugin-mcp.md` §3.9、§5 S9、§7 P7-S9；执行 Prompt `docs/execution-prompts/P7-S9-skill-library.md`（其 §0 的决定以 Prompt 为准）；威胁模型 T12
**基线**：main `4a73edc`（S9 执行 Prompt 经 PR #36 合入、修订经 PR #37 合入之后）；范围块的 `frozen_json` 指针修正为 PR #38

---

## 0. 偏离与修订

### 0.1 执行 Prompt §0 的决定（照做）

| 决定 | 落点 |
|---|---|
| skill = `<root>/<name>/SKILL.md`，frontmatter 白名单、kebab-case 名字与目录一致、长度上限、单层目录 | `skills/models.py::SkillManifest`（`extra="forbid"`）、`skills/loader.py::parse_skill` / `scan_root` |
| 正文以 `SYSTEM_POLICY` 分片进系统提示；`INSTRUCTION_BEARING_SOURCES` 不改 | `skills/context.py::_block`（装配器只在这里构造 `SYSTEM_POLICY` 信封）；`prompting/assembler.py::_build_skill_context` |
| 只有两级来源：随包内置 + 运维只读目录 `WATER_AGENT_SKILLS_DIR`；同名内置优先 | `skills/library.py::build_skill_library`（`SKILL_SHADOWED`） |
| 装配期安检：注入检测（不脱敏放行）→ 铁律否定式改写 → `requires_tools` | `skills/safety.py::check_skill` + `SKILL_FORBIDDEN_DIRECTIVES`（5 条） |
| 特性开关 `WATER_AGENT_SKILLS=off\|on`，代码缺省 off，Web compose 设 on | `skills/library.py::resolve_skills_switch`；`compose.yml` |
| 目录在 `load_skill` 工具描述里（≤ 900 字符，超出按字母序截断记 `SKILL_CATALOG_FULL`），intent 暴露、sticky，最紧组合下整组让位 | `tools/load_skill.py::fit_catalog`；`plugins/builtin.py::SkillsPlugin` |
| `LOAD_SKILL` 进默认能力集与 Web 成员能力集（PR #37 修订），`GRANTABLE_CAPABILITIES` 不改 | `runtime/governance/contracts.py`、`webapp/auth/dependencies.py`、`runtime/governance/inventory.py` |
| state 键 `loaded_skills` 只存名字 / 来源 / 版本 / 摘要；正文不进 checkpoint；被删 / 改 / 停用即不注入并记 `skill_unavailable` | `graphs/agent_rag.py::_project_loaded_skills`；`skills/context.py::resolve_loaded` |
| 缓存键新增 `skill_library_digest`（全量库，不是本轮加载集合），没有 skill 时与历史键同键 | `runtime/governance/cache.py`、`cache_service.py` |
| 设置页 Skill 库区（所有登录用户可见）；管理员启停（写进 S6 Web 管理层新顶层键 `skills`）与查看正文；不能新增 / 编辑 / 上传 | `webapp/skill_routes.py`；`mcp/web_layer.py::with_skill_toggle`；`web/app/src/SkillLibraryPanel.tsx` |
| MCP 只在 Skill 库区下方显示能力端点的计数，不新增 MCP 列表接口 | `skillsState.mcpCountRows` |
| 首批 4 个内置 skill，只用现有工具、不含数值限值与规范号 | `skills/builtin/{calc-book-workflow,drawing-review-report,spec-conflict-handling,missing-input-clarification}` |
| `skill_p0` 先冻结再写机制；接进 `ci_gate.sh` 与入仓基线对比 | `data/eval/skill_p0.jsonl`（`e80b1b6`）；`harness/skill_eval.py`；`scripts/compare_skill_p0.py` |
| 快照钉住两个新变量；现有 15 个 profile 只有主体能力集变化；新增 `session_skills_builtin` | `harness/tool_assembly_snapshot.PINNED_ENV_KEYS`；§3 |
| 运维目录是信任扩张，登记 T12 | `docs/design/p2.5-threat-model.md` |

### 0.2 实测后追加的偏离

| 执行 Prompt 原文 | 本步做法 | 原因 |
|---|---|---|
| 原 Prompt：`LOAD_SKILL` 由插件授予 | PR #37 改为进默认能力集与 Web 成员能力集；快照里现有 15 个 profile 的主体能力集都多一项 | 原写法与 `test_every_inventoried_tool_capability_is_granted_locally` 冲突（清单里每个工具的能力都必须在本地默认集里）；用户选择改设计、重写快照（2026-10-09） |
| 范围块 `frozen_json` 指针 `/profiles/*/X` | PR #38 拆成 `/profiles/session_[!s]*/X` 与 `/profiles/runner_*/X`（53 个指针） | 原通配也匹配新增的 `session_skills_builtin`，它在基线里本来就不存在，冻结它等于禁止新增 profile |
| §6「会变的锁定数字」只列 `test_governance_contracts` 的覆盖率断言 | PR #38 追加：`tests/test_dxf_upload.py::test_tool_schema_budget_and_governance_counts_are_untouched` 同样 5 → 6（范围块 `allow` 补上）；任务书与 S1–S7 执行 Prompt 的 `protect` 去掉 `"tools_guardrail_validated": 5` | 首次全量门禁红在这两处：P5-S50 锁的是同一组覆盖率数字；`test_repo_scope_blocks_are_usable`（P7-S0-A31）要求**所有** P7 文档的 protect 在当前代码上完好，S9 合入后旧文档的这条锁必红。同一节点的 `"provider_call_sites": 7` 在每份文档里都保留 |
| PyYAML "已在锁文件里，不新增依赖" | `pyproject.toml` 显式声明 `pyyaml>=6.0,<7`，`uv lock --offline` 更新 `uv.lock`（PR #37 把 `uv.lock` 加进范围块）；`import yaml  # type: ignore[import-untyped]` | 它原本只是传递依赖（来自 langchain），直接 import 一个传递依赖会在上游换掉它时静默断裂；mypy 下没有 stub |
| `load_skill(name)` | 参数名是 `skill`；冻结数据集 `skill_p0.jsonl` 里 20 处脚本调用的参数名随之由 `name` 改成 `skill`（在 Step 3 的 commit 里，数据集摘要随之变化，基线在 Step 9 才首次固化） | `ToolRegistry.invoke(name, **args)` 的位置参数就叫 `name`：`invoke("load_skill", name=...)` 直接 `TypeError`。改工具参数名比改 `ToolRegistry`（`forbid`）代价小；数据集的**期望**一条没改，只改了脚本模型发出的参数名 |
| 参数 `name` 是目录名 `enum` | `Literal[...]` 动态构造（`Literal.__getitem__(tuple(names))`），pydantic 生成的 JSON schema 是 `enum`；目录为空时插件 skipped、不构造 | 让 schema 校验在 `ToolRegistry.invoke` 里拦下不存在的名字（`SKILL_NOT_FOUND` 只剩竞态兜底） |
| `load_skill` 返回 `{"name", "source", "version", "content"}` | 返回回执 `{ok, name, source, version, digest, message}`，**不含正文** | Prompt 同一行又要求"工具结果只留一行回执，避免正文在历史里重复"；正文进 `ToolMessage` 就进了 checkpoint，与"正文不进 checkpoint"矛盾。`digest` 用来在下一轮核对库里那份是否还是同一份 |
| 指引行"load_skill：按名加载操作规程。" | 工具描述开头 `_HEAD = "按名加载操作规程。"`，其后每行 `- name：description 首句` | 指引只取描述首句（S4），`_HEAD` 短使目录在 900 字符里放得下更多条；内置 4 份目录 717 字符 |
| 固定词表"按规程 / 按流程 / 用 skill / load_skill" | `SKILL_FIXED_KEYWORDS` 另加 `按步骤`、`规程`、`流程`、`操作规程`、`skill` | `skill_p0` 0019"按计算书流程来"：`按流程` 不是它的子串，只靠被停用的那份 skill 的 triggers 才命中——停用之后这类问法就开不出目录 |
| （未提及）trace 事件 | 会话在 `start_trace` 时记 `skill_library`（只有名字、来源、错误码），`skill_unavailable` 也进 trace | `skill_p0` 的 `skill_error_match_rate` 需要从 trace 读"哪些 skill 被跳过、为什么"；不记正文 |
| （未提及）只在开关 on 时把库交给图 | `AgenticSession` / runner 只在 `library.enabled` 非空时把 `skill_library` 传给 `build_agentic_app`（放进已有的 kwargs 字典） | 开关 off 时 `build_agentic_app` 的入参与 S7 逐字相同，快照 lifecycle 不变 |
| skills 插件按"先于它的插件贡献的工具"核对 `requires_tools` | 插件构造时拿到前序插件，经 `_Remembering` 包装记下它们的贡献 | `plugins/assembly.py` 在 `forbid` 里；两阶段装配不把前序贡献传给后面的插件，只能在插件这一侧记 |
| 铁律否定式改写的正则 | 窗口收紧到 0–4 字、加"不"的反向断言 | 首版把"不自行裁决"和夹具 `leak-test-notes` 的"不代填"判成违规，又漏了"凭经验估算"；正例与反例两组测试都锁住 |
| `skill_p0` harness 按工具结果判错 | ToolNode 回填的 `Error: ...` 纯文本也算错误 | 参数不合 schema 时 ToolNode 不调我们的执行函数、直接回填 `Error:` 文本，此前判成成功（0016） |
| Web 管理层顶层键 | `skills` 只在有停用记录时写出；启用即删掉这一条 | 没有停用记录时层文件与 S6 写出的逐字相同（S6 的层文件测试不用改） |
| 面板"成员只读、管理员带开关" | 列表接口多一个 `can_manage`（`user.role is ADMIN`）；真正的判定仍是管理接口上的 `require_admin` | 前端不该靠"先请求一次管理接口看会不会 403"来猜角色——那会给每个成员打一条 403 |
| 列表里 `requires_tools` 的核对口径 | 列表按 Web 总有的 4 个本地工具 + `DRAWING_ROOT` 可用时的 `read_drawing` 核对，不计 MCP 工具 | 列表接口不装配会话；它显示的是"下一个 run 大概率看到的状态"，真正装配时仍按实际贡献的工具核对 |
| 快照已知数字 `checked == 40` | `tests/test_p7_known_figures.py` 改为 44 | 新增 profile 带 4 个探针 |

---

## 1. 交付

| commit | 产物 | 要点 |
|---|---|---|
| `3e48653`（PR #36）/ `4a73edc`（PR #37）/ PR #38 | 执行 Prompt、修订、`frozen_json` 指针修正 | §0.2 前三条 |
| `e80b1b6` | `skill_p0` 冻结（26 条）+ harness + 运维 skill 夹具（A10 前半） | 机制尚不存在时现行代码 5/26（关闭开关、寒暄与 3 条铁律用例本来就成立），报告记 `mechanism=absent` |
| `9180396` | 格式、加载、两级来源、安检、开关（A01、A02） | 单个坏 skill 只跳过；运维目录设了却不存在 → `SKILL_LIBRARY_INVALID` |
| `0f81b2e` | `load_skill` 工具、skills 插件、能力与暴露（A03、A04） | 工具数 5 → 6、P7-I04、`TOOL_POLICY_VERSION` → `p7-step9-2026-10-09`；快照只有主体能力集变化 |
| `03cda32` | `skill_context` 段与 `loaded_skills`（A05） | `PROMPT_TEMPLATE_VERSION` → `p7-s9-skill-context-2026-10-09`；开关 off 时系统提示逐字不变 |
| `49af7cf` | 缓存键 `skill_library_digest`（A06） | 空串时 `model_dump` 去掉该键，键与加字段前逐字相同（有测试） |
| `290d3d1` | 首批 4 个内置 skill、`skill_library` trace、快照新 profile（A07、A11） | 目录 717 / 900 字符 |
| `c68a43b` | 面板、API、审计、Web 管理层 `skills` 键、前端（A08、A12） | 会话与 runner 装配时读 `disabled_skill_names()` |
| `9bc8095` | 部署（A09） | `app_skills:/etc/water-agent/skills:ro`，不在备份范围 |
| `4c7c673` | `skill_p0` 接门禁 + 入仓基线（A10 后半） | `compare_skill_p0.py` 照 tool_exposure 比较器 |
| 本 commit | 规格 §2 / §3.9 / §5 / §7 / §10、威胁模型 T12、本记录、AGENTS.md、快照测试 A11 断言 | — |

---

## 2. 实测

### 2.1 先红后绿与反证（实现之后把关键一行改回去重跑）

- A01：去掉符号链接逃逸检查 → `test_skill_manifest_is_strict` 失败；同名时改成运维优先 → `test_two_sources_builtin_wins` 失败。
- A02：去掉注入检测 → `test_unsafe_skills_are_not_loaded` 失败（夹具 `unsafe-override` 被加载）；去掉任一铁律规则 → `test_iron_rule_conflicts_are_rejected` 对应参数失败；正例组锁住"不自行估算""不代填""不要编造"不被误拦。
- A04：目录不截断 → `test_catalog_fits_and_skills_group_yields_under_budget` 失败；skills 组改成 core → 同一用例失败（最紧组合下超 4500）。
- A05：去掉摘要核对 → `test_removed_or_changed_skill_is_not_injected` 失败（运维改过的正文照样注入）；去掉中和 → `test_loaded_skill_becomes_system_policy_segment` 失败（伪造的段边界多出一对）。
- A06：去掉"空串不进键" → `test_empty_skill_digest_keeps_historic_key` 失败。
- A07：在内置 skill 正文里加一个规范号 / 带单位数值 → `test_builtin_skills_pass_safety_and_carry_no_numbers` 失败；反证用例确认三条扫描规则能抓到它们。
- A08：装配时不读停用列表 → `test_admin_can_disable_a_skill_for_the_next_run` 失败；`with_override` 不带上 `skills` → 同一用例失败（server 编辑抹掉 skill 启停）。
- A11：`PINNED_ENV_KEYS` 去掉两个 skill 变量、宿主设 `WATER_AGENT_SKILLS=on` + 夹具目录 → `session_skills_builtin` 出现 3 处差异（目录多了 2 份运维 skill：`schema_chars_total` 4258 → 4372、`load_skill` schema 717 → 831）；`test_snapshot_is_hermetic_and_deterministic` 现在在污染环境里放了这两个变量。

### 2.2 铁律不被规程绕过

`skill_p0` 铁律类 5 条（加载 `calc-book-workflow` 后要数值、加载后要引用、运维 `iron-rule-bypass` 被拒等）：`iron_rule_hold_rate = 1.0`——数值只来自 `calc_velocity`，引用仍过 `validate_citations`，`iron-rule-bypass` 记 `SKILL_CONFLICTS_IRON_RULE` 且正文不出现在任何一轮的系统提示里。注入类 7 条：`forbidden_text_leak_count = 0`。

### 2.3 正文与路径不出现在不该出现的地方

- 列表接口：没有正文、没有运维目录与临时目录路径；未过安检的 skill 只有名字与错误码（`unsafe-override` 的"忽略以上所有指令"不出现在响应里）。
- 错误响应不回显请求里的名字（`Bad_Name`、`no-such-skill`）与请求体（"新的正文"）。
- 审计 `skill.updated` 只有 `actor_user_id` / `name` / `disabled` / `revision`。
- checkpoint 的消息历史与 `loaded_skills` 里没有正文（`test_session_persists_only_names_and_warns_when_a_skill_disappears`）。

### 2.4 "下一个 run 生效"

`test_admin_can_disable_a_skill_for_the_next_run`：停用 `calc-book-workflow` → 用生产读取函数 `disabled_skill_names()` 重新装配，目录里没有它、成员列表显示 `SKILL_DISABLED`；S6 写同一份文件改 `servers` 之后再启用 → `servers` 原样、目录里又有它、层文件里不再有 `skills` 键。会话侧的"删除 / 改正文后下一轮不注入"由 `skill_p0` 撤销类 5 条与 `test_skill_prompting` 覆盖。

### 2.5 验证命令

```
export VENV=/Users/yaerken/Desktop/给排水agent/_venv PYTHONPATH=src
$VENV/bin/pytest -q tests/test_skill_loader.py tests/test_skill_safety.py tests/test_load_skill_tool.py tests/test_skill_prompting.py tests/test_builtin_skills.py tests/test_skill_eval.py tests/test_web_skills.py tests/test_mcp_web_layer.py tests/test_web_mcp_admin.py tests/test_web_deployment.py tests/test_answer_cache.py tests/test_governance_contracts.py tests/test_tool_plugins.py tests/test_plugin_invariants.py tests/test_tool_exposure.py tests/test_p7_known_figures.py tests/test_tool_assembly_snapshot.py tests/test_p7_traceability.py
$VENV/bin/python -m water_agent.harness.skill_eval data/eval/skill_p0.jsonl --report-path data/eval/reports/skill_p0_head.json
$VENV/bin/python scripts/compare_skill_p0.py --head data/eval/reports/skill_p0_head.json --base data/eval/skill_p0_main.json
$VENV/bin/python -m water_agent.harness.tool_assembly_snapshot --check
(cd web/app && npm test && npm run build)
docker compose -f compose.yml -f compose.production.yml config -q
$VENV/bin/python scripts/check_step_scope.py docs/execution-prompts/P7-S9-skill-library.md --base origin/main
COMPOSE_PROJECT_NAME=p7s9 VENV=$VENV bash scripts/ci_gate.sh
```

### 2.6 实测输出（2026-10-12，本机 macOS）

| 命令 | 结果 |
|---|---|
| `skill_p0` | 26/26，十项硬指标全达标（`mechanism=present`）；报告两次运行逐字相同 |
| `compare_skill_p0.py`（HEAD vs 基线） | exit 0 |
| `tool_assembly_snapshot --check` | 装配快照与基线一致（16 个 profile） |
| `tool_exposure_p0` + 比较器 | 36/36，exit 0（S4 基线未动） |
| `npm test` / `npm run build` | 84/84 pass（本步新增 5 条）；`tsc -b` + vite 构建通过 |
| 生产 overlay 解析 | exit 0；app 的 `WATER_AGENT_SKILLS=on`、`app_skills` 只读挂载，smoke 不带这两个变量 |
| `ci_gate.sh` | 见 §2.7 |

### 2.7 全量门禁

- **第一次**（`4c7c673`）：exit 1，pytest 3 条失败，其余各步（ruff、mypy、全部机制层 runner 含 `skill_p0` 26/26 与 `tool_exposure_p0` 36/36、基线对比、memory_p0）全部通过：
  1. `tests/test_dxf_upload.py::test_tool_schema_budget_and_governance_counts_are_untouched`：`{'tools': 6} != {'tools': 5}`——P5-S50 的同一组覆盖率锁，执行 Prompt §6 漏列；
  2. `tests/test_check_step_scope.py::test_repo_scope_blocks_are_usable`：任务书与 S1–S7 执行 Prompt 的 protect 仍要求 `"tools_guardrail_validated": 5`；
  3. `tests/test_web_deployment_overlay.py::test_image_check_skips_structurally_instead_of_failing`：`'not built locally' in 'docker daemon unreachable'`——本机 Docker daemon 没起；main `1e5e086` 上同样失败，与本步无关。
  1、2 是 S9 的计数变化本身引起的，按 §0.2 第 3 行处理（PR #38 + 本 commit 改 `test_dxf_upload.py`）。
- **第二次**：见 PR 描述（贴原文）。

---

## 3. 快照变化

**Step 3**（`0f81b2e`）`--write --reason "P7-S9：LOAD_SKILL 进默认能力集与 Web 成员能力集；开关 off，skills 插件 skipped，其余不变"` 之前的 `--diff`（24 行主体能力集，原文的同构行省略为一行示意 + 计数）：

```
/profiles/<15 个 profile 中的 13 个>/surface/principal/capabilities:          [... "llm_inference", "material_stats", ...] -> [... "llm_inference", "load_skill", "material_stats", ...]
/profiles/<同上>/surface/registry_principal/capabilities:                     （同上）
/profiles/runner_drawing_env|runner_mcp_fake|runner_no_mcp|session_web_member/lifecycle/app/chat_model_principal/capabilities: （同上）
/versions/TOOL_POLICY_VERSION: "p7-step7-2026-10-07" -> "p7-step9-2026-10-09"
```

共 24 行能力集 + 1 行版本号；装配失败的 4 个 profile（`*_mcp_config_invalid` / `*_mcp_register_fault` / `session_mcp_ready_flag`）没有 surface，不变。其余 surface 与全部 lifecycle 逐字不变（范围块 `frozen_json` 冻结 53 个指针）。

**Step 4**（`03cda32`）：

```
/versions/PROMPT_TEMPLATE_VERSION: "p7-s4-intent-tool-guide-2026-10-06" -> "p7-s9-skill-context-2026-10-09"
```

**Step 6**（`290d3d1`）：

```
/profiles/session_skills_builtin: "<absent>" -> {...}
```

新 profile 的注册顺序 `retrieve_clauses, cite_clauses, calc_velocity, material_stats, load_skill`，`load_skill` schema 717 字符，`schema_chars_total` 4258。`test_snapshot_matches_baseline` 断言它是唯一注册了 `load_skill` 的 profile。之后每个 commit `--check` 均为"装配快照与基线一致（16 个 profile）"。

---

## 4. 不做

- 扫描项目 / 用户目录、上传文件、MCP 资源里的 skill；从网络安装；面板新增 / 编辑 / 上传 / 导入 skill。
- skill 执行脚本或引用资源文件；skill 改变工具集或能力；skill 里写数值限值或规范号。
- 把目录放进静态提示段；上调 `< 4500` / `< 1950` / 段预算。
- 新 `SourceTag`；改 `INSTRUCTION_BEARING_SOURCES`。
- CLI `/skill` 命令与用户直接点名调用。
- Streamable HTTP（S8）、entry point 发现。

---

## 5. 残余风险

- **安检是确定性正则**：注入检测与五条铁律规则都必然存在未覆盖的改写变体（T12，与 T1/T2 同一局限）；不得声称能拦住任意越权规程。
- **内容错误的规程检不出来**：措辞合规但步骤写错的 skill 会被当系统策略执行；这是内容审核问题，靠运维的变更流程，运维目录没有签名或来源证明。
- **管理员能停用内置 skill**：停用是止血开关，不需要运维；但被盗的管理员账号可以借此让模型"不按规程答"（铁律仍由工具与校验器保证）。
- **真实模型未验证**（P7-S9-A13 blocked）：`skill_p0` 用脚本模型，只证明"目录发出去了、加载了就进系统提示、铁律不被绕过"，不证明真实模型会在该加载时加载、加载后照做；没有 `DEEPSEEK_API_KEY` 时不得声称规程遵循率。
- **漏开目录**：skills 组是 intent，问法不命中 triggers 与固定词表时目录不开；"读图 + 材料"同时命中时整组让位（如实记 dropped）。代价是"没按规程答"，不是"心算数值"。
- **列表与实际装配可能不一致**：列表按 Web 本地工具核对 `requires_tools`，不计 MCP 工具；要求 MCP 工具的 skill 在列表里显示为工具不可用。
- **层文件坏即装配失败**：Web 管理层损坏时会话与 runner 都 `MCP_CONFIG_INVALID`（不能把"停用"静默当成"启用"）；恢复路径是 MCP 扩展区的"恢复为部署配置"，它同时清掉 skill 启停。
- **浏览器手动验收未做**：面板只由 node 测试与 HTTP 往返覆盖（P7-S9-A12）。
