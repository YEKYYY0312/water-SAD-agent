# P2 规格：计算书/设计说明生成 + 材料统计（V1.0）

> 本文档是 P2（计算书/设计说明 docgen + 材料统计 materials）的**权威规格**（被
> `docs/design/06-roadmap.md` P2 一节与 `AGENTS.md` 引用为 P2 口径来源），对齐
> `docs/design/10-sdd-harness.md`（SDD：每条验收都能映射到测试/评测/gate 步骤）。
>
> 文档覆盖两层：
> - **机制层（已交付，2026-09-03）**：确定性模板引擎 + 材料聚合引擎 + 确定性评测；
> - **质量层（2026-09-03 后置）**：真实 LLM 基于结构化底座（`RenderedDoc`）润色
>   prose + 质量自检 + real 变体评测（SKIP 语义）。
>
> 三条铁律在本规格的实例化：
> ① 数值程序化：文档/清单里出现的数值只能来自输入浮点原值（或程序化求和/舍入），
>    LLM 永不参与数值运算、不推断、不补数；
> ② 引用可追溯：正文只允许出现输入声明的 source_id，模板/润色禁止自造条款号；
> ③ 评测先行：每条验收映射到确定性命中（`data/eval/docgen_p0.jsonl` /
>    `material_p0.jsonl`）或回归测试，机制层改动不得使 gated 指标下降。

---

## 1. 范围与目标

### Goal
- 输入计算结果 + 引用（+ 可选材料清单）→ 输出**结构化、可评测、可追溯**的
  计算书/设计说明（草稿预览与正式文档同一数据源）；
- 输入管段表 → 输出按「材质 × 规格」聚合、逐行可溯源、数量程序化舍入的材料清单
  （`.xlsx`/CSV 导出）。

### 非目标（P2 不做）
- 不生成正式图纸/表格文件（材料 `.xlsx` 只含清单与说明 sheet）；
- docgen 不做 DWG 读取（材料统计的自动抽取依赖 P3）；
- 不做真实 LLM 的数值计算、不做格式外的自由发挥（prose 润色只允许在章节内改写措辞，
  数值/引用/章节骨架仍由结构化底座程序化决定——见 §6）。

---

## 2. 输入契约（Inputs）

### 2.1 docgen 渲染输入

`render_docgen_doc(**inputs)`（`src/water_agent/docgen/templates.py`）唯一输入面：
docgen 图 draft/finalize 与机制层评测共用，字段如下。

| 字段 | 类型 | 必填 | 语义与约束 |
|---|---|---|---|
| `query` | `str` | 必填 | 项目/查询描述；渲染进「工程概况」。空串渲染为「（未提供）」。 |
| `doc_type` | `str` | 必填 | `calculation_book`（计算书）\| `design_note`（设计说明）；枚举外值直接结构化错误（未知文档类型），禁止静默映射。 |
| `calc_result` | `dict[str, Any] \| None` | 可选 | 计算子图/检索汇集的**结构化结果**。键分两类（见下）。`None` = 本次未执行程序化计算。 |
| `citations` | `Sequence[CitationClaim \| {source_id} \| str]` | 可选 | 已声明的规范引用（设计依据）。只渲染各元素的 `source_id`；空 → 正文明确写「未声明规范依据」。 |
| `feedback` | `str` | 可选 | HITL 拒绝回流的人审反馈；非空追加「用户反馈（已吸收）」章节（可追溯）。 |
| `draft_no` | `int \| None` | 可选 | 给定时标题为「XXX草稿（第 N 稿）」；`None` = 正式标题。 |
| `material_rows` | `Sequence[dict]` | 可选 | 可选材料统计展示（模板**不计算**材料，仅展示调用方聚合结果行，形态见 §3.2 的 `MaterialRow.to_dict()`）。 |

`calc_result` 键的语义分流（模板层 `_split_numeric` 执行，**数值进 `calc_values`、
其余进备注**；数值键必须有限（isfinite），非有限数值走备注且注明原因）：

