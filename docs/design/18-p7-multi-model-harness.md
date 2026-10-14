# P7 扩展评测 harness 设计：多模型选择与思考型模型

> 配套规格：`docs/design/18-p7-multi-model.md`（以下简称 **SDD**）。本文只讲"怎么证明"：评测集、假端点、指标、反证、门禁接线。
> 沿用 `docs/design/16-p7-plugin-mcp.md` §6 的 H0–H5，并按 `skill_p0` / `mcp_http_p0` / `17-p7-external-capabilities-harness.md` 的套路建两套机制层评测。
> **本文是设计，不是已冻结的数据集**：条数是设计目标，S15、S16 各自的 Step 1 冻结时定数；冻结之后改动任何一条用例，都要在该步决策记录里写明原因。

---

## 0. 定位与原则

1. **机制层，离线确定性**：假 OpenAI 兼容端点 + 脚本模型 + 生产装配（`AgenticSession`），不连真实服务、不计费。它证明"机制按规格工作、保护没被拿掉"，**不证明真实模型的质量或行为**（§6）。
2. **评测先行**：每步先冻结用例与指标阈值，再写机制。
3. **正负成对**：每个"应拒绝"的用例配一条邻近的"应放行"用例（目录内合法模型、良性回答），否则"全拒"也能拿满分。
4. **指标只从用例上报的计数器聚合**：缺计数器 = `unmeasured_surface` 违规，不是记 0；速率指标未执行时记 `None`。
5. **每个硬指标都有反证**：一条测试把对应保护"拿掉"（monkeypatch 成放行版本），断言指标变红；没有反证的指标在比较器里标 `unproven_metric`（违规）。
6. **假端点进程内线程**：S8 决策记录 D13 的教训——评测 / 快照 harness 里起子进程会新增"子进程调用点"。

## 1. 总览

| 套件 | 步 | 数据集 | harness | 基线 | 比较器 | 设计条数 | 层次 |
|---|---|---|---|---|---|---|---|
| `model_choice_p0` | S15 | `data/eval/model_choice_p0.jsonl` | `harness/model_choice_eval.py` | `data/eval/model_choice_p0_main.json` | `scripts/compare_model_choice_p0.py` | ~42 | 会话 14 · Web 22 · 库 6 |
| `thinking_p0` | S16 | `data/eval/thinking_p0.jsonl` | `harness/thinking_eval.py` | `data/eval/thinking_p0_main.json` | `scripts/compare_thinking_p0.py` | ~38 | 会话 22 · Web 8 · 库 8 |

另有**不进 JSONL 的 pytest 层**：模块单测（目录 / 逐对验证 / 子类 / 清理 / `ThinkingPump`）、前端 node 测试、迁移测试、不变量 P7-I15…I21——它们是 SDD §7 验收行的验证项，评测集是在它们之上的场景层。

## 2. 通用设施

### 2.1 扩展共享假端点 `tests/fixtures/endpoint_compat/fake_openai_server.py`

现状（已读）：`modes` 机制现成（`no_tools` / `no_stream_usage` / `accept_unknown_model` / `no_models` / `models_junk` / `models_ollama`）；带 `tools` 就**永远**回 `tool_calls`；流式路径不发 `tool_calls`；`requests` 只存 `path / model / stream / tools`。需要补：

| 新增 | 内容 |
|---|---|
| `requests` 追加 `messages` | 追加键对现有断言安全（它们只读 `["path"]`、`len()`、`== []`）|
| `two_step` | 末条消息是 `role=tool` 时回文本，否则回对第一个工具的调用——模拟 agent 循环的第 2 次请求 |
| `reasoning` | 非流式在 `message` 里加 `reasoning_content`；流式在 `content` 之前发 `delta.reasoning_content` |
| `require_passback` | 请求带 `tools` 且**任一**历史 assistant 缺 `reasoning_content` 即回 `400`（模拟 SDD F2）；另有 `passback_empty_ok` 变体检验空串 |
| `stream_tools` | 流式路径也发 `tool_calls` 分块 |
| `served_model` | 响应 `model` 字段与请求不同（模拟别名改道）|
| `inline_think` | `content` 以 `<think>…</think>` 开头（vLLM 式内联）|
| `slow_reasoning` | 推理阶段延迟若干秒后才出首个 `content`（检验超时画像与取消）|

`test_endpoint_compatibility.py:290-303` 会遍历现有 modes，新增模式必须不影响它（新模式用独立名字且默认关闭）。

### 2.2 运行形态

