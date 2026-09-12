# P5 威胁模型：Web 自托管与长期记忆（Step 35）

> 状态：Step 35 审计基线；本文是设计与发布前威胁清单，不是“已安全上线”证明。
> 日期：2026-09-12；工作树基线：`726f7af`（当前工作树另有用户修改）。
> 事实来源：P5 任务书、P2.5/P4/P4.5 设计文档、当前源码/测试/配置和本 Step 审计命令。

## 0. 范围、资产和当前形态

建模目标是 P5 计划中的单机自托管 Web：一套部署服务一个组织或工作室，但服务内允许多个本地账号、项目、
会话和记忆。Step 35 不实现目标运行态；当前实际运行形态仍是 CLI + harness，没有 HTTP 服务、认证、中间件、
PostgreSQL 或公开注册。

资产：

| ID | 资产 | 当前位置/证据 | 安全目标 |
| --- | --- | --- | --- |
| A1 | provider API Key、数据库密码、session signing key | `.env`/`.env.local` 为本地文件；P5 任务书 §11 要求 secret 文件 | 不进 Git、镜像、浏览器、trace、异常和模型上下文；泄漏后必须轮换 |
| A2 | 规范语料、索引和引用关系 | `data/curated/`、`data/indexes/`、`CitationClaim`、版本注册表 | 来源可追溯；版权/版本未知时不对外声称正式依据 |
| A3 | 水力计算正确性 | `src/water_agent/tools/`、P4.5 CalcPackage | 数值只能来自程序化工具；记忆和模型不能改写计算结果 |
| A4 | 项目、会话和产物 | P4.5 `ArtifactEnvelope`、checkpointer、docgen/drawing 输出 | tenant/user/project/thread 级隔离；批准哈希与发布文件一致 |
| A5 | 长期记忆正文和事件 | P5 任务书 §6；当前尚不存在 | 显式确认、最小化、可追溯、可更正、可过期、可忘记；事件不复制正文 |
| A6 | 用户 PII、上传文件和真实图纸 | 当前没有 P5 上传 API；仓库含 `knowledge/` 和本地生成数据 | 默认不进入记忆；云调用、trace、备份和开源发布前分类/授权 |
| A7 | trace、SSE 事件、运行状态和反馈 | 当前 `data/traces/`、`data/eval/traces/` 被忽略；P5 SSE 尚未实现 | 只保留安全摘要；事件重放不能跨用户或跨 run |
| A8 | 数据库、备份和删除残留 | 当前有 SQLite/索引/trace 生成物；PostgreSQL/备份尚未实现 | 最小权限、保留期、删除覆盖活动存储/索引/缓存/备份 |
| A9 | 依赖、锁文件、发布许可证 | `pyproject.toml` + `uv.lock`；当前 `LICENSE` 未跟踪且主体不明 | 可复现、漏洞可审计、许可证可核对、版权主体真实 |

## 1. 信任边界和数据流

```text
B0 服务端确定性边界（未来）
  auth/session → Principal.authenticated → resource authorization
  programmatic calc / citation validator / artifact hash / release gate
             │
             ├─ B1 浏览器请求、Cookie、Last-Event-ID、JSON、上传文件（不可信）
             ├─ B2 语料、图纸、MCP 返回、检索结果、当前/长期记忆（不可信数据）
             ├─ B3 模型输出、工具参数、SSE payload（不可信输入/需 schema 验证）
             ├─ B4 PostgreSQL/checkpoint/trace/backup（敏感持久化边界）
             └─ B5 DeepSeek/百炼等 provider（出站边界，Key 和用户数据离开本机）
```

已存在的代码边界：P2.5 以 `ProviderGateway` 收敛远程调用，以 `ToolRegistry.invoke` 收敛工具执行，以
`GuardrailPipeline` 做工具前置策略，以 `ContentEnvelope`/`SourceTag` 区分不可信内容；P4 MCP 的工具描述、
schema 和结果均当作第三方不可信输入；P4.5 编排器不是新的权限主体。尚未存在的 HTTP 认证、数据库查询授权、
SSE replay 授权和长期记忆 repository 不能写成已有控制。

