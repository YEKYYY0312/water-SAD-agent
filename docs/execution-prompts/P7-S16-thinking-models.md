# P7-S16 执行 Prompt：思考型模型（回传、清理、探针、展示）

> **工作区**：仓库 `YEKYYY0312/water-SAD-agent`，基于最新 `main`（必须已包含本 Prompt 与 **P7-S15**）。新建分支 `YEK/p7-s16-thinking-models`。
> **2026-11-04 准入基线**：main `dd8c314`，11 个出站调用点、21 个装配 profile；本次只校正准入口径，不代表 D4–D8 已放行。真实证据见 `docs/evals/p7-s16-preflight-2026-11-04.md`。
> **必读**：
> - `AGENTS.md`
> - `docs/design/18-p7-multi-model.md`（SDD：§2 事实表 F1–F23、§4 设计、§6 不变量 P7-I18/I19/I20、§7 的 P7-S16 验收、§10 待实测项）与 `docs/design/18-p7-multi-model-harness.md` §2.1、§4
> - `docs/design/p7-step-15-decision.md`（S15：目录 / 逐对验证 / 选择链路 / `thinking: off`）
> - `docs/design/p2.5-threat-model.md` T14，以及本步新增的 T18
> - 参考实现：`graphs/agent_rag.py::DraftPump`（逐句 `check_output`、撤回、关闭通道、`offset`）、`webapp/run_manager.py::_on_draft`（事件白名单与脱敏）、`generation/governed_chat.py`（`_stream` / `_generate`）、`prompting/history.py`
> - 官方文档：`api-docs.deepseek.com/guides/thinking_mode`（SDD F1–F4 的出处）

---

## 0. 相对规格的修订与本步决定（以本文为准）

| 疑问 | 本步做法 | 原因 |
|---|---|---|
| **开工前置（硬门槛）** | SDD §10 的 **D4–D8（旧轮 / 合成形态是否 400、空串是否被接受、超时分布、`deepseek-chat` 的实际路由）必须已用真实 Key 实测并回填 SDD §2 / §4.2**。任何一项仍是"待实测"→ **本步停止，不得凭文档推测写策略表** | 回传策略表是假设；文档措辞很强，实际严格度只能实测。没有 Key = blocked |
| 子类位置 | `ChatOpenAI` 子类写在 `generation/chat_model.py` 内，覆写 `_create_chat_result`、`_convert_chunk_to_generation_chunk`、`_get_request_payload`；**不猴补丁**模块级 `_convert_*` 函数（全局生效）；**仅目录 `thinking: roundtrip` 的条目使用**，默认路径保留字面量 `ChatOpenAI(` | SDD F12/F13；默认路径与 `test_chat_model.py` 的构造断言不变 |
| 出站白名单 | assistant 消息**只**允许多一个非标准键 `reasoning_content`；其余 `additional_kwargs`（如 `water_agent_turn_status`）一律不外发（`test_the_status_stamp_never_reaches_the_provider`）；`_get_request_payload` 只改 `payload["messages"]`（Responses 路径用 `input`，不碰）| P7-I19 |
| 开关 thinking | 用 `extra_body={"thinking":{"type":"enabled"}}`；**不用** `reasoning=` 字段（会让 `_use_responses_api` 变 True，走错路径）| 调研 §3 |
| 回传策略 | 以 SDD §4.2 回填后的结论为准；默认实现 = 本轮全部回传 + 旧轮终稿保留 reasoning（`history.py` 对 `roundtrip` 条目）+ 合成 assistant 按实测处理 + 目标模型不支持时剥离 | SDD F14/F15；跨模型串历史 |
| 清理 | `finalize` / `_rewrite` 在回答被拦截或脱敏时**同时清除** reasoning；收口时清除本轮中间 AIMessage 的 reasoning（同 id 替换）；当前轮 reasoning 设字符上限；内联 `<think>` 剥离或拒绝该模型，**绝不进入答案** | SDD F16；P7-I18 |
| 探针 | `endpoint_compat.py` 新增检查：**工具往返**、**流式 + 工具**、**合成 assistant 形态**；evidence 记**响应里的实际服务模型**；**递进 `ENDPOINT_PROBE_VERSION`**（所有既有非缺省报告作废——决策记录写部署影响与运维重跑步骤）；复用 `local_model_compat.probe_tools()`（P7-I02），新检查放进 `endpoint_compat.py`，不改 Step 46 本地模型探针的检查集 | SDD F11；P7-I02 |
| 展示 | 新 `ThinkingPump`：事件 `thinking_delta`（带 `offset`）/ `thinking_closed`；**每句放行前对累计文本调 `check_output`**，命中即整段撤回并关闭通道，原文**不进任何事件 / trace**；每 run 字符上限；`aask` / `aresume` 加 `on_thinking`，工厂标志 `supports_thinking_stream` 保护协议兼容；**永不进** answer / 历史投影 / 导出 / 缓存 / `ConversationMessage` / 旧 `token` 通道 | P7-I18；用户已决定"折叠展示，先过护栏" |
| 前端 | `<details className="thinking-block">`，纯文本、`white-space: pre-wrap`，**不渲染 Markdown / 链接 / 图片**（不复用 `AnswerMarkdown`），文案"思考摘要 · 未经校验 · 不构成设计依据"，位置在草稿之前；`finalize` 清空；`offset` 不衔接的 delta 丢弃；不进 `archiveRun` / `mergeHistory` | SDD §4.5 |
| 治理 | 思考模型的超时画像（取值以实测 p95 为准，初值沿用 60s 并**禁用超时重试**以免成本翻倍）；`ProviderUsage` / `UsageRecord` 增 reasoning token 字段（`extra="forbid"`，同步测试）；输入估算计入 reasoning；`GOVERNANCE_CONFIG_VERSION` 递进；`400` 被网关吞成"上游不可用"的误诊要收口成**可诊断**的错误（提示"该模型要求回传思考内容"）| SDD F17 |
| 放行 | S15 里返回 `WEB_MODEL_THINKING_UNSUPPORTED` 的 `roundtrip` 条目，**仅在该对通过新版探针后**才可选 | 不放行假通过 |
| 评测 | `thinking_p0`（~38 条，Step 1 冻结）接进 `ci_gate.sh` 与入仓基线；假端点按**官方文档原文**建模 `require_passback` | 评测先行；harness §8 的缓解 |

