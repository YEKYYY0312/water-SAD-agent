# P7-S15 执行 Prompt：模型目录与按会话 / 按消息选择（不开思考）

> **工作区**：仓库 `YEKYYY0312/water-SAD-agent`，基于最新 `main`（必须已包含本 Prompt 与 `docs/design/18-p7-multi-model.md`）。新建分支 `YEK/p7-s15-model-catalog`。
> **必读**：
> - `AGENTS.md`
> - `docs/design/18-p7-multi-model.md`（SDD：§0 修订、§2 事实表、§3 设计、§6 不变量 P7-I15/I16/I17/I21、§7 的 P7-S15 验收、§8 实施协议）与 `docs/design/18-p7-multi-model-harness.md` §2、§3
> - `docs/design/16-p7-plugin-mcp.md` §3.11（端点与兼容性门槛）、§6（防漂移 harness）、§8
> - `docs/design/p7-step-10-decision.md`（S10：端点可配置、兼容性硬门槛、**§4 "多端点路由 / 按用户选模型"当时列为不做**——本步是它的有条件放开）、`docs/design/p2.5-threat-model.md` T14
> - 参考实现：`generation/endpoints.py::write_panel_endpoint`（0600 原子写）、`webapp/settings_routes.py`（管理员 + CSRF 路由、审计、`_locked_reason()`）、`webapp/run_routes.py::create_message`（校验顺序）、`webapp/run_manager.py`（`supports_*` 标志与"有值才传 kwarg"的写法，约 628–637 行注释）

---

## 0. 相对规格的修订与本步决定（以本文为准）

| 疑问 | 本步做法 | 原因 |
|---|---|---|
| 开工前置 | **Step 0 的真实事实（SDD §10 的 D6、D7）可以没回填**：本步只做 `thinking: off` 与目录 / 选择 / 持久化，不依赖思考行为。但 `deepseek-flash` 的**真实模型 ID** 必须已从设置页「获取模型」确认，目录样例用它 | 本步的机制不依赖真实质量；质量证据是 blocked 项 P7-S15-A13 |
| 范围 | **同端点、同 Key 槽、只换 model id**；请求体与会话 PATCH **不接受 `base_url`、`api_key`**；`LOCAL_MODEL_ENABLED` 或 env 锁定（`_locked_reason()` 非空）时**拒绝**任何逐条 / 会话级覆盖（SDD F20） | 密钥只发给所属服务商；本地模型模式下只带 `model` 的覆盖会跳到 DeepSeek 缺省端点 |
| 目录存放 | 凭据卷 `llm_models.json`（0600、原子写），**不动** `llm_endpoint.json`；缺省不配目录 = 行为逐字不变 | 与 `write_panel_endpoint` 同构；回滚 = 删文件 |
| `thinking` 取值 | 目录项 `thinking: "off"` 才可被选；`"roundtrip"` **可以保存但选择时返回 `WEB_MODEL_THINKING_UNSUPPORTED`**（S16 才放行） | 本步没有回传与展示能力；放行等于把假通过的探针暴露给用户 |
| 逐对验证 | 新增"每对一个文件"的报告存储；**保持 7 项检查与 `ENDPOINT_PROBE_VERSION` 不变**；`endpoint_verification` 先走原单文件逻辑再回落逐对；读出后**仍调 `matches()`** | 不使既有报告作废；文件名只是索引 |
| 免检 | `ChatEndpoint.is_default` 定义**不变**（仅 `deepseek-chat` + 缺省端点）；目录项永远走逐对验证；**不得为用 flash 改 `DEFAULT_CHAT_MODEL`** | 铁律③：免检转给没有质量证据的模型 |
| 校验顺序 | 会话归属 → 项目写权限 → **模型校验（新）** → 附件 → 互斥 / 挂起 → 建 run；`403` 先于 `424`（`test_project_write_refusal_precedes_model_configuration_disclosure`） | 授权在前，配置披露在后 |
| 协议兼容 | 工厂声明 `supports_model_choice` 才传 `model` kwarg；`_args()` 默认值保持 `None` | 10 处严格签名替身不破（SDD F23）；`test_defaults_are_unchanged` |
| 持久化 | 迁移 `0008`：`web_sessions.model`、`runs.model`（nullable `VARCHAR(200)`）；revision ≤ 32 字符；同步 `storage/database.py:21`、`compose.yml` 的 `SMOKE_EXPECTED_MIGRATION_HEAD`、`tests/test_memory_store.py:103` | 用户已决定新增迁移（会话默认跨设备同步、重启后 resume 恢复所选模型）|
| resume | `resume` 优先读 `runs.model`；读不到 → 会话默认 → 全局，**并发 `warning`**；**绝不换成目录里的另一个模型**（P7-I21） | 修 SDD F21；禁止静默换模型 |
| 价格 / 成本 | 目录项 `price` 并入 `_default_prices`（`confirmed=False`，**不做硬拒绝**）；agentic 路径的 trace 成本改走 `price_for(model)`，**未登记 = `None`**；legacy / docgen / judge 的写死价**不动**（已知限制） | 铁律：未核对的价格不得当账单 |
| 限流桶 | 仍含 model；靠目录白名单有界；请求体不收未登记 id | SDD F19 |
| 前端 | composer 内选择器（目录非空才渲染）、会话默认控件、Inspector「本次模型」卡、目录管理区放在「模型与密钥」页内（**不新增副栏子页**）；文案不得暗示"高级 = 更准" | `settings_nav.test.mjs:18` 钉死子页 id；T14 |
| 评测 | `model_choice_p0`（~42 条，Step 1 冻结）接进 `ci_gate.sh` 与入仓基线 | 评测先行 |

