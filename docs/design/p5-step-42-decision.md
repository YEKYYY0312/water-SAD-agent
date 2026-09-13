# P5 Step 42 决策记录：AgenticSession、Web run 与 SSE 主链路

## 结论

Web 消息入口创建 PostgreSQL `runs` 记录后交给 `RunManager` 异步执行。CLI 和 Web
都通过 `runtime/agentic.py::build_agentic_session` 装配 AgenticSession，因此模型、
检索、MCP、ToolRegistry、ProviderGateway、GuardrailPipeline、引用校验和程序化计算
只有一套实现。

Web 的运行事件使用单进程生命周期内存日志：每个 run 的事件从 1 开始单调递增，
SSE envelope 固定包含 `event_id`、`type`、`run_id`、`thread_id`、`time`、
`payload_version` 和 `payload`。`Last-Event-ID` 只解释为同一 run 的数字游标；重连
从游标之后补发，终态只追加一次 `finalize`。当前 P5 默认是单 app 实例，事件日志不
跨进程持久化；PostgreSQL 运行行、状态和 checkpoint 仍是持久化边界。多实例事件分发
与跨重启 replay 留给后续部署增强，不在本 Step 虚称已支持。

## 授权与取消

- 消息创建、取消由现有 session cookie、CSRF 和 `require_password_changed` 保护；
  会话/项目/运行定位最终交给 `WebSessionRepository` 和 `RunRepository` 做 owner/
  membership 过滤。
- SSE 建连先做授权，重放每条事件前再次检查活动账号和当前 membership；撤权后流停止，
  不返回新的事件。取消额外要求 run 的服务端 owner，项目成员只读运行事件。
- run 状态使用 `queued → running → succeeded|failed|blocked|cancelled` 的窄状态集，
  数据库更新采用 expected-status 条件；取消是协作式检查，不杀线程。

## 记忆与安全边界

运行开始后，使用当前认证用户、租户和 Web session 的 `project_id` 调用现有
`MemoryRecallService`。`MEMORY_ENABLED=false` 完全跳过召回；存储暂时不可用时追加
脱敏 warning 并继续无长期记忆回答；授权、损坏或其他记忆策略异常则将 run BLOCK。
召回结果只作为 Agentic state 的 `memory_context`，由 `PromptAssembler` 渲染为
`SESSION_MEMORY` 数据段，不参与工具注册、能力授予、引用校验、程序化计算或审批。
SSE/trace 只发送安全摘要、引用 ID、状态和经现有脱敏器处理的文本，不发送完整记忆正文、
系统提示、路径或 secret。

## 验证

`tests/test_web_runs.py` 覆盖正常完成、本人记忆、项目隔离、未授权 SSE/trace、
Last-Event-ID 重放、单次 finalize、取消、memory 开关、memory failure 分流、
共享装配和无模型 Key 的结构化前置条件失败。CLI 的 SQLite checkpointer 与已有
legacy/agentic/docgen/P3/P4 路径保持不变。
