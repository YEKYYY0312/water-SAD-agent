# P6-S1 执行 Prompt：参数化生成给水系统 DXF/DWG 图纸

> **工作区**：仓库 `YEKYYY0312/water-SAD-agent`，基于 `main`（`5707b4b` 及之后）。新建分支 `YEK/p6-s1-drawing-generation`。
> **必读**：
> - `AGENTS.md`（三条铁律、验证命令、改动纪律）
> - `docs/design/p6-step-1-parametric-drawing-generation.md`（spec，本 prompt 对其做了若干**修订**，见下文 §0）
> - `docs/design/10-sdd-harness.md`
> - P3/P5 的图纸链决策记录：`p3-step-21-decision.md`、`p5-step-50-decision.md`、`p5-step-51-dwg-read.md`

---

## 0. 相对 spec 的修订（以本 prompt 为准）

spec 写于 S49–S52 之前，下面几处与现状冲突或写得不准，以本节为准：

| spec 原文 | 修订 | 原因 |
|---|---|---|
| 生成后"自动触发 `review_drawing`"，并报告"0 缺陷 / 3 警告" | 生成后**用 S50 的 `review_uploaded_dxf()` 复审**，把审查报告**原样**附在结果里，包括"未检查项" | S50 起审查结论只有这一个出口；现有规则大多针对**排水**（`drain.*`），给水图多数规则会落到"未检查"，**不得写成"审查通过"** |
| 用 `ezdxf.addons.drawing` 渲染 PDF 预览 | **本步不做 PDF 预览** | 该插件依赖 Pillow 和 matplotlib，这两个都不在依赖里（已实测 `ModuleNotFoundError: PIL`）。引入它们需要单独拍板 |
| 字体"仿宋_GB2312" | 文字样式名用 `WS-FS`，字体文件用 `simfang.ttf`，另加 SHX 大字体回退 `gbcbig.shx` | CAD 里找不到字体只会替换显示，不会报错；只写字体名，在没装仿宋的机器上会乱码 |
| 输出只有 DXF | DXF 为主。**可选**用 LibreDWG `dxf2dwg` 转出 DWG，但必须标注"实验性"，并用 `dwg2dxf` 回读校验，校验失败就只交付 DXF 并说明原因 | 与已批准的计划一致；LibreDWG 写 DWG 仍是实验功能，S51 实测过它写出的 DWG 会带 `handle 0` |
| 新路由放在 `run_routes.py` | 新建 `webapp/drawing_routes.py` 薄适配器 | 与 `upload_routes.py`、`export_routes.py` 同一风格 |
| 生成文件落到任意位置 | 落到 `resolve_export_dir()` 下的 `drawings/{generation_id}/` | `/app/data` 是只读的，导出卷是现成的可写位置 |

---

## 1. 目标与边界

**做**：输入结构化的给水设计参数，由**确定性程序**生成三张 DXF：
- 平面图（标准层）
- 给水系统轴测图
- 节点大样图

打成 ZIP 供下载，附上程序化复审报告，Web 端提供表单和下载入口。

**不做**（spec D5 加上本 prompt 的补充）：
- 排水、消防、热水系统
- 建筑结构，三维或 BIM
- 长度尺寸标注
- 图签、会签栏、图框、图纸目录（由设计院模板决定，**不猜**）
- 读取用户上传的底图来布管
- 把生成功能做成模型工具（本步只提供 HTTP 路由。是否暴露给模型，等主链接线时再按"按意图最小暴露"裁决。工具 schema 只剩 130 token 余量，放不下新工具）
- PDF 预览

## 2. 铁律在本步的落点（不得违反）

1. **计算程序化**：
   - 图上的每一个数值（管径、标高、流速）都**只能来自输入参数，或 `tools/hydraulics.py` 的计算结果**。
   - LLM 不参与任何几何或数值推导。**整个生成器不得 import 任何模型或 HTTP 客户端**，要加一条 AST 静态断言，写法参照 `harness/memory_metrics.py` 检测器的"不得调用 LLM"断言。
   - 流速校核用 `calc_velocity`。缺少参数时返回 `missing_inputs`，**不补默认值**。
