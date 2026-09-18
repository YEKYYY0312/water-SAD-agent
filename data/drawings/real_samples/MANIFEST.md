# 真实图纸样本 MANIFEST

P2 真实图纸样本集。**18 份脱敏 DXF**，覆盖平面图 / 系统图 / 大样图 / 泵房图四类。

> **本文件只记录非识别性元信息**（类型、图层数、实体数、版本、单位）。
> 原始工程名称、文件路径、来源项目一律**不入库** —— 溯源映射写在仓库外
> （`/tmp/p2_samples/provenance.json`），不得复制进仓库或随样本分发。
>
> 样本本身（`*.dxf`）不入版本库，见 `.gitignore` 的 `data/drawings/real_samples/`。

## 1. 概览

| 项 | 值 |
| --- | --- |
| 样本数 | 18 |
| 合计大小 | 13.2 MB（单份最大 1.76 MB，均 ≤2 MB） |
| 可读性 | **18/18**（`read_drawing` 全部 `ok=true`） |
| 图元合计 | 36,605（截断 **0** 张） |
| 图层数 | 4 – 89（中位数 10） |
| DXF 版本 | 全部 **AC1032**（ACAD2018），由 ODA File Converter 统一转换 |
| 单位声明 | `$INSUNITS` 已声明 **4/18**（P-01 / P-03 / P-04 / P-05，值 = 4 毫米） |
| 含 INSERT | 16/18（合计 4,037 个） |
| 含 ARC | 15/18（合计 926 个） |
| PII 复扫 | **18/18 干净**（2026-09-15 P-01 替换后复扫，`undecodable_total = 0`） |
| MIF 转义 | 模型空间 3,816 条文本中原始 1,320 条（35%）为转义形态；**Step 8 后解码残留 0 条** |
| CP1252 重编码损坏 | **0/18**（原 P-01 的 58 处已随该样本替换而移出，见 §4.5） |

## 2. 逐样本

| 样本 | 类型 | 图层 | 实体 | 文本 | 原始 MIF 转义 | 解码残留 | 大小 | 单位 | 主要实体构成（前 5） |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| D-01 | 大样图 | 9 | 351 | 74 | 26 | 0 | 239 KB | 未声明 | line:217 text:72 other:28 polyline:16 arc:10 |
| D-02 | 大样图 | 10 | 3,608 | 228 | 111 | 0 | 1,013 KB | 未声明 | polyline:1837 line:1321 text:189 other:79 arc:54 |
| D-03 | 大样图 | 7 | 159 | 20 | 11 | 0 | 260 KB | 未声明 | line:77 other:33 text:20 polyline:14 insert:11 |
| P-01 | 平面图 | 36 | 4,560 | 540 | 134 | 0 | 1,331 KB | 毫米 | line:1837 circle:1040 polyline:675 text:544 insert:227 |
| P-02 | 平面图 | 6 | 791 | 75 | 64 | 0 | 234 KB | 未声明 | line:587 polyline:58 mtext:48 arc:28 text:28 |
| P-03 | 平面图 | 28 | 2,997 | 216 | 0 | 0 | 822 KB | 毫米 | line:2554 mtext:114 text:102 arc:73 circle:71 |
| P-04 | 平面图 | 32 | 386 | 158 | 110 | 0 | 568 KB | 毫米 | text:159 line:110 other:64 arc:24 polyline:18 |
| P-05 | 平面图 | 89 | 3,309 | 169 | 65 | 0 | 1,803 KB | 毫米 | line:2852 text:171 arc:143 other:62 polyline:47 |
| P-06 | 平面图 | 10 | 715 | 47 | 34 | 0 | 242 KB | 未声明 | line:565 text:45 polyline:43 arc:34 circle:12 |
| P-07 | 平面图 | 25 | 3,393 | 589 | 128 | 0 | 1,226 KB | 未声明 | line:2194 text:591 insert:274 circle:104 arc:83 |
| PUMP-01 | 泵房图 | 4 | 1,009 | 757 | 339 | 0 | 292 KB | 未声明 | text:757 line:112 circle:102 polyline:38 |
| PUMP-02 | 泵房图 | 24 | 2,653 | 105 | 58 | 0 | 979 KB | 未声明 | line:1487 polyline:589 circle:167 arc:158 text:105 |
| PUMP-03 | 泵房图 | 22 | 5,729 | 336 | 145 | 0 | 1,717 KB | 未声明 | insert:3285 line:1523 text:397 polyline:225 other:89 |
| S-01 | 系统图 | 6 | 610 | 82 | 19 | 0 | 240 KB | 未声明 | line:369 text:78 arc:69 polyline:41 circle:20 |
| S-02 | 系统图 | 8 | 599 | 100 | 53 | 0 | 234 KB | 未声明 | line:368 polyline:109 text:100 circle:14 other:7 |
| S-03 | 系统图 | 4 | 1,186 | 156 | 0 | 0 | 355 KB | 未声明 | line:935 mtext:154 other:39 circle:33 arc:12 |
| S-04 | 系统图 | 10 | 1,567 | 64 | 0 | 0 | 1,118 KB | 未声明 | line:750 other:703 text:76 polyline:18 arc:13 |
| S-05 | 系统图 | 12 | 2,983 | 100 | 23 | 0 | 816 KB | 未声明 | line:2579 circle:149 mtext:100 insert:91 polyline:40 |

