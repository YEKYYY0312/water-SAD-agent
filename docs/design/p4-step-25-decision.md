# P4 Step 25 决策记录：MCP网关 CLI/harness 接线 + 危险操作确认门

**日期**：2026-09-10
**状态**：已完成（真实 CAD/BIM 实机端到端验证仍为 blocked，见 §3）
**规格来源**：`14-p4-mcp-gateway.md §6 Step 25`；`04-file-reading-and-mcp.md §2`；本步 Prompt

> 本文档不含任何 API Key、凭据或 trace 原文。`autocad-mcp` 的 ezdxf 验证用合成 mock odafc（`ezdxf_backend` 直接调用，不依赖 ODA 二进制）；`revit_python` 在 darwin 上离线验证为"确认门跳过"。

---

## 0. 最重要的结论（先说）

1. **网关从 fake 切到真进程**：`data/mcp_servers.json` 驱动 → `MCPStdioClient.connect → tools/list → bridge_server_tools → inventory.register_mcp_tool_binding(MCP_INVOKE) → ToolRegistry.register → Principal(MCP_INVOKE)显式授予 → ToolRegistry.invoke护栏` 全链路贯通。`autocad_mcp`(ezdxf) 在 darwin 上实测 8 工具、护栏放行；`revit_python` 因 `human_confirmation=true + confirmation_handler_ready=false` 整台 `MCP_CONFIRMATION_UNAVAILABLE` 不暴露、是预期正确。
2. **危险操作确认门 = 结构性拒绝**：确认门未接线时整台 server 不 spawn（不给"先问一句再执行"的假象）；interrupt/HITL 接线是后续 Step。本步不新增"批准后执行"的交互态。
3. **写/危险操作仍不在此步验证**：`autocad_mcp` 的写与 DWG 导出是只进本地 ezdxf 文档（无持久化副作用），产出 `{ok, payload:{format:DWG, version:ACAD2018}}` 的结构化前后一致性由 mock odafc 在 ezdxf 后端直接调用上证实；真实 ODA 需二进制、Revit 需 Windows+Revit+pyRevit，端到端为 Step 26 blocked。
4. **缓存失效含 MCP 清单**：`CacheKeyFactors.mcp_server_manifest`（`wiring.compute_manifest_hash`）进 `AnswerCacheService` 的键；server 清单/白名单/超时/DWG 版本任一变化旧键自然失效，`servers:[]` 时为空串与历史键同键（不无谓清空）。
5. **既有指标零下降**：`tests/test_mcp_gateway.py + test_mcp_behaviors.py 33/33` 仍绿 + `test_mcp_wiring.py 19` 新增全绿；全量 `pytest` 全绿；`mypy src/water_agent 0`；`ruff check src/water_agent/mcp 0`；`bash scripts/ci_gate.sh` 全绿。

---

## 1. 关键决策与取舍

### D1 `command` list 归一（配置面）

`data/mcp_servers.json` 模板的 `command` 写成 list（`[exec, ...args]`，与家目录仓库的真实路径一致），校验前由 `MCPServerConfig._normalize_command_list` 归一为 `command(str)+args`。理由：家目录真实 wiring 的命令含多段（`uv run --with mcp[cli] mcp run main.py`），list 形态无 shell 拼接、最可审计；未改既有单字符串形态（兼容 fake 夹具）。`uv` 用 `which uv` 的绝对路径 `/Users/yaerken/.local/bin/uv`（更稳）。

### D2 schema 的 FastMCP 兼容（确定性子集的最小放松）

`autocad_mcp` 的 8 个 dispatch 工具由 FastMCP 生成，其 `inputSchema` 带 `title`、`additionalProperties` 与 `anyOf: [T, {"type":"null"}]` 可空包装（可选参数），另有 `data: {type:object, additionalProperties:true}` / `points: {type:array}` 的自由形态。上游这是"可空包装"而非业务语义，确定性校验前归一化剥掉，不计为不支持；其余 `anyOf/oneOf/$ref/pattern` 仍拒绝。`object` 要求 `additionalProperties:true` 且禁 `properties`（nested object 仍 fail closed），`array` 只接受自由 `items`，都映射为 `dict`/`list` 透传——安全仍由 `GuardrailPipeline` 兜底。代价：现有 `test_title_key_rejected` 的"title 一律拒"语义放松为"title 装饰性键可接受"（最小放松，记录在此）。

