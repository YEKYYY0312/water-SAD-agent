# P7-S9 执行 Prompt：Skill 库（Step F）

> **工作区**：仓库 `YEKYYY0312/water-SAD-agent`，基于最新 `main`（必须已包含 P7-S6 与本 Prompt）。新建分支 `YEK/p7-s9-skill-library`。
> **必读**：
> - `AGENTS.md`
> - `docs/design/16-p7-plugin-mcp.md`：§1（插件不是权限主体）、§3.1–3.3 插件契约与内置顺序、§3.6 按意图暴露、§3.8 Web 扩展面板、§4 不变量、§5 S9 约束、§7 的 P7-S9 验收、§8 实施协议
> - `docs/design/p2.5-threat-model.md` §2（信任边界）、§3 T1 / T2 / T11、§5 残余风险——本步会**扩大**可承载指令的来源（见 §0），必须在这里登记
> - `docs/design/p7-step-4-decision.md`（两项预算、整组裁决）、`docs/design/p7-step-6-decision.md`（Web 管理层、面板只读 / 只写边界）
> - 参考：本机 dsh 的 `@deepseek-ai/dsh-tool-skill` / `dsh-skill-filesystem` / `dsh-client-ui-skill`（`~/.dsh/profiles/node_modules/@deepseek-ai/` 下的 README）——目录 + `skill(name)` 工具 + `SKILL.md` frontmatter 的做法；**照搬目录格式与加载语义，不照搬"扫描项目目录与用户目录"**（dsh 是单机桌面，我们是多用户服务）

---

## 0. 相对规格的修订与本步决定（以本文为准）

> **2026-10-09 修订（开工后读码发现，用户确认）**：① `tests/test_tool_policy.py::TestToolPolicy::test_every_inventoried_tool_capability_is_granted_locally` 要求每个登记工具的能力都在 `DEFAULT_LOCAL_CAPABILITIES` 里——原写法"`LOAD_SKILL` 由插件授予、不进默认集"与它冲突；用户决定 **`LOAD_SKILL` 进默认能力集与 Web 成员能力集**，代价是现有快照里每个 profile 的主体能力集多一项（**只有**主体能力变化，工具 / 指引 / 暴露 / 系统提示仍逐字不变），`GRANTABLE_CAPABILITIES` 与 P7-I06 不改；② 补上已确认的特性开关 `WATER_AGENT_SKILLS=off|on`（代码缺省 `off`，Web compose 设 `on`）；③ 范围块补 `tests/test_tool_policy.py` 等遗漏文件、冻结范围改为"除主体能力外全部冻结"；⑤ 冻结范围里的 `/profiles/*/…` 会把本步必须新增的 `session_skills_builtin` 也算成"被改动"，改为按名字模式只冻结现有 15 个 profile（`session_[!s]*` 与 `runner_*`；现有会话侧 profile 没有以 `session_s` 开头的）。④ `skills/loader.py` 直接 `import yaml`，PyYAML 此前只是 langchain-core 的传递依赖，须在 `pyproject.toml` 显式声明并同步 `uv.lock`（已在 allow）。下表与后文已按修订改写。

S9 是 2026-10-09 新增的一步（S8 Streamable HTTP 仍未开工，编号不复用）。用户的两个决定：**skill 正文要能当指令用**、**来源分两级（仓库内置 + 运维目录）**；首批写 3–5 个领域 skill。下面是读码后定下的做法，规格在本 Prompt 的同一个 PR 里同步：