实体类型合计：`line=20437, insert=4037, polyline=3901, text=3436, circle=1880, other=1457, arc=926, mtext=531`

命名规则：`P-` 平面图 / `S-` 系统图 / `D-` 大样图 / `PUMP-` 泵房图。

## 3. 验收状态

| 验收项 | 要求 | 实际 | 状态 |
| --- | --- | --- | --- |
| 样本数量 | 10–20 张 | 18 | 通过 |
| 全部可解析 | 100% | 18/18 | 通过 |
| 覆盖多种图纸类型 | ≥2 类 | 4 类（平面/系统/大样/泵房） | 通过 |
| 识别 INSERT 实体 | 有 | 16/18 样本含 INSERT，合计 4,037 个 | 通过 |
| 识别 ARC 实体 | 有 | 15/18 样本含 ARC，合计 926 个 | 通过 |
| 生成 MANIFEST | 有 | 本文件 | 通过 |
| 无敏感信息泄漏 | 0 处 | **18/18 通过**（`undecodable_total = 0`；2026-09-15 P-01 替换后复扫） | 通过 |
| 单份 ≤2 MB | 是 | 最大 1.76 MB（P-05） | 通过 |
| 合计 ≤30 MB | 是 | 13.2 MB | 通过 |
| **MIF 转义可读**（Step 8 追加） | 解码残留 0 | 0 条（原 1,320 条不可读） | 通过 |
| **编码损坏可见**（Step 8 追加） | 无静默盲区 | 当前 0 处；扫描器 `undecodable` 维度常驻并**阻断**（退出码 1） | 通过 |

## 4. 已知限制（**这些是结论的边界，不是待办**）

1. **单位多数未声明。** 仅 4/18 声明了 `$INSUNITS`（=4 毫米），其余 14 张的单位
   只能从图面文字推断。**长度类结论在这 14 张上需人工核对**，不能直接采信数值。

2. **INSERT 块展开存在边界。** `dxf_reader` 保留 INSERT 实体并使用
   `virtual_entities()` 展开可见的块内图元；展开结果带 `source_block` 来源链，嵌套块用
   `/` 连接。部分 CAD 代理实体或空块没有可展开内容，因此展开后的实体数不一定等于
   原始 INSERT 数与块定义实体数的简单乘积。PUMP-03 当前解析为 5,922 个实体，块来源实体
   193 个，展开耗时约 0.4 秒；失败会记录在 `errors`，不阻断整张图。

