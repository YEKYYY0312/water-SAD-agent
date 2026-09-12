# P5 规格：Web 自托管与长期记忆契约（Step 35）

> 状态：Step 35 规格、威胁模型与开源预检产出；P5 运行时实现尚未开始。
> 日期：2026-09-12
> 适用范围：P5 Step 35–46 的设计边界。
> 本文不实现 Web、认证、数据库、迁移、SSE 或长期记忆。

## 0. 结论与证据边界

Step 35 固定 P5 的服务端安全边界、长期记忆数据契约、自托管发布边界和回滚路径。后续实现
必须复用现有治理单点，不得为 Web 或记忆再建一套 Principal、ToolRegistry、ProviderGateway、
GuardrailPipeline、引用校验或计算内核。

本规格的结论来自以下实际仓库证据：

| 证据 | 当前事实 | 对 P5 的约束 |
| --- | --- | --- |
| `docs/给排水agentP5长期记忆与Web自托管任务书.md` §2、§5–§15 | P5 要求单租户自托管、多本地账号、显式记忆、FastAPI + React、PostgreSQL、SSE 和 Compose | 本文沿用其范围；P5 任务书是范围、契约、验收和顺序的单一权威 |
| `src/water_agent/runtime/governance/contracts.py`、`pipeline.py`、`inventory.py` | 已有 `Principal`、`Capability`、`ContentEnvelope`、记忆写入检查、工具能力清单 | Web 认证结果必须服务端构造 `Principal.authenticated()`；记忆不得提升为 `SYSTEM_POLICY` |
| `src/water_agent/graphs/agent_rag.py`、`src/water_agent/session.py` | 当前 Agentic 图依赖 checkpointer，当前会话恢复是 SQLite 路径 | Web PostgreSQL checkpointer 是后续 Step 38；CLI SQLite legacy 路径必须保留 |
| `docs/design/12-p2.5-production-governance.md`、`docs/design/p2.5-threat-model.md` | 远程调用、工具调用、输入和输出已有治理边界，但 HTTP 身份、真实记忆写入、删除和外部告警尚未接线 | P5 只能扩展既有边界；未知项保持 `blocked`/`unknown` |
| `docs/design/14-p4-mcp-gateway.md` | MCP 是不可信第三方代码；`MCP_INVOKE` 默认不授予主体；危险确认门尚未接线 | P5 不得把配置 env 或模型描述当作认证或授权 |
| `docs/给排水agentP4.5多Agent协同任务书.md` §8、§12、§18 | 四 Agent 共享一个受治理主体；产物、审批哈希和导出边界由确定性编排器控制 | Web 展示真实事件和产物状态，不模拟工具成功或发布成功 |
| `pyproject.toml`、`uv.lock`、`scripts/ci_gate.sh` | 当前只有 Python/uv 依赖边界；锁文件存在；CI 是本地脚本，无 Docker/Compose/GitHub Actions | Step 35 不增加依赖；Web 栈、数据库和容器只能在后续 Step 明确引入并审计 |
| `web/overview.md`、`web/index.html` | 当前是零依赖静态原型，含明确的占位条文和前端占位计算 | 原型不能视为已实现 Web，也不能作为正式规范或计算证据 |

审计报告见：

- [依赖、许可证和 SBOM 预检](p5-dependency-license-sbom-preflight-2026-09-12.md)
- [secret scan 报告](p5-secret-scan-2026-09-12.md)
- [发布数据清单](p5-release-data-inventory.md)
- [P5 威胁模型](p5-threat-model.md)

## 1. Goal、非目标与发布前置条件

### 1.1 Goal

P5 最终允许认证用户在一套 Docker Compose 自托管实例中：

1. 创建项目和会话，并从服务端 SSE 查看真实 Agent 事件；
2. 通过明确的用户动作创建、确认、更正、停用、过期和忘记长期记忆；
3. 在用户和项目权限范围内召回有限记忆；
4. 查看记忆的来源、作用域、revision 和使用记录；
5. 重启后恢复账号、项目、会话、checkpoints 和活动记忆；
6. 在人工批准和产物哈希仍一致时发布设计书或图纸。

### 1.2 非目标

Step 35 及 P5 P0 不做以下事情：