## 2. 威胁清单（STRIDE + LLM）

评级仅表示按当前证据的设计优先级，不是渗透测试结论。确定性正则规则是有限规则集，不能覆盖任意注入改写。

| ID / 类别 | 攻击路径与影响 | 当前证据/已有控制 | P5 必须控制与验收证据 | 状态 |
| --- | --- | --- | --- | --- |
| TM-01 身份伪造（S/E） | 伪造 `user_id`、裸 cookie、Principal、管理员角色或模型声明的主体，读取/修改他人资源 | 当前无 HTTP 身份；`Principal.local` 只允许 `local:<thread_id>`，`authenticated()` 尚无生产调用方（P2.5 threat §6） | session 只由服务端解析；登录后构造 `Principal.authenticated()`；subject 不来自 JSON/URL/模型；密码 Argon2id、登录限速、cookie 属性；测试 cookie 篡改、伪 user ID、停用账号和角色伪造 | **blocked：Step 37** |
| TM-02 IDOR（E/I） | 猜测 `project_id/thread_id/run_id/memory_id` 访问或修改跨用户、跨项目对象；列表分页边界泄漏元数据 | 当前无 Web API；P5 任务书 §5.1/§9 要求 URL ID 仅作定位，最终要查 owner/membership | repository 查询先做 tenant + owner/membership + scope 硬过滤；单对象和列表均复核授权；越权只返回 404/403，不返回标题/正文/事件元数据；覆盖每种 ID 与分页 | **blocked：Step 37–45** |
| TM-03 CSRF（T/E） | 浏览器自动附带 session cookie，恶意站点诱导创建记忆、修改成员、取消/发布或删除 | 当前无 cookie/API；P5 §9.1 明确状态改变需 session + CSRF | unsafe method 强制 CSRF token + Origin allowlist + SameSite cookie；登录/登出/密码/删除/发布反例；前端 token 不能替代服务端校验 | **blocked：Step 37** |
| TM-04 session fixation（S/E） | 攻击者预先设置或复用登录前 session ID，在登录后接管账号 | 当前无 session 服务 | 登录成功、权限提升、密码重置轮换 session ID；旧 session 失效；logout/停用/改密撤销；测试固定 ID、并发旧 cookie、重置后旧 cookie | **blocked：Step 37** |
| TM-05 跨用户和跨项目记忆泄漏（I） | 全库 FTS/排序后再过滤、缓存键缺主体/授权域、项目切换沿用旧上下文，导致正文/score/usage/SSE 交叉泄漏 | P2.5 AnswerCache 已有 `principal_scope` 设计；P5 记忆存储尚不存在；当前 checkpointer 以 thread 为边界，不能推导 Web 用户授权 | DB 查询先过滤 tenant/user/membership/ACTIVE/project；cache key 包主体 + 授权域 + 版本；项目切换清空/重建 context；跨用户相同 key、猜 ID、缓存和 SSE gold 泄漏计数必须为 0 | **blocked：Step 38–42** |
| TM-06 记忆投毒（T/LLM01/LLM06） | 用户消息、导入或图纸/MCP 文本含“忽略规则/泄漏提示/改算结果”，被显式保存后跨会话持久污染或诱导工具 | P2.5 `check_memory_write()` 已有但未接真实长期写入；`SourceTag.SESSION_MEMORY` 不能升格；确定性护栏有红队测试但不是分类器 | 仅显式候选 + 最终确认落盘；`ContentEnvelope(USER_MESSAGE)` → 现有 guard → credential hard block → PII/kind/schema/conflict；注入正文不落盘，必要时只保留 hash/quarantine；召回独立 data 段；memory 内容不能改 capability、citation、calc 或 approval；至少覆盖直接、间接、零宽/Base64 合成变体，并如实记录规则盲区 | **blocked：Step 39–42** |
| TM-07 secret 落盘/泄漏（I） | Key/password/token 进入 `.env`、数据库、trace、SSE、日志、缓存、备份、浏览器或模型上下文；被 Git/镜像发布 | 本次安全扫描在 `.env`/`.env.local` 发现非空 DeepSeek/百炼 Key 候选；`.gitignore` 忽略 env、trace、sessions/index；无 Docker 层可扫 | 按真实秘密处理并轮换；secret 文件/`*_FILE`；写库、trace、SSE、错误和 prompt 前阻断/无条件脱敏；构建上下文和镜像层排除；工作树+历史+镜像三层扫描；secret_persisted_count 必须 0 | **当前发布阻塞；轮换待人工完成** |
| TM-08 删除残留（I/T） | “忘记”只隐藏 UI，正文仍在活动表、FTS、embedding、cache、checkpoint 投影、trace、备份或导出；后续召回复活 | 当前没有长期记忆删除路径；P2.5 trace 已有脱敏但没有 P5 retention/deletion | 事务化删除活动正文/索引/可选 embedding/cache/projection；保留无正文 tombstone/hash；历史回答不改写但标为历史；备份保留期和删除补偿任务可验证；forget_content_residual_count 必须 0 | **blocked：Step 39–45** |
| TM-09 SSE 未授权重连（I/T） | 猜 `run_id` 或 `Last-Event-ID`，重放他人事件；membership 被撤销后旧连接继续收数据；重复 finalize 造成状态误判 | 当前没有 SSE；P5 §9.2 只给出目标 envelope/Last-Event-ID 契约 | handshake 与每条 replay 均认证/授权；游标绑定 run + 主体；跨 run/跨用户不可解释；撤权后停止；单调 ID 不重复 finalize；unauthorized_sse_replay_count 必须 0；测试断连、重连、撤权、猜 ID、重复 cursor | **blocked：Step 42/45** |
| TM-10 备份泄漏（I） | 逻辑 dump、上传目录、导出、校验清单或恢复临时文件含用户记忆/图纸/Key，权限过宽或上传公开仓库 | 当前无 backup/restore；仓库有被忽略的 SQLite、trace、indexes 生成物；P5 §11.3 要求逻辑备份 + 文件清单 | 备份目录最小权限、独立 secret、加密/访问控制由部署文档明确；不把 secret 作为备份内容；restore 默认空目标或显式 force，先生成当前备份；全新卷恢复后账号/项目/活动记忆一致且忘记项不可恢复召回；备份文件和路径扫描为发布门禁 | **blocked：Step 45** |
| TM-11 Prompt/tool schema 膨胀（D/LLM06/LLM10） | 把全部 memory/MCP/Agent 工具一次性暴露，突破上下文/成本预算、扩大过度代理面，模型被不必要工具诱导 | P3 已实测 ToolSpec schema 约 4370/4500，余量约 130；P5 任务书明确 P0 不注册通用 memory 工具；P4/P4.5 都要求按意图最小暴露 | 按阶段生成最小 ToolRegistry 视图；memory P0 用 server API；schema token 硬上限和版本测试；context/历史/事件有界；危险工具仍需 capability + HITL；不得用 prompt 代替权限；测试 budget overflow、隐藏工具和未知 schema | **设计已固定；实现待 Step 42/46** |
| TM-12 真实图纸误发布（I/IP/T） | 用户上传或设计院 DXF/DWG 进入公开 repo、测试 fixture、trace、镜像、备份或示例；真实坐标/业主信息泄漏；DWG 能力被误称支持 | P3 `drawing_p0` 明确是合成 smoke，真实样本/召回 blocked；`.dwg` 直读 blocked；本仓扫描有本地生成 DB/trace | 默认只发布合成图纸和脱敏 fixture；真实文件存储/备份不进 Git/镜像/trace；P3 能力状态原样展示；发布前逐文件分类和版权/隐私确认；真实样本没有授权时保持 blocked | **当前发布边界阻塞** |
| TM-13 规范文本误发布/未授权引用（I/IP/T） | `knowledge/`、`data/curated/`、设计文档或导出的响应包含未获授权的规范全文、旧版本或不可追溯条款，公开后造成版权和工程误导 | `data/versions.json` 四本规范为 unknown；P2.5/P3 明确正式引用和部分条文支撑 blocked；`web` 原型条文是占位 | 发布清单只允许授权、可追溯、必要最小片段；版本状态/来源同时核实；正式引用沿用 `validate_citations`；未授权全文不入库/镜像/fixture/trace；宣传和 README 必须区分占位、合成和真实证据 | **当前 blocked** |
| TM-14 用户数据误发布（I/IP） | 会话、反馈、记忆、姓名/单位/地址、上传文件和模型输出被放入 eval、README、issue、截图、备份或公开日志 | 当前没有 Web 用户数据模型；P2.5 已要求 trace 脱敏，但 P5 retention/DSAR 仍未实现；仓库有本地生成数据 | 发布数据 allowlist；只发布合成数据和脱敏哈希；PII 默认不进 P0 记忆；日志/trace/SSE 不带原文；未来数据导出/删除覆盖数据库、索引、缓存、备份；发布前人工抽查和路径/secret/PII 扫描 | **blocked：Step 39–45** |

