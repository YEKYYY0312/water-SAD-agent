# P3 Step 22 决策记录：审查子图 + drawing_p0 评测集 + 门禁接入

**日期**：2026-09-07
**状态**：已完成（真实样本集 `drawing_p1` 为 blocked 待办，见 §3）
**规格来源**：`docs/design/13-p3-drawing-review.md` §7/§8；Step 21 决策记录 §4

> 本文档不含任何 API Key、真实图纸内容或 trace 原文。合成图纸用例由 ezdxf 生成。

## 0. 最重要的结论（先说）

1. **审查子图已交付**：`graphs/drawing_review.py` = load → map → check → report
   四节点线性编排，全确定性不调模型；报告渲染唯一出口仍是
   `Finding.rendered_finding()`（有测试断言 `report_text` 与 `rendered` 逐字一致）。
2. **`drawing_p0` 是合成 smoke 集，不是验收证据**：`tests/eval/drawing_p0/` 的
   8 条用例全部由 ezdxf 程序化合成（`real_samples=false` /
   `accepts_recall_gate=false`），只验证链路完整性与判分口径。
   **在真实设计院样本 + `ANNOTATION_MATCH_RADIUS` 重标定之前，不得声称
   缺陷召回率达标**（Step 21 决策记录残余风险 1–3 的硬约束）。
3. **召回度量独立落地**：`harness/drawing_metrics.py` 分母是**人工标注的缺陷数**、
   逐条 `(rule_id, handle)` 精确匹配；静态测试断言不 import `retrieval_metrics.py`。
4. **未注册 `review_drawing` 模型工具**：按意图最小暴露的最小 schema 实测
   **551 tokens**，headroom 130 放不下，且本步没有调用方 —— 审查编排是程序管线，
   何时暴露给模型留到主图接线时裁决（D5）。
5. **门禁对比是"HEAD 报告 vs 入仓基线报告"**：`ci_gate.sh` 跑 runner 产出
   `data/eval/reports/drawing_p0_head.json`，对比 `data/eval/drawing_p0_main.json`
   （main 固化基线，同 `baseline.json` 的机制）：用例级 PTP + 观测指标 >5pt 红灯。
   **基线的有效性存疑** —— 它是合成样本上的回归基线，真实样本重新标定前
   不代表真实质量（D6）。

## 1. 关键决策与取舍

### D1 smoke 集与验收门槛分离：门禁先接 smoke，`drawing_p1` 列 blocked

任务书允许两条路（拿不到真实图纸时）：接 smoke 或只记录等待。选**前者 + 后者**：

- 交付 `tests/eval/drawing_p0/`（suite.json / cases/ / README.md），8 条用例：
  最小管径缺陷 2 条（含**同 rule_id 双缺陷**，验证逐条覆盖不是 any-hit）、
  合规图 3 条（排水合规 / 给水规则作用域外 / 投毒标注隔离）、
  单位缺失 1 条、解析失败 1 条 —— 正/负例均衡，并给两条恒未检查规则
  每张排水图配了 `expected_not_checked_contains`（**"遇到缺输入就如实报未检查"
  是判分项，不是装饰**）；
- 门禁对比只守护 smoke 集的**回归**（PTP + 观测指标阈值），不设召回率门槛；
- `drawing_p1`（真实样本集）与验收开启条件列为 blocked 待办（§3）。

**为什么不直接"等真实样本"**：链路完整性的回归守护本身有价值 —— 合成图上的
管线逻辑（读图、映射、判分、报告）与真实图共享同一实现，回归下降同样会伤到
未来的真实评测。smoke 集守的是"实现没退化"，不是"对真实图纸的召回率"。

### D2 子图用独立内部状态，不占 `WaterAgentState`

`DrawingReviewState` 是图的私有 TypedDict（同 `graphs/calc.py` 的 `CalcState`、
`graphs/docgen.py` 的 `DocGenState` 模式）。`WaterAgentState.drawings: list[Any]`
**仍零消费** —— 消费它意味着主图接线（意图路由 → 调子图 → 结果写回状态），
那是下一步的事，本步没有调用方，不预埋死字段（P2.5 §4 纪律）。
`root`/`policy`/`pipeline` 在 `build_drawing_review_graph` 装配期注入，
不入图状态（与 `read_drawing` 工具的同一路径防御口径）。

### D3 失败传播走全管线，不在中途短路

`extraction.ok=False` **不跳过** map/check：`map_pipes` 对失败抽取产出空管段 +
missing_inputs（"图纸抽取失败，无可映射图元"），`review_drawing` 产出
`pipes_reviewed=0` 的"**未执行任何管段审查**…不含任何合规判断"结论。
最终报告的 `ok=false` / `status="failed"` / `extraction_errors`（原样携带
`DRAWING_UNREADABLE` 等稳定错误码）与"检查过且干净"一眼可分。