| 疑问 | 本步做法 | 原因 |
|---|---|---|
| skill 是什么 | 一份带 frontmatter 的 Markdown 操作规程：`<root>/<name>/SKILL.md`（单层目录，不递归），frontmatter 只认 `name`（kebab-case，与目录名相同）、`description`（≤ 300 字，进目录）、`version`、`triggers`（关键词列表，供暴露策略）、`requires_tools`（正文里点名要用的工具名，装配期核对存在）；正文 ≤ 6 000 字。未知 frontmatter 键 / 坏 YAML / 名字不匹配 → 这一个 skill 记 `SKILL_INVALID` 不加载（其余照常），**不**静默改名 | 与 dsh / Claude Code 的 `SKILL.md` 同形，方便以后搬现成 skill；限长是为了一次加载不挤掉检索条文 |
| skill 能不能改变模型行为 | **能**：`skill` 工具返回的正文以 `SourceTag.SYSTEM_POLICY` 进上下文（工具结果里带 `<skill_content name="…" source="builtin\|operator">` 包裹）。`INSTRUCTION_BEARING_SOURCES` **不改**（仍只有 `SYSTEM_POLICY`）——skill 不是新的信任级别，而是"系统策略的一个按需加载分片" | 用户决定 skill 要当指令用；把它定义成新来源会改动受保护的信任偏序，定义成 `SYSTEM_POLICY` 分片则信任模型一行不改，扩大的只是"谁能写系统策略" |
| 谁能写系统策略（信任扩张） | 两级来源，**只有这两级**：① **内置**：`src/water_agent/skills/builtin/<name>/SKILL.md`，随包分发、经代码评审合入；② **运维**：部署覆盖层顶层新键不行（覆盖层只认 `version` / `servers`），改为环境变量 `WATER_AGENT_SKILLS_DIR` 指向的一个只读目录（compose 里挂只读卷），运维对它负责。**不扫描**项目目录、用户主目录、上传文件、MCP 资源；**面板不能新增或编辑 skill**；导入 skill 不做 | 运维本来就能改镜像、改覆盖层里的 `command`，给他写系统策略不是新的权限；但这是 T1 防线的一次显式放宽（运维目录里的文字会被当指令），必须在威胁模型里登记，并配下面的装配期安检 |
| 运维 skill 的安检 | 装配期逐个检查：描述与正文过 `detect_injection`（命中即 `SKILL_UNSAFE`，不加载，**不脱敏放行**）；正文不得含三条铁律的否定式改写（"不必调用 calc_velocity""可以不引用"等，一条固定的确定性正则表 `SKILL_FORBIDDEN_DIRECTIVES`，命中即 `SKILL_CONFLICTS_IRON_RULE`）；`requires_tools` 里的工具当前装配没有 → `SKILL_TOOL_UNAVAILABLE`（不加载：一份要求调用不存在工具的规程会诱导模型编造调用）。内置 skill 同样过这三道检查（由测试保证入库时就过） | 运维目录是新增的指令来源；规则集是确定性正则，**必然有未覆盖的改写变体**——这一点写进威胁模型残余风险，不得声称"能拦住任意越权 skill" |
| 同名 | 内置与运维同名时**内置优先**，运维那份记 `SKILL_SHADOWED`；运维目录内重名不可能（目录名即 name） | 运维不能用同名 skill 替换经评审的内置规程 |
| 特性开关 | `WATER_AGENT_SKILLS=off\|on`（`skills/library.py::resolve_skills_switch`，空值即 `off`，其他值装配期 `ValueError`）。`off`：不读任何 skill 目录、`skills` 插件 skipped、提示与 S7 逐字相同（除主体能力集多 `load_skill`）；`on`：读内置 + 运维目录。代码缺省 `off`（CLI、评测 runner、现有快照场景不变），`compose.yml` 给 Web 设 `on`；`skill_p0` 与新增快照场景显式设 `on` | AGENTS.md「未完成功能用特性开关护住」；内置 skill 随包分发，没有开关时每个装配都会多出 `load_skill`，快照与 4497 全变 |
| 模型怎么看到 skill | **不进系统提示的静态段**（`role_and_principles` 等受预算断言保护，系统提示 < 1950 锁死）。新增一个 `skills` 插件，贡献一个工具 `load_skill(name)`；工具的 `description` 里就是目录（每个已启用 skill 一行 `name：description 首句`），`name` 参数是这些名字的 `enum`。目录随工具 schema 一起发出、一起被暴露策略裁决 | 静态段与系统提示总量都有受保护的上限；把目录塞进工具描述，就由 S4 的两项预算统一管住，不另开一个预算口子 |
| 暴露策略 | `skills` 组是 **intent**：本轮问题命中任一已启用 skill 的 `triggers`，或出现"按规程 / 按流程 / 用 skill / load_skill"这类词，才开放；上一轮调用过 `load_skill` 则 sticky。**不是 core**（core 已用掉 2125 字符，再加目录可能让最紧的"读图 + 材料"组合撞 4500）。放不下就整组让位、记 `dropped`，与 MCP 同规则 | 4500 只剩 3 字符余量，`< 4500` 断言不得上调；skill 不是铁律，漏开的代价是"没按规程答"，而不是"心算数值" |
| 目录大小 | `load_skill` 的完整 schema（含目录）装配期测量，**超过 900 字符即装配期把多出的 skill 按名字字母序截掉并记 `SKILL_CATALOG_FULL`**（不是运行期静默截断）；指引行只占一行（"load_skill：按名加载操作规程。"） | 900 让"core + skills"≤ 3025、"core + 读图 + skills"≤ 3981，均在 4500 内；"读图 + 材料 + skills"同时命中时 skills 让位（与 MCP 同），在评测里如实记 dropped |
| skill 正文怎么进上下文 | `load_skill` 是普通 `ToolSpec`（`Capability.LOAD_SKILL`，新的只读能力，**进** `DEFAULT_LOCAL_CAPABILITIES` 与 Web 成员能力集——与检索、计算同属本地只读能力，`test_every_inventoried_tool_capability_is_granted_locally` 原样成立；`GRANTABLE_CAPABILITIES` 仍只有 `MCP_INVOKE`，P7-I06 不改。能力在默认集里不等于工具在：开关关闭或没有已启用 skill 时 `skills` 插件 skipped、不注册工具），返回 `{"name", "source", "version", "content"}`；`agent_rag` 在**提示装配**时把本 thread 已加载的 skill 正文作为独立段 `skill_context` 放在工具指引之后（`SEGMENT_BUDGETS["skill_context"] = 6000`，超出按 skill 整份丢弃最早加载的、并在段尾写明省略了哪几个），工具结果本身只留一行回执（"已加载 skill X，规程已进入系统提示"），避免正文在历史里重复 | 正文要以 `SYSTEM_POLICY` 身份生效，就必须进系统提示而不是留在 `ToolMessage`（工具结果是 `TOOL_OUTPUT` 数据）；`skill_context` 是动态段，不碰受保护的静态段 |
| 加载状态存哪 | state 新键 `loaded_skills: list[{name, source, version, digest}]`（跨轮累积、不在 `PER_TURN_RESET`；同名再次加载去重）；每轮装配时按 `name` + `digest` 从当前 skill 库取正文，**库里已没有或 digest 变了**就不注入并记 warning `skill_unavailable`（不复用 checkpoint 里的旧正文——正文不进 checkpoint） | 正文不进 checkpoint：运维删掉一个有问题的 skill，下一轮就不再生效；checkpoint 里只存名字与摘要 |
| 缓存 | `CacheKeyFactors` 新增 `skill_library_digest`（全部已启用 skill 的 name + digest 的摘要；没有 skill 时为空串，与历史键同键）。**不**按"本轮加载了哪些"进键（lookup 在加载之前，同 S2.5 规范版本摘要的教训） | skill 是系统策略的一部分：规程变了，旧回答不该再命中；`CacheKeyFactors` 是 `extra="forbid"`，新增失效维度必须改契约（`TestInvalidationFactors` 加一条） |
| 面板 | 设置页新增只读"Skill 库"区（**所有已登录用户可见**——这是能力说明，不是部署细节）：名称、来源（内置 / 运维）、版本、描述、`triggers`、`requires_tools`、状态（已启用 / 各错误码）。管理员另可按 skill **停用 / 启用**（写进 S6 的 Web 管理层，新顶层键 `skills: [{name, disabled}]`，与 `servers` 同一份文件、同一套 revision / 文件锁 / 整份校验）；**面板不能新增、编辑、上传 skill，不显示正文**（管理员可看正文：`GET /api/admin/skills/{name}`，只读） | "查看有哪些 skill"是用户的原始需求；停用是运维出问题时的止血开关，与 MCP 的启停同一机制 |
| MCP 一并列出 | Skill 库区下方复用能力端点的计数展示 MCP（启用 / 停用 / 需确认 / 共享），管理员点击跳到 MCP 扩展区；**不**新增 MCP 列表接口 | 用户说的是"skill 和 MCP 的库"；MCP 的详细清单已在 S6 管理员面板，成员不该看到 server 清单（S6 决定） |
| 首批内容 | 内置 4 个领域 skill（见 §1 第 7 条），每个都必须：只用现有工具、不含任何数值限值（数值只来自检索条文与计算工具）、不含规范号（规范号只来自检索结果）、写明"缺参就问、检索不到就说" | 铁律①②：规程只能规定"怎么做"，不能替条文与计算给出"是多少" |
| 评测 | 新增 `skill_p0`（≥ 24 条合成用例，Step 1 冻结）：经生产 `AgenticSession` + 脚本模型跑，断言 ① 目录与暴露（该开的开了、不该开的没开、预算内）② `load_skill` 之后 `skill_context` 段里确实是那份正文、来源标记正确 ③ 停用 / 运维不安全 / 同名遮蔽 / 正文被删时如实不注入 ④ 加载 skill 之后数值仍只来自 `calc_velocity`、引用仍过校验器（规程不能绕过铁律）。接进 `ci_gate.sh` 与入仓基线对比 | 评测先行：先冻结数据集，再写机制。**不证明真实模型会按规程做**（无 Key 时为 blocked，写进未覆盖项） |