## 1. 目标与边界

**做**（编号对应 SDD §7 的 P7-S15-Axx）：

1. **目录（A01）**：`generation/model_catalog.py`（读写、校验、原子写 0600）；管理员读写路由在 `settings_routes.py`（`require_admin` + CSRF，env 锁定只读，审计只记 id / 动作 / 结果）；成员只读 id / label / tier / 已验证。
2. **逐对验证（A02）**：`compatibility.py` 增逐对存储；`endpoint_compat.py::run_endpoint_compatibility` 与面板接口接受可选 `model`，且**按目录项的 `thinking` 配置构造被测模型**（与生产同一个 `build_chat_model(extra_body=…)`），否则探针验证的配置与生产不一致（SDD §3.3）。
3. **构造（A03）**：`build_chat_model(extra_body=)`，仅设置时传；`thinking: off` → `{"thinking":{"type":"disabled"}}`。
4. **选择链路（A04、A05）**：`MessageRequest.model`、预检 `chat_model_problem(model=)`、`manager.start(model=)`、`_RunHandle.model`、工厂 `supports_model_choice`。
5. **持久化（A06、A07）**：迁移 0008；`PATCH /api/sessions/{thread_id}`；`resume` 读 `runs.model`；会话 payload 新增 `model`（有值才出现）。
6. **治理（A08）**：价格并入、成本按模型、`model_selected` trace 事件、`_result_payload` 带 `model`、`GOVERNANCE_CONFIG_VERSION` 递进。
7. **前端（A10）**与**评测（A11）**、**部署（A12）**。
8. **文档**：威胁模型 T14 扩展、`p2.5-provider-inventory.md`、`16-p7` §3.11、AGENTS.md、决策记录 `docs/design/p7-step-15-decision.md`。

**不做**：自动选模型 / 失败换模型 / 预算降级；多端点与跨服务商 Key；新增 `Capability` 或 `ProviderKind` 或出站点（`PROVIDER_CALL_SITES` 仍是 8）；`thinking: roundtrip` 的任何行为（S16）；改探针检查项与 `ENDPOINT_PROBE_VERSION`；改 `DEFAULT_CHAT_MODEL` 或免检语义；改 `prompting/**`；legacy / docgen / judge 的成本写死价。

## 2. 步骤序列（评测先行；一个逻辑面一个 commit）

1. **Step 1 冻结**：`data/eval/model_choice_p0.jsonl`（~42 条，类别见 harness §3）、指标阈值、比较器骨架。
2. **Step 2 harness**：扩展 `tests/fixtures/endpoint_compat/fake_openai_server.py`（`requests` 追加 `messages`、多模型；**S15 不需要** `reasoning` 类模式）、`harness/model_choice_eval.py`；此时机制未实现，红灯基线记入决策记录。
3. **Step 3 机制**：目录 → 逐对验证 → 构造 → 选择链路 → 迁移与持久化 → 治理 → 前端；每块先写失败测试（docstring 写验收 ID）。
4. **Step 4 反证与基线**：补齐每个硬指标的反证；生成入仓基线；接 `scripts/ci_gate.sh`。
5. **Step 5 快照与追溯**：新增快照 profile `session_pick_model_off`（以 `session_p` 开头，不被 `session_[!p]*` 匹配）；`PINNED_ENV_KEYS` 仅在新增 env 时增加；追溯检查把 SDD 纳入。

## 3. 验证命令