**为什么不在 load 后短路**：短路的 report 节点就得自己拼"未执行审查"这类
结论措辞 —— 而那个措辞属于 Step 21 `ReviewResult.summary()` 的所有权。
让失败走完同一管线，结论文字全部来自既有语义，Step 22 只做编排不改写。

附带一处报告层修复：ezdxf 的异常消息内嵌**文件绝对路径**，而报告是设计为
可落盘/落 trace 的产物 —— 报告层把 `DRAWING_ROOT` 前缀替换为
`<DRAWING_ROOT>`（`extraction.errors` 原文保留供进程内调试），
有失败路径的专项测试（与 `file_hash` 不含路径同纪律）。

### D4 报告渲染：rendered_finding 唯一出口，not_checked 一等公民，零管段无缺陷小节

- `report_text` 的缺陷行逐条来自 `Finding.rendered_finding()` 的 `rendered` 字段；
  未检查 / 缺输入 / 假设 / 结论逐条透传 Step 21 原文，本模块只排版不改写；
- `not_checked` 独立成节（【未检查项】），且每条排水 case 的 gold 强制要求
  坡度/流速两条未检查项出现 —— **"没检查"与"没问题"在报告里分列**；
- `pipes_reviewed == 0` 时**不渲染**"缺陷（0 条）"小节：零审查的图若出现
  "缺陷 0 条"的计数，读者会读成"没问题"，正是 Step 21 D7 要防的读法。

### D5 不做模型可调用工具 `review_drawing`（实测 551 tokens > headroom 130）

按任务书要求先测"按意图最小暴露"的最小形态（单参数 `drawing_id`、最短 description、
无路径概念）：

| 配置 | 实测 tokens |
|---|---|
| 5 工具基线（含 read_drawing） | 4370（headroom 130） |
| 最小化 `review_drawing` schema | **551** |
| 若注册进装配 | 4370 + 551 ≈ 4921 **> 4500，断言红灯** |

结论：**本步不注册**。理由不止预算 —— 审查是确定性子图，模型调它拿回一份
长报告，既没有参数理解需求也没有数值判断需求；评测与 CLI 直接调
`run_drawing_review()` 就是同一管线（有测试锁死走的是 LangGraph 图）。
将来主图接线需要"模型触发审查"时，按 P2.5 §5 登记的"工具按意图最小暴露"
裁决（那时可考虑把 `read_drawing` 的 schema 一起裁剪腾出预算），
**不得上调 `< 4500` 断言**。

### D6 门禁基线：独立对比脚本 + 入仓基线，不进 `MECHANISM_SUITES`

`drawing_p0` **不**加入 `compare_baseline.py` 的 `MECHANISM_SUITES`：

- 那套基线按 suite 聚合 pass_rate/citation_ok_rate 语义；drawing 的观测指标是
  `defect_recall` / `false_positive_rate`，混进去会破坏既有套件的对比口径；
- `drawing_p0` 当前是合成 smoke 集，把它的召回率"建档"会制造"召回率已受基线
  守护"的假象。

独立机制：runner 写结构化 HEAD 报告（`data/eval/reports/drawing_p0_head.json`，
可重建不入仓）→ `scripts/compare_drawing_p0.py` 对比入仓基线
（`data/eval/drawing_p0_main.json`，main 的最后一次固化结果，同
`baseline.json` 模式）→ 用例级 **PASS_TO_PASS**（main 通过的用例新失败即红灯）
+ `case_pass_rate`/`defect_recall` 下降 >5pt + `false_positive_rate` 上升 >5pt 红灯。

**基线有效性声明（任务书硬约束）**：该基线建立在合成样本上，
`ANNOTATION_MATCH_RADIUS=12.0` 与合成标注的几何形态都**几乎肯定需按真实图纸
重新标定** —— 因此本基线只守"链路没退化"，**不代表**真实缺陷召回率水平。
在 `drawing_p1` 落地前，这条基线的数字对验收没有说服力（门禁输出的
`gate_note` / `real_samples=false` / `accepts_recall_gate=false` 强制带出）。

### D7 gold 由生成器写出，handle 从重读文件取权威值

`tests/eval/drawing_p0/build_cases.py` 是 cases/ 的**唯一编辑入口**：CASE_SPECS
只描述几何与期望，生成器建图 → saveas → **重读** → 按起点坐标定位管线 →
把权威 handle 写进 gold。有两道一致性校验：

- 生成时 handle 与重读 handle 不一致 → 抛错（生成器自检）；
- runner 判分前校验 gold handle 真实存在于该图抽取结果
  （`gold_handle_not_in_drawing` 失败原因，测试用改坏 handle 的反例锁死）。

**为什么不在运行时推导 gold**：gold 是"人工标注的缺陷数"（规格 §7 的分母），
它必须是**静态资产**，不是 runner 按同一套逻辑反推的结果 —— 反推会让评测
与实现共享同一 bug，漏判时会一起漏判。

