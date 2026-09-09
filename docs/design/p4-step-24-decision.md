# P4 Step 24 决策记录：自研 MCP Server 端（水力计算 + 规范检索）

**日期**：2026-09-09
**状态**：已完成（真实 CAD/BIM 与企业库 Server 验证仍为 blocked，见 §3）
**规格来源**：`docs/design/14-p4-mcp-gateway.md` §6 Step 24；`docs/design/04-file-reading-and-mcp.md` §2

> 本文档不含任何 API Key、凭据或 trace 原文。两台 Server 为纯合成实现（`src/water_agent/mcp/servers/`），语料来自 `data/curated/clauses/` 与 `data/fixtures/` 本地 fixture。

---

## 0. 最重要的结论（先说）

1. **Server 端已交付**：`mcp/servers/hydraulics_server.py`（7 个水力公式工具）与 `mcp/servers/specs_server.py`（3 个检索工具）均为 **stdio JSON-RPC 行分隔**实现，可直接被 Step 23 的 `MCPStdioClient` 连接（`open_bridge` 直连验证通过）。
2. **无 SDK、无网络**：两台 Server 均为纯标准库（`json`+`sys.stdin/stdout`），与 Step 23 客户端同协议（§2.2），未依赖官方 `mcp` SDK（本机 `pypi.org` 仍不可达，无法安装）。
3. **数值仍走程序化**：`hydraulics_server` 内核直接调用 `water_agent.tools.hydraulics.run_calc`，缺参/非法/未知公式均走 `isError=true` 结构化返回，不猜默认值；检索 Server 为只读内存检索，无写入。
4. **仍不得声称已接通外部 CAD/BIM**：两台自研 Server 只证明“自研能力已可被网关纳管”的闭环，真实天正/Revit/中望与企业库仍为 blocked（与 Step 23 同口径）。

---

## 1. 关键决策与取舍

### D1 复用既有 `hydraulics.run_calc`，不二次实现公式

7 个公式的参数校验、默认重力加速度、舍维列夫越界 `assumptions`、曼宁 `fill_ratio` 区间等**全部**由 `hydraulics.run_calc` 承担。Server 只做 `tools/call` 的参数透传与结果形态转换（`values/assumptions` → `content.text` + `structuredContent`），业务失败一律 `isError=true`。理由：公式口径是产品级资产（`docs/design/09-hydraulics-formulas.md`），二次实现必漂移；复用也使 `data/eval/calc_p0.jsonl` 的金标准对 Server 侧可复用（同一内核）。

### D2 检索 Server 复用 InMemory 语料与 bigram 口径，不引入向量依赖

`specs_server` 启动时一次性加载 `data/curated/clauses/*.jsonl`（命中即不回退 `fixtures/rag_fixture.jsonl`），检索用与 `retrieval/in_memory.py` 相同的 bigram 重叠 + 命中 bonus（`5 if q in text`）。理由：`data/curated` 已是本仓唯一的规范真源（`load_clauses_jsonl` 同源），引入向量检索需 `triviumdb`/embedding 依赖与索引构建，与“离线即可跑”的 Step 24 目标冲突；bigram 足以证明往返与治理语义。`WATER_AGENT_CLAUSES_PATH` 环境变量允许测试覆盖。

### D3 inputSchema 全部落在确定性子集内，使网关零改动即可纳管

两台 Server 的 10 个工具的 `inputSchema` 均满足 `mcp/schema.py` 的确定性子集：顶层 `object` + `properties` + `required`，属性仅 `string/integer/number/boolean` + `minimum/maximum/minLength/maxLength/default/enum`。无 `anyOf/$ref/pattern/nested object`。效果：`bridge_server_tools` 可直接 `build_args_model` 成功，`ensure_description_safe` 清洗通过（描述均已写成非注入文本），无需为自研 Server 开例外。

### D4 协议面与 Step 23 客户端严格对齐（生命周期 + 错误分层）

