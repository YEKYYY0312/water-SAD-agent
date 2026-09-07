# P3 Step 21 决策记录：语义映射与规则引擎

**日期**：2026-09-07
**状态**：已完成
**规格来源**：`docs/design/13-p3-drawing-review.md` §5.2/§6；Step 20 决策记录 §4

> 本文档不含任何 API Key、真实图纸内容或 trace 原文。攻击串均为合成样例。

## 0. 最重要的结论（先说）

1. **图纸 → 材料清单链路已闭环**：`DrawingExtraction` → `MappedPipe` →
   `materials.PipeRun` → `aggregate_materials`，实测产出 `DN400 钢筋混凝土 40.0 m`。
2. **三条规则的来源等级由语料事实决定，不由文案选择决定**。实测核对后：
   压力流流速 `formal_clause`、排水横管坡度 `informal_summary`、
   最小管径 `engineering_practice`。
3. **两条规则当前恒为"未检查"且刻意保留**（坡度缺标高、流速缺流量）。
   删掉它们会让报告读起来"这两项没问题"，而事实是"根本没查"。
4. 实测发现一处**规格未提及的语料结构事实**：`GB50014-2021 5.2.9` 有两条
   `duplicate_article_variant`，含 0.7~2.0 流速区间的是 `table_fragment` 那条，
   `normal_article` 那条**不含**区间。定级因此依赖"table_fragment 会被保留"这条
   ingestion 口径，已单独锁死（见 D2）。
5. 修了一处实现缺陷：零管段时 summary 会说"已跑的规则未发现问题" ——
   一条规则都没跑过，这话就是 `success_hallucination`（见 D7）。

## 1. 关键决策与取舍

### D1 `MappedPipe` 与 `PipeRun` 分开，不合并

`PipeRun` 要求 dn/length/material/system **齐全**；`MappedPipe` 的这些字段**可缺**。

**为何不复用一个类**：把可缺字段塞进 `PipeRun`（改成 `int | None`）会让材料统计的
必填约束失效，而那条约束正是"不猜工程量"的实现 —— 一旦 `dn_mm` 可以是 None，
`aggregate_materials` 就得自己决定"缺管径怎么办"，而它没有任何依据做这个决定。

转换由 `to_pipe_runs()` 承担，**缺字段就不转并说明缺什么**。实测：单位未声明的
管段被跳过并报 `DR-PIPE#30：缺 length_m`，而不是带着 `length_m=40.0`（按毫米瞎猜）
进材料清单。

### D2 来源分级：标准是"能否指回可检索的 source_id"，不是"文本是否在库"

实测语料（2026-09-07，非引述规格）：

| 阈值 | 条文 | 语料事实 | 定级 |
|---|---|---|---|
| 压力流流速 0.7–2.0 m/s | `GB50014-2021 5.2.9` | **在库**，区间在 `table_fragment` 变体里 | `FORMAL_CLAUSE` |
| 排水横管坡度 0.005–0.01 | `GB50015-2019 4.7.1` | 在库但原文自述"非正式规范条文" | `INFORMAL_SUMMARY` |
| 最小管径 300mm | `GB50014-2021 5.2.10` | **无 `article == "5.2.10"` 记录** | `ENGINEERING_PRACTICE` |

**最关键的一条**：`5.2.10` 的表格文本（"表 5.2.10 最小管径和相应最小设计坡度"）
因 OCR 串行**混进了 `5.2.9` 的 `table_fragment` 变体的 text 里**。
所以"grep 一下发现文本在库里"是个陷阱 —— 库里没有那个 `article`，
`validate_citations`（引用必须在检索结果中）无法通过，
**故不得标 `formal_clause`**。分级标准写死为"能否指回一个真实可检索的 source_id"。

**5.2.9 的两条变体**是实测新发现，规格未提及：含区间的是 `table_fragment`，
`normal_article` 那条只是条文说明（"压力管道在排水工程泵站输水中较为适用…"）
**不含区间**。写测试时按 article 建 dict 会让后一条覆盖前一条而丢掉这个事实 ——
测试因此改为遍历全部记录。

定级依赖 `ingestion/curated_corpus.py::_decision` 对 `table_fragment` 的裁决是
`include` / `retain_as_table_fragment`。这条口径若改变，`5.2.9` 就不再可检索，
`RULE_PRESSURE_VELOCITY` 必须降级 —— 已单独写测试锁死该裁决，会先红灯提醒。