- 不实现公网 SaaS、多租户计费、OAuth/OIDC、企业 SSO 或开放注册；
- 不把聊天历史、模型推断、规范原文、凭据、未经程序校验的计算结论或完整文件正文自动写入长期记忆；
- 不让记忆改变系统规则、工具白名单、能力、引用校验、程序化计算或人工审批状态；
- 不在当前工具 schema 预算中直接增加通用 `remember/search/forget` 模型工具；现有工具 schema 已实测约
  4370/4500 tokens，余量约 130，后续仍须按意图最小暴露；
- 不在 Step 35 引入 FastAPI、React 构建链、PostgreSQL、Alembic、Redis、向量库或第三方 Memory MCP；
- 不将确定性正则护栏描述为能拦截任意注入改写；
- 不将合成 `drawing_p0`、占位条文或 mock 回答描述为真实工程质量、正式规范引用或真实图纸验收证据。

### 1.3 发布前置条件

公开 GitHub 或发布镜像前，至少需要：

- 版权主体确认并重写当前来源不明的 `LICENSE`、`CONTRIBUTING.md` 和 `TROUBLESHOOTING.md`；补齐并审核
  `SECURITY.md`、`CODE_OF_CONDUCT.md`；
- 轮换本机 `.env`/`.env.local` 中按真实密钥处理的 API Key；完成工作树、可达 Git 历史和镜像层扫描；
- 用锁文件生成 SPDX 或 CycloneDX SBOM，逐项核对第三方许可证和 notice；
- 明确 `knowledge/`、`data/curated/`、设计院图纸、用户数据和规范全文的版权/授权；未核实项不发布；
- 后续 Step 45 完成 Compose 全新卷启动、迁移、bootstrap、备份恢复、非 root、healthcheck 和资源限制实测；
- 后续 P5 测试和既有 `bash scripts/ci_gate.sh` 均通过。Step 35 本身不把这些未来证据伪装成已完成。

## 2. 总体架构与信任边界

目标运行态为：

```text
Browser
  └─ HTTPS / same-origin REST + SSE
       └─ FastAPI app
            ├─ server-side session/authz/CSRF
            ├─ existing AgenticSession + LangGraph runtime
            ├─ MemoryService (PostgreSQL source of truth)
            ├─ ProviderGateway / ToolRegistry / GuardrailPipeline
            └─ PostgreSQL (business data + checkpoints + memory events)
```

当前该架构尚不存在。浏览器、上传文件、恢复的记忆、MCP 返回值、检索文本和模型输出都属于不可信数据。
只有服务端代码生成的系统策略、程序化计算结果、校验过的引用关系和授权结果才能改变运行状态；记忆
统一使用 `SourceTag.SESSION_MEMORY`，永远不是 `SYSTEM_POLICY`。

部署边界固定如下：

| 边界 | 规则 |
| --- | --- |
| 浏览器 → API | HTTPS；服务端 session cookie；状态改变请求需要 CSRF token 和同源校验；客户端 user/project/thread ID 只是定位条件，不是授权事实 |
| API → Principal | 认证中间件从服务端 session 和数据库记录解析 user/tenant/role，再构造 `Principal.authenticated()`；不接受 JSON、URL、裸 cookie 或模型输出中的主体 |
| API → 资源 | 所有查询、列表、分页、SSE 重放和写入在数据库查询层先按 tenant/user/membership 过滤；不能先全库搜索再应用层过滤 |
| API → Agent | 复用现有受治理装配；Agent 角色不是新的权限主体；工具、模型、引用和计算仍走现有唯一入口 |
| Agent → Memory | 只有显式用户动作和人工确认后的候选才能写入；写入前经过作用域、护栏、凭据、PII、冲突和 revision 检查 |
| API → 文件/备份 | 路径由装配期配置决定；模型输入不含路径；导出和恢复是人工批准后的窄适配器；备份不进入 Git 或镜像 |

## 3. 身份、权限和 session 契约

### 3.1 稳定 ID 与 Principal

```text
tenant_id  = 固定部署租户；P0 单值
user_id    = 服务端生成的不可变 UUID
project_id = 服务端生成的项目 UUID
thread_id  = 服务端生成的会话 UUID，不承担用户身份
run_id     = 服务端生成的运行 UUID
memory_id  = 服务端生成的记忆 UUID
```

Web 请求只能由服务端根据已认证 session 构造：

