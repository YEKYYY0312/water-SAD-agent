# P5 Step 44 决策记录：可追溯工程 Web 工作台

## 结论

Step 44 在 Step 42 的 Web run/SSE 之上增加项目、会话和反馈工作台，不新增 Agent runtime、
ToolRegistry、ProviderGateway、计算、引用校验或图纸解析实现。项目、会话、run 和反馈都通过
现有认证主体与 repository 层做 owner/membership 过滤；客户端 ID 只用于定位，不能授予访问权。

运行事件仍由 Step 42 的 `RunManager` 产生。Web 观察器只读取已经产生的 `ToolMessage` 和终态，
把工具开始/结果转为安全摘要；计算卡只收录已执行 `calc_velocity` 的后端程序化结果，前端不做
数值运算。引用卡只从最终状态中经过既有 `validate_citations` 的 `source_id` 渲染，中间工具声明
不会直接变成引用卡。

trace API 返回去除 query、完整检索正文和正文型 data 的浏览器安全投影，保留节点、状态、时长、
计数、脱敏错误、引用 ID 和检索计数。SSE 仍使用同一 run 的数字游标；前端用 `fetch` 读取 SSE，
断线后携带 `Last-Event-ID` 重连，刷新后从服务端会话的最近 run 从游标 0 重放，以恢复工作台状态。

反馈进入独立的 `feedback_candidates` 表，状态固定从 `candidate` 开始，并以 `(run_id, user_id)`
幂等。该表不被评测 harness 或 gold loader 读取，候选不得未经人工审查变成正式 eval gold。

## 能力状态

- P3 读取状态来自 `resolve_drawing_root()`：DXF 按真实目录状态展示；DWG 直读恒为 `blocked`；
  `real_samples=false`、`recall_gate=false` 原样展示。
- P4 MCP 能力状态只读取配置，不启动 server。已有配置仍标记真实 CAD/BIM/企业库验证为 `blocked`；
  未配置和配置不可用分别展示，不把本地 fake server 验证描述为真实接通。

## 验证边界

后端新增资源授权、反馈隔离、工具生命周期、计算来源、trace 投影和能力状态测试；前端用纯状态/API
测试验证工具开始/成功/失败、终态引用、计算结果、Last-Event-ID 和服务端错误码。`npm` 的
typecheck/test/build 是 Step 44 的前端门禁；真实浏览器自动化因本机 DevTools/CUA 初始化超时未取得
视觉/无障碍运行证据，不能把该项描述为已完成。