## 1. 目标与边界

**做**（编号对应规格 §7 的 P7-S9-Axx）：

1. **Skill 格式与加载（A01）**：新包 `src/water_agent/skills/`：`models.py`（`SkillManifest` / `Skill`，pydantic `extra="forbid"`）、`loader.py`（解析 `SKILL.md`：frontmatter 用 PyYAML `safe_load`，已在锁文件里，不新增依赖；单层目录、不跟随符号链接逃出根目录、单文件 ≤ 64 KiB、正文 ≤ 6 000 字）、`library.py`（`SkillLibrary`：两级来源合并、同名内置优先、`digest`、错误码逐个记录、`catalog()` / `get(name)`）。新错误码集合 `SKILL_ERROR_CODES`（`SKILL_INVALID` / `SKILL_UNSAFE` / `SKILL_CONFLICTS_IRON_RULE` / `SKILL_TOOL_UNAVAILABLE` / `SKILL_SHADOWED` / `SKILL_CATALOG_FULL` / `SKILL_DISABLED`），有测试锁死不缩小。
2. **安检（A02）**：`skills/safety.py`：`detect_injection`（复用治理层，不另写）+ `SKILL_FORBIDDEN_DIRECTIVES`（铁律否定式改写的确定性正则表，带反例与正例测试）+ `requires_tools` 核对。内置 skill 有测试保证入库即过。
3. **`load_skill` 工具与插件（A03）**：`tools/load_skill.py`（`ToolSpec` 定义在 `tools/` 里，治理清单静态扫描的前提，P7-I05）；`Capability.LOAD_SKILL`（只读）进 `TOOL_CAPABILITIES`（工具计数 5 → 6，`tools_guardrail_validated` 5 → 6）、`DEFAULT_LOCAL_CAPABILITIES` 与 Web 成员能力集（`webapp/auth/dependencies.py::_MEMBER_CAPABILITIES`）；`GRANTABLE_CAPABILITIES` 不改；新插件 `plugins/builtin.py::SkillsPlugin`，`BUILTIN_PLUGIN_ORDER` 末尾追加 `"skills"`（在 `mcp_gateway` 之后：指引里排最后、预算不够最先让位）；开关 `off` 或没有任何已启用 skill 时插件 `status="skipped"`、不贡献工具（**现有快照场景开关都是 off → 只有主体能力集多一项**）。
4. **暴露（A04）**：`SkillsPlugin` 的 `ExposureRule(mode="intent")`，关键词 = 全部已启用 skill 的 `triggers` ∪ 固定词表；目录 schema 装配期 ≤ 900 字符（超出按字母序截断并记 `SKILL_CATALOG_FULL`）。
5. **正文注入（A05）**：`PromptAssembler` 新增动态段 `skill_context`（工具指引之后、记忆段之前），只接受 `SkillLibrary` 取回的正文并以 `ContentEnvelope(source_tag=SYSTEM_POLICY)` 包装（装配器只在这里构造 `SYSTEM_POLICY` 信封，其他来源仍拒绝）；`agent_rag` 新 state 键 `loaded_skills`；`load_skill` 的 `ToolMessage` 只留回执。`PROMPT_TEMPLATE_VERSION` 递进（新增分段）；`TOOL_POLICY_VERSION` 递进（新工具 / 新能力）；`RULESET_VERSION` 递进（新增 `SKILL_FORBIDDEN_DIRECTIVES`）。
6. **缓存键（A06）**：`CacheKeyFactors.skill_library_digest`（缺省 `""`）；`AnswerCacheService` 与 `AgenticSession` 传入；`TestInvalidationFactors` 加一条。
7. **首批内置 skill（A07）**：`calc-book-workflow`（计算书编写流程：先澄清已知量与缺参 → 只用 `calc_velocity` 出数 → 引用条文经 `cite_clauses` → 结论只复述工具输出）、`drawing-review-report`（转述图纸审查报告：保留"未检查项"、不把未检查写成合规、报告外的图面信息不编造）、`spec-conflict-handling`（多本规范规定不一致时：分别检索、并列引用、说明口径差异、不自行裁决取严）、`missing-input-clarification`（缺参澄清话术：列缺什么、为什么需要、不给默认值）。每份正文不含数值限值与规范号（有测试扫描）。
8. **面板与 API（A08）**：`webapp/skill_routes.py`：`GET /api/skills`（`require_password_changed`：名称 / 来源 / 版本 / 描述 / triggers / requires_tools / 状态码，不含正文、不含运维目录路径）、`GET /api/admin/skills/{name}`（`require_admin`，含正文，只读）、`PATCH /api/admin/skills/{name}`（`require_admin` + CSRF，`{"disabled": bool, "expected_revision": int}`，写进 S6 Web 管理层的新顶层键 `skills`；`web_layer.py` 的键白名单与整份校验同步扩展；停用内置 skill 也允许）。变更进结构化审计（`skill.updated`）。前端设置页"Skill 库"区（`SkillLibraryPanel.tsx` + `skillsApi.ts` + `skillsState.ts`；成员只读、管理员带启停开关与"查看正文"；下方 MCP 计数卡片复用能力端点）。
9. **部署（A09）**：`compose.yml` 给 app 设 `WATER_AGENT_SKILLS=on`、`WATER_AGENT_SKILLS_DIR=/etc/water-agent/skills` 并挂只读卷 `app_skills:/etc/water-agent/skills:ro`（运维把 skill 拷进卷；卷不在 `/var/lib/water-agent` 之下——它是部署配置不是用户数据，备份范围照旧）；`pyproject.toml` 的 `package-data` 加 `"water_agent.skills" = ["builtin/*/SKILL.md"]`（内置 skill 随包分发，镜像里不靠 `data/`）。
10. **评测（A10）**：`data/eval/skill_p0.jsonl`（Step 1 冻结）+ `harness/skill_eval.py` + `scripts/compare_skill_p0.py` + 入仓基线 `data/eval/skill_p0_main.json`；接进 `scripts/ci_gate.sh`。
11. **快照（A11）**：`PINNED_ENV_KEYS` 加 `WATER_AGENT_SKILLS` 与 `WATER_AGENT_SKILLS_DIR`；新增 profile `session_skills_builtin`（开关 `on`）。现有 15 个 profile 只允许**主体能力集**变化（`surface/principal`、`surface/registry_principal`、`lifecycle/app/chat_model_principal` 多 `load_skill`），其余 surface 与 lifecycle **逐字不变**（开关 off → `skills` 插件 skipped、新分段为空不渲染）；`/versions/` 下 `PROMPT_TEMPLATE_VERSION` / `TOOL_POLICY_VERSION` 变化（`--write --reason`）。