```python
Principal.authenticated(
    subject=f"user:{user_id}",
    auth_domain=f"tenant:{tenant_id}",
    capabilities=resolved_capabilities,
)
```

`subject` 原文不进入 trace 或 API 错误；沿用现有安全摘要和 `principal_hash`。任何无法确定的认证、租户、
成员关系或 capability 状态均 fail closed。管理员不是默认读取他人记忆正文的后门；如未来增加恢复入口，
必须单独审计操作者、原因和对象。

### 3.2 账号与 session

- `/api/auth/register` 不提供，或服务端固定返回 404；默认只允许一次性 bootstrap 创建管理员。
- bootstrap 只从 Docker secret 文件读初始密码，不从命令行参数或镜像 ENV 传递。
- 密码使用 Argon2id；登录失败不区分账号不存在和密码错误；登录接口按账号/IP 限速。
- session ID 在登录成功、权限提升、密码重置后轮换；logout、密码修改和账号停用使旧 session 失效。
- session cookie 使用 `HttpOnly`、`SameSite=Lax`；公网生产必须 `Secure`；session signing key 只通过 secret
  文件或受控 secret store 注入，不返回浏览器、trace、异常或 capabilities。
- 所有不安全方法需要 CSRF token，同时校验允许的 Origin；SameSite cookie 只是纵深控制，不替代 CSRF 防护。

### 3.3 角色与资源授权

| 角色 | 允许 | 禁止/额外条件 |
| --- | --- | --- |
| `admin` | 管理账号、停用账号、系统恢复 | 默认不读他人记忆正文；读取必须走单独审计入口 |
| `member` | 自己的项目/会话/用户记忆；已加入项目 | 项目写入必须有项目写权限 |
| `viewer` | 已授权项目的只读资源 | 不可写项目记忆、运行有副作用流程或发布 |

每个 `project_id`、`thread_id`、`run_id`、`memory_id` 在每次 API 请求中重新做资源级授权。猜中 ID 只能
命中 404/403，不得泄漏标题、时间、成员、正文、事件数量或运行状态。授权过滤必须是 SQL/repository
层的硬条件，不能依赖前端隐藏、路由命名或模型判断。

## 4. 长期记忆契约

### 4.1 允许和禁止的内容

允许的 `MemoryKind` 是 `user_preference`、`project_fact`、`project_decision`、`workflow_note`。
P0 只接受 `EXPLICIT_USER` 或经同一治理链批准的 `APPROVED_IMPORT`。

以下内容禁止成为活动长期记忆：规范原文、API Key/密码/token、未确认的模型推断、未经程序校验的计算
结论、完整上传文件正文，以及 P0 默认禁止的身份证、手机号、邮箱、住址等 PII。凭据始终强制阻断；
确定性规则只能覆盖已编码的变体，不能承诺拦截任意注入改写。

### 4.2 记录和作用域

逻辑记录沿用 P5 任务书 §6.2 的 `MemoryRecord`：`memory_id`、`tenant_id`、`owner_user_id`、
`scope_type`、`project_id`、`kind`、程序生成的 `semantic_key`、正文、状态、来源、源会话/消息 ID、
revision、时间和过期时间。

硬约束：

- `scope_type=user` 时 `project_id` 必须为空；`scope_type=project` 时必须有项目且调用者具备项目写权限；
- 活动 key 按 tenant + 作用域 + kind + semantic_key 唯一；冲突不得静默覆盖；
- 更正使用 optimistic concurrency，旧 revision 返回 `MEMORY_REVISION_CONFLICT`；
- `semantic_key` 由程序规范化生成，不由模型决定授权、SQL 条件或项目归属；
- 记忆正文从不进入 `ROLE_AND_PRINCIPLES`，只作为独立、带边界的数据段进入上下文；
- 记忆正文中的“忽略规则”“执行命令”等文本永远不能改变能力、工具白名单、规范引用、程序化计算或审批。

### 4.3 写入、事件和忘记

P0 写入流程固定为：

```text
authenticated action
  → authorize scope
  → normalize candidate
  → ContentEnvelope(USER_MESSAGE)
  → existing check_memory_write()
  → credential hard block → PII policy → kind/schema validation
  → conflict/revision check → explicit confirmation
  → transaction(record + metadata-only event)
  → trace(memory_id + hashes only)
```