## 3. 风险优先级和处理策略

高优先级为 TM-01/02/03/04/05/07/08/09/10/12/13/14：它们都涉及认证、跨主体授权、持久化删除、秘密或
公开发布，一旦 P5 暴露 HTTP 就可达。TM-06 和 TM-11 是 LLM 特有的持久污染和资源/权限扩大风险；它们不能
只靠系统提示或正则解决，必须与数据库隔离、类型契约、最小工具暴露、人工确认、程序化计算和产物校验组合。

本项目当前最重要的事实不是“护栏存在”，而是哪些边界还没有实现：没有 HTTP 认证就没有 Web 授权证据；没有
长期记忆 repository 就没有删除证据；没有 SSE 服务就没有重连授权证据；没有镜像就不能完成层扫描。所有这些
在后续 Step 形成闭环前保持 `blocked`，不以设计文档替代测试。

## 4. 已有控制、残余风险与交接

已由当前代码/测试证明的控制：

- 计算走程序化工具，模型不参与数值运算；
- `ProviderGateway` 是远程调用单一入口，治理清单有静态覆盖检查；
- `ToolRegistry.invoke` 是工具执行单一入口，护栏先于 schema/execute；
- `ContentEnvelope`/`SourceTag` 规定检索、工具、图纸和记忆是数据，不得升格为系统指令；
- P4 MCP 对 allowlist、schema 子集、描述注入、环境变量透传和危险确认做 fail closed；
- AnswerCache 的主体/授权域、版本和引用失效维度已在 P2.5 机制层存在；
- `.gitignore` 排除 `.env*`、trace、sessions、indexes 和报告；但忽略不等于已从文件系统或历史删除。