3. **PII 扫描的覆盖边界。** 复扫口径是**组码流里的文本值**（26,147 个），
   覆盖 ENTITIES / BLOCKS / CLASSES / HEADER 全部节的组码 1 与 3。
   不覆盖：二进制形态的 XDATA、缩略图（已整段删除）、以及**非文本的可识别信息**
   （例如图形本身绘制的院徽、图签里作为图元画的字）。后者需要人眼过一遍。

4. **厂商样板未删。** CLASSES 节有 46 处 CAD 软件自身写入的代理类注册记录
   （`expresstools@autodesk.com`、`www.tangent.com.cn` 等）。判定为**非用户 PII**
   且改写有破坏代理实体解析的风险，故保留；在复扫报告里单列供人工复核，
   **不做静默忽略**。

5. **【已处置】P-01 曾因不可逆的编码损坏而无法出具 PII 结论 —— 该样本已替换。**
   2026-09-15 复核时发现原 P-01 有 **58/111 条**模型空间文本是 **CP1252 重编码损坏**：
   源 DWG 的字节先被当成 CP1252 单字节解（`锅炉房` 的 GBK 字节 `B9 F8 C2 AF B7 BF`
   → `¹øÂ¯·¿`），回写 DXF 时又经一次 **GB2312 最佳拟合**（`¯` → GB2312 `A1A5` 的 `ˉ`），
   磁盘上最终是 `¹øÂˉ·¿`。这 58 处含**图签栏**字段组（`校 对`/`审 定`/`审 核`/`设 计`/
   `版 次`/`图 号`/`设计阶段`/`出图日期`）与含人名形态的短串，而**脱敏模块与 PII 扫描器
   两侧读到的都是乱码**，因此该样本「无敏感信息泄漏」的结论**无法出具**。

   **为什么不能还原**（实测）：最后那步最佳拟合**多对一**。GB2312 的 A8 行只有**全角
   小写**带音符号，CP1252 的大小写对因此**坍缩成同一个字符** —— `À`/`à`(C0/E0)、
   `Á`/`á`(C1/E1)、`É`/`é`(C9/E9)、`Í`/`í`(CD/ED)、`Ó`/`ó`(D3/F3)、`Ú`/`ú`(DA/FA)、
   `Ü`/`ü`(DC/FC) 两组字节输出**完全相同**。实测冲突：

   | 磁盘字符 | 在「沉淀池」中需 | 在「脱水间」中需 |
   | --- | --- | --- |
   | `í` | `ED`（淀 = `B5ED`） | `CD`（脱 = `CDD1`） |
   | `ó` | `D3`（加 = `BCD3`，加药泵） | `F3`（审 = `C9F3`，审定） |
   | `¼` | `BC`（加药） | `CD`（图 = `CDBC`） |

   两个候选**往往都是合法 GBK**，"解出来必须是汉字"筛不掉。用一张"最合理"的表硬解，
   会得到 `沉淀池` → `翅雾池`、`加药` → `俭颟`、`管` → `裹`、`设计` → `殍计` ——
   **貌似通顺、实则错误**。这比留着乱码更危险：乱码一眼看得出，假中文会被当成
   事实引用（违反铁律 2）。**所以本样本集不做还原**，只做检测
   （`water_agent.drawing.mif.looks_like_mojibake`）。

   **处置（2026-09-15）**：原 P-01 已从样本集**移出**（留证在仓库外
   `/tmp/p2_samples/quarantine_mojibake/P-01-mojibake-REPLACED.dxf`），
   换成同类干净平面图「某酒厂厂区给排水管道总平面图」（4,560 图元 / 36 图层 /
   540 文本 / 134 条 MIF 转义 / 1,331 KB，**声明了 `$INSUNITS`**）。
   筛选时对 8 份候选逐份跑了扫描器，**`undecodable` 全为 0**；
   替换后全库复扫 `undecodable_total = 0`、**18/18 干净**。

   **教训（保留）**：这不是"换掉一个坏样本"就完了 —— 换批样本仍可能出现同类损坏。
   因此**检测已固化在 `scripts/scan_drawing_pii.py`**：`undecodable` 单列进报告
   且**默认阻断**（退出码 1，「无法判定 ≠ 通过」，`--tolerate-undecodable` 才降为警告）。
   任何一批新样本引入同类问题时都会立刻暴露，不再静默通过。