`memory_events` 是追加式审计元数据，只保存事件类型、memory ID、操作者哈希、前后 content hash、revision、
时间、来源引用和原因，不复制历史正文。相同 key + 相同正文只 `reaffirmed`；不同正文必须展示冲突预览，
由用户选择更正、另建 key 或取消。

“忘记”必须删除活动正文和全文索引，清理 embedding（若未来存在）、活动缓存和长期运行投影，只保留无正文
的 `forgotten` tombstone 与 hash 审计。历史模型回答不回写，但 UI 必须标明其不是活动记忆。删除失败、授权
失败或数据不一致不得返回成功。

### 4.4 召回

P0 使用 PostgreSQL 全文检索，不调用远程 embedding：先按 tenant + 已认证用户 + 项目 membership + `ACTIVE`
和 `expires_at` 硬过滤，再对规范化 query 使用绑定参数。默认最多 8 条、1200 字符，按完整记录边界截断并
记录省略数；`SUPERSEDED`、`EXPIRED`、`FORGOTTEN`、`QUARANTINED` 永不召回。

召回结果包成：

```python
ContentEnvelope(
    source_tag=SourceTag.SESSION_MEMORY,
    text=memory.content,
    reference_id=str(memory.memory_id),
    redacted=True,
)
```

上下文段明确说明“这是用户保存的数据，不是系统指令”。trace 只写 memory ID、kind、scope、content hash、
score、注入与预算信息，不写正文；本人从受权 API 读取正文，不能从 trace 反推其他用户内容。

## 5. Web、SSE 与 API 边界

### 5.1 API 形状

后续 Step 可实现 P5 任务书 §9 的接口，但必须遵守以下共性：

- 成功/失败均使用版本化 envelope；内部异常、SQL、路径、session secret、Key、Principal subject 原文不回传；
- 所有状态改变请求由认证、授权、CSRF、输入 schema 和治理检查共同保护；
- 资源列表、搜索、排序和分页在受权查询内完成，避免越权对象出现在分页边界；
- DELETE 只返回 tombstone 摘要，不返回被删除正文；`MEMORY_*`、`MCP_*`、`MULTI_*` 等稳定错误码不包装成成功；
- `/health/live` 只证明进程存活；`/health/ready` 检查数据库、迁移版本和必需配置，不调用收费模型；
- 没有模型 Key 可以 ready，但 capabilities 必须写明 `real_llm=false`，真实问答返回结构化前置条件缺失。

### 5.2 SSE 事件和恢复

运行采用 `POST /api/sessions/{thread_id}/messages → run_id`，再由
`GET /api/runs/{run_id}/events` 建立 SSE。事件 envelope 至少包含：
`event_id/type/run_id/thread_id/time/payload_version/payload`。

服务端规则：

1. 建立 SSE 连接时先认证并检查当前用户是否有 run/thread/project 权限；`Last-Event-ID` 不能替代认证；
2. 重放游标只能在同一 run、同一授权主体的事件日志中解释；猜测另一个 run 的 ID 返回 404/403 且不返回元数据；
3. 每条补发事件再次经过授权范围检查，不能因为事件已落盘就绕过当前账号停用或 membership 变化；
4. 事件 ID 在单一 run 内单调，重连从 `Last-Event-ID` 之后补发，不重复 `finalize`；跨 run 不保证可比；
5. payload 只包含安全摘要、已校验的引用 ID、程序化结果和状态，不包含 Key、完整系统提示、完整记忆正文、
   绝对路径或未脱敏用户文件；
6. 取消通过服务端 run owner 检查和协作式状态转换完成；前端未收到后端成功事件不得显示完成。

## 6. 与现有 Agent、P4.5 和治理的接线规则

- Web 是新的入口，不是新的 Agent 装配。CLI 与 Web 必须共享核心 session/registry/gateway 构造函数；
- 四个 P4.5 Agent 不是四个 Principal。编排器仍负责确定性路由、上限、HITL、产物血缘和发布门；
- 水力数值仍只来自现有程序化工具；记忆中的数值最多作为待校验输入或已批准背景，不能直接成为计算结果；
- 规范引用仍来自检索上下文和既有 `validate_citations`；规范版本 `unknown` 不能被记忆或 Web 状态覆盖；
- 文件路径仍由装配期目录策略解析；模型、SSE payload 和用户 JSON 不得提供任意路径；
- MCP 返回值仍是 `TOOL_OUTPUT` 数据；P4 的 allowlist、`MCP_INVOKE` 和未接线确认门继续有效；
- GuardrailPipeline 是确定性正则规则集，不是分类器。上下文分隔、类型/权限/预算/审批和产物校验共同承担安全责任；
- 不为 Web、记忆或四 Agent 复制一套引用校验、计算、材料聚合、图纸规则、工具注册或 provider 连接逻辑。

