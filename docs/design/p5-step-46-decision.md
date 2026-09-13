# P5 Step 46 决策记录：可选本地模型与可插拔记忆 provider

**日期**：2026-09-13
**状态**：机制已实现；本机本地模型兼容性与 pgvector 实机证据仍为 blocked/未验证
**规格来源**：`docs/给排水agentP5长期记忆与Web自托管任务书.md` §12–§15、
`docs/design/15-p5-web-memory.md`、`docs/design/14-p4-mcp-gateway.md`

## 0. 结论

1. 默认 provider 仍是原生 PostgreSQL FTS；Step 46 的 provider 失败只回退到该路径，
   不覆盖、迁移或删除原生 memory。
2. `PgVectorMemoryProvider` 只在调用方显式选择且数据库已单独启用 pgvector 后工作。
   向量放在独立的 `memory_embeddings` 表，不进入默认 Alembic migration，不把 pgvector
   变成 P0 的运行时依赖。
3. `MemoryMCPProvider` 把第三方 MCP 限制为“记忆 ID + 分数”的排序提示；正文始终由本项目
   的受权 repository 重新解析。第三方 Memory MCP 不是核心事实来源。
4. Memory MCP 只读适配器仅允许 `memory_search`、`search_memory`、`memory_query`，并通过
   P4 `open_bridge`、schema 安检、allowlist、确认门和 `ToolRegistry` 执行；写入/忘记/管理
   工具不会被适配器暴露。
5. `local-model` Compose profile 默认关闭。应用只有在 `LOCAL_MODEL_ENABLED=true` 且存在
   与 endpoint/model 精确匹配、七项检查全部通过且工具 schema `<4500` 的证据报告时才会
   选择本地模型；否则不静默回退到 DeepSeek，而是保持未配置/blocked。

## 1. 记忆 provider 边界

`MemoryProvider` 的唯一职责是返回已经完成硬作用域过滤的候选；排序、字符预算、`SESSION_MEMORY`
封装、usage、trace 和最终 prompt 仍由 `MemoryRecallService` 负责。这样 provider 不能通过自定义
排序绕过用户、租户、项目成员、ACTIVE 或过期条件。

`PostgresFtsMemoryProvider` 只是现有 Step 41 查询的公开适配，不复制 FTS 实现。可选 provider
抛出内部 `MemoryProviderUnavailableError` 后，recall service 使用原生 FTS；FTS 自身不可用时
仍返回既有 `MEMORY_STORE_UNAVAILABLE`。

`PgVectorMemoryProvider` 使用 pgvector 的 cosine distance 运算。它只保存向量、memory ID 和
时间，不保存正文；查询 SQL 同时要求租户、认证用户、项目 membership、活动状态和未过期。向量
索引是显式操作，不由 P0 的创建记忆路径自动触发。`MemoryRepository` 的可选 cleanup hook
在忘记事务提交前删除向量，失败即回滚删除状态。

## 2. Memory MCP 适配

外部 server 的描述、schema 和返回值均是不可信数据。适配器在生产打开 MCP session 时走现有
P4 bridge，故工具必须同时通过：

- `allow` 白名单；
- 确定性 schema 子集和描述安检；
- `human_confirmation=true` 未接线时的结构性拒绝；
- `inventory.register_mcp_tool_binding` 的 `MCP_INVOKE` 登记；
- `ToolRegistry.invoke` 的主体能力和护栏检查。

远程返回的 `content` 正文被忽略，只接受结构化 hit 中的 UUID 和有限分数。每个 UUID 再由
原生 `MemoryRepository.list_active_by_ids` 解析；越权 ID、其他租户 ID、非活动 ID 和不存在
的 ID 都不会进入结果。adapter 故障或返回形态不可信时回退 FTS。

## 3. 本地模型 profile 与证据

profile 使用官方 Ollama OpenAI-compatible endpoint 形态：
`http://local-model:11434/v1`。仓库不自动 pull 模型，不把模型下载或 GPU 能力假装成兼容性证据。
应用沿用既有 `build_chat_model` / `GovernedChatModel` / `ProviderGateway`；没有第二套模型
连接或 Agent 装配路径。

Ollama 上游仓库声明为 MIT；具体模型权重可能有独立许可证，项目不替部署者做权利推断。
pgvector 未作为 Python 依赖引入，数据库扩展/镜像应由部署者按其上游许可证和 PostgreSQL
版本单独核对；本机未完成扩展安装验证。

兼容性报告必须覆盖：function calling、structured citations、程序化计算工具调用、
`memory_p0`、取消/超时、错误处理和工具 schema 预算。报告只在全部状态为 `passed`、schema
token 数严格小于 4500，且 endpoint/model 与当前配置相同的情况下才会被接受。

本机实测环境：Python 3.12.14、现有 LangChain OpenAI/psycopg/SQLAlchemy/FastAPI 可用；
没有 `ollama` CLI、`llama-server`、Python `pgvector` 包或运行中的本地模型 endpoint。因此
本步运行的兼容性 probe 只能返回 `blocked`，不会生成 `compatible` 结论。真实本地模型与
pgvector 集成测试必须在具备对应服务/扩展的主机上重跑。

官方资料：

- Ollama OpenAI compatibility：<https://docs.ollama.com/api/openai-compatibility>
- Ollama Docker：<https://docs.ollama.com/docker>
- pgvector README：<https://github.com/pgvector/pgvector>

## 4. 验收边界与回滚

- 未设置 `LOCAL_MODEL_ENABLED` 时，默认 app/CLI 行为和 tool schema 不增加任何字段或工具。
- 未选择 `MEMORY_PROVIDER=pgvector` 或 Memory MCP 时，P0 只实例化原生 FTS provider。
- 关闭可选配置后已有 memory 数据不删除；recall 继续走 FTS。
- 兼容性证据缺失、过期、字段不全、endpoint/model 不匹配或 schema 达到 4500 时一律不接受。
- 本机没有真实模型和 pgvector extension，故不声称 local-model compatible，也不声称语义召回
  优于 FTS；当前验证只证明可选边界、权限隔离、forget 清理和故障回退机制。