| 键类别 | 键示例 | 渲染行为 |
|---|---|---|
| 数值键 | `velocity_mps`、`flow_lps`、`diameter_mm`、`head_loss_m`、`pump_head_m`、`slope`、`length_m`、`static_head_m`、`friction_head_m`、`local_head_m`、`residual_head_m`、`fill_ratio`、`manning_n`、`hazen_williams_c`、`friction_factor` | 浮点原值进 `calc_values`（判分用）；展示 = `{key} = {value:g} {单位}` |
| 数值入参（P2 收口后 docgen gather 合并） | 同上名称（如 `diameter_mm=150`、`flow_lps=20` 表示入参） | 与结果同表展示，仍只渲染浮点原值 |
| 备注键 | `missing_inputs`、`errors`、`assumptions`、`candidates`、`formula`、`spec_record`、其他非数值文本 | 以「备注标签：文本」行输出（`NOTE_LABELS` 有展示名；缺省回退键名） |
| 非法数值（`inf`/`nan`） | 任意数值键 | **不进 `calc_values`**；渲染为备注「非有限数值（inf/nan），不渲染为数值」 |

> 备注键的**内容由调用方负责成文**（如 docgen gather 把 `missing_inputs` 列表 join 成
> 文本）；模板只透传，不推断、不补数。空串备注值视为无内容，但此时若整个 `calc_result`
> 非空却既无数值也无备注，自检判定为「输入被静默丢弃」并报错（见 §4.4/§6.2）。

### 2.2 materials 输入：管段表

`aggregate_materials(segments)`（`src/water_agent/materials/core.py`）/ 工具
`material_stats`（`src/water_agent/tools/material_stats.py`）输入单条管段
（`PipeRun` 或同构 dict / `PipeSegmentArgs`）：

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| `segment_id` | `str` | **必填** | 非空、唯一（重复 = 结构化错误，防长度重复计数） |
| `system` | `str` | **必填（无默认）** | `water_supply`（给水）\| `drainage`（排水/污水）；缺失或未知值 = 结构化错误（禁止静默回退 water_supply） |
| `dn_mm` | `int` | 必填 | 正整数（>0；工具层 ≤4000）；小数/非整数 = 非法，不静默截断 |
| `length_m` | `float` | 必填 | 正数且**有限**（`isfinite`）；`inf`/`nan`/`≤0` = 结构化错误 |
| `material` | `str` | 必填 | 非空；不在常见管材清单只进 `assumptions`（人工核对），不阻断 |

工具 schema 层（`PipeSegmentArgs`，`extra="forbid"`）在引擎层之外再挡一层：
`system` 无默认值、`dn_mm>0`、`length_m` 有限且 `>0`。

---

## 3. 输出契约（Outputs）

### 3.1 `RenderedDoc`（docgen 渲染产物）

```python
@dataclass(slots=True)
class RenderedDoc:
    doc_type: str                      # calculation_book | design_note
    title: str                         # "# 计算书" / "# 计算书草稿（第 N 稿）" …
    sections: list[DocSection]         # 结构化章节：key（无编号短名）+ heading + lines
    calc_values: dict[str, float]      # 数值键 → 浮点原值（判分/润色数值注入的唯一来源）
    citation_ids: list[str]            # 去重后的 source_id（正文允许出现的引用全集）
    notes: list[str]                   # 缺参/错误/假设等备注（未参与数值）

    @property
    def text(self) -> str: ...         # Markdown 全文（草稿/终稿共用渲染路径）
```

必需章节（`REQUIRED_SECTIONS`，**canonical，不可跳过**，渲染与判分共用）：

| `doc_type` | 必需章节 |
|---|---|
| `calculation_book` 计算书 | 工程概况、设计依据、计算参数与结果、结论 |
| `design_note` 设计说明 | 工程概况、设计依据、设计参数与计算结果、结论 |

可选章节：材料统计（仅当 `material_rows` 非空）；用户反馈（仅当 `feedback` 非空）。

### 3.2 `MaterialTable` / `MaterialRow`