- **会话**：经生产 `AgenticSession(model=ScriptedChatModel, …)` 或指向假端点的真实 `build_chat_model`，脚本**恰好用完**（多调抛 `ScriptExhausted`、少调报剩余步）；多轮每轮新建 session、共用 checkpointer 与 thread（与 Web `RunManager` 同构）。
- **Web**：`create_app(database=sqlite)` + `TestClient`，真实鉴权与 CSRF；SQLite 上真跑 Alembic 迁移（`test_p5_step38_storage.py` 的做法）。
- **库**：直接调用 `model_catalog` / `compatibility` / 子类 / `ThinkingPump`。
- 环境变量一律 `monkeypatch.setenv`；`PINNED_ENV_KEYS` 钉住新增键。

### 2.3 用例 schema（两套共用骨架）

```json
{
  "id": "mc-select-unverified-424",
  "suite": "model_choice_p0",
  "category": "selection",
  "layer": "web",
  "requires": [],
  "setup": {"catalog": [{"id": "flash-a", "tier": "fast", "thinking": "off", "enabled": true}],
            "verified": [], "role": "member", "fake": {"modes": []}},
  "steps": [{"post_message": {"model": "flash-a"}}],
  "expect": {"status": 424, "error.code": "WEB_MODEL_NOT_VERIFIED", "fake.requests": 0,
             "counters": ["unauthorized_model_use_count"]},
  "labels": {"benign": false}
}
```

用例 id 前缀：`mc-`（model_choice）、`th-`（thinking）。`requires` 声明前置条件，未声明前置条件的 SKIP 是违规（`undeclared_skip`）。

### 2.4 报告与比较器纪律（照 `compare_skill_p0.py`）

harness 是报告的**唯一**生产者，比较器拒绝人工编写的报告；报告逐字可复现；基线用例缺失 / 被跳过、数据集条数变化、硬指标回退、缺计数器、`undeclared_skip`、基线文件缺失（首次接入须显式 `CI_GATE_ALLOW_MISSING_BASELINE=1`）一律违规；报告写进 `data/eval/reports/ci_gate/`，退出码 0 才算通过。

### 2.5 门禁与快照接线

- `scripts/ci_gate.sh`：每套件一行 runner + 一行比较器（紧随 `skill_p0` / `mcp_http_p0`）。
- `PINNED_ENV_KEYS`：仅在新增环境变量时增加（目录文件位置、逐对报告目录）。
- 装配快照：**现有 18 个 profile 逐字不变**；S15 新增 profile `session_model_catalog_off`（目录内 `thinking: off` 模型被选中，断言 `extra_body` 与 `inner.model_name`）；S16 新增 `session_reasoning_roundtrip`。范围块 `frozen_json` 冻结其余片段。
- 追溯：`tests/test_p7_traceability.py` 把 SDD 加入解析范围（`parse_spec(text, prefix="P7")`）。
- 既有门禁（H5）不得回退：`rag_p0` / `calc_p0` / `docgen_p0` / `material_p0` / `governance_p0` / `agentic_p0` / `tool_exposure_p0` / `skill_p0` / `mcp_http_p0` / `memory_p0` / 装配快照。

### 2.6 反证（mutation）约定

每个硬指标在 `tests/test_<suite>_eval.py` 里至少一条形如 `test_metric_<name>_goes_red_when_<protection>_is_removed` 的测试：用 `monkeypatch` 把受测保护换成放行版本，跑相关用例子集，断言该指标不再达标。**不修改源码、不改数据集**。

---

## 3. 套件一：`model_choice_p0`（S15，设计 ~42 条）

证明：目录 / 优先级 / 逐对验证 / 选择链路 / 持久化 / 治理在生产装配下成立，且**从不自动换模型、不让数据去向翻转**。

