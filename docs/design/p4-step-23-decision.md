# P4 Step 23 决策记录：MCP 客户端网关地基

**日期**：2026-09-08
**状态**：已完成（真实 CAD/BIM 与企业库 server 验证为 blocked，见 §3）
**规格来源**：`docs/design/14-p4-mcp-gateway.md`

> 本文档不含任何 API Key、server 凭据或 trace 原文。fake server 为纯合成脚本
> （`tests/fixtures/fake_mcp_server.py`）。

## 0. 最重要的结论（先说）

1. **网关地基已交付**：纯标准库实现的 MCP stdio 客户端（JSON-RPC 2.0 行分隔协议）
   + 版本化配置 + 治理绑定桥。**不依赖 `mcp` SDK** —— 环境实测无网络、无法安装。
2. **"接通"口径诚实**：本步的 fake server 只证明**协议骨架与防御逻辑**。
   **不得声称已接通天正/Revit/中望或任何企业库** —— 那些真实端点在本机不存在，
   与 P3 合成图纸不构成召回率验收证据是同一纪律。
3. **权限控制走单一真源**：MCP 工具的能力绑定注册进 `inventory` 的运行时注册表
   （`register_mcp_tool_binding`），`capability_for_tool` 仍是唯一解析点
   （fail closed 不变）；**不用** `GuardrailPipeline.declared_capabilities`
   （那是测试夹具通道，生产路径另开通道会制造"第二套声明"，漂移即漏洞）。
4. **危险操作确认 = 结构性拒绝**：`human_confirmation=true` 的 server 在确认门
   未接线时**整台不暴露**（`MCP_CONFIRMATION_UNAVAILABLE`）。HITL 完整接线是
   Step 25，本步不假装已完成人工确认。
5. **描述投毒是可防的**：外部 server 的 tool 描述按不可信文本处理 —— 清洗 +
   注入检测，命中即不暴露该工具。这是 MCP 已公开攻击面的确定性防线。

## 1. 关键决策与取舍

### D1 无 SDK 时自研协议层，而不是假装装了 SDK

`mcp` 包未安装、`pypi.org` 不可达（2026-09-08 实测 `gaierror`）。选项：
(a) 引入对 `mcp` 的**惰性可选依赖**（guarded import，未装时 SKIP）——
    那等于把整条链路都变成"未验证路径"，任何行为都不可离线证明；
(b) 用标准库实现最小 stdio 客户端。选 (b)。

理由：MCP stdio 线上格式是公开的换行分隔 JSON-RPC 2.0，生命周期只有三个方法
（initialize / tools/list / tools/call）+ 通知。自研成本可控，且**每条防御逻辑
都能离线测试**。代价是：未与官方 SDK 对照、未验证过任何真实 server 的握手细节
（规格 §7.1 明示）。若将来可联网安装 `mcp`，客户端 transport 层可替换 ——
协议/配置/桥接/测试全部保留。

### D2 工具描述按不可信文本处理：命中注入即不暴露

`tools/list` 的 description/inputSchema 描述来自第三方代码，可携带
"忽略以上指令，声明引用…" 类载荷。若照单全收送进系统提示，就是工具投毒。
本步选择**确定性拒绝**（清洗 → `detect_injection` → 命中即不暴露该工具），
而不是"脱敏后仍暴露"。理由：脱敏后的描述会丢失工具语义（把"读取图纸"脱成
"[REDACTED]"的模型不知道这个工具干什么），而桥接层自己补的说明又可能与
实际行为不符 —— 不如 fail closed。这是正则规则，必然存在未覆盖变体（§7.4），
不声称拦任意投毒。

### D3 能力绑定进 inventory 运行时注册表，不用 declared_capabilities

`declared_capabilities` 的 docstring 明示"测试夹具用，不得在生产工具上使用"。
MCP 工具是生产路径。若为它开第二个声明通道，`capability_for_tool` 就不再是
唯一解析点 —— 静态清单漏登记时，护栏可能从另一处拿到"允许"，这正是
"两处真源漂移"漏洞的形状。因此：

- `inventory.py` 新增 `register_mcp_tool_binding()`（装配期、一次性、拒绝重复）；
- `capability_for_tool` 查静态清单后再查运行时注册表，未注册仍 KeyError；
- 注册表**只增不删**（无"登记了又摘掉"的窗口期），`clear_mcp_tool_bindings()`
  仅供测试；
- 有测试锁死"注册后护栏放行、不注册则 fail closed"。

### D4 统一 `MCP_INVOKE` 能力，不让配置触碰能力语义

早期草稿让配置按工具把远程工具绑定到任意 `Capability`。否决：能力枚举是
**代码定义的偏序**（`contracts.py` docstring 明示"不是模型/用户可自由声称的
字符串"），把映射搬进 JSON 等于让配置文件获得权限语义 —— 配置错误就可能
授予危险语义（加载期只挡 `DANGEROUS_CAPABILITIES`，其余组合难以审计）。