```bash
export PYTHONPATH=src   # worktree 里必须；并 unset WATER_AGENT_* / *_API_KEY / HTTPS_PROXY / LOCAL_MODEL_*
_venv/bin/pytest -q tests/test_model_catalog.py tests/test_model_verification.py tests/test_web_model_selection.py tests/test_model_governance.py tests/test_model_choice_eval.py tests/test_chat_model.py tests/test_llm_endpoints.py tests/test_endpoint_compatibility.py tests/test_web_llm_endpoint.py tests/test_p5_step38_storage.py tests/test_web_deployment_overlay.py tests/test_memory_store.py
_venv/bin/python -m water_agent.harness.model_choice_eval data/eval/model_choice_p0.jsonl --report-path data/eval/reports/model_choice_p0_head.json
_venv/bin/python scripts/compare_model_choice_p0.py --head data/eval/reports/model_choice_p0_head.json --base data/eval/model_choice_p0_main.json
_venv/bin/python -m water_agent.harness.tool_assembly_snapshot --check
(cd web/app && npm test && npm run build)
COMPOSE_PROJECT_NAME=p7s15 bash scripts/ci_gate.sh      # 本机有别的 water-agent 栈在跑时必须隔离
_venv/bin/python scripts/check_step_scope.py docs/execution-prompts/P7-S15-model-catalog-and-selection.md --base origin/main
```

另跑 `pytest -o addopts= -q`（ci_gate 的 pytest 是 `-qq`，没有汇总行）取全量计数。

## 4. 受保护断言

- 预算：`assert cost < 4500`、`schema_chars_total == 4497`、系统提示 `< 1950`（本步不碰工具与提示）。
- 信任：`INSTRUCTION_BEARING_SOURCES == {SYSTEM_POLICY}`。
- 出站：`len(PROVIDER_CALL_SITES) == 8`（本步不新增出站点）。
- 缺省不变：`test_defaults_are_unchanged`（`resolve_chat_endpoint()` 缺省 + `is_default`）、`temperature == 0.0`、`max_retries == 0`、`stream_usage is True`。
- **会变的锁定数字 / 语义**（本步唯一允许改的）：会话 payload 键集合（`test_web_authorization.py`）、`MIGRATION_HEAD` 三处、`GOVERNANCE_CONFIG_VERSION`、前端 `sendMessage` 请求体断言（仍要求"未选就不出现"）。

## 5. 停止条件

满足任一条就停下，写清现象后等确认：

- 现有 18 个快照 profile 有任何变化（`session_[!p]*` 与 `runner_*` 冻结）；缺省配置（不配目录）下任何机制层评测或 `baseline.json` 对比出现差异；
- 需要改 `gateway.py` / `pipeline.py` / `contracts.py` / `inventory.py` / `plugins/` / `mcp/` / `prompting/` / `graphs/confirmation.py`，或需要新增 `Capability` / `ProviderKind` / 出站模块；
- 需要放宽 `is_default`、收 `base_url`、让模型被自动更换，或上调任何预算上限；
- 严格签名替身因新增 kwarg 而 `TypeError`（说明"有值才传"没做对）；
- 兼容性探针在缺省端点上（有 Key 时）不能全部 `passed`——不得通过放宽检查解决；
- 门禁出现与本步无关的红灯。

## 6. 汇报格式

改了哪些文件；跑了哪些验证命令以及实际输出（失败照贴）；范围守卫输出；快照差异；**未验证项与原因（P7-S15-A13 真实 Key 实测、真实质量层）**；遗留风险。**不得**写"换模型后质量不变"或"高级模型更准"。

---