**不做**：

- 扫描项目目录、用户主目录、上传文件或 MCP 资源里的 skill；从网络安装 skill；面板新增 / 编辑 / 上传 / 导入 skill。
- skill 执行脚本、引用资源文件（dsh 的 `resourceBase`）；skill 里写数值限值或规范号；skill 改变工具集或能力。
- 把 skill 目录放进静态提示段；上调 `< 4500` / `< 1950` / 段预算的任何受保护数字。
- 把 `skill` 定义成新的 `SourceTag`；改 `INSTRUCTION_BEARING_SOURCES`。
- CLI 的 `/skill` 命令、用户直接点名调用（只有模型经 `load_skill` 加载）。
- Streamable HTTP（S8）、entry point 发现。

## 2. 铁律与治理落点（不得违反）

- **铁律①②不被规程绕过**：skill 正文不得给出数值限值或规范号；加载 skill 后数值仍只来自计算工具、引用仍经 `cite_clauses` + 校验器（`skill_p0` 第 ④ 类用例锁住）。
- **信任偏序不改**：`INSTRUCTION_BEARING_SOURCES == {SYSTEM_POLICY}` 原样保留；skill 正文是 `SYSTEM_POLICY` 分片，只能来自两级来源，装配器只在 `skill_context` 一处构造 `SYSTEM_POLICY` 信封。
- **信任扩张必须可见**：运维目录成为新的指令来源，威胁模型登记为 T12（含残余风险："确定性正则必然有未覆盖的改写变体"）；不得声称 skill 安检能拦住任意越权规程。
- **坏 skill 不拖垮装配**：单个 skill 出错只记错误码、不加载，其余照常；但 `WATER_AGENT_SKILLS_DIR` 设了却不存在 / 不可读 → 装配期响亮失败（与覆盖层同语义）。
- **预算不上调**：skills 组按 S4 规则整组裁决；`< 4500`、`< 1950`、`SEGMENT_BUDGETS` 既有项不改。
- **评测先行**：`skill_p0` 在 Step 1 冻结；每条验收先写失败测试（docstring 写验收 ID）。