```python
class MaterialTable:                    # ok = not errors；ok=False 时 rows 为空
    rows: list[MaterialRow]
    total_length_m: float               # 全部管段长度之和，round(QUANTITY_PRECISION=3)
    errors: list[str]                   # 结构化问题清单（空表/重复段号/缺 system/非有限…）
    assumptions: list[str]              # 未知材质等人工核对提示

class MaterialRow:
    material: str                       # 材质（按输入原样）
    spec: str                           # 规格 "DN{dn}"
    unit: str = "m"
    quantity: float                     # = round(组内长度和, 3)（展示/判分用）
    raw_quantity: float                 # 未舍入精确和（口径审计）
    segments: list[str]                 # 参与本行的段号（有序去重）
    detail: str                         # "S1(120.5m)、S2(80m)" 逐段明细
```

- **数量 = 组内各管段长度之和**（程序化；`QUANTITY_PRECISION = 3` 为单一舍入口径，
  工具输出、评测 gold、Excel 数值单元格共用）；
- 行顺序 = `(material, spec)` 字典序，确定性；
- 聚合键 = `(material, dn_mm)`；`system` 不参与分组（同材质同径跨系统合并）但必须
  作为输入存在（§2.2）。

### 3.3 `.xlsx`/CSV 布局

- `.xlsx`（`write_materials_xlsx`，openpyxl，已入依赖）：sheet1「材料清单」
  表头 `序号/材料名称/规格/单位/数量/来源说明（管段明细）`（数值单元格，格式 `0.000`，
  冻结首行 + 筛选）；sheet2「统计说明」：行数、管道总长度、逐行材料与来源、assumptions。
- CSV（`write_materials_csv`）：utf-8-sig，同一表头；后续统计说明行。
- openpyxl 缺失 → `ImportError` 附安装指引（不静默降级、不产出空文件）。

---

## 4. 不变量（Invariants）

| # | 不变量 | 强制位置 |
|---|---|---|
| I-1 | 文档中的数值只来自输入浮点原值（渲染/展示不运算、不推断、不回填） | 模板 `_split_numeric` 只读输入；`calc_values` 不进任何数值运算 |
| I-2 | 引用只渲染输入 `source_id`；正文出现任何输入外的「规范号+条款号」= 违规 | `validate_citation_policy`（模板 + 评测 + 自检） |
| I-3 | canonical 必需章节按 `doc_type` **永远**强制（不可跳过、不可被评测配置清空） | 渲染 `check_structure`；判分 `score_docgen_case` 恒查 `REQUIRED_SECTIONS[doc_type]`；`DocgenCase.required_sections` 只能追加约束 |
| I-4 | 材料数量 = 组内管段长度和，`round(3)`，单一舍入口径 | `materials/core.py::_fmt_qty`；xlsx 数值单元格 |
| I-5 | 每个 `segment_id` 必须可追溯到至少一行材料（无静默丢弃） | 聚合 `segments` 列表；评测溯源覆盖检查；引擎重复段号报错 |
| I-6 | NaN/inf 拒绝进入数值路径 | 材料引擎边界 `isfinite` 校验（工具 schema `gt/le` + 引擎显式检查）；模板数值分流 isfinite 检查 |
| I-7 | 缺必填字段（如 `system`）= 结构化错误，**永不静默默认** | 引擎 `_parse_segments` + 工具 schema |
| I-8 | 审批只接受严格 `bool`（`True`/`False`）；字符串/数字等其他形态 = 明确错误 | `graphs/docgen.py` approval 节点 |
| I-9 | `failed` 状态终止图流，不进入 `finalize`；`final_doc` 只在批准后产出 | 条件路由 + finalize 守卫（双保险） |
| I-10 | 草稿必须通过自检（结构/引用/数值完整性）才能到达审批/终稿；自检失败有界重试（≤2 次）或带可见错误收口 | `graphs/docgen.py` self_check 节点 |
| I-11 | 无设计依据时正文必须显式说明，而非留空/编造 | 模板「未声明规范依据」备注 |

---

## 5. 验收 → 测试映射（Acceptance → Test）

roadmap §P2 验收项 → 证明它的测试文件 / 评测用例 / gate 步骤。**任何没有测试的
验收项显式标注「无测试」**。