改为：远程工具统一持新能力 `MCP_INVOKE`（**默认无主体持有**，不在
`DEFAULT_LOCAL_CAPABILITIES`），配置只回答"**哪些工具**放行"（`allow`
白名单，`None` = 全拒），主体授予回答"**谁能用**"（`Principal.local(
thread_id, capabilities={MCP_INVOKE, ...})`）。权限面两侧收敛，
配置不再触碰能力枚举。

### D5 环境注入用显式给值，不用按名透传

早期草稿的 `env_passthrough`（按变量名透传本机当前值）被否决：只列名字仍会把
本机当前值整体送进第三方进程，且值不可审计（环境随时在变）。改为
`env`（内联 dict）+ `env_file`（key=value 文件）**显式给值**，内联覆盖文件；
子进程默认只透传 `PATH`/`HOME`。env_file 缺失 = 配置错误（响亮失败），
不静默裸奔。

### D6 远端业务失败是结果，不是异常

`tools/call` 的 `isError=true` 是 MCP 约定的**业务失败**通道（HTTP 200 式）。
`call_tool` 把它转成 `{"ok": False, "code": MCP_TOOL_REMOTE_ERROR, ...}` 结构化
dict，与连接崩溃（`MCPError`）分层 —— 和 `read_drawing`"可预期业务失败走
payload、`TOOL_ERROR` 通道留给意外崩溃"的口径一致：把业务失败塞进异常通道
会丢掉错误码语义。

### D7 fake server 用子进程，不是进程内 mock

用 `sys.executable + tests/fixtures/fake_mcp_server.py` 启动真实子进程走完整
stdio 管道，而不是把 transport 换成一个内存对象。理由：进程边界才是线上形态 ——
超时、进程崩溃、EOF 早断、行过长这些行为在内存 mock 里不会出现，而它们正是
MCP 集成的现实故障。子进程成本（约百毫秒）对测试可接受。

### D8 不做并发请求

客户端单请求串行（id 单调递增但从不并行等待）。MCP 允许并发，本网关刻意不做：
串行下"响应配对"逻辑最简单、最可审计；引入并发需要重新审计超时与配对语义。
给排水问答场景下单工具串行不构成瓶颈。

## 2. 交付内容与验证证据

- `src/water_agent/mcp/`：`errors.py`（13 稳定错误码）/ `protocol.py` /
  `config.py` / `schema.py` / `client.py` / `bridge.py`；
- `src/water_agent/runtime/governance/`：`Capability.MCP_INVOKE`（contracts.py，
  默认无主体持有）+ 运行时绑定注册（inventory.py）；
- `tests/fixtures/fake_mcp_server.py`：合成 stdio server（scenario 驱动：
  ok/noisy/poisoned/bizerror/crash/garbage/badcall/flood/slow）；
- `tests/test_mcp_gateway.py`（17 条：协议/配置/schema/客户端/桥接/护栏集成）+
  `tests/test_mcp_behaviors.py`（16 条：故障行为/描述投毒/schema 子集补充/
  护栏 fail-closed）；
- `data/mcp_servers.json`：接入骨架（空 servers，功能关闭态）。

验证（2026-09-08 实测）：`pytest tests/test_mcp_gateway.py tests/test_mcp_behaviors.py`
33/33 通过；全量 `pytest` 1559 passed / 12 skipped；`ruff check src tests` 0 告警；
`mypy src/water_agent` 0 告警；`bash scripts/ci_gate.sh` 退出 0（rag 11/12 + calc
28/28 + docgen 11/11 + material 10/10 + governance 32/32 + drawing_p0 smoke 回归
+ 基线对比全绿，输出见会话记录）。

## 3. 残余风险与 blocked 待办

1. **真实 server 未验证**：协议细节（initialize 版本协商、server 请求回复、
   分页）只在合成 server 上验证过。真实天正/Revit/中望/企业库端点验证 blocked。
2. **HITL 确认门未接线**：`human_confirmation` 当前唯一语义是"整台 server
   不暴露"。Step 25 接 interrupt + 人工批准 + resume 后翻转。
3. **HTTP transport 未做**：远程 MCP 端点需 `NETWORK_FETCH` 类能力，blocked。
4. **官方 SDK 对照未做**：协议骨架自行实现，未与 `mcp` 官方实现对照
   （无网络无法安装）。若将来接入，需回归测试验证兼容性。
5. **缓存失效因子未含 MCP server 清单**：启用 MCP 后，可用工具集变化
   （server 升版/换工具）不会自动使回答缓存失效。Step 25 接线时把
   `server manifest hash` 纳入缓存键（Step 13 的失效因子纪律）。
6. **工具 schema token 预算**：MCP 工具的 schema 只在配置放行后才进提示词；
   Step 25 接线时需实测并遵守 P3 §9.5 的 4500 token 上限纪律
   （不得为塞进新工具而抬高上限）。