### D3 危险操作确认门的当前形态是"整台不暴露"

`human_confirmation=true` 的 server 在 `confirmation_handler_ready=false` 时由 `wiring._confirmation_skips` 产出 `MCP_CONFIRMATION_UNAVAILABLE` 的整台 skip，且不 spawn 进程。`bridge_server_tools` 侧同码仅作单工具兜底。本步不新增"工具级二次确认"或"批准后 resume"——那会把确认门变成"执行前的提示"，与规格"危险操作需人工确认"的 HITL 语义不等价。后续 Step 接 `langgraph interrupt` 时再把"拒绝暴露"翻为"挂起待批"。

### D4 manifest hash 的失效粒度

`compute_manifest_hash(config.model_dump(mode=json))` 覆盖全部已校验 server（含被确认门跳过的）——白名单/超时/env 显式值/DWG 版本任一变化都使旧回答失效；空 `servers` 返回空串（功能关闭态与历史键同键，不无谓失效）。hash 只进 `CacheKeyFactors.mcp_server_manifest`，不进 `corpus_version`——两者失效维度正交，合并为单一键会把"MCP 换工具"与"语料重建"混为同一失效原因，排障时误导。

### D5 装配期单入口（无第二条装配路径）

`mcp/wiring.open_gateway_sessions + register_mcp_tools` 是 CLI（`AgenticSession.__init__`）与 harness（`HarnessRunner._run_real_agentic_case`）的唯一装配入口：两者都 `load_mcp_gateway() → open_gateway_sessions → register_mcp_tools`，主体补发 `MCP_INVOKE`、摘要进 trace。未另开"测试专用 wiring"——双路径即双真源，漂移时"测试绿、线上不通"。

### D6 `validate_dwg_version(None)` 小修

`~/autocad-mcp` 已改写 `validate_dwg_version(version: str|None)` + `target_version归一`（审计通过），家目录残留旧版会在本仓子进程外被 _venv python 误引——本步 wiring 测试用 `autocad-mcp/.venv/bin/python -c` 隔离验证该小修（`None → ACAD2018`、`R2018 → ACAD2018`、`acad2013 → ACAD2013`），避免"修在本仓却验在旧进程"的假绿。

### D7 DWG 导出验证不依赖 ODA 二进制

`odafc.is_installed()` 在 darwin 上为 False（`which ODAFileConverter` 无），真实 `save_as_dwg` 会回结构化 `{ok:false, error:ODA_NOT_INSTALLED}`。端到端"网关 → drawing 工具 → DWG 产出"改在 `ezdxf_backend.EzdxfBackend.drawing_save_as_dwg` 上用进程内 mock odafc（`is_installed=True / export_dwg 写假 DWG`）证 `{format:DWG, version:ACAD2018}` 的形态一致性；真实 ODA 转码为 Step 26 Windows 验证。

### D8 子进程 env 最小权限不变

`config.child_env` 仍只透传 `PATH`/`HOME` + 配置显式 `env`/`env_file`，本机其余变量（含 API Key）不进第三方进程（`tests/test_mcp_wiring.py::test_child_env_minimal` 断言）。`env_file` 相对路径按配置文件所在目录解析；缺失即 `MCP_CONFIG_INVALID` 响亮失败。

## 2. 交付内容与验证证据