## 3. 必须复用的现有组件（不得另写一份）

| 需要 | 用这个 | 位置 |
|---|---|---|
| 注入检测 | `detect_injection` | `runtime/governance/rules.py` |
| 来源信封 | `ContentEnvelope` / `SourceTag.SYSTEM_POLICY` | `runtime/governance/contracts.py` |
| 插件与暴露 | `PluginContribution` / `ExposureRule` / `ToolExposurePolicy` | `plugins/` |
| 工具执行 | `ToolSpec` + `ToolRegistry.invoke`（护栏不变） | `tools/` |
| 能力登记 | `TOOL_CAPABILITIES` / `capability_for_tool` | `runtime/governance/inventory.py` |
| 面板写入 | `WebLayerStore.update` + `check_candidate`（扩展键白名单，不另写一份锁与原子替换） | `mcp/web_layer.py` |
| 审计 | `_emit` 风格事件 | `webapp/auth/audit.py` |
| 本轮切片 | `current_turn_messages` | `graphs/agent_rag.py` |
| 评测运行 | 生产 `AgenticSession` + `ScriptedChatModel`，`hermetic_environment` | `harness/doubles.py`、`harness/tool_assembly_snapshot.py` |
| 比较器 | 照 `compare_tool_exposure_p0.py`（拒手改、数据集摘要、PASS_TO_PASS、用例消失） | `scripts/` |

## 4. 实施步骤（一步一个 commit；每个 commit 结束时快照 `--check` 必须一致或按本节写明的变化 `--write --reason`）

### Step 1：评测集冻结（A10 前半）
`data/eval/skill_p0.jsonl`（≥ 24 条，四类各 ≥ 5 条，含"读图 + 材料 + skill 触发词"这一最紧组合至少 2 条——预期 skills 组 dropped）+ `harness/skill_eval.py`（此时机制不存在，运行结果全部失败，报告如实写失败原因）。本 commit 只加数据集与 harness，不改运行时代码。

### Step 2：格式、加载、安检（A01、A02）
`skills/` 包（不含内置 skill 内容）；`tests/test_skill_loader.py`、`tests/test_skill_safety.py`：frontmatter 白名单、名字与目录一致、单层不递归、符号链接不逃逸、长度上限、错误码逐个、同名遮蔽、`SKILL_FORBIDDEN_DIRECTIVES` 正反例、注入命中不脱敏放行、`WATER_AGENT_SKILLS_DIR` 缺失响亮失败。

### Step 3：工具、能力、插件、暴露（A03、A04）
`tools/load_skill.py`、`Capability.LOAD_SKILL`（进默认能力集与 Web 成员能力集）、`TOOL_CAPABILITIES`（5 → 6，计数断言 `tools_guardrail_validated: 5` **会变**，见 §6）、`SkillsPlugin`、`BUILTIN_PLUGIN_ORDER` 追加 `skills`（P7-I04）、开关 `WATER_AGENT_SKILLS`、目录 900 字符上限；`TOOL_POLICY_VERSION` 递进。快照：现有 15 个 profile 开关 off → 只有主体能力集与 `/versions/TOOL_POLICY_VERSION` 变化（`--write --reason`，`--diff` 贴进决策记录）。

### Step 4：正文注入与状态（A05）
`PromptAssembler` 的 `skill_context` 段、`loaded_skills` state、回执、`skill_unavailable` warning；`PROMPT_TEMPLATE_VERSION`、`RULESET_VERSION` 递进。快照：只有版本号变化（新段为空不渲染，`system_prompt` 摘要逐字不变）。

### Step 5：缓存键（A06）
`skill_library_digest`；`TestInvalidationFactors` 加一条。

### Step 6：首批内置 skill（A07）
4 份 `SKILL.md`；`tests/test_builtin_skills.py`：全部过安检、`requires_tools` 都在默认装配里、正文不含数值限值与规范号（复用 `agent_rag._NORMATIVE_MARKERS` 的规范号与带单位数值两条）；`pyproject.toml` package-data。新增快照 profile `session_skills_builtin`（`--write --reason`）。

### Step 7：面板、API、审计（A08）
`webapp/skill_routes.py`、`web_layer.py` 键白名单扩展（`skills`）、`audit.py`、前端三件套与 `web/app/tests/skills.test.mjs`；`tests/test_web_skills.py`：成员可读列表不含正文、成员 403 看正文 / 改启停、临时密码管理员 403、写接口缺 CSRF 403、revision 冲突 409、停用后下一个 run 不再出现在目录、层文件里的 `servers` 改动不受影响（S6 回归）。