## 1. 目标与边界

**做**（编号对应 SDD §7 的 P7-S16-Axx）：子类与往返（A01、A02）；清理（A03）；探针扩展与版本递进（A04）；`ThinkingPump` 与协议标志（A05、A07）；通道隔离（A06）；治理（A08）；前端（A09）；`thinking_p0`（A10）；快照（A11）；文档（威胁模型 T18、`p2.5-provider-inventory.md`、`16-p7` §3.11、AGENTS.md、决策记录 `docs/design/p7-step-16-decision.md`）。

**不做**：自动选模型 / 失败换模型；多端点；新增 `Capability` / 出站点（`PROVIDER_CALL_SITES` 仍是 11）；改 `prompting/assembler.py`（若需要递进 `PROMPT_TEMPLATE_VERSION` → **停止**，见 §5）；"再调一次模型总结思考"；把思考纳入答案、引用校验、缓存或导出；声称思考模型更准或质量不变。

## 2. 步骤序列（评测先行；一个逻辑面一个 commit）

1. **Step 0 前置核对**：确认 SDD §10 D4–D8 已回填；未回填则停止。
2. **Step 1 冻结**：`data/eval/thinking_p0.jsonl`（~38 条）、指标阈值、比较器骨架。
3. **Step 2 harness**：扩展假端点（`reasoning`、`require_passback`、`passback_empty_ok`、`two_step`、`stream_tools`、`served_model`、`inline_think`、`slow_reasoning`）、`harness/thinking_eval.py`；红灯基线记入决策记录。
4. **Step 3 机制**：子类 → 回传策略 → 清理 → 探针 → `ThinkingPump` / 标志 → 治理 → 前端。
5. **Step 4 反证与基线**；**Step 5 快照**（新 profile `session_reasoning_roundtrip`，`session_r` 开头；冻结规则 `session_[!r]*`）与追溯。
6. **真实 Key 对照**（有 Key 才做，否则 blocked）：用真实 `deepseek-flash` 对照假端点的 `require_passback` 行为，差异写进决策记录。

## 3. 验证命令

```bash
export PYTHONPATH=src   # worktree 里必须；并 unset WATER_AGENT_* / *_API_KEY / HTTPS_PROXY / LOCAL_MODEL_*
_venv/bin/pytest -q tests/test_reasoning_chat_model.py tests/test_reasoning_roundtrip.py tests/test_reasoning_cleanup.py tests/test_thinking_pump.py tests/test_thinking_isolation.py tests/test_thinking_eval.py tests/test_endpoint_compatibility.py tests/test_governed_chat_streaming.py tests/test_agentic_runtime_robustness.py tests/test_prompt_history_window.py tests/test_provider_gateway.py tests/test_governance_contracts.py tests/test_web_runs.py
_venv/bin/python -m water_agent.harness.thinking_eval data/eval/thinking_p0.jsonl --report-path data/eval/reports/thinking_p0_head.json
_venv/bin/python scripts/compare_thinking_p0.py --head data/eval/reports/thinking_p0_head.json --base data/eval/thinking_p0_main.json
_venv/bin/python -m water_agent.harness.tool_assembly_snapshot --check
(cd web/app && npm test && npm run build)
COMPOSE_PROJECT_NAME=p7s16 bash scripts/ci_gate.sh
_venv/bin/python scripts/check_step_scope.py docs/execution-prompts/P7-S16-thinking-models.md --base origin/main
```