### D8 评测集位置与命名

`tests/eval/drawing_p0/`（任务书明确指定），内部结构与 material_p0 同构：
suite.json（描述）/ cases/（图纸 + gold）/ README.md（合成 or 真实、标注口径）。
runner 是独立模块 `harness/drawing_eval.py`（`python -m` 入口），**不**塞进
`harness/runner.py` 的 JSONL 体系 —— drawing 评测的输入不是 JSONL 行，而是
每张图一个 DXF + 一个 gold，塞进 JSONL loader 会扭曲两边的模型。

### D9 对 Step 21 契约的改动：零

本步**未修改** `drawing/mapping.py` / `drawing/rules.py` / `models.py` 的任何行为
（发现并修复的唯一问题是 Step 22 报告层的路径泄漏，不属于 Step 21 契约）。
Step 21 全部测试重跑通过，见交付记录。

## 2. 明确拒绝的做法

1. **不声称召回率 ≥80%** —— 没有真实样本，smoke 集的 `defect_recall=1.0`
   只说明合成图上的管线自洽。
2. **不用 `retrieval_metrics.py` 度量缺陷召回** —— 分母是"全部文档"不是
   "标注缺陷数"，任何-hit 会稀释漏判率（静态测试锁死无 import）。
3. **不自己拼缺陷措辞** —— 报告缺陷行只来自 `rendered_finding()`；
   "未执行审查"等结论措辞来自 `ReviewResult.summary()`。
4. **不把 not_checked 当可选输出** —— 它是判分项（gold 的
   `expected_not_checked_contains`），不是装饰。
5. **不注册必然超预算的 `review_drawing` 工具** —— 见 D5，`<4500` 断言不动。
6. **不给报告暴露路径** —— `drawing_id` 是唯一输入；报告内绝对路径被脱敏。
7. **不把合成图测试结果当"真实图纸也能过"的证据** —— README/suite.json/
   门禁输出三层标记。
8. **不把 `drawing_p0` 塞进 `MECHANISM_SUITES` / `baseline.json`** —— 见 D6。

## 3. 残余风险（承自 Step 21 + 本步新增）

1. **真实图纸未验证**（承自 Step 19/20/21）：全仓 0 个真实样例。
   `ANNOTATION_MATCH_RADIUS=12.0` 几乎肯定需按真实图纸重新标定。
2. **只覆盖 LINE/POLYLINE 管段**：真实图纸的管线可能是 INSERT 块、多段 ARC、
   带宽度 LWPOLYLINE，当前会漏。
3. **`system` 依赖图层名前缀猜测**：非标图层命名 → `system=None` →
   不进材料清单（保守侧，但会漏统计）。
4. **管段级去重未做**（Step 20 残余风险 6）：本步子图对每张图**单次**读取
   （`system="all"`），不触发拼接去重；若将来主图接线做多次过滤读取拼接，
   必须按 handle 去重（已写进交接 §4）。
5. **规则只有 3 条、其中 2 条恒未检查**：可判定缺陷只有最小管径。
   真实样本集的召回率分母若只有一类缺陷，指标意义有限 ——
   `drawing_p1` 需配合补规则（坡度/流速的输入接缝已预留）。
6. **合成图与真实图的标注形态差异**：合成标注都规规矩矩压在管线 3 单位处；
   真实图纸标注可能离管线很远、旋转、跨图层 —— 这些正是半径标定要解决的事。
7. **门禁基线的有效性存疑**（见 D6）：合成基线不等于真实质量基线，
   真实样本重标定前不得引用数字做验收。

**blocked 待办（开启 P3 验收门槛的前提）**：
- [ ] 采集真实设计院 DXF 样本（≥10 张，含真实缺陷与合规图，脱敏业主 PII）；
- [ ] 按真实样本重新标定 `ANNOTATION_MATCH_RADIUS`；
- [ ] 补 INSERT / ARC / 带宽度 LWPOLYLINE 的实体覆盖与相应规则；
- [ ] 建立 `drawing_p1` 真实样本集并设召回率门槛（此后才谈验收）。

## 4. 交接给下一步

- **主图接线**：把 `drawing_review` 子图挂进主图（意图路由 + `WaterAgentState.drawings`
  消费 + 结果进 answer），届时才收紧 `drawings` 类型；若需"模型触发审查"，
  按 D5 的预算现实做工具暴露裁决。
- **drawing_p1**：按 §3 blocked 清单推进；评测目录结构与 runner 复用
  （`harness/drawing_eval.py` 的 suite 加载 / 判分 / 报告格式已与套件解耦）。
- **报告消费端**：CLI 或未来前端展示 `report_text` 时，不得绕过 `rendered_finding()`
  重写缺陷行；not_checked 与 findings 必须并列展示。