| roadmap §P2 验收 | 测试文件 / 评测集 / gate | 证明方式 |
|---|---|---|
| 计算书/设计说明基于计算结果+引用生成、结构合规 | `tests/test_docgen_templates.py`、`tests/test_p2_docgen_graph.py`；`data/eval/docgen_p0.jsonl`；ci_gate docgen_p0 ≥95% | 必需章节齐全（`check_structure`/scorer canonical）；模板单元测试逐 doc_type 断言 |
| 数值与原值一致（I-1） | `tests/test_docgen_templates.py::test_numeric_values_kept_exact_*`；docgen_p0 `required_values` 双阈值 | 渲染 `calc_values` 浮点原值 == 输入；正文展示带单位 |
| 引用不编造（I-2） | `tests/test_docgen_templates.py::test_citation_policy_*`；docgen_p0 `citations_required/forbidden` | 策略校验器 + 评测正反例 |
| 材料清单数值与计算一致、可追溯（I-4/I-5） | `tests/test_materials.py`；`data/eval/material_p0.jsonl`（行集合精确一致 + 数量双阈值 + 全段溯源覆盖） | scorer `score_material_case` |
| 关键文档生成前人工确认（I-8/I-9） | `tests/test_docgen_hitl.py`（interrupt/resume/拒绝回流/上限/failed）；`tests/test_p2_docgen_graph.py`（sqlite resume）；`tests/test_cli_flows.py`（CLI 人审 y/n + trace） | interrupt 挂起、approval_log、`final_doc` 仅在批准后产出 |
| docgen 草稿自检（I-10，P2 收口新增） | `tests/test_docgen_templates.py`（自检函数）+ `tests/test_p2_docgen_graph.py`（自检节点路由/重试/收口） | 自检失败不达审批；重试 ≤2 或失败可见 |
| 结构 gate 不可绕过（I-3，P2 收口新增） | `tests/test_p2_harness.py::TestDocgenScoring`（空 `required_sections` 用例仍查 canonical） | scorer 恒查 `REQUIRED_SECTIONS[doc_type]` |
| docgen 输入完整性（I-6/I-7，P2 收口新增） | `tests/test_docgen_templates.py`（inf 不进 calc_values）、`tests/test_materials.py`/`tests/test_p2_tool.py`（缺 system/非有限 = 结构化错误）、docgen_p0/material_p0 invalid 用例 | 引擎 + 工具 + 评测三处 |
| docgen 无效输入可评测（P2 收口新增） | `data/eval/docgen_p0.jsonl` invalid_input 用例（未知 doc_type） | runner/scorer 把渲染异常映射为 `invalid_input` |
| 材料统计导出（.xlsx/CSV） | `tests/test_materials.py::TestExport`（round-trip 列值/数值单元格/utf-8-sig/openpyxl 缺失报错） | 导出内容与引擎一致 |
| 工具层唯一入口与结构化错误 | `tests/test_p2_tool.py`（schema 拒绝、重复段号 → `TOOL_ERROR`、负长度 → `INVALID_ARGUMENTS`） | `ToolRegistry.invoke` 契约 |
| P2 通过率回归不下降（评测先行） | `scripts/ci_gate.sh`（pytest + ruff + mypy + rag_p0/calc_p0/docgen_p0/material_p0 + baseline compare）；`data/eval/baseline.json` 含 docgen_p0/material_p0 | ci_gate 全绿；compare_baseline 阈值（pass ≤2pt decline / PTP=0） |
| **质量层：真实 LLM prose 润色**（§6，网络+Key 后） | `data/eval/docgen_quality.jsonl --variant real`（`tests/test_docgen_quality.py` 机制层 + runner real 变体 SKIP） | **无 Key/网络 → 结构化 SKIP**；产出报告为 blocked 证据，不伪造通过 |
| **质量层：材料鲁棒性套件**（§6.4） | `data/eval/material_quality.jsonl` + `tests/test_materials_quality.py` | 离线全绿（见 §6.4） |

### 未覆盖项（显式声明）

- **真实 LLM 润色结果**：无测试能离线证明真实润色质量（需网络+Key）；机制层只能证明
  「润色管线正确性」（数值注入、引用受限、结构保留），质量结论以 real 变体报告为准，
  无报告期间为 **blocked**，不是 pass。