2. **引用可追溯**：
   - 图面上的规范说明文字（例如"给水管流速不宜大于 2.0 m/s"），必须能指回语料库里真实存在的 `source_id`。
   - 语料中 GB50015-2019 只有 9 条，而且 `4.7.1` 自述"非正式规范条文"。凡是没有正式条文支撑的布置规则，一律在 `building_type_rules.json` 里标 `"basis": "engineering_practice"`，**不得在图面写"按 GB50015 第 x.x.x 条"**。
   - 生成器的说明文字只能从规则表的 `basis` 和 `source_id` 渲染，不得手写。
3. **评测先行**：
   - 先写评测集 `tests/eval/drawing_gen_p0/`，再写生成器。
   - 最低要求 **≥12 条**：三种建筑类型各 3 条合法参数，加 3 条非法参数。
   - 每条的断言项：图层集合、图元计数下限、全部管径标注、每层一根立管、`dxf_reader` 能读回、S50 复审能跑通。
   - 接入 `scripts/ci_gate.sh`，并做入仓基线对比，参照 `drawing_p0` 与 `compare_drawing_p0.py` 的做法。

## 3. 必须复用的现有组件（不得另写一份）

| 需要 | 用这个 | 位置 |
|---|---|---|
| 流速校核 | `calc_velocity(inputs)` → `CalcResult` | `tools/hydraulics.py` |
| 读回校验 | `read_drawing(drawing_id, root=..., policy=ToolPolicy(allowed_read_dirs=...))` | `drawing/dxf_reader.py` |
| 复审 + 摘要 | `review_uploaded_dxf(data, display_name=...)`（在只含一张图的临时目录里跑，报告格式唯一出口） | `documents/drawing_upload.py` |
| 图层 → 系统归属 | `_layer_system()` 认 `ws-` / `w-` / `给水` 前缀 | `drawing/dxf_reader.py:115` |
| 可写输出目录 | `resolve_export_dir()` | `documents/storage.py` |
| 下载响应头 | `content_disposition(filename)`（RFC 5987） | `webapp/export_routes.py` |
| 可选 DWG 输出 | `converter_path()` / 受控子进程约束（固定 argv、无 shell、超时、rlimit、环境变量白名单） | `documents/dwg_convert.py` |
| 合成图参考 | `build_synthetic_drawing()`（仅供参考写法，**不要扩展它**，它是测试夹具） | `drawing/synth.py` |

**兼容性要求**：生成的图纸必须能被本仓库自己的审查器正确读懂。
- 主管、支管图层以 `WS-` 开头，让 `_layer_system` 判为给水。
- 管径标注写成显式的 `DN50` / `De63`，并放在管线 20 图纸单位以内（`ANNOTATION_MATCH_RADIUS`）。
- `$INSUNITS=4`（毫米）。
- 要有测试断言：生成图读回后，**每条管段的 `dn_mm` 都能被 `map_pipes` 匹配到**。这正是 S50/S51 实测里真实图纸做不到的一点，生成图必须做到。

## 4. 实施步骤（一步一个 commit）

### Step 1：规格与决策记录
- 新建 `docs/design/p6-step-1-decision.md`，写明 §0 的修订、建筑类型规则的来源分级，以及不做项。
- 新建 `src/water_agent/drawing/building_type_rules.json`，按住宅、办公、商业分别给出：立管默认位置、横管敷设方式（沿墙 / 沿走廊吊顶 / 沿柱网）、默认柱网、阀门与水表的设置规则，以及对应节点大样类型。**每条规则都带 `basis` 字段**（取值 `formal_clause` / `engineering_practice`），`formal_clause` 必须同时带 `source_id`，并用测试校验这个 `source_id` 在语料里存在。
- 验收：JSON schema 用 pydantic `extra="forbid"` 校验；有一条测试断言"规则表中所有 `formal_clause` 的 `source_id` 都在 `data/curated/clauses/`"。