### D3 表述纪律做成渲染层的**唯一出口**，不靠调用方自觉

`Finding.rendered_finding()` 强制加等级前缀：

- `formal_clause` → `[按规范校验（GB50014-2021 5.2.9）]`
- `informal_summary` → `[按设计要点提示（非正式条文，需人工确认）（GB50015-2019 4.7.1）]`
- `engineering_practice` → `[工程惯例提示（无条文支撑，需人工确认）]`

**为何集中在渲染而不是各处拼字符串**：分散拼接意味着每个调用方都要记得"这条规则
不能说违反规范"，而这种纪律靠自觉必然漏。集中后新增调用方**自动**合规。

`RuleSpec.__post_init__` 双向门控 `source_id`：`ENGINEERING_PRACTICE` 带
`source_id` 直接 `ValueError`（给无条文支撑的阈值配 source_id 就是伪造依据），
`FORMAL_CLAUSE`/`INFORMAL_SUMMARY` 缺 `source_id` 也 `ValueError`
（声称按规范校验却指不出条文）。这是**构造期**拒绝，不是运行期检查。

理由不是文案偏好：设计院拿着"违反 GB50014 第 5.2.10 条"的报告去改图，
而那条条文我们根本没有，那是凭空造出的规范依据（铁律②在 P3 的实例化）。

### D4 管径只来自标注的显式声明，不从线宽/图层猜

`_DN_RE` 只认 `DN`/`De` 前缀 + 2–4 位数字。**裸数字 `200` 不当管径** ——
图纸上的裸数字可能是标高、编号、坐标、图框尺寸。

一条标注含多个管径时（"DN200 变 DN150"）**取最小**。取最小是保守侧：
最小管径规则若因取大值而漏判，结果是把不合规的管段报成合规，
那是审查工具最不该犯的错。

**不猜默认材质**：`_MATERIAL_HINTS` 没命中就是 None，落进 missing_inputs。
给排水管材差价很大（球墨铸铁 vs PVC-U），猜错材质直接影响造价。
材质按**最长匹配优先**（"镀锌钢管" 先于 "钢管"、"钢筋混凝土" 先于 "混凝土"），
否则会把镀锌钢管归成普通钢管。

### D5 标注关联按"点到线段距离"，不是到无限直线

`ANNOTATION_MATCH_RADIUS = 12.0`（图纸单位）。用**线段**而非无限直线：
一条管线的标注应该在**这一段旁边**，而不是在它延长线的百米之外
（有反例测试：管段 0→10，标注在 x=200 处不被吸附）。

**近者优先且不被后续覆盖**：第一条给出某属性的标注即采用。有测试断言
反序输入结果不变 —— 覆盖会让结果依赖遍历顺序，而顺序不是工程事实。

半径值是**工程惯例而非规范**，故如实写进 `assumptions`，不假装是硬规则。

### D6 可疑标注整体排除出数值推导，但如实披露

`suspicious` 非空的标注**完全不参与** dn/material 推导（Step 20 决策记录 §4 的
交接约束）。投毒标注写个 `DN2000` 就能污染整张材料清单。

但**不静默丢弃**：`MappedPipe.ignored_suspicious_handles` 记下被跳过的 handle，
`MappingResult.assumptions` 汇总条数，`Finding.detail` 追加"附近有 N 条可疑标注
未采信"。静默丢弃会掩盖"这张图被投毒了"这个本身重要的事实。

实测端到端：投毒标注写 `DN2000`、正常标注写 `DN400`，映射结果取 400，
`ignored_suspicious_handles=('32',)`。

### D7 零管段不得读作"干净"（修了一处实现缺陷）

初版 `summary()` 在无 findings 时说"已跑的规则未发现问题，但有 N 项未检查"。
写反例测试时发现：**图纸解析失败（0 条管段）也会走到这句**——
一条规则都没跑过，说"已跑的规则未发现问题"是 `success_hallucination`
（Step 15 那个检测器盯的正是这类）。

修法是加 `pipes_reviewed` 字段并单列一支：

```
**未执行任何管段审查**：无可审查的管段（图纸解析失败或图上无可识别管线图元），
本结论不含任何合规判断
```

这不是措辞打磨。审查报告的读者会把"未发现问题"当成结论去签字。

### D8 `not_checked` 与 findings 同等重要，两条规则恒未检查也保留