- **真实语料正式引用**：现行语料为「要点综述/非正式条文」，正式引用验证仍 blocked
  （与 rag_p1 同一数据质量阻塞）。

---

## 6. 质量层设计（P2 收口）

机制层 = 确定性底座（已交付）。质量层在其上叠加，**所有数值仍程序化注入**：

### 6.1 分层

```
RenderedDoc（结构化底座：calc_values / citation_ids / sections）
        │  程序化：只允许在章节内改写措辞
        ▼
prose polish（真实 LLM；可选，无 Key 时跳过 → 原样输出底座文本）
        │
        ▼
quality self-check（确定性，离线）：数值保真 / 引用保真 / 结构合规 / 不新增条款
        │
        ▼
approval（HITL，严格 bool）── 通过 → finalize（同一数据源正式渲染）
```

- 草稿与终稿都只以 `RenderedDoc.text` 为输出形态；polish 只作用于**章节行文本**，
  不触碰 `calc_values`/`citation_ids`/章节骨架；
- polish 的数值只能从 `calc_values` 以字符串注入（程序化侧 `_fmt_number`），引用只能
  从 `citation_ids` 选取；
- 正文里带「要点综述 / 非正式规范条文」性质的素材必须显式披露其非正式性质（不能写成
  正式规范原文）——沿用 rag 路径的语料纪律。

### 6.2 质量自检内容（确定性，`docgen/quality.py`）

对 polish 后的 `RenderedDoc` 复检：
1. **结构合规**：`check_structure`（canonical 章节仍在，无新增/删除必需章节）；
2. **数值保真**：解析正文中出现的数字/`key = value` 行，与 `calc_values` 双阈值比对
   （≤1e-6 相对）；数字不可凭空多出/改值；底座正文里逐字出现过的 token 先按白名单放行、
   再判有限性——编号的 hex 尾巴（`seg-21634e590093`）会被读成溢出的科学计数法（2026-10-05）；
3. **引用保真**：`validate_citation_policy`（正文「规范号+条款号」⊆ `citation_ids`）；
4. **无编造条款**：与 3 同一校验器（引用策略即编造检测）；
5. **数值完整性**：`calc_values` 全有限；`calc_result` 有输入但无数值也无备注 → 报错
   （输入被静默丢弃）。
任何一项失败 → 结构化问题清单（可见），不给用户"看起来完成"的产物。

### 6.3 质量层评测证据语义

- **机制层质量评测（离线、确定性判分）**：`data/eval/docgen_quality.jsonl`，judge =
  确定性 self-check + 结构/数值/引用断言（不需要真实模型）；
- **real 变体**：复用 harness `variant` / SKIP 语义 —— `--variant real` 无 Key/网络
  不可达 → 全部结构化 SKIP（exit 0、不参与基线）；有 Key 但组件构建失败 → exit 3；
- 真实运行产出报告进 `data/eval/reports/`（gitignore），结果如实记入
  `docs/evals/p2-quality-<date>.md`：**blocked 写 blocked，绝不写成 pass**。

### 6.4 材料质量套件（鲁棒性/回归，离线）

`data/eval/material_quality.jsonl` + `tests/test_materials_quality.py`：
inf/nan 长度、缺 `system`、重复 segment_id、混合系统合并、舍入精度（round 3）、
溯源覆盖、xlsx round-trip。全部离线确定性，机制层判分。

---

## 7. 相关文件索引

- 引擎/评测：`src/water_agent/docgen/templates.py`、`graphs/docgen.py`、
  `materials/core.py`、`materials/export.py`、`tools/material_stats.py`、
  `harness/{loader,scorer,runner}.py`、`docgen/quality.py`（质量层）
- 数据：`data/eval/docgen_p0.jsonl`、`data/eval/material_p0.jsonl`、
  `data/eval/docgen_quality.jsonl`、`data/eval/material_quality.jsonl`、
  `data/eval/baseline.json`
- 门禁：`scripts/ci_gate.sh`、`scripts/compare_baseline.py`
- 评审/收口：`docs/reviews/step10-production-checklist-2026-09-03.md`、
  `docs/evals/p2-quality-<date>.md`（质量层汇总）