### Step 8：部署（A09）
`compose.yml`、`tests/test_web_deployment.py` 补一条（只读挂载、不在备份范围）、生产 overlay 解析。

### Step 9：评测接线（A10 后半）
`scripts/compare_skill_p0.py`、入仓基线、`ci_gate.sh` 接入；`skill_p0` 硬指标全部达标。

### Step 10：收尾
规格 §2（若有新事实）、§3 新增 3.9 Skill 库、§4 新增不变量（若有）、§5 S9 行与 §7 S9 验收翻成 delivered；威胁模型新增 T12；`docs/design/p7-step-9-decision.md`；AGENTS.md 补一条并更新 P7 交付状态。

## 5. 范围块

<!-- step-scope -->
```json
{
  "step": "P7-S9",
  "allow": [
    "src/water_agent/skills/**",
    "src/water_agent/tools/load_skill.py",
    "src/water_agent/plugins/builtin.py",
    "src/water_agent/plugins/base.py",
    "src/water_agent/plugins/__init__.py",
    "src/water_agent/prompting/assembler.py",
    "src/water_agent/graphs/agent_rag.py",
    "src/water_agent/runtime/agentic.py",
    "src/water_agent/runtime/governance/contracts.py",
    "src/water_agent/runtime/governance/inventory.py",
    "src/water_agent/runtime/governance/rules.py",
    "src/water_agent/runtime/governance/cache.py",
    "src/water_agent/runtime/governance/cache_service.py",
    "src/water_agent/mcp/web_layer.py",
    "src/water_agent/harness/runner.py",
    "src/water_agent/harness/skill_eval.py",
    "src/water_agent/harness/tool_assembly_snapshot.py",
    "src/water_agent/webapp/skill_routes.py",
    "src/water_agent/webapp/app.py",
    "src/water_agent/webapp/auth/audit.py",
    "src/water_agent/webapp/auth/dependencies.py",
    "scripts/compare_skill_p0.py",
    "scripts/ci_gate.sh",
    "compose.yml",
    "pyproject.toml",
    "uv.lock",
    "data/eval/skill_p0.jsonl",
    "data/eval/skill_p0_main.json",
    "data/eval/tool_assembly_main.json",
    "web/app/src/**",
    "web/app/tests/**",
    "tests/fixtures/skills/**",
    "tests/test_skill_loader.py",
    "tests/test_skill_safety.py",
    "tests/test_load_skill_tool.py",
    "tests/test_skill_prompting.py",
    "tests/test_builtin_skills.py",
    "tests/test_skill_eval.py",
    "tests/test_web_skills.py",
    "tests/test_web_deployment.py",
    "tests/test_answer_cache.py",
    "tests/test_governance_contracts.py",
    "tests/test_tool_plugins.py",
    "tests/test_plugin_invariants.py",
    "tests/test_tool_exposure.py",
    "tests/test_mcp_web_layer.py",
    "tests/test_web_mcp_admin.py",
    "tests/test_tool_policy.py",
    "tests/test_upload_retrieval.py",
    "tests/test_dxf_upload.py",
    "tests/test_p7_known_figures.py",
    "tests/test_tool_assembly_snapshot.py",
    "docs/design/p7-step-9-decision.md",
    "docs/design/16-p7-plugin-mcp.md",
    "docs/design/p2.5-threat-model.md",
    "docs/execution-prompts/P7-S9-skill-library.md",
    "AGENTS.md"
  ],
  "forbid": [
    "src/water_agent/mcp/bridge.py",
    "src/water_agent/mcp/client.py",
    "src/water_agent/mcp/pool.py",
    "src/water_agent/mcp/config.py",
    "src/water_agent/plugins/exposure.py",
    "src/water_agent/plugins/assembly.py",
    "src/water_agent/graphs/confirmation.py",
    "src/water_agent/runtime/governance/pipeline.py",
    "src/water_agent/runtime/governance/gateway.py",
    "src/water_agent/tools/registry.py",
    "src/water_agent/tools/ds_adapters.py",
    "src/water_agent/multi_agent/**",
    "src/water_agent/storage/**",
    "alembic/**",
    "alembic.ini",
    ".github/**",
    "compose.production.yml",
    "tests/conftest.py",
    "tests/test_p7_traceability.py",
    "tests/test_check_step_scope.py",
    "tests/test_import_guard.py",
    "tests/test_tool_exposure_eval.py",
    "tests/test_confirmation_grant.py",
    "tests/test_confirmation_gate.py",
    "tests/test_web_confirmation.py",
    "data/eval/baseline.json",
    "data/eval/tool_exposure_p0.jsonl",
    "data/eval/tool_exposure_p0_main.json",
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
      "node": "tests/test_governance_contracts.py::TestInventoryCoverage::test_all_provider_sites_governed_after_step12",
      "contains": "\"provider_call_sites\": 7"
    },
    {
      "node": "tests/test_p7_known_figures.py::test_local_tool_schema_headroom_is_three_characters",
      "contains": "assert surface[\"schema_chars_total\"] == 4497"
    },
    {
      "node": "tests/test_tool_policy.py::TestConfirmationGate::test_confirmation_required_tool_blocked_at_call_time",
      "contains": "assert blocked.decision.rule_id == \"tool.confirmation_required\""
    },
    {
      "node": "tests/test_mcp_layered_config.py::test_overlay_cannot_widen_the_gate",
      "contains": "assert exc.value.code == \"MCP_CONFIG_INVALID\", bad"
    }
  ],
  "frozen_json": [
    {
      "path": "data/eval/tool_assembly_main.json",
      "pointers": [
        "/versions/MCP_CONFIG_VERSION",
        "/profiles/session_[!s]*/description",
        "/profiles/runner_*/description",
        "/profiles/session_[!s]*/side",
        "/profiles/runner_*/side",
        "/profiles/session_[!s]*/surface/tools",
        "/profiles/runner_*/surface/tools",
        "/profiles/session_[!s]*/surface/registration_order",
        "/profiles/runner_*/surface/registration_order",
        "/profiles/session_[!s]*/surface/model_tool_order",
        "/profiles/runner_*/surface/model_tool_order",
        "/profiles/session_[!s]*/surface/schema_chars_total",
        "/profiles/runner_*/surface/schema_chars_total",
        "/profiles/session_[!s]*/surface/tool_guide",
        "/profiles/runner_*/surface/tool_guide",
        "/profiles/session_[!s]*/surface/system_prompt",
        "/profiles/runner_*/surface/system_prompt",
        "/profiles/session_[!s]*/surface/exposure",
        "/profiles/runner_*/surface/exposure",
        "/profiles/session_[!s]*/lifecycle/captures",
        "/profiles/runner_*/lifecycle/captures",
        "/profiles/session_[!s]*/lifecycle/exception",
        "/profiles/runner_*/lifecycle/exception",
        "/profiles/session_[!s]*/lifecycle/mcp",
        "/profiles/runner_*/lifecycle/mcp",
        "/profiles/session_[!s]*/lifecycle/mcp_clients_alive_after_scope",
        "/profiles/runner_*/lifecycle/mcp_clients_alive_after_scope",
        "/profiles/session_[!s]*/lifecycle/stack",
        "/profiles/runner_*/lifecycle/stack",
        "/profiles/session_[!s]*/lifecycle/trace_record_calls",
        "/profiles/runner_*/lifecycle/trace_record_calls",
        "/profiles/session_[!s]*/lifecycle/app/app_kwargs",
        "/profiles/runner_*/lifecycle/app/app_kwargs",
        "/profiles/session_[!s]*/lifecycle/app/cache_service",
        "/profiles/runner_*/lifecycle/app/cache_service",
        "/profiles/session_[!s]*/lifecycle/app/checkpointer",
        "/profiles/runner_*/lifecycle/app/checkpointer",
        "/profiles/session_[!s]*/lifecycle/app/max_tool_turns",
        "/profiles/runner_*/lifecycle/app/max_tool_turns",
        "/profiles/session_[!s]*/lifecycle/app/probes",
        "/profiles/runner_*/lifecycle/app/probes",
        "/profiles/runner_mcp_config_invalid/surface",
        "/profiles/runner_mcp_config_invalid/lifecycle/app",
        "/profiles/runner_mcp_register_fault/surface",
        "/profiles/runner_mcp_register_fault/lifecycle/app",
        "/profiles/session_mcp_config_invalid/surface",
        "/profiles/session_mcp_config_invalid/lifecycle/app",
        "/profiles/session_mcp_ready_flag/surface",
        "/profiles/session_mcp_ready_flag/lifecycle/app",
        "/profiles/session_mcp_register_fault/surface",
        "/profiles/session_mcp_register_fault/lifecycle/app",
        "/profiles/session_[!s]*/lifecycle/app/tools_bound_to_prompt_registry",
        "/profiles/runner_*/lifecycle/app/tools_bound_to_prompt_registry"
      ]
    }
  ]
}
```