## 4. 受保护断言

- 预算：`assert cost < 4500`、`schema_chars_total == 4497`、系统提示 `< 1950`。
- 信任：`INSTRUCTION_BEARING_SOURCES == {SYSTEM_POLICY}`。
- 出站：`len(PROVIDER_CALL_SITES) == 11`；默认构造参数（`temperature == 0.0`、`max_retries == 0`、`stream_usage is True`）不变。
- 出站白名单：`test_the_status_stamp_never_reaches_the_provider`。
- **会变的锁定数字 / 语义**（本步唯一允许改的）：`ENDPOINT_PROBE_VERSION` 与检查集（`ENDPOINT_COMPATIBILITY_CHECKS`）、`UsageRecord` / `ProviderUsage` 的字段集合测试、`GOVERNANCE_CONFIG_VERSION`、`_EGRESS_PATTERNS` 的模式数量断言（若有）。

## 5. 停止条件

满足任一条就停下，写清现象后等确认：

- SDD §10 D4–D8 未回填（Step 0）；
- 现有快照 profile 有任何变化（`session_[!r]*` 与 `runner_*` 冻结）；缺省配置（目录里没有 `roundtrip` 条目）下任何机制层评测或 `baseline.json` 对比出现差异；
- 需要改 `prompting/assembler.py`（含递进 `PROMPT_TEMPLATE_VERSION`）、`pipeline.py`、`rules.py`、`plugins/`、`mcp/`、`tools/`，或需要新增 `Capability` / `ProviderKind` / 出站模块；
- 思考文本需要绕过 `check_output` 才能展示、或需要进入答案 / 历史 / 导出 / 缓存；
- 新探针在缺省 `deepseek-chat`（有 Key 时）上不能全部 `passed`——不得放宽检查；
- 门禁出现与本步无关的红灯。

## 6. 汇报格式

改了哪些文件；跑了哪些验证命令以及实际输出（失败照贴）；范围守卫输出；快照差异；**探针版本递进的部署影响与运维重跑步骤**；**未验证项与原因（P7-S16-A12 真实 `deepseek-flash` 实测）**；遗留风险。**不得**写"思考模型更准"或"换模型后质量不变"。

---