- `src/water_agent/mcp/wiring.py`（装配接线单入口：`MCPGatewayAssembly / open_gateway_sessions / register_mcp_tools / compute_manifest_hash / principal_with_mcp`）；
- `src/water_agent/mcp/schema.py`（FastMCP 兼容：`title`/`anyOf可空` 归一 + 自由 `object`/`array`，nested 仍拒）；
- `src/water_agent/mcp/config.py`（`command` list 归一）；
- `src/water_agent/runtime/governance/cache.py` + `cache_service.py`（`mcp_server_manifest` 失效因子）；
- `src/water_agent/cli.py`（`AgenticSession` 装配期接入 MCP 网关，`mcp_assembly` 存活期由 `ExitStack` 托、主体补发 `MCP_INVOKE`、摘要进 trace/`--json`）；
- `src/water_agent/harness/runner.py`（`--variant real` agentic 路径同链路接入，摘要进每条用例 trace；模块级缓存 `_MCP_ASSEMBLY_CACHE` 按配置路径+mtime 失效，配置变化时关闭旧装配避免资源泄漏，评测结束时 `_cleanup_mcp_assembly_cache()` 清理全部子进程）；
- `data/mcp_servers.json`（模板：`autocad_mcp allow 8 + revit_python allow 7 + human_confirmation` 分流）；
- `tests/test_mcp_wiring.py` 包含 `TestManifestCacheInvalidation` 7 项缓存失效测试（配置驱动、确认门、8 工具、save_as_dwg mock、护栏、投毒/unsupported/allowlist/capability fail closed、manifest hash 完整性、child_env 最小、DWG 版本小修）；
- `tests/test_mcp_behaviors.py` 的 `test_title_key_rejected` 随 D2 调整为"title 装饰性键可接受"（最小放松，见 D2）。

验证（2026-09-10 实测，离线）：

- `MCPStdioClient(autocad_mcp ezdxf).connect → list_tools 8（drawing/entity/layer/block/annotation/pid/view/system）→ call_tool drawing create/entity create_line → {ok:true}`；
- `EzdxfBackend(Mocked odafc).drawing_save_as_dwg(None) → {ok:true, payload:{format:DWG, version:ACAD2018, path:...}}`；
- `open_gateway_sessions(data/mcp_servers.json)` → `autocad_mcp 8 暴露 + revit_python 1 条 MCP_CONFIRMATION_UNAVAILABLE skip`，`ToolRegistry(Principal MCP_INVOKE).invoke(mcp_autocad_mcp_drawing)` 护栏放行、缺能力时 `GUARDRAIL_BLOCKED`；
- `bridge(revit_python human_confirmation) + confirmation_ready=False → 0 工具 + MCP_CONFIRMATION_UNAVAILABLE`；
- `pytest tests/test_mcp_gateway.py tests/test_mcp_behaviors.py 33/33` + `tests/test_mcp_wiring.py 19/19`；全量 `pytest` 全绿；`ruff check src/water_agent/mcp 0`；`mypy src/water_agent 0`；`bash scripts/ci_gate.sh` 全绿（`rag 11/12 + calc 28/28 + docgen 11/11 + material 10/10 + governance 32/32 + drawing_p0`）。

## 3. 残余风险与 blocked 待办

1. **真实 Revit + ODA 端到端 blocked**：`revit_python` 需 Windows+Revit 2022-2027+pyRevit Routes（`127.0.0.1:48884`），`save_as_dwg` 需 ODA File Converter 二进制；darwin 上二者皆不可达，真实链路为 Step 26。
2. **HITL 确认门未接线**：当前唯一语义是"整台不暴露"（结构性拒绝）；完整 interrupt → 人工批准 → resume 在 Step 26 接线，届时 `confirmation_handler_ready` 翻转为 true 且危险工具按"挂起态"而非"不存在"呈现。
3. **HTTP/SSE transport 未做**：远程 MCP 端点需 `NETWORK_FETCH` 类能力，blocked（规格 §7.3）。
4. **prompt token 预算**：MCP 8 工具的 schema 已进装配（自由 object/array 为宽松透传，安全由护栏兜底）；后续若再加 server，需按 `p3-step-21-decision.md` 的 4500 上限实测并按意图最小暴露。
5. **官方 SDK 对照未做**：客户端仍为手写行分隔 JSON-RPC，未与 `mcp` 官方 SDK 对照（无网络无法安装）；若将来可联网，需回归验证分页/version 协商等细节。
