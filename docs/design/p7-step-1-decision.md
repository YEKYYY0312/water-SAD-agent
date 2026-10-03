# P7-S1 决策记录：MCP 确认门执行期强制（F7）

**日期**：2026-10-03
**状态**：已完成
**规格来源**：`docs/design/16-p7-plugin-mcp.md` §2 F7、§7 P7-S1；执行 Prompt `docs/execution-prompts/P7-S1-confirmation-gate.md`
**基线**：main `3cbc996`（P7-S0 合入后）

---

## 0. 偏离与修订

### 0.1 执行 Prompt §0 的修订

执行 Prompt §0 为"暂无"，照做。

### 0.2 实测后追加的偏离

| 执行 Prompt 原文 | 本步做法 | 原因 |
|---|---|---|
| "判断是否需要同步递进 `RULESET_VERSION`" | **递进**为 `p7-s1-2026-10-03` | `rules.py` 的约定是"改动任何规则都必须改它"；新增 `tool.confirmation_required` 是一条新规则，且版本号进 trace 与 `PolicyDecision`，不递进就无法从 trace 回溯裁定依据。它不进缓存键，无连带影响 |
| （未提及） | `MCP_CONFIG_VERSION` **不递进** | `version` 只是写进配置文件的元数据、加载时不比对；递进只会让入仓配置与 `test_default_config_loads` 跟着改，不带来任何校验。原因写进了常量注释 |
| A02 的验证节点 `tests/test_tool_policy.py::test_confirmation_required_tool_blocked_at_call_time` | 实际节点在类里：`…::TestConfirmationGate::test_confirmation_required_tool_blocked_at_call_time` | 规格行改为真实节点路径（追溯检查按 AST 精确定位） |
| P7-S0-A15 仍指向 `test_confirmation_flag_is_not_enforced_at_call_time` | 该测试改写为 `test_confirmation_gate_is_enforced`（P7-S1-A03），A15 改为 `untested` 并在"未覆盖项"说明 | A15 锁的是 S0 时的缺陷现状，修复后那个现状不存在了；保留行作为历史记录，避免删除已交付行造成编号空洞 |

---

## 1. 交付

| 面 | 改动 | 要点 |
|---|---|---|
| 配置期 | `mcp/config.py`：`MCPGatewayConfig` 增加 `field_validator("confirmation_handler_ready")` | `True` 一律 `ValueError`；`load_gateway_config` 把它包成 `MCP_CONFIG_INVALID`。文件加载与直接构造走同一口径；报错写明原因（确认门未接线）与解锁条件（P7-S7） |
| 执行期 | `runtime/governance/pipeline.py::validate_tool_call` 在"主体授权"之后、"危险能力"之前查运行时 MCP 绑定 | `requires_human_confirmation=True` → `tool.confirmation_required` / `GUARDRAIL_BLOCKED`。放在授权之后：没有 `MCP_INVOKE` 的主体仍得到原来的 `tool.capability_denied`，不泄露"这是个需确认的工具" |
| 查询口 | `runtime/governance/inventory.py` 新增只读 `mcp_tool_binding(tool_name)` | pipeline 不再需要碰私有 dict；`McpToolBinding.requires_human_confirmation` 的注释改为与行为一致 |
| 版本号 | `TOOL_POLICY_VERSION` → `p7-step1-2026-10-03`；`RULESET_VERSION` → `p7-s1-2026-10-03` | 前者进缓存键（可暴露的工具集变了），后者进 trace |
| 测试 | `tests/test_mcp_wiring.py::test_ready_flag_rejected_until_confirmation_gate_exists`（A01）、`tests/test_tool_policy.py::TestConfirmationGate::…`（A02，含"不需要确认的同类绑定照常放行"反证）、`tests/test_p7_known_figures.py::test_confirmation_gate_is_enforced`（A03） | 先写、先确认失败，再改实现 |

---