### Step 2：输入契约 + 校验
- 新建 `drawing/generation_models.py`：`GenerationRequest`（pydantic，`extra="forbid"`，frozen）。
- 字段按 spec D1：`system_type` 固定为 `"给水"`；`building_type` ∈ {住宅, 办公, 商业}；`floors` 取 1–33；`units_per_floor` 取 1–8；`riser_position`；`main_pipe` / `branch_pipe`（`dn` 与 `material` 用枚举，DN15–DN150 白名单，材质 PPR / PE / 钢塑复合 / 不锈钢）；`flow_rate_lps` > 0；`pressure_mpa` 取 0.1–1.0；`floor_height_m` 默认 3.0，取 2.4–6.0。
- 流速校核：对主管、支管分别调用 `calc_velocity`，结果写进 `GenerationPlan.calculations`。**流速超出建议区间不阻断生成**，只写 `warnings`，并在图面注"流速 x.xx m/s，超出建议区间，需复核"（区间与依据取自规则表）。
- 错误码，新增稳定码并用测试锁定：`GEN_INVALID_INPUT`（422）、`GEN_UNSUPPORTED_SYSTEM`（422）、`GEN_RENDER_FAILED`（500，**不回显**内部异常文本）。

### Step 3：几何规划（纯函数，不碰 ezdxf）
- 新建 `drawing/layout_engine.py`：`plan_floor(request, rules) -> FloorPlan`。输出只含坐标、管段、阀门位、标注位这类**数据**，不输出图元。
- 新建 `drawing/system_schematic.py`：`plan_schematic(request, rules) -> SchematicPlan`，画 45° 斜测，给出立管、各层支管、阀门、水表、管径变化点和标高。
- 新建 `drawing/detail_drawings.py`：`plan_details(request, rules) -> list[DetailPlan]`，按建筑类型选节点（住宅：水表井；办公：吊顶内阀门组；商业：泵房接管），比例 1:20。
- 为什么要先出数据、再渲染：几何规划可以脱离 ezdxf 做单元测试和属性测试（例如"管段两端都落在已知节点上""标注都在对应管线 20 mm 以内""层数 = 系统图支管组数"），渲染层只做翻译。
- **确定性**：同一个 `GenerationRequest` 两次生成，几何数据逐字节相同，要有测试断言。不许用随机数；排序键必须稳定。

### Step 4：渲染 DXF
- 新建 `drawing/blocks.py`：闸阀、截止阀、止回阀、水表、立管编号圈，定义成 BLOCK。块名用 `WS_` 前缀，避免与用户 CAD 模板里已有的块冲突。
- 新建 `drawing/generator.py`：`render(plan) -> bytes`。
  - 格式 R2010（AC1024），`$INSUNITS=4`。
  - 图层：`WS-PIPE-MAIN`、`WS-PIPE-BRANCH`、`WS-VALVE`、`WS-METER`、`WS-TEXT`、`WS-DIM`、`WS-AXIS`（轴网用 CENTER 线型）。
  - 文字样式 `WS-FS`，字高 3.5 mm × 出图比例。
  - 每张图左下角写一段固定说明："本图由程序按参数生成，是初稿，需设计人复核；管道敷设未考虑结构梁高与其他专业管线。"不画图框。
- 生成的 DXF **必须能被 `ezdxf.readfile` 与本仓库 `read_drawing` 直接打开**，不走 recover 路径。
- 可选的 DWG 输出：如果 `converter_path()` 可用，就用 `dxf2dwg` 转换，再用 `dwg2dxf` 回读，并用 `read_drawing` 核对管段数与 DXF 版一致；不一致就丢弃 DWG，并在结果里注明原因。

### Step 5：生成服务 + 复审
- 新建 `drawing/generation_service.py`：`generate_drawing_set(request, *, export_root) -> GenerationResult`。
- 流程：校验 → 规划 → 渲染三张 → 逐张 `review_uploaded_dxf` 复审 → 写入 `export_root/drawings/{generation_id}/` → 打包 ZIP（含 `README.txt`：参数、计算结果、复审报告、"初稿需复核"声明）。
- **复审结果如实呈现**：现有规则集以排水为主，给水图大多会落进"未检查"。结果里要原样带出"未检查 N 项"，**不得写"审查通过 / 0 缺陷"**。

