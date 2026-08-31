# Harness 三件套代码审查报告

- **审查对象**：`src/water_agent/harness/runner.py`、`scorer.py`、`retrieval_metrics.py`（附依赖上下文：`domain/models.py`、`tools/hydraulics.py`、`harness/loader.py`、`graphs/rag.py` 状态契约核实）
- **审查方式**：⚠️ 本次审查由主模型（GLM）完成。原计划通过 oracle CLI 调用另一个 AI 模型（ChatGPT）交叉审查，因会话 Bash 执行环境损坏（所有命令 exit 127，沙箱层故障，`true` 均无法执行）未能执行。**重启 WorkBuddy 后可重跑 oracle 交叉验证。**
- **审查日期**：2026-08-31
- **参照**：`docs/review/step1-code-review.md`（上一轮 Step 1 评审，本轮核对了其修复状态）

## 上一轮问题修复状态核对

| 上轮编号 | 问题 | 当前状态 |
|---|---|---|
| P1-1 | `_within_tolerance` OR 语义 | ✅ 已修复（现为 AND，`scorer.py:94`） |
| P1-2 | `calc_pump_head` 未走 `_positive_numbers` | ✅ 已修复（`hydraulics.py:238`） |
| P1-3 | 达西错误信息张冠李戴 | ✅ 已修复（`hydraulics.py:134-145` 重写为独立分支） |
| P2 | `EvalReport.output_path` 落盘恒空 | ❌ 仍存在（见 P2-4） |
| P2 | `CalcTolerances` 默认 0/0 地雷 | ❌ 仍存在（见 P2-8） |
| P2 | `score_case()` 死代码 | ❌ 仍存在（见 P2-6） |
| P2 | `expected_assumptions` 不参与判分 | ❌ 仍存在（见 P2-7） |

## P1 — 严重（可导致评测崩溃或结果不可信）

### P1-1 `runner.py:99` — line_no 全链路丢失，报告无法定位失败用例

`run_bundle` 调用 `self._run_case(case)` 时从不传 `line_no`；而 `EvalCase`/`CalcCase` 模型本身没有 line_no 字段，loader 解析成功后行号即被丢弃（只有 issues 保留行号）。

结果：所有 `EvalResult.line_no` 恒为 `None`。评测失败时无法定位到 JSONL 的哪一行，违背项目"引用可追溯"精神在评测层的延伸。

**修复建议**：`EvalLoadBundle.cases` 改为 `list[tuple[EvalCase | CalcCase, int]]`，或 bundle 增加 `line_nos: list[int]` 平行数组；`run_bundle` 逐例传入。

### P1-2 `runner.py:160` + `hydraulics.py:216` — 字符串参数穿透导致 CLI 裸崩溃

`main()` 只捕获 `(FileNotFoundError, EvalLoadError, OSError)`。但存在两个数值参数绕过校验直通 `float()`：

1. `calc_slope_manning`：`fill_ratio` 不在 `_positive_numbers` 检查列表中（`hydraulics.py:212`），`fill_ratio: "abc"` 时 `_numeric(inputs, "fill_ratio")` 的 `float()` 抛 **ValueError** 未捕获 → 整个 harness traceback 崩溃，而非按设计返回 `invalid_input` 判分结果。
2. `calc_head_loss_darcy_weisbach`：`gravity_mps2` 字符串非数字时 `float(inputs.get(...) or GRAVITY_DEFAULT)`（`hydraulics.py:150`）同样抛 ValueError。

按 scorer 语义，非法输入应记为 `invalid_input` 用例；现在却是评测器本身崩溃。

**修复建议**：a) 将 `fill_ratio`、`gravity_mps2` 纳入显式数值校验（try/float 后再算）；b) `main()` 兜底 `except Exception` 输出结构化 JSON 错误并返回非零退出码。

### P1-3 `runner.py:98-123` — 单用例异常无隔离，一崩全崩

`run_bundle` 的列表推导 `[self._run_case(case) for case in bundle.cases]` 中，任何一个 RAG 用例在 `app.invoke` 抛异常（LangGraph 内部错误、生成器异常、trace 写盘失败等），整个评测 run 崩溃：此前所有用例的结果全部丢失、报告不落盘。

评测跑分器的基本职责是"无论被测系统多烂都要产出报告"。

**修复建议**：`_run_case` 外层包 per-case try/except，异常用例构造 `EvalResult(passed=False, actual_status="error", failure_reason=repr(exc))` 计入报告继续跑。

## P2 — 一般（影响可维护性/口径清晰度）

### P2-1 `runner.py:63-66` — variant 白名单静默降级