| 类别 | 条数 | 代表用例 | 断言 |
|---|---|---|---|
| 目录 | 6 | `mc-cat-empty-identical`（不配目录时请求 / 响应 / 提示逐字不变）、`mc-cat-invalid-id`、`mc-cat-disabled-hidden`、`mc-cat-env-locked-readonly`、`mc-cat-role-limited`、`mc-cat-member-readonly` | 缺省不变；成员只读；env 锁定只读 |
| 选择校验 | 10 | `mc-sel-unknown-422`、`mc-sel-disabled`、`mc-sel-role-forbidden-403`、`mc-sel-unverified-424`、`mc-sel-no-key-424`、`mc-sel-local-model-refused`、`mc-sel-env-locked-refused`、`mc-sel-base_url-field-422`、`mc-sel-order-403-before-424`、`mc-sel-roundtrip-not-selectable` | 码与顺序；假端点零请求；LOCAL 覆盖不外泄 |
| 优先级 | 5 | `mc-prio-message-over-session`、`mc-prio-session-over-global`、`mc-prio-none-uses-global`、`mc-prio-session-patch-authz`、`mc-prio-viewer-cannot-patch` | 消息 > 会话 > 全局 |
| 逐对验证 | 6 | `mc-ver-per-pair-store`、`mc-ver-mismatch`、`mc-ver-legacy-single-file-intact`、`mc-ver-change-invalidates-only-that-pair`、`mc-ver-panel-runs-for-model`、`mc-ver-default-exempt-unchanged` | 文件名不是信任根；缺省免检不扩大 |
| 持久化与 resume | 6 | `mc-persist-session-default`、`mc-persist-run-model`、`mc-resume-after-restart-uses-run-model`、`mc-resume-missing-record-warns-not-swaps`、`mc-persist-payload-keys`、`mc-migrate-0008-sqlite` | 重启后不静默换回默认；读不到只降级告警 |
| 治理与可观测 | 6 | `mc-gov-price-unconfirmed`、`mc-gov-cost-per-model`、`mc-gov-unregistered-none`、`mc-gov-model-selected-event`、`mc-gov-payload-model`、`mc-gov-rate-bucket-bounded` | 成本按模型；未登记为 `None`；桶有界 |
| 安全 | 3 | `mc-sec-key-only-to-own-endpoint`、`mc-sec-audit-no-key`、`mc-sec-off-sends-thinking-disabled`（`thinking: off` 的请求体含 `thinking.type=disabled`）| Key 不外发；审计无 Key |

**硬指标**：`selection_validation_correct_rate = 1.0` · `unauthorized_model_use_count = 0` · `base_url_accepted_count = 0` · `silent_fallback_count = 0`（resume / 失败 / 超时 / 缺记录都不换成另一个目录模型）· `default_behavior_identical_rate = 1.0`（空目录）· `per_pair_verification_isolation_rate = 1.0` · `cost_attribution_correct_rate = 1.0` · `local_model_override_leak_count = 0` · `key_leak_count = 0` · `benign_selection_rejected_rate ≤ 0.0`（目录内已验证的合法选择必须放行）· `harness_error_count = 0`。

**反证**（节选）：预检不看所选模型 → `unauthorized_model_use_count` > 0；`resume` 不读 `runs.model` → `silent_fallback_count` > 0；放行 `base_url` 字段 → `base_url_accepted_count` > 0；LOCAL 模式不拒绝 → `local_model_override_leak_count` > 0；成本写死 → `cost_attribution_correct_rate` < 1。

**不证明**：任何真实模型的质量、延迟或价格；flash（关思考）与 `MODEL_NAME` 在工具调用与引用上的差异（需 Key，P7-S15-A13）；目录里的模型在真实端点上真的存在。

---

## 4. 套件二：`thinking_p0`（S16，设计 ~38 条）

证明：思考内容的**往返、清理、展示、隔离**成立，且"缺回传即 400"这类 provider 行为被收口成可诊断的结果。

| 类别 | 条数 | 代表用例 | 断言 |
|---|---|---|---|
| 子类与往返 | 8 | `th-rt-nonstream-parse`、`th-rt-stream-merge`（流式 delta 拼接）、`th-rt-outbound-whitelist`（出站 assistant 只多 `reasoning_content` 一个键）、`th-rt-other-keys-not-sent`、`th-rt-multi-step-loop`、`th-rt-old-turn-policy`、`th-rt-synthetic-assistant`、`th-rt-cross-model-strip` | P7-I19；`two_step` 状态机 |
| provider 行为 | 5 | `th-pv-require-passback-400`、`th-pv-with-passback-ok`、`th-pv-empty-string`、`th-pv-400-is-diagnosable`（错误对用户可诊断，不是"上游不可用"）、`th-pv-inline-think` | F2 / F17 |
| 清理 | 6 | `th-cl-blocked-clears`、`th-cl-redacted-clears`、`th-cl-intermediate-cleared`、`th-cl-cap`、`th-cl-inline-think-not-in-answer`、`th-cl-checkpoint-no-residue` | F16 |
| `ThinkingPump` | 8 | `th-tp-sentence-split`、`th-tp-check-output-per-sentence`、`th-tp-guard-hit-retract-close`、`th-tp-no-raw-in-events-or-trace`、`th-tp-offset-continuity`、`th-tp-char-cap`、`th-tp-cancel`、`th-tp-run-failed-retract` | 命中即整段撤回并关闭通道 |
| 隔离 | 6 | `th-iso-not-in-answer`、`th-iso-not-in-history`、`th-iso-not-in-export`、`th-iso-not-in-cache`、`th-iso-not-in-old-token-channel`、`th-iso-status-unaffected` | P7-I18 |
| 探针 | 5 | `th-pr-tool-roundtrip-check`、`th-pr-stream-tools-check`、`th-pr-synthetic-shape-check`、`th-pr-version-invalidates-old`、`th-pr-evidence-records-served-model` | 各检查有坏模式；版本递进 |