<!-- step-scope -->
```json
{
  "step": "P7-S16",
  "allow": [
    "src/water_agent/generation/chat_model.py",
    "src/water_agent/generation/governed_chat.py",
    "src/water_agent/generation/model_catalog.py",
    "src/water_agent/generation/compatibility.py",
    "src/water_agent/harness/endpoint_compat.py",
    "src/water_agent/harness/thinking_eval.py",
    "src/water_agent/harness/doubles.py",
    "src/water_agent/harness/tool_assembly_snapshot.py",
    "src/water_agent/prompting/history.py",
    "src/water_agent/runtime/agentic.py",
    "src/water_agent/runtime/cancellation.py",
    "src/water_agent/runtime/governance/config.py",
    "src/water_agent/runtime/governance/contracts.py",
    "src/water_agent/runtime/governance/gateway.py",
    "src/water_agent/runtime/governance/inventory.py",
    "src/water_agent/graphs/agent_rag.py",
    "src/water_agent/webapp/run_manager.py",
    "src/water_agent/webapp/run_routes.py",
    "src/water_agent/webapp/settings_routes.py",
    "src/water_agent/webapp/app.py",
    "src/water_agent/webapp/auth/audit.py",
    "scripts/compare_thinking_p0.py",
    "scripts/ci_gate.sh",
    "data/eval/thinking_p0.jsonl",
    "data/eval/thinking_p0_main.json",
    "data/eval/tool_assembly_main.json",
    "web/app/src/**",
    "web/app/tests/**",
    "tests/fixtures/endpoint_compat/fake_openai_server.py",
    "tests/test_reasoning_chat_model.py",
    "tests/test_reasoning_roundtrip.py",
    "tests/test_reasoning_cleanup.py",
    "tests/test_thinking_pump.py",
    "tests/test_thinking_isolation.py",
    "tests/test_thinking_eval.py",
    "tests/test_endpoint_compatibility.py",
    "tests/test_governed_chat_streaming.py",
    "tests/test_agentic_runtime_robustness.py",
    "tests/test_prompt_history_window.py",
    "tests/test_draft_pump.py",
    "tests/test_web_stream_guardrail.py",
    "tests/test_provider_gateway.py",
    "tests/test_governance_contracts.py",
    "tests/test_chat_model.py",
    "tests/test_llm_endpoints.py",
    "tests/test_web_runs.py",
    "tests/test_web_sse.py",
    "tests/test_web_sessions.py",
    "tests/test_web_model_selection.py",
    "tests/test_model_verification.py",
    "tests/test_tool_assembly_snapshot.py",
    "tests/test_p7_known_figures.py",
    "docs/design/p7-step-16-decision.md",
    "docs/design/18-p7-multi-model.md",
    "docs/design/18-p7-multi-model-harness.md",
    "docs/design/16-p7-plugin-mcp.md",
    "docs/design/p2.5-threat-model.md",
    "docs/design/p2.5-provider-inventory.md",
    "docs/execution-prompts/P7-S16-thinking-models.md",
    "AGENTS.md"
  ],
  "forbid": [
    "src/water_agent/runtime/governance/pipeline.py",
    "src/water_agent/runtime/governance/rules.py",
    "src/water_agent/prompting/assembler.py",
    "src/water_agent/plugins/**",
    "src/water_agent/mcp/**",
    "src/water_agent/graphs/confirmation.py",
    "src/water_agent/tools/**",
    "src/water_agent/multi_agent/**",
    "src/water_agent/storage/**",
    "alembic/**",
    "alembic.ini",
    "src/water_agent/generation/endpoints.py",
    ".github/**",
    "compose.yml",
    "compose.production.yml",
    "tests/conftest.py",
    "tests/test_p7_traceability.py",
    "tests/test_check_step_scope.py",
    "tests/test_import_guard.py",
    "tests/test_tool_exposure_eval.py",
    "data/eval/baseline.json",
    "data/eval/model_choice_p0.jsonl",
    "data/eval/model_choice_p0_main.json",
    "data/eval/tool_exposure_p0.jsonl",
    "data/eval/tool_exposure_p0_main.json",
    "data/eval/skill_p0_main.json",
    "data/eval/mcp_http_p0_main.json",
    "data/eval/drawing_p0_main.json",
    "data/eval/drawing_gen_p0_main.json",
    "data/eval/memory_p0_main.json",
    "data/eval/multi_agent_p0_main.json"
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
      "node": "tests/test_governance_contracts.py::TestSourceTagNotDowngradable::test_only_system_policy_bears_instructions",
      "contains": "assert INSTRUCTION_BEARING_SOURCES == frozenset({SourceTag.SYSTEM_POLICY})"
    },
    {
      "node": "tests/test_p7_known_figures.py::test_local_tool_schema_headroom_is_three_characters",
      "contains": "assert surface[\"schema_chars_total\"] == 4497"
    },
    {
      "node": "tests/test_chat_model.py::test_build_chat_model_defaults_temperature_zero",
      "contains": "assert captured[\"temperature\"] == 0.0"
    },
    {
      "node": "tests/test_chat_model.py::test_build_chat_model_enables_stream_usage",
      "contains": "assert captured[\"max_retries\"] == 0"
    },
    {
      "node": "tests/test_agentic_runtime_robustness.py::TestHistoryKeepsEachTurnsOutcome::test_the_status_stamp_never_reaches_the_provider",
      "contains": "assert TURN_STATUS_KEY not in wire"
    }
  ],
  "frozen_json": [
    {
      "path": "data/eval/tool_assembly_main.json",
      "pointers": [
        "/versions/MCP_CONFIG_VERSION",
        "/versions/TOOL_POLICY_VERSION",
        "/profiles/session_[!r]*",
        "/profiles/runner_*"
      ]
    }
  ]
}
```
<!-- /step-scope -->

> `frozen_json` 说明：现有 profile（S12 / S13 合入后是 21 个：16 个 `session_*` + 5 个 `runner_*`）**整段**冻结（包含 S15 新增的 `session_pick_model_off`）；`MCP_CONFIG_VERSION` 与 `TOOL_POLICY_VERSION` 冻结，**`PROMPT_TEMPLATE_VERSION` 刻意不冻结**——`prompting/history.py` 的窗口策略若改变了发给模型的输入，应递进它；但递进它需要改 `prompting/assembler.py`（在 forbid 里），所以按 §5 **先停下确认**，由评审决定是把 `assembler.py` 临时加入 allow，还是把窗口改动限定在不影响版本号的范围。新增的 `session_reasoning_roundtrip` 以 `session_r` 开头，不被 `session_[!r]*` 匹配。
>
> `compose.yml`、`storage/**`、`alembic/**` 在 forbid 里：本步不动部署与数据库（S15 已完成迁移）；`contracts.py` / `gateway.py` / `inventory.py` 在 allow 里是因为 reasoning token 字段、超时重试策略与出站扫描模式扩展都落在那里，改动必须逐处附测试。