- `initialize` 回 `protocolVersion`（回显客户端声明值）+ `capabilities:{tools:{}}` + `serverInfo`；
- `notifications/initialized` 无回包；
- `tools/list` 一次性返回全量（本实现无分页，客户端的 `MAX_LIST_PAGES=20` 仍兜底）；
- `tools/call` 成功 → `{content:[{type:"text", text}], structuredContent?, isError:false}`，业务失败 → `{content:[{type:"text", text: <detail>}], isError:true}`，协议失败才走 JSON-RPC `error`；
- 每条消息 `json.dumps(..., separators=(",",":"))+ "\n"`，与 `client.encode_message` 同格式。

因此 `open_bridge(config, gateway)` 对两台真实业务 Server 的行为与对 `fake_mcp_server.py` 一致：`connect → list_tools → bridge_server_tools → ToolRegistry.invoke` 链路无需任何分支。

### D5 只读/纯计算 Server 设 `human_confirmation=false`

两台 Server 均为只读或纯计算，无文件写入、无网络、无 shell。按规格 §6 即使 `confirmation_handler_ready=false` 也可暴露，为 Step 25（网关在 CLI/harness 装配期 `open_bridge` 并注册进 `ToolRegistry`）提供可测端点。写/危险操作 Server 将在后续步骤按 `human_confirmation=true` 交付（当前唯一语义仍是“整台不暴露”）。

---

## 2. 交付内容与验证证据

- `src/water_agent/mcp/servers/__init__.py`（包标记）
- `src/water_agent/mcp/servers/hydraulics_server.py`（7 工具：`calc_velocity`/`calc_head_loss_{darcy_weisbach,hazen_williams,shevelev}`/`calc_diameter`/`calc_slope_manning`/`calc_pump_head`）
- `src/water_agent/mcp/servers/specs_server.py`（3 工具：`retrieve_clauses`/`get_article`/`get_spec_info`）

验证（2026-09-09 实测，离线）：

- `MCPStdioClient(hydraulics).connect → list_tools (7) → call_tool("calc_velocity",{flow_lps:20,diameter_mm:150}) → {ok:true, structuredContent:{values:{velocity_mps:1.13...}}}`；缺参如 `calc_velocity` 少 `diameter_mm` → `{ok:false, code:MCP_TOOL_REMOTE_ERROR, message:"…flow_lps 必须为正数…"}`（复用内核校验）；
- `MCPStdioClient(specs).connect → call_tool("get_spec_info",{}) → {specs:[…], total_clauses:…}`；`retrieve_clauses({query:"设计秒流量",top_k:3}) → hits`；`get_article({spec_id:"FIX",article:"1.1.1"}) → FIX 1.1.1`（命中 curated/fixture）；
- `open_bridge(hydraulics_cfg, gw) → bridge_tool_name 前缀 mcp_hydraulics_/mcp_specs_ → ToolRegistry(Principal MCP_INVOKE).invoke("mcp_hydraulics_calc_velocity", flow_lps=20, diameter_mm=150) → 护栏放行、返回 structuredContent`；
- `pytest tests/test_mcp_gateway.py tests/test_mcp_behaviors.py` 33/33 通过（Fake Server 行为未变）；全量 `pytest` 通过；`ruff check src/water_agent/mcp/servers` 0 告警；`mypy src/water_agent/mcp/servers` 0 告警；`bash scripts/ci_gate.sh` 全绿（既有 harness 0 回退）。

---

## 3. 残余风险与 blocked 待办

1. **真实 CAD/BIM 与企业库仍 blocked**：自研 Server 只证明纳管链路，外部端点验证为 Step 26。
2. **CLI/harness 装配未接线**：`data/mcp_servers.json` 仍为骨架（`servers:[]`，功能关闭态）。Step 25 才把两台 Server 的配置模板接入 CLI 装配期（`open_bridge` + `register_mcp_tool_binding` + `ToolRegistry` 注册），并处理 `server manifest hash` 进缓存失效键（Step 23 决策记录残余风险 5）。
3. **检索精度为 bigram 基线**：`specs_server` 的检索召回率与真实 `TriviumRetriever` + `LocalReranker` 的矩阵（`evaluate_retrieval_matrix.py`）不可比；生产检索仍走 `retrieval/trivium.py`。
4. **无分页**：`tools/list` 一次性返回；若后续工具数增长需按 MCP 规范分页，需补 `nextCursor`。