<!-- step-scope -->
```json
{
  "step": "P7-S15",
  "allow": [
    "src/water_agent/generation/model_catalog.py",
    "src/water_agent/generation/compatibility.py",
    "src/water_agent/generation/chat_model.py",
    "src/water_agent/generation/cost.py",
    "src/water_agent/harness/endpoint_compat.py",
    "src/water_agent/harness/model_choice_eval.py",
    "src/water_agent/harness/tool_assembly_snapshot.py",
    "src/water_agent/runtime/agentic.py",
    "src/water_agent/runtime/governance/config.py",
    "src/water_agent/graphs/agent_rag.py",
    "src/water_agent/observability/json_trace.py",
    "src/water_agent/webapp/model_choice.py",
    "src/water_agent/webapp/run_routes.py",
    "src/water_agent/webapp/run_manager.py",
    "src/water_agent/webapp/workspace_routes.py",
    "src/water_agent/webapp/settings_routes.py",
    "src/water_agent/webapp/app.py",
    "src/water_agent/webapp/auth/audit.py",
    "src/water_agent/webapp/auth/dependencies.py",
    "src/water_agent/storage/models.py",
    "src/water_agent/storage/database.py",
    "src/water_agent/storage/repository.py",
    "alembic/versions/0008_*.py",
    "scripts/compare_model_choice_p0.py",
    "scripts/ci_gate.sh",
    "compose.yml",
    "data/eval/model_choice_p0.jsonl",
    "data/eval/model_choice_p0_main.json",
    "data/eval/tool_assembly_main.json",
    "web/app/src/**",
    "web/app/tests/**",
    "tests/fixtures/endpoint_compat/fake_openai_server.py",
    "tests/test_model_catalog.py",
    "tests/test_model_verification.py",
    "tests/test_web_model_selection.py",
    "tests/test_model_governance.py",
    "tests/test_model_choice_eval.py",
    "tests/test_chat_model.py",
    "tests/test_llm_endpoints.py",
    "tests/test_endpoint_compatibility.py",
    "tests/test_web_llm_endpoint.py",
    "tests/test_web_settings_api.py",
    "tests/test_web_runs.py",
    "tests/test_web_authorization.py",
    "tests/test_web_sessions.py",
    "tests/test_p5_step38_storage.py",
    "tests/test_web_deployment_overlay.py",
    "tests/test_web_deployment.py",
    "tests/test_memory_store.py",
    "tests/test_tool_assembly_snapshot.py",
    "tests/test_p7_known_figures.py",
    "tests/test_governance_contracts.py",
    "tests/test_provider_gateway.py",
    "tests/test_webapp.py",
    "docs/design/p7-step-15-decision.md",
    "docs/design/18-p7-multi-model.md",
    "docs/design/18-p7-multi-model-harness.md",
    "docs/design/16-p7-plugin-mcp.md",
    "docs/design/p2.5-threat-model.md",
    "docs/design/p2.5-provider-inventory.md",
    "docs/execution-prompts/P7-S15-model-catalog-and-selection.md",
    "AGENTS.md"
  ],
  "forbid": [
    "src/water_agent/runtime/governance/gateway.py",
    "src/water_agent/runtime/governance/pipeline.py",
    "src/water_agent/runtime/governance/contracts.py",
    "src/water_agent/runtime/governance/inventory.py",
    "src/water_agent/runtime/governance/rules.py",
    "src/water_agent/plugins/**",
    "src/water_agent/mcp/**",
    "src/water_agent/prompting/**",
    "src/water_agent/graphs/confirmation.py",
    "src/water_agent/tools/**",
    "src/water_agent/multi_agent/**",
    "src/water_agent/generation/endpoints.py",
    ".github/**",
    "compose.production.yml",
    "tests/conftest.py",
    "tests/test_p7_traceability.py",
    "tests/test_check_step_scope.py",
    "tests/test_import_guard.py",
    "tests/test_tool_exposure_eval.py",
    "tests/test_vision_review.py",
    "data/eval/baseline.json",
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
      "node": "tests/test_vision_review.py::test_vision_call_site_is_registered_and_governed",
      "contains": "assert len(PROVIDER_CALL_SITES) == 8"
    },
    {
      "node": "tests/test_llm_endpoints.py::test_defaults_are_unchanged",
      "contains": "assert chat.is_default"
    },
    {
      "node": "tests/test_chat_model.py::test_build_chat_model_defaults_temperature_zero",
      "contains": "assert captured[\"temperature\"] == 0.0"
    },
    {
      "node": "tests/test_chat_model.py::test_build_chat_model_enables_stream_usage",
      "contains": "assert captured[\"max_retries\"] == 0"
    }
  ],
  "frozen_json": [
    {
      "path": "data/eval/tool_assembly_main.json",
      "pointers": [
        "/versions/MCP_CONFIG_VERSION",
        "/versions/TOOL_POLICY_VERSION",
        "/versions/PROMPT_TEMPLATE_VERSION",
        "/profiles/session_[!p]*",
        "/profiles/runner_*"
      ]
    }
  ]
}
```
<!-- /step-scope -->

> `frozen_json` 说明：现有 18 个 profile（13 个 `session_*` + 5 个 `runner_*`——写 Prompt 时已用快照基线核对：13 个 `session_` 名字去掉前缀后的首字母依次为 c、d、d、e、h、m×6、s、w，没有 `p`）**整段**冻结、三个版本号都冻结；本步新增的 `session_pick_model_off` 以 `session_p` 开头，不被 `session_[!p]*` 匹配。**本步不得改变任何现有 profile**：不配目录时装配面必须逐字不变；若实测必须变化，先停下说明。S16 的新 profile 取 `session_reasoning_roundtrip`（`session_r` 开头），冻结规则相应改为 `session_[!r]*`（也会冻结本步的 `session_pick_model_off`）。
>
> `tests/test_governance_contracts.py` 在 allow 里，是因为 `GOVERNANCE_CONFIG_VERSION` 递进、价格表变化可能触及其中的锁定数字；`contracts.py` / `inventory.py` 在 forbid 里：出站清单与契约都不变。若实测需要改它们，按 §5 停止。
