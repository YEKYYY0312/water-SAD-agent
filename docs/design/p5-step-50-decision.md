# P5-S50 决策记录：DXF 上传 → 程序化审查 → 进入对话

日期：2026-09-29　状态：已交付（本地 compose + 真实 PG 18 + 真实 DeepSeek 实测）　规格：`p5-step-50-dxf-upload-review.md`

## 交付

- **`documents/drawing_upload.py`**：DXF 魔数校验与上传审查。
  - 魔数：ASCII 文件要求首个组码为 `0` 且其后是 `SECTION`；二进制 DXF 按哨兵识别；改名为 .dxf 的 DWG（`AC10xx`）单独给出"另存为 DXF"提示。
  - 审查在**只含 `drawing.dxf` 的临时目录**里跑（root 就是这个目录，`read_dir_allowed` 与 `relative_to` 两道防线原样生效），用完即删。
  - 报告通过审查子图的 `_build_report` / `render_report_text` 生成，报告格式只有这一处定义；只替换标题里的登记号，并加一个计数前言。
- **`extractors.py`**：白名单加入 `.dxf`，抽取产物就是审查报告（engine `dxf-review`）。新增 `ExtractedDocument.suspicious`，用来带出注入图元：报告里没有图元原文，分块护栏看不到这些文字，可疑标记必须从抽取阶段带出来。
- **迁移 `0006_step50_dxf_uploads`**：`ck_uploads_suffix` 放行 `.dxf`（用 batch 模式，SQLite 与 PG 通用）。`MIGRATION_HEAD` 三处同步：database.py、compose.yml 的 smoke、test_memory_store。
- **`dxf_reader.read_drawing`**：截断的 DXF（头部合法、缺 EOF）会让 ezdxf 抛 `StopIteration`，现在收进 `DRAWING_UNREADABLE`。这是**上传测试新暴露的真实缺陷**：违反了该函数"永不抛出"的契约，在协程里还会变成 RuntimeError。已加回归参数。
- **能力说明**（`file_context`）随白名单推导：DXF 可上传并自动审查，转述时必须保留未检查项；DWG 暂不支持。
- **前端**：`.dxf` 可选；`UPLOAD_TYPE_NOT_ALLOWED` 文案提示 DWG 另存为 DXF；文件卡片从报告首句固定格式读出"已审查 N 条管段，M 条待核问题，K 项未检查"，列表接口没有预览时不猜数字。

## 没有改动（规格 D2）

- 不在 Web 路径注册 `read_drawing`，没有新增模型可调用工具。`governance_coverage()` 仍为 6/6/5/5，schema token 不变（有测试断言）。
- P3 的规则、映射、关联半径与报告措辞均未改动。

## 实测

- 18 张真实样图全图审查（`max_entities=20000`）：最慢 1.04 s（P-01，5,377 个图元），17/18 张未截断（P-07 超出上限，报告中如实声明）。
- 真实 PG 18.2：`0005 → 0006` 迁移成功，约束定义已包含 `.dxf`，`/health/ready` 报告 `0006_step50_dxf_uploads`。
- 本地 compose + 真实 DeepSeek：
  1. 改名为 .dxf 的 DWG → 400 `UPLOAD_MAGIC_MISMATCH`，提示"请在 CAD 中另存为 DXF"。
  2. 上传 P-02.dxf → 201，4 个分块，预览为"已审查管段 645 条，待核问题 2 条，未检查 6 项"。
  3. 附上图纸问"审查这张图" → `attachment_read` 读全 4/4 段；run 为 `succeeded`，引用 `GB50014-2021 5.2.9`、`5.2.5`（两条都经过 `cite_clauses` 校验）。回答先说"不能判定为合规或不合规"，列出 2 条待核问题、6 项未检查（标明"不得视为合规"）、4 项缺输入，并把报告的"工程惯例"口径与检索到的正式条款分开表述。
- `ci_gate.sh` 全绿；前端 43/43。

## 残余

- **模型会把"工程惯例"补强为正式条款**：实测回答里把报告标为 `engineering_practice` 的最小管径 DN300，对照到了 GB50014-2021 表 5.2.10（经 5.2.9 引出）。这条引用通过了校验，因为 5.2.9 的 `table_fragment` 变体里确实有这张表。但 P3 Step 21 已经裁定"库里没有 5.2.10 这个 article，所以只能标工程惯例"。模型这次的说法（"若确属污水管则不符"）有条件、措辞谨慎，但仍比规则引擎走得更远。需要评估：提示词是否应要求"不得替报告升级来源等级"。
- 召回率仍是 blocked（真实缺陷 gold 只有 2 条），P-02 的 2 条发现不构成准确率证据；误报率问题见 P5 memory 中的记录。
- 标注关联半径 20 图纸单位，没有按真实图纸标定。
- 只有显式附件和字面召回两条路径；报告只会在问题与报告字面重合时被召回，追问时建议重新附上图纸。