## 6. 受保护断言

- 预算：`assert cost < 4500`、`schema_chars_total == 4497`（5 个本地工具；`load_skill` 不进这个 profile）、系统提示 `< 1950`（新分段为空时不渲染，现有 profile 逐字不变）。
- 信任：`INSTRUCTION_BEARING_SOURCES == {SYSTEM_POLICY}`。
- 出站清单 `provider_call_sites: 7`（skill 不新增远程调用）。
- S1 / S3：确认门无凭证拒绝；覆盖层不能写 `confirmation_handler_ready` 或未知键（S9 不给覆盖层加任何键）。
- **会变的锁定数字**（本步唯一允许改的）：`test_all_provider_sites_governed_after_step12` 里的 `"tools": 5` / `"tools_guardrail_validated": 5` → 6（docstring 记一行原因）；P7-I04 的 `BUILTIN_PLUGIN_ORDER` 末尾加 `"skills"`（规格 §4 同步写明原因）；`tests/test_upload_retrieval.py::test_prompt_template_version_was_bumped_for_the_new_segment` 的版本字面量随 `PROMPT_TEMPLATE_VERSION` 递进。`tests/test_dxf_upload.py::test_tool_schema_budget_and_governance_counts_are_untouched`（P5-S50「不给模型开放新工具」）锁的是同一组覆盖率数字，`"tools"` / `"tools_guardrail_validated"` 同样 5 → 6，`provider_call_sites` 不变。`GRANTABLE_CAPABILITIES` / P7-I06 **不改**。

## 7. 停止条件

满足任一条就停下，写清现象后等确认：