**硬指标**：`reasoning_leak_count = 0`（进入 answer / 历史 / 导出 / 缓存 / trace / 非 `thinking_*` 事件的次数）· `guardrail_bypass_count = 0`（思考文本未过 `check_output` 即下发的次数）· `passback_whitelist_violation_count = 0` · `roundtrip_success_rate = 1.0`（对 `reasoning` + `require_passback` 假端点，工具循环全程不 400）· `diagnosable_400_rate = 1.0` · `cleanup_complete_rate = 1.0` · `cap_violation_count = 0` · `inline_think_in_answer_count = 0` · `benign_thinking_altered_rate ≤ 0.0`（良性思考被改写的比例）· `harness_error_count = 0`。

**反证**（节选）：`ThinkingPump` 不调 `check_output` → `guardrail_bypass_count` > 0；`_rewrite` 不清 reasoning → `cleanup_complete_rate` < 1；出站放行所有 `additional_kwargs` → `passback_whitelist_violation_count` > 0；旧轮不回传 → `roundtrip_success_rate` < 1（在 `require_passback` 模式下）。

**不证明**：真实 `MODEL_NAME` 是否按假端点建模的规则行事（SDD F2 是文档规则，实际严格度需 Key 实测，P7-S16-A12）；思考内容的语义质量；护栏对未覆盖改写的有效性；真实延迟与超时分布。

---

## 5. 指标语义小结

| 约定 | 说明 |
|---|---|
| `*_rate = 1.0` / `*_count = 0` | 安全类指标不留容错；任何一条失败即门禁红灯 |
| 误拦类 `≤ 0.0` | 良性对照必须全部放行——避免"全拒"拿满分 |
| 未测量 | 缺计数器 = `unmeasured_surface` 违规；未执行的速率 = `None` |
| `harness_error_count = 0` | harness 自身异常（假端点起不来、脚本耗尽）算失败，不算跳过 |
| 基线 | 入仓 `*_main.json`，首次生成须显式 `CI_GATE_ALLOW_MISSING_BASELINE=1` |
| SKIP | 仅 `requires` 声明的前置条件可 SKIP；`--release-gate` 下任何 SKIP 都算失败 |

## 6. 这些评测**不证明**什么（对外口径）

1. 不证明任何真实模型的质量；**绝不据此声称换模型后质量不变**（T14）。
2. 不证明 `MODEL_NAME` / `MODEL_NAME` 的真实行为：模型名、默认思考、"缺回传即 400"、别名改道都来自文档或转述，需 Key 实测（P7-S15-A13、P7-S16-A12）。
3. 不证明确定性护栏（`check_output`、`detect_injection`）能拦住任意改写。
4. 不证明价格：所有价格 `confirmed=False`，成本仍是估算。
5. 不证明目录里的模型在真实端点上存在、可用。

## 7. 每步的实施顺序（评测先行）

1. **Step 1 冻结**：数据集 + 指标阈值 + 比较器骨架；`unproven_metric` 把"尚无反证"的指标显式标红。
2. **Step 2 harness**：扩展假端点 + runner + 报告；此时机制未实现，用例预期失败（红灯基线）——记入决策记录，证明评测"咬得到"。
3. **Step 3 机制**：按 SDD 逐验收行实现，一个逻辑面一个 commit。
4. **Step 4 反证与基线**：补齐反证，生成入仓基线，接 `ci_gate.sh`。
5. **Step 5 快照与追溯**：新增快照 profile、`PINNED_ENV_KEYS`、追溯检查、范围守卫块；现有 profile 逐字不变。

## 8. 风险

- **评测与实现同一作者**：缓解 = 假端点规则（尤其 `require_passback`）按**官方文档原文**建模并在用例里引用其措辞；S16 开工前用真实 Key 对照一次假端点与真实端点的差异，差异写进决策记录。
- **假端点过于理想**：真实服务的分块、慢速滴漏、压缩、限流只部分覆盖；`slow_reasoning` / `served_model` 是下限，不是完备。
- **探针版本递进的部署影响**：S16 使所有既有非缺省报告作废，需在决策记录写明升级影响，并给运维一条重跑兼容性检查的步骤。