### Step 6：Web 路由 + 前端
- 新建 `webapp/drawing_routes.py`：
  - `POST /api/drawings/generate`：需要 CSRF + `require_password_changed`，VIEWER 返回 403。请求体就是 `GenerationRequest`，返回 generation_id、计算结果、warnings、复审摘要、每张图的管段数。
  - `GET /api/drawings/{generation_id}/download`：返回 ZIP，文件名用 `content_disposition`。**归属校验**：生成记录要绑定生成者；别人的 id 和不存在的 id 一律返回 404。不需要新表的话，可以写一个 `meta.json` 记录 `owner_user_id`，读取时核对。
  - 生成是同步的：S51 实测的量级是单张图 < 1 s；如果实测超过 5 s，改成后台任务，并在决策记录里写明。
- 前端 `web/app/src/`：
  - 新增"生成图纸"面板，入口放在侧栏或会话工具栏，二选一，理由写进决策记录。表单字段与 `GenerationRequest` 一一对应，枚举用下拉框。
  - 提交后显示计算结果、warnings、复审摘要（原样显示"未检查 N 项"）和下载按钮。
  - 文案必须出现"程序生成的初稿，需设计人复核"。
  - 测试沿用 `node --experimental-strip-types`，被测模块之间的相对导入要带 `.ts` 后缀。

### Step 7：评测与门禁
- `harness/drawing_gen_eval.py` + `tests/eval/drawing_gen_p0/`（≥12 条）；`scripts/compare_drawing_gen_p0.py`，基线存 `data/eval/drawing_gen_p0_main.json`。
- 接入 `scripts/ci_gate.sh`。
- `smoke.py` 新增一项 `drawing_generate_and_download`：在镜像内生成一套图、下载、解压，三张 DXF 都要能用 `ezdxf.readfile` 打开。**改了 `smoke.py` 之后，app 和 smoke 两个镜像都要重建**（AGENTS.md 记录过：只重建一个时，新检查项会静默缺席）。

## 5. 验证命令（完成前全部跑过，贴出输出）

```bash
bash scripts/ci_gate.sh
_venv/bin/pytest -q tests/test_drawing_generator.py tests/test_drawing_generation_service.py tests/test_drawing_routes.py
_venv/bin/python -m water_agent.harness.drawing_gen_eval tests/eval/drawing_gen_p0
cd web/app && npm test && npm run typecheck && npm run build
docker build -t water-agent-app:local . && docker compose run --rm smoke
```

还需要手动做一次验收：三种建筑类型各生成一套，用 LibreDWG `dwgread` 或任意 CAD 查看器打开，截图记进决策记录。**没有 CAD 软件的实测结果，不得声称"可在 AutoCAD/天正中编辑"**，只能写"ezdxf / LibreDWG 可正常读取"。

## 6. 完成定义

- 所有 Step 的 commit 都已推送；`ci_gate.sh` 全绿；前端测试、typecheck、build 通过；容器 smoke 通过。
- `docs/design/p6-step-1-decision.md` 写清楚：
  - 实测数字：生成耗时、每张图的图元数与管段数；
  - 复审结果的原样摘录；
  - DWG 输出是否启用，以及回读核对的结果；
  - **残余风险**：布置规则只有"沿墙 / 吊顶 / 柱网"三种；未考虑结构与其他专业；给水审查规则覆盖不足；没有真实 CAD 打开验证（如果确实没做）。
- AGENTS.md 的"架构现状"补一条 P6-S1，和其他条目同一风格：只写可验证的事实与边界。

## 7. 常见坑（本仓库实测过）

- worktree 里 `_venv` 的 editable install 指向主 checkout，跑测试要加 `PYTHONPATH=src`。
- zsh 里 `"$t:local"` 会被 `:l` 修饰符吃掉，打镜像 tag 要写成 `"${t}:local"`。
- `ruff` 的 ISC004：列表里的隐式字符串拼接必须加括号。
- 工具 schema 的 token 断言是 `< 4500`，**不得上调**。本步不新增模型工具。
- 零宽字符在测试和规则里一律写成 `\uXXXX` 转义。
- `tests/conftest.py` 默认关闭了视觉审阅（`UPLOAD_VISION_ENABLED=false`），生成图的复审走 DXF 路径，不受影响。