`variant` 只识别 `trivium`（retriever）和 `bad_citation`（generator），其他值静默回落默认组件。评测集里 `variant: "trivuim"`（拼错）不会报错，而是被当成普通 mock 用例跑 → 评测口径被污染且无告警。

**建议**：未知 variant 显式抛错（loader 层校验枚举更佳，`EvalCase.type` 同理——`type: "xxx"` 现在会被静默当作 rag 处理）。

### P2-2 `runner.py:174-176` — 退出码 2 语义双关

exit 2 既表示"加载/执行异常"（`except` 分支）也表示"跑完但有 load_issues"（末尾 `if load_issues: return 2`）。CI 脚本无法区分"没跑"和"跑了但有脏行"。且 load_issues 非空时即使用例 100% 通过也 exit 2——若是门禁设计应在文档写明。

### P2-3 `retrieval_metrics.py:20-27` — Recall@k 是 any-hit 口径，需文档化

`set(retrieved[:k]) & gold` 非空即记命中：多 gold 文档时只要求命中任意一个，不是 full recall。作为基线指标是合法定义，但必须在口径表/README 写明，否则横向对比（如与别的系统比 recall@5）会得出错误结论。

另：`row.get("retrieved")` 若传入字符串而非列表，`for value in "abc"` 会逐字符迭代静默产出错误指标。

### P2-4 `runner.py:119-135` — 落盘 JSON 里 `output_path` 恒为空（上轮遗留）

`_write_report` 在 `model_dump` 写盘**之后**才由 `run_bundle` 给 `report.output_path` 赋值，磁盘上的报告里 `output_path` 永远是 `""`。**修复**：先确定路径再 dump，或在 `_write_report` 内回填后重写。

### P2-5 `hydraulics.py:240` — 错误信息与行为不符 + 物理合理性

信息说"不允许为负数"，实际检查 `<= 0`（0 也拒绝）。`local_head_m=0`（无局部水头损失）在物理上完全合法，却会被判 `invalid_input`。

### P2-6 `scorer.py:139-164` — `score_case()` 疑似死代码（上轮遗留）

带 `run_calc` 内联调用的兼容入口，绕过计时；若确认无调用方应删除，避免两套判分入口漂移。

### P2-7 `scorer.py:97-127` — `expected_assumptions` 不参与判分（上轮遗留）

`CalcCase.expected_assumptions` 字段存在、`calc_result.assumptions`（流速越界标注、未知 scenario 标注）从未被比对。"越界不拦截但必须标注"的红线在评测层落空——标注丢了评测照样绿。

### P2-8 `models.py:118-119` — `CalcTolerances` 默认 0/0 地雷（上轮遗留）

用例忘写 tolerances → 退化为精确浮点相等比对，浮点舍入误差极易误判。建议默认给安全容差（如 rel=1e-6）或强制必填。

### P2-9 `scorer.py:114-116` — 非成功状态时 gold 静默跳过

`expected_status="missing_inputs"` 且 gold 非空的畸形用例，gold 数值不校验、静默通过。建议状态匹配但 gold 非空时追加告警。

### P2-10 `hydraulics.py:77-88` — NaN/inf 穿透正数校验

`float("nan") <= 0` 为 False → NaN 通过正数校验进入计算，产出 NaN 结果（与 gold 比对恒 False 会判 fail，不至于误判通过，但 failure_reason 会是费解的数值比对信息）。`float("inf")` 同理。建议 `_positive_numbers` 加 `math.isfinite` 检查。

## 正面确认（重点核查过，无问题）

- `scorer.py` 的 `values.get(key) is None` 判断正确（gold 值为 0.0 不会误判缺失）；
- `_within_tolerance` 现为 AND 双阈值语义，与口径表一致（上轮 P1-1 修复确认）；
- gold=0 时 `rel_err` 置 0 只看绝对阈值，除零已防护；
- citation grounding 判分（`invalid_citations` 必须来自当次 context）与 `gold_any`/`gold_all`/`forbidden` 三组逻辑正确；
- MRR（`reciprocal_rank`）实现标准正确，空集返回 0.0；
- `run_bundle` 的 `pass_rate` 除零已防（total=0 → 0.0）；
- `graphs/rag.py` 状态契约核实：`result["citations"]` 为 `Citation` 对象列表，runner 取 `.source_id` 一致，无误报。

## 修复优先级建议

1. **立即**：P1-2（填两个校验漏洞 + main 兜底）、P1-3（per-case 异常隔离）——这两个是"评测器自己先崩"的稳定性问题；
2. **本迭代**：P1-1（line_no 贯通）+ P2-1（variant 白名单）——评测可追溯性；
3. **顺手清**：P2-4/6/7/8 均为上轮已指出的小修，二轮未修应排入。