- **坡度**（`RULE_DRAIN_SLOPE`）：需两端标高，而 `DrawingEntity.points` 是二维
  模型空间坐标，标高只存在于标注自由文本里（`井底标高 -1.850`）。
  Step 21 **不做**标高文本的结构化解析 —— 从自由文本推标高再算坡度，
  错一个小数点就是几十厘米的埋深偏差。
- **流速**（`RULE_PRESSURE_VELOCITY`）：流速 = 流量 / 过水断面，而流量不在图纸上，
  它来自水力计算或设计条件。图纸只给管径。

**为何保留而不删**：删掉会让报告读起来"坡度和流速没问题"，而事实是"根本没查"。
保留 + 如实报未检查，Step 22 的审查子图可以在有计算结果时喂进来。

`unit_known=False` 时额外追加一条"任何依赖长度的检查都未执行"。

## 2. 明确拒绝的做法

1. **不按默认单位换算长度** —— `unit_known=False` 一律 `missing_inputs`，
   `length_m=None`，`length_raw` 保留。估算的长度进材料清单就是错的采购数量。
2. **不用可疑标注的内容做任何数值推导** —— 见 D6。
3. **不给 `engineering_practice` 配 `source_id`** —— 构造期 `ValueError`，见 D3。
4. **不因"文本在库里"就标 `formal_clause`** —— 见 D2，标准是可检索的 source_id。
5. **不从线宽/图层猜管径**，**不假定默认材质** —— 见 D4。
6. **不做标高文本解析** —— 见 D8。
7. **不让映射层碰文件系统** —— 输入是 `DrawingExtraction`，路径防御全留在
   `dxf_reader.py`。一旦映射层能打开文件，Step 20 的"模型无法表达路径"就多一个绕过口。
8. **不合并 `MappedPipe` 与 `PipeRun`** —— 见 D1。

## 3. 残余风险

1. **真实图纸未验证**（承自 Step 19/20）：全仓 0 个真实样例。真实设计院 DXF 的标注
   位置、图层命名、块参照都与合成图不同，`ANNOTATION_MATCH_RADIUS=12.0` 这个值
   **几乎肯定需要按真实图纸重新标定**。
2. **只覆盖 LINE/POLYLINE 作为管段**：真实图纸的管线可能是 INSERT 块、多段
   ARC、或带宽度的 LWPOLYLINE。当前会漏掉这些。
3. **`system` 依赖图层名前缀猜测**（复用 `dxf_reader._layer_system`）：
   非标图层命名会得到 `system=None` → 不进材料清单（保守侧，但会漏统计）。
4. **管段级去重未做**：`system` 过滤发生在截断之前（Step 20 残余风险 6），
   多次调用拼接时要按 handle 去重，Step 22 需处理。
5. **规则只有 3 条，其中 2 条恒未检查**：实际可判定的只有最小管径。
   缺陷召回率（规格 §7 要求 ≥80%）**尚无法度量** —— 那是 Step 22 的 `drawing_p0`
   评测集的事，本步不声称达标。
6. **坡度阈值区间来自非正式综述**：`0.005~0.01` 取自 4.7.1 的要点综述文本，
   若将来补入正式条文可能需修正（届时定级可升为 `formal_clause`）。

## 4. 交接给 Step 22

Step 22 做审查子图 + `drawing_p0` 评测集 + 门禁接入，约束：

- **子图管线**按 `02-loop-engineering.md:67-71`：
  `load → extract_entities → map_to_schema → rule_check → report_defects`。
  前三步已有实现（`read_drawing` / `map_pipes`），Step 22 接的是编排与报告。
- **缺陷召回率口径见规格 §7，不得复用 `harness/retrieval_metrics.py`** ——
  那套是检索指标，缺陷召回的分母是"人工标注的缺陷数"，不是检索命中。
- **报告渲染必须走 `Finding.rendered_finding()`**，不得自己拼字符串
  （表述纪律的唯一出口，见 D3）。
- **`not_checked` 必须进最终报告**，且 `pipes_reviewed == 0` 时不得出现任何
  合规性措辞（见 D7、D8）。
- **`state.drawings: list[Any]`** 目前零消费，Step 22 消费它时才收紧类型
  （P3 规格 §8"本轮不改动"登记项）。
- **加工具前先核对 token 预算**：headroom 130（4370/4500），`< 4500` 断言不得上调。
