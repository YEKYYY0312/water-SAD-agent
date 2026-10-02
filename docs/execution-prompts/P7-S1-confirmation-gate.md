# P7-S1 执行 Prompt：MCP 确认门执行期强制（F7 安全修复）

> **工作区**：仓库 `YEKYYY0312/water-SAD-agent`，基于最新 `main`（必须已包含 P7-S0）。新建分支 `YEK/p7-s1-confirmation-gate`。
> **必读**：
> - `AGENTS.md`（三条铁律、验证命令、改动纪律）
> - `docs/design/16-p7-plugin-mcp.md`：§2 F7、§6 防漂移 harness、§7 的 P7-S1 验收、§8 实施协议
> - `docs/design/14-p4-mcp-gateway.md` §4（治理绑定）与 `docs/design/p4-step-25-decision.md` D3（确认门 = 结构性拒绝）

---

## 0. 相对规格的修订（以本文为准）

暂无。实施中发现规格与代码现实冲突时，按 §7 停止条件处理，并写进决策记录 §0.2。

## 1. 目标与边界

**现状（F7）**：

- `requires_human_confirmation` 只在装配期检查：`confirmation_handler_ready=false` 时整台 server
  不暴露。
- 但只要有人把配置改成 `confirmation_handler_ready=true`，这类工具就会直接暴露、并且**可以调用**，
  执行期没有任何检查。
- `inventory.py` 里 `McpToolBinding.requires_human_confirmation` 的注释声称"未确认前调用一律被护栏
  拒绝"，实际上 `GuardrailPipeline.validate_tool_call` 从不读这个字段。

**做**：

1. **配置期**：确认门（S7）接线之前，`confirmation_handler_ready=true` 一律拒绝。
   - 推荐写在 `MCPGatewayConfig` 的 `model_validator` 里：文件加载与直接构造走同一口径；
     `load_gateway_config` 已把校验失败包成 `MCP_CONFIG_INVALID`。
   - 错误信息要说明两件事：为什么拒绝（HITL 未接线）；什么条件下解锁（S7）。
2. **执行期（纵深防御）**：`validate_tool_call` 查到工具的运行时 MCP 绑定带
   `requires_human_confirmation=True` 时，一律拒绝。
   - 规则 id 用 `tool.confirmation_required`，错误码沿用 `GUARDRAIL_BLOCKED`，不新增错误码；
   - 判断是否需要同步递进 `RULESET_VERSION`，依据写进决策记录。
3. **对齐文档与实现**：`inventory.py` 的注释改成与行为一致；桥接层给模型的描述措辞同步核对。

**不做**：

- HITL 本身（interrupt / 批准 / 恢复是 S7）；
- 不改 allow 白名单、schema 子集、描述安检；
- 不碰任何非 MCP 工具；
- 不动插件化相关代码（S2）。

## 2. 铁律与治理落点（不得违反）

- **不放宽任何检查**：本步只新增拒绝路径。
- **失败要响亮**：配置被拒绝时报 `MCP_CONFIG_INVALID`，不得静默跳过这台 server。
- **评测先行**：先写失败的测试（§4 Step 1），再改实现。

## 3. 必须复用的现有组件（不得另写一份）

| 需要 | 用这个 | 位置 |
|---|---|---|
| 运行时绑定 | `_MCP_RUNTIME_BINDINGS` / `McpToolBinding`（如需只读查询函数，加在 inventory 里） | `runtime/governance/inventory.py` |
| 拒绝的返回形态 | `_block(rule_id, CODE_GUARDRAIL_BLOCKED, detail)` | `runtime/governance/pipeline.py` |
| 配置错误码 | `MCP_CONFIG_INVALID`（`load_gateway_config` 已包装 pydantic 校验失败） | `mcp/config.py`、`mcp/errors.py` |
| 快照 | `python -m water_agent.harness.tool_assembly_snapshot --diff / --write --reason` | `harness/tool_assembly_snapshot.py` |

## 4. 实施步骤（一步一个 commit）

### Step 1：先写失败的测试

- 新增 `tests/test_mcp_wiring.py::test_ready_flag_rejected_until_confirmation_gate_exists`：
  - 一份把 `confirmation_handler_ready` 设为 true 的配置，期望 `MCP_CONFIG_INVALID`；
  - 直接构造 `MCPGatewayConfig(confirmation_handler_ready=True)` 也被拒绝。
- 新增 `tests/test_tool_policy.py::test_confirmation_required_tool_blocked_at_call_time`：
  - 用 `register_mcp_tool_binding` 登记一个 `requires_human_confirmation=True` 的绑定；
  - 主体持有 `MCP_INVOKE` 时调用仍被拒绝，规则 id 为 `tool.confirmation_required`；
  - 同名但不需要确认的绑定照常放行（反证）。
- docstring 写上验收 ID（P7-S1-A01 / A02）。

### Step 2：配置期拒绝

### Step 3：执行期拒绝 + 注释对齐

### Step 4：快照有意变更

- 预期变化：`session_mcp_ready_flag` 由"暴露并放行"变为"配置错误"
  （`exception.code == "MCP_CONFIG_INVALID"`、captures 0、surface 为 null）。
  其余 14 个 profile 不变，范围块的 `frozen_json` 已冻结它们。
- 递进 `TOOL_POLICY_VERSION`（策略变了）。
- 跑 `--diff`，确认只有上述变化后，再 `--write --reason "P7-S1：确认门执行期强制"`。
- 把 `tests/test_p7_known_figures.py::test_confirmation_flag_is_not_enforced_at_call_time` 改写为
  `test_confirmation_gate_is_enforced`，docstring 写 P7-S1-A03，断言新的事实。