6. **`drawing_p0` 召回率仍未验收。** 本样本集尚未接入 `drawing_eval`；
   真实缺陷标注（gold）未建立，因此**不能声称真实召回率**。
   本 MANIFEST 只证明"样本可读、覆盖多类型、无 PII 泄漏、MIF 可读"，
   **不构成审查质量的证据**。

## 5. 再生成

```bash
# 1) DWG -> DXF（需 ODA File Converter，输出目录须为空或使用独立目录）
bash scripts/convert_dwg_batch.sh --map <mapping.json> --out /tmp/dxf_converted

# 2) 脱敏
_venv/bin/python -m water_agent.drawing.desensitize \
    --in /tmp/dxf_converted --out /tmp/dxf_desens \
    --config data/drawings/desensitize_terms.json --report /tmp/dxf_desens_report.json

# 3) 归档（命名 P-/S-/D-/PUMP-；溯源映射必须写在仓库外）
_venv/bin/python scripts/archive_drawing_samples.py \
    --src /tmp/dxf_desens --map /tmp/p2_samples/classification.json \
    --out data/drawings/real_samples --provenance /tmp/p2_samples/provenance.json --force

# 4) 可读性验证（图元统计 + MIF 占比）
_venv/bin/python scripts/verify_drawing_samples.py --root data/drawings/real_samples \
    --report data/eval/reports/real_samples_readability.json

# 5) PII 验收（退出码 0 干净 / 1 有泄漏）
_venv/bin/python scripts/scan_drawing_pii.py data/drawings/real_samples \
    --report data/eval/reports/real_samples_pii_scan.json

# 6) 阴性对照 —— 改判据后必跑。在脱敏前目录上必须扫出命中，
#    否则「脱敏后 0 命中」不构成任何证据
_venv/bin/python scripts/scan_drawing_pii.py /tmp/dxf_converted --expect-hits
```

### 换样本（以 2026-09-15 的 P-01 替换为例）

单份样本的替换**不走全量重建**，而是原地换入脱敏件后重跑第 3–5 步
（`classification.json` 是权威映射，改它即可，归档脚本按 sid 排序确定性重编号）：

```bash
# a) 旧样本先移到仓库外留证（不是删除 —— 它是可复现的证据）
#    /tmp/p2_samples/quarantine_mojibake/P-01-mojibake-REPLACED.dxf
# b) 候选必须过同一套筛查，缺一不可：
#      - 同类（P-01 是平面图 → 替换件 tags 含「平面图」）
#      - 未用过的工程目录 + 不与现有样本 MD5 重复
#      - 源 DWG ≤300KB（保证 DXF ≤2MB）
#      - 转换后跑 scripts/scan_drawing_pii.py，undecodable 必须为 0
# c) 脱敏件换入 /tmp/dxf_desens/<sid>.dxf，更新 classification.json[<sid>]
#    （含 replaces 字段，记录被替换者与留证路径），再重跑第 3–5 步
```

**筛选依据**：本次对 8 份候选逐份转换 + 扫描，`undecodable` 全为 0；
最终选「某酒厂厂区给排水管道总平面图」，理由是它**文本最密**（540 条，
全库最高之一，审查链读的就是标注）且领域最贴合给排水专业。

---

生成时间：2026-09-15 · 对应任务 P2 Step 6（同日 Step 8 追加 MIF 解码与
编码损坏检测，并完成 P-01 替换；本次为**替换后**的复扫数据）