- 现有 15 个 profile 除主体能力集多 `load_skill` 外有任何变化（尤其 `tools`、`registration_order`、`system_prompt`、`tool_guide`、`exposure`、`schema_chars_total`）；
- `load_skill` 加目录后 900 字符装不下 4 个内置 skill 的目录（说明描述写太长，先改描述，不得上调 900 或 4500）；
- 需要改 `plugins/exposure.py` / `plugins/assembly.py` / `tools/registry.py` / 治理层的 `pipeline.py` / `gateway.py`，或需要数据库迁移；
- 需要新增 `SourceTag` 或改 `INSTRUCTION_BEARING_SOURCES` 才能让 skill 正文生效；
- `tool_exposure_p0` 的任何用例回退（S9 不改它的数据集与基线：现有用例都没有 skill 目录，`skills` 插件 skipped）；
- 需要放宽任何一条受保护断言；
- 门禁出现与本步无关的红灯。

## 8. 验证命令（完成前全部跑过，贴出输出）

```bash
export VENV=/Users/yaerken/Desktop/给排水agent/_venv PYTHONPATH=src
unset WATER_AGENT_MCP_OVERLAY WATER_AGENT_MCP_CONFIG WATER_AGENT_MCP_WEB_LAYER WATER_AGENT_TOOL_EXPOSURE WATER_AGENT_SKILLS WATER_AGENT_SKILLS_DIR
git fetch origin main
$VENV/bin/pytest -q tests/test_skill_loader.py tests/test_skill_safety.py tests/test_load_skill_tool.py tests/test_skill_prompting.py tests/test_builtin_skills.py tests/test_skill_eval.py tests/test_web_skills.py tests/test_answer_cache.py tests/test_governance_contracts.py tests/test_tool_plugins.py tests/test_plugin_invariants.py tests/test_tool_exposure.py tests/test_tool_exposure_eval.py tests/test_mcp_web_layer.py tests/test_web_mcp_admin.py tests/test_p7_known_figures.py tests/test_tool_assembly_snapshot.py tests/test_p7_traceability.py
$VENV/bin/python -m water_agent.harness.skill_eval data/eval/skill_p0.jsonl --report-path data/eval/reports/skill_p0_head.json
$VENV/bin/python scripts/compare_skill_p0.py --head data/eval/reports/skill_p0_head.json --base data/eval/skill_p0_main.json
$VENV/bin/python -m water_agent.harness.tool_exposure_eval data/eval/tool_exposure_p0.jsonl --report-path data/eval/reports/tool_exposure_p0_head.json
$VENV/bin/python scripts/compare_tool_exposure_p0.py --head data/eval/reports/tool_exposure_p0_head.json --base data/eval/tool_exposure_p0_main.json
(cd web/app && npm test && npm run build)
$VENV/bin/python -m water_agent.harness.tool_assembly_snapshot --check
$VENV/bin/python scripts/check_step_scope.py docs/execution-prompts/P7-S9-skill-library.md --base origin/main
docker compose -f compose.yml -f compose.production.yml config -q
COMPOSE_PROJECT_NAME=p7s9 VENV=$VENV bash scripts/ci_gate.sh
```

手动验收（有条件时做，没有就在决策记录里写明未做）：本机 Web 栈里，成员账号在设置页看到 4 个内置 skill；管理员停用 `calc-book-workflow` 后成员列表显示已停用；问一个计算书问题，trace 里看到 `load_skill` 调用、系统提示里出现 `skill_context` 段，回答里的数值来自 `calc_velocity`。

## 9. 完成定义

- 满足规格 §8 第 5 条的全部项；
- PR 上的 `p7-scope` 与 `p5-ci` 通过；
- 决策记录贴出：信任扩张的威胁模型条目、目录 schema 实测字符数与四种组合的暴露结果、`skill_p0` 指标、快照 `--diff` 原文、加载 skill 后铁律仍成立的用例输出。

## 10. 汇报格式

- 改了哪些文件；
- 验证命令与实际输出（失败照贴）；
- 范围守卫的输出；
- 快照差异；
- 未验证项（含：真实模型是否按规程作答——无 Key 即 blocked；手动验收是否做了）；
- 残余风险（至少包括：运维目录是新的指令来源、确定性正则拦不住所有越权改写、skill 正文变更即时生效且只有停用一个止血开关、目录让位时模型不知道有这个规程）。

## 11. 常见坑（本仓库实测过）

- worktree 里不设 `PYTHONPATH=src` 时 pytest 被 H0 拦下（exit 4）；web/app 新 worktree 先 `npm ci`（走代理）再 `npm run build`。
- 本机设了 `WATER_AGENT_MCP_*`、`WATER_AGENT_TOOL_EXPOSURE`、`WATER_AGENT_SKILLS`、`WATER_AGENT_SKILLS_DIR` 时先 unset 再跑测试和快照。
- 动态段为空时必须返回空串（不渲染标题）：S48 上传段实测过"空段仍渲染标题"会让系统提示摘要变化。
- 规则 / 测试里的零宽字符只能用 `\uXXXX` 转义。
- 运行时 MCP 绑定登记只增不删；skill 库没有全局登记，测试里每个用例自建 `SkillLibrary`。
- JSON 请求体最坏每字符 12 字节：`tests/test_upload_body_limit.py` 要求消息请求是最大的合法请求体（S6 实测踩过），PATCH 请求体只有布尔与整数，不受影响；不要给 API 加长文本字段。
- 提交前单独确认测试退出码为 0，不要把 `git commit` 串在测试命令后面。
- 只含执行 Prompt 的文档 PR，分支名不能以 `YEK/p7-s<n>-` 开头。