## 2. 实测

- 改实现前，两条新测试按预期失败：A01 `DID NOT RAISE`；A02 `assert not True`（护栏返回 `tool.allowed`，`capability=mcp_invoke`）。
- 改实现后：`tests/test_mcp_wiring.py tests/test_tool_policy.py tests/test_mcp_gateway.py tests/test_mcp_behaviors.py tests/test_guardrails.py tests/test_injection_defense.py tests/test_redteam_dataset.py tests/test_governance_contracts.py` 全部通过；P7 harness 与上述两文件合跑 102 passed / 2 skipped（skip 是依赖本机 autocad-mcp 的既有用例）。
- `ruff check src` 与 `mypy src/water_agent` 无问题。
- 范围守卫：`python scripts/check_step_scope.py docs/execution-prompts/P7-S1-confirmation-gate.md --base origin/main` 通过（9 个文件，`frozen_json` 冻结的 14 个 profile 与 `PROMPT_TEMPLATE_VERSION` 未变）。
- 全量门禁：`COMPOSE_PROJECT_NAME=p7s1 bash scripts/ci_gate.sh`（worktree 内、不预设 PYTHONPATH）**EXIT=0**；日志首行 `water_agent 导入自：<p7-s1 worktree>/src/water_agent`，governance_p0 32/32、agentic_p0 21/21，各基线对比通过。

---

## 3. 快照变化（`--diff` 原文）

```
/profiles/session_mcp_ready_flag/lifecycle/app: {"app_kwargs": ["cache_service", "checkpointer", "model", "prompt", "request_context", "tools", "trace"], "cache_serv... -> null
/profiles/session_mcp_ready_flag/lifecycle/captures: 1 -> 0
/profiles/session_mcp_ready_flag/lifecycle/exception: null -> {"code": "MCP_CONFIG_INVALID", "type": "MCPError"}
/profiles/session_mcp_ready_flag/lifecycle/mcp: [{"exposed": ["mcp_snapready_echo_query", "mcp_snapready_lookup_project"], "manifest_hash_nonempty": true, "skipped_s... -> []
/profiles/session_mcp_ready_flag/lifecycle/stack: ["callback:MCPGatewayAssembly.close"] -> []
/profiles/session_mcp_ready_flag/surface: {"model_tool_order": ["calc_velocity", "cite_clauses", "material_stats", "mcp_snapready_echo_query", "mcp_snapready_l... -> null
/versions/TOOL_POLICY_VERSION: "p3-step20-2026-09-07" -> "p7-step1-2026-10-03"
```

只有执行 Prompt §4 Step 4 预期的那个 profile 变化，以及 `TOOL_POLICY_VERSION`；其余 14 个 profile 逐字段不变。`--write --reason "P7-S1：确认门执行期强制——confirmation_handler_ready=true 配置期拒绝，TOOL_POLICY_VERSION 递进"` 写入。

---

## 4. 不做

- HITL 本身（interrupt → 批准 → resume）：P7-S7。确认门接线时，配置期校验与执行期规则要一起改成"确认后放行"。
- 不改 allow 白名单、schema 子集、描述安检、桥接层的装配期拒绝（它仍是第一道防线）。
- 不碰任何非 MCP 工具与插件化（S2）。

## 5. 残余风险

- 执行期规则依赖运行时绑定表：一个**没走桥接登记**就被注册进 `ToolRegistry` 的 MCP 工具，`capability_for_tool` 会 fail closed（`tool.unregistered`），所以不会绕过——但这依赖"桥接是唯一登记入口"，S2/S3 插件化时必须保持。
- 已有部署若在 `data/mcp_servers.json` 里把 `confirmation_handler_ready` 写成 `true`，升级后会话装配会响亮失败（`MCP_CONFIG_INVALID`）。这是有意的：此前那份配置的效果就是"不经确认暴露危险工具"。入仓配置是 `false`，不受影响。
