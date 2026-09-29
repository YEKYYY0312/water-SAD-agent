# P6-S1 决策记录：参数化生成给水系统 DXF 图纸

日期：2026-09-29　状态：实施中（Step 1 已交付；实测数字在 Step 7 回填）
规格：`docs/design/p6-step-1-parametric-drawing-generation.md`；执行 prompt：`docs/execution-prompts/P6-S1-parametric-drawing-generation.md`（其 §0 对 spec 的修订以 prompt 为准）

## 0. 偏离与修订

### 0.1 执行 prompt §0 的修订（照做）

| spec 原文 | 本步做法 |
|---|---|
| 生成后自动触发 `review_drawing`，报告"0 缺陷 / 3 警告" | 逐张用 S50 `review_uploaded_dxf()` 复审，报告**原样**附上（含未检查项），不写"审查通过" |
| `ezdxf.addons.drawing` 渲染 PDF 预览 | 不做（Pillow / matplotlib 不在依赖里） |
| 字体"仿宋_GB2312" | 文字样式 `WS-FS`：字体 `simfang.ttf` + SHX 大字体 `gbcbig.shx` |
| 只输出 DXF | DXF 为主；DWG 为**实验性**可选输出（LibreDWG `dxf2dwg`，`dwg2dxf` 回读核对，核对不过只交付 DXF） |
| 路由放 `run_routes.py` | 新建薄适配器 `webapp/drawing_routes.py` |
| 文件落任意位置 | `resolve_export_dir()/drawings/{generation_id}/` |

### 0.2 本步实测后追加的偏离（执行 prompt 的前提与代码现实不符）

1. **规则表 `basis` 用三档，不是两档**。prompt 写 `formal_clause` / `engineering_practice` 两档，前提是"GB50015-2019 只有 4.7.1 自述非正式"。实测 `GB50015-2019` 在库 **9 条全部**自述"联网整理的设计要点综述，非正式规范条文"（`tests/test_drawing_building_rules.py::test_gb50015_in_corpus_is_entirely_informal` 锁死）。两档制下，3.7.3（流速）、3.3.1（防倒流）这类能指回真实 source_id 的要点综述只能降为工程惯例，溯源就丢了；而标 formal 又是伪造依据。故沿用 P3 `drawing/rules.py` 的 `EvidenceLevel` 三档，中间档 `informal_summary` 必须带 source_id，图面写"设计要点提示（非正式条文，需人工确认）"。
2. **S50 复审对纯给水图会说"已检查的规则均未发现问题，未检查 0 项"**。prompt 以为"给水图多数规则会落到未检查"，实测不是：`drawing/rules.py` 的三条排水规则在图上没有排水管段时直接 `return [], []`，给水管段根本没有规则去查，`summary()` 却落到"已检查的规则均未发现问题"。原样附上这份报告就等于写了"审查通过"。修法放在唯一出口 `rules.py`（Step 5 单独 commit）：给水管段如实计入 `not_checked`，全图都是给水时结论改为"未对任何管段执行规则判定"。
3. **复审的"已审查管段"比生成器画的管段多**。`map_pipes` 把**所有** LINE / LWPOLYLINE 都当管段（P3 Step 21 残余风险 2），轴网线、图例与阀门符号里的线条、大样轮廓都会被计入。本步不改审查器，结果里把两个数分开报：生成器自己的管段数（`WS-PIPE-*` 图层）与复审的计数。
4. **请求契约多三个可选字段**：`riser_position` 缺省时取规则表默认（spec D2"用户指定或规则默认"）；`branch_flow_rate_lps` 支管设计流量，缺省时支管流速**不校核**并进 `missing_inputs`（不按总流量均分估算）；`include_dwg` 默认 `false`，DWG 是实验性输出，需用户显式勾选。
5. **多一个图层 `WS-OUTLINE`**（大样里的井壁、楼板、吊顶、吊架）。prompt 列的七个图层里没有能放构筑物轮廓的。
6. **管径标注只写 `DN50`，不写材质**；材质写进说明。标注要压在管线 20 图纸单位以内（`ANNOTATION_MATCH_RADIUS`），基线距管线 12 mm，1:100 出图时约 0.12 mm，图面上标注紧贴管线。代价是复审会报"material：N 条管段无材质标注"；且审查器的 `_MATERIAL_HINTS` 不认识"钢塑复合"，即使写上也读不出（审查器词表缺口，不在本步修）。

## 1. 目标与边界

输入结构化给水设计参数，由确定性程序生成三张 DXF（标准层平面图、给水系统轴测图、节点大样图），打 ZIP 下载，附程序化复审报告。整个生成器不 import 任何模型或 HTTP 客户端（AST 静态断言）。

## 2. 规则来源分级（`src/water_agent/drawing/building_type_rules.json`）

分级标准与 P3 Step 21 D2 相同：**能否指回一个真实可检索的 source_id，且原文不自述非正式**，不是"文本看起来在库里"。

| rule_id | 内容 | basis | source_id |
|---|---|---|---|
| `ws.pipe_velocity` | 流速不宜大于约 2.0 m/s，常规 0.6~1.2 m/s；超出只提示复核 | informal_summary | GB50015-2019 3.7.3 |
| `ws.backflow_prevention` | 倒流污染防护设止回阀等；本图引入管水表后设止回阀 | informal_summary | GB50015-2019 3.3.1 |
| `ws.pump_suction_valve` | 最高进水液位高于水泵进水管时设手动检修阀 | formal_clause | GB50013-2018 6.3.3 |
| `ws.pump_discharge_valves` | 出水管设工作阀和检修阀，不用无缓闭功能的普通逆止阀 | formal_clause | GB50013-2018 6.3.5 |
| `res.*` / `off.*` / `com.*` | 柱网、立管位置、敷设方式、阀门水表设置、引入管、大样类型 | engineering_practice | — |

- 两条 formal 都来自《室外给水设计标准》的泵房条文。用于建筑内加压泵房是否合适需要设计人判断，规则表用 `scope_note` 把这句提醒一起渲染到图面。
- 每条可指回语料的规则都带 `corpus_quote`（原文逐字片段），测试逐条核对它在该 source_id 的语料里真实存在；formal 的原文不得含"非正式"，informal 的原文必须含。
- 图面说明的唯一出口是 `RuleText.rendered_note()`：工程惯例一档不得出现任何规范号，非正式档不得出现 `FORBIDDEN_IN_INFORMAL` 里的合规措辞（与 P3 共用常量）。

## 3. 不做

- 排水、消防、热水；建筑结构（梁板柱、墙体）；三维 / BIM。
- 长度尺寸标注；图签、会签栏、图框、图纸目录。
- 读取用户上传的底图来布管。
- 模型可调用工具（工具 schema headroom 只剩 130 token；本步只有 HTTP 路由）。
- PDF 预览。
- 供水分区与减压阀（`pressure_mpa` 只作为设计参数写进说明）；横管坡度数值；立管逐层变径（缺逐层设计秒流量，不猜）；DN 与 De 的换算（换算表不在输入与计算结果里）。

## 4. 实测（Step 7 回填）

## 5. 复审结果原样摘录（Step 7 回填）

## 6. DWG 输出与回读核对（Step 7 回填）

## 7. 残余风险（Step 7 回填）