残余风险：

- 当前规则是确定性正则，必然存在未覆盖变体；不能声称“能拦住任意注入”；
- `check_memory_write()` 尚未接入长期记忆写入路径；
- `Principal.authenticated()` 尚无 Web 调用方；
- 多实例共享限流/SSE 分发、真实备份加密、保留期、合规义务和第三方 provider 数据留存均 `unknown`；
- `.env`/`.env.local` 仍有非空 Key 候选，必须轮换；当前许可证与开源文件来源不明；
- 真实图纸、正式规范引用、真实 MCP/CAD/BIM 和真实 RAG 质量均保持既有 blocked 状态。

交接要求：Step 36–38 先形成服务端身份和资源授权；Step 39–41 形成记忆写入/删除/召回契约；Step 42 形成
SSE 授权重放；Step 45 形成 backup/restore、镜像扫描和 memory_p0。任一步失败时使用 P5 规格的 feature flag
回滚，不删除或改写 legacy、agentic、docgen、P3、P4 路径。

## 5. 复核命令和限制

本 Step 实际执行过：

```text
git status --short --branch
uv lock --check
uv tree --locked --no-dev
uv pip check --python _venv/bin/python
uv audit --locked
git diff --check
```

结果与脱敏扫描细节分别见 [依赖/许可证/SBOM 预检](p5-dependency-license-sbom-preflight-2026-09-12.md) 和
[secret scan 报告](p5-secret-scan-2026-09-12.md)。本 Step 未运行 Docker、数据库集成、HTTP、SSE 或真实
provider 调用：对应运行环境/实现尚不存在，不能把缺少测试写成通过。