## 7. 部署、secret、备份与数据生命周期

目标默认服务为 `app + postgres`，`migrate` 与 `bootstrap-admin` 是一次性任务；反向代理和本地模型是可选
profile。容器使用非 root 用户，只写显式 volume；app 启动前迁移成功，失败时 readiness 为 false。

- provider Key、数据库密码、session signing key、bootstrap 密码使用 Compose secrets 或 `*_FILE` 约定；
  普通环境变量只承载非敏感配置；
- 镜像和构建上下文排除 `.env*`、trace、报告、数据库、索引、用户上传和真实规范/图纸文件；
- PostgreSQL、上传、导出和备份目录分开挂载并设置最小权限；
- 备份包括数据库逻辑备份、用户文件清单和校验摘要；恢复默认要求空目标或显式 `--force`，执行前先备份；
- 删除策略必须覆盖活动表、全文索引、未来 embedding、缓存、checkpoint 投影和备份保留期；保留期、地区合规
  和是否允许 PII 仍是 `unknown`，在用户确认前不把 PII 加入 P0 记忆；
- 备份/恢复是敏感数据操作，不进入 Git、镜像层、公开 issue、示例日志或外部分析服务。

## 8. 版本、验收、回滚和阻塞项

### 8.1 版本与验收

每个公开 API envelope、memory schema、SSE payload、配置和 migration 都有显式版本。新增字段优先向后兼容；
不兼容变更使用新 schema/适配器，不重解释旧 payload。Step 35 的确定性文档契约测试为
`tests/test_p5_step35_docs.py`；运行时认证、数据库、记忆、SSE 集成测试属于 Step 37–45，不能在 Step 35
提前声称通过。

P5 关键门禁至少包括：跨用户/项目泄漏为 0、secret 落盘为 0、非活动记忆召回为 0、忘记后残留为 0、显式确认率
1.0、合成 gold 精确召回率 1.0、数字重算率 1.0、memory prompt 超预算为 0、未授权 SSE 重放为 0。它们只
证明合成机制，不证明自然语言记忆召回或真实图纸/工程质量。

### 8.2 回滚

- `WEB_ENABLED=false`：保留现有 CLI、legacy、agentic、docgen、P3 和 P4 路径；Web import 不成为 CLI 硬依赖；
- `MEMORY_ENABLED=false`：跳过长期记忆读写，agentic 行为回到当前会话记忆；已有数据不删除；
- migration 采用前滚修复，已有用户数据后不依赖破坏性 downgrade；
- recall 故障可降级为无长期记忆继续回答并记录 warning，但授权失败、数据损坏和治理失败必须 BLOCK；
- 可选 provider 故障退回原生 PostgreSQL FTS，不覆盖或丢失原生记忆；
- 关闭 Web/记忆开关后，已生成但未批准的文档/图纸仍不得绕过 P4.5 release gate 发布。

### 8.3 Step 35 明确的 blocked/unknown

- 当前没有 Web 服务、认证、数据库、migration、SSE、Compose、备份恢复或长期记忆实现；
- `.env` 与 `.env.local` 中的非空 API Key 形态按真实秘密处理，必须由部署者轮换；本 Step 不调用外部平台；
- 当前 `LICENSE`、`CONTRIBUTING.md`、`TROUBLESHOOTING.md` 未有可达 Git 历史且内容属于其他 IFC/Bonsai 项目；
  版权主体和最终许可证待人工确认；
- `SECURITY.md`、`CODE_OF_CONDUCT.md`、Docker/Compose、GitHub Actions 尚不存在；
- 规范版本、生效日期、公告/来源、真实 RAG 质量、真实图纸召回、真实 MCP/CAD/BIM 均按既有文档保持
  `unknown` 或 `blocked`；不能借 P5 规格改变这些状态；
- 真实合规义务、数据驻留、PII 是否允许保存、保留期、目标 CPU 架构、GitHub owner 和镜像名称仍待用户确认。