- 同步改规格 §2 的 F7 一行。

### Step 5：收尾

- 规格 §7 的 P7-S1 三行状态翻成 `delivered`；
- 写 `docs/design/p7-step-1-decision.md`（结构见规格 §8）；
- AGENTS.md 补一条。

## 5. 范围块

<!-- step-scope -->
```json
{
  "step": "P7-S1",
  "allow": [
    "src/water_agent/mcp/config.py",
    "src/water_agent/mcp/bridge.py",
    "src/water_agent/mcp/errors.py",
    "src/water_agent/runtime/governance/pipeline.py",
    "src/water_agent/runtime/governance/inventory.py",
    "src/water_agent/runtime/governance/rules.py",
    "tests/test_mcp_wiring.py",
    "tests/test_mcp_gateway.py",
    "tests/test_mcp_behaviors.py",
    "tests/test_tool_policy.py",
    "tests/test_governance_contracts.py",
    "tests/test_p7_known_figures.py",
    "data/eval/tool_assembly_main.json",
    "data/mcp_servers.json",
    "docs/design/p7-step-1-decision.md",
    "docs/design/16-p7-plugin-mcp.md",
    "docs/design/14-p4-mcp-gateway.md",
    "docs/execution-prompts/P7-S1-confirmation-gate.md",
    "AGENTS.md"
  ],
  "forbid": [
    "src/water_agent/plugins/**",
    "src/water_agent/runtime/agentic.py",
    "src/water_agent/harness/**",
    "src/water_agent/tools/**",
    "src/water_agent/prompting/**",
    "src/water_agent/graphs/**",
    "src/water_agent/webapp/**",
    "src/water_agent/cli.py",
    "scripts/**",
    ".github/**",
    "tests/conftest.py",
    "tests/test_tool_assembly_snapshot.py",
    "tests/test_p7_traceability.py",
    "tests/test_check_step_scope.py",
    "tests/test_import_guard.py",
    "tests/test_answer_cache.py",
    "data/eval/baseline.json"
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
      "node": "tests/test_governance_contracts.py::TestInventoryCoverage::test_all_provider_sites_governed_after_step12",
      "contains": "\"provider_call_sites\": 7"
    },
    {
      "node": "tests/test_governance_contracts.py::TestInventoryCoverage::test_all_provider_sites_governed_after_step12",
      "contains": "\"tools_guardrail_validated\": 5"
    }
  ],
  "frozen_json": [
    {
      "path": "data/eval/tool_assembly_main.json",
      "pointers": [
        "/versions/PROMPT_TEMPLATE_VERSION",
        "/profiles/runner_*",
        "/profiles/session_mock_default",
        "/profiles/session_cli_default",
        "/profiles/session_drawing_arg",
        "/profiles/session_drawing_env",
        "/profiles/session_mcp_fake",
        "/profiles/session_mcp_confirm_skip",
        "/profiles/session_mcp_config_invalid",
        "/profiles/session_web_member",
        "/profiles/session_mcp_register_fault"
      ]
    }
  ]
}
```

## 6. 受保护断言

- 本地工具 schema `< 4500`、系统提示 `< 1950`、治理覆盖率 `{7, 7, 5, 5}`，都不得改动。
- 本步不新增工具，覆盖率数字本来就不该变；如果变了，说明改错了地方。

## 7. 停止条件

满足任一条就停下，写清现象后等确认，不自行扩大设计：

- `--diff` 出现 `session_mcp_ready_flag` 与 `/versions/TOOL_POLICY_VERSION` 以外的变化；
- 需要改范围块 `forbid` 里的文件，或 `allow` 之外的文件；
- 现有测试依赖 `confirmation_handler_ready=true` 的旧行为（S0 时查过：没有；如果出现，先报告）；
- 门禁出现与本步无关的红灯。

## 8. 验证命令（完成前全部跑过，贴出输出）

```bash
export VENV=/Users/yaerken/Desktop/给排水agent/_venv PYTHONPATH=src
git fetch origin main
$VENV/bin/pytest -q tests/test_mcp_wiring.py tests/test_tool_policy.py tests/test_p7_known_figures.py tests/test_tool_assembly_snapshot.py tests/test_p7_traceability.py
$VENV/bin/python -m water_agent.harness.tool_assembly_snapshot --check
$VENV/bin/python scripts/check_step_scope.py docs/execution-prompts/P7-S1-confirmation-gate.md --base origin/main
COMPOSE_PROJECT_NAME=p7s1 VENV=$VENV bash scripts/ci_gate.sh
```

（`PYTHONPATH=src` 让 worktree 里的 pytest 测本检出的代码；`ci_gate.sh` 会自己设置。）

## 9. 完成定义

- 规格 §8 第 5 条的全部项；
- PR 上的 `p7-scope` 检查与 `p5-ci` 通过；
- 决策记录 §3 贴出 `--diff` 原文。

## 10. 汇报格式

- 改了哪些文件；
- 验证命令与实际输出（失败照贴）；
- 范围守卫的输出；
- 快照差异；
- 未验证项；
- 残余风险。

## 11. 常见坑（本仓库实测过）

- worktree 里 `_venv` 的 editable install 指向主检出：不设 `PYTHONPATH=src` 时，pytest 会被 H0 拦下（exit 4）。
- 运行时绑定登记 `_MCP_RUNTIME_BINDINGS` 只增不删：测试里登记绑定后要用 `clear_mcp_tool_bindings` 清理，
  或者换一个不会冲突的工具名。
- zsh 下 `"$t:local"` 会被 `:l` 修饰符吃掉，打镜像 tag 要写 `"${t}:local"`。
- 零宽字符在测试和规则里一律写成 `\uXXXX` 转义。
