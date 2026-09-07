# drawing_p0 —— 图纸审查链路 smoke 评测集（**合成样本，非真实图纸**）

## 这套评测集是什么、不是什么

| | |
|---|---|
| **是** | `load → extract → map → check → report` 审查子图（`graphs/drawing_review.py`）的**链路完整性**验证集；判分口径（`(rule_id, handle)` 逐条精确匹配）与聚合指标（`defect_recall` / `false_positive_rate`）的回归基线 |
| **不是** | **真实缺陷召回率的证据**。全部图纸由 `build_cases.py` 用 ezdxf 程序化合成（R2010，`$INSUNITS=6` 米），不是设计院样本 |

在拿到真实设计院图纸并重新标定之前（见文末 blocked 项），**不得**引用本套件
声明"缺陷召回率 ≥80%"或"P3 验收通过"。这是 P3 规格 §9 第 2 条与
Step 21 决策记录残余风险 1–3 的既定口径。

## 结构

```
tests/eval/drawing_p0/
├── suite.json        # 套件元数据（real_samples=false，accepts_recall_gate=false）
├── README.md         # 本文件
├── build_cases.py    # 图纸 + gold 的生成器（cases/ 的唯一编辑入口）
└── cases/            # 8 张图，每张一个 .dxf + 一个 gold .json
```

**gold 文件由生成器写出，不手改**（handle 是生成时从重读 DXF 取的权威值）。
改动 case 定义 → 改 `build_cases.py` 里的 `CASE_SPECS` → 重跑生成器。

## gold 格式

```json
{
  "case_id": "dw-001-min-dn-violation",
  "drawing": "dw-001-min-dn-violation.dxf",
  "expected_status": "succeeded",
  "expected_summary_contains": ["发现 1 条待核问题"],
  "expected_not_checked_contains": ["drain.horizontal_slope_range", "drain.pressure_flow_velocity"],
  "expected_missing_inputs_contains": [],
  "defects": [
    {
      "rule_id": "drain.min_diameter",
      "handle": "33",
      "segment_id": "DR-PIPE#33",
      "layer": "DR-PIPE",
      "evidence": "标注 DN200 钢筋混凝土（DN200 < 下限 DN300）"
    }
  ]
}
```

- `defects[].handle` 是**缺陷定位主键**（P3 规格验收"能定位缺陷对象"）；
- `defects` 为空 = 该图应为无缺陷报告（合规图 / 规则作用域外 / 结构性用例）；
- `expected_not_checked_contains` 把 **not_checked 当一等公民判分**：
  含排水管段的图纸必须如实报坡度/流速两条恒未检查（Step 21 D8），
  缺管径标注的图纸必须报"未检查"而非默认通过。

## 用例覆盖（8 条：3 缺陷 + 3 合规 + 2 结构性）

| case | 覆盖点 | gold 缺陷 |
|---|---|---|
| dw-001 | DN200 < 300 → 最小管径规则缺陷分支 | 1 |
| dw-002 | 两条违规 → **逐条覆盖**（分母为 2，不是 any-hit） | 2 |
| dw-003 | DN400 合规 + 坡度/流速恒未检查 | 0 |
| dw-004 | 无管径标注 → 未检查（不是假装通过） | 0 |
| dw-005 | 给水管 DN200 不适用排水规则（作用域） | 0 |
| dw-006 | 投毒标注 DN2000 被隔离（suspicious 不参与判定） | 0 |
| dw-007 | `$INSUNITS` 缺失 → 长度类检查未执行 + `drawing_unit` 缺输入 | 0 |
| dw-008 | 坏文件 → 解析失败，报告不得出现合规措辞 | 0 |

正例/负例刻意均衡：全缺陷会让"全部标 bad"得到 100% 召回但 0% 精度；
全合规会让召回率分母为 0。

## 运行与门禁

```bash
_venv/bin/python -m water_agent.harness.drawing_eval tests/eval/drawing_p0
```

结果写 `data/eval/reports/drawing_p0_*.json`（结构化、可 diff）。
`scripts/ci_gate.sh` 会对比 HEAD 与 `main` 的该报告：**用例通过数下降即红灯**
（`scripts/compare_drawing_p0.py`）。当前阶段门禁**只守护这套 smoke 集的回归**，
不是真实召回率门槛 —— 真实门槛的开启条件见下。

## blocked：真实样本与验收门槛

1. **采集真实设计院 DXF 样本**（≥10 张、含真实缺陷与合规图，注意脱敏业主 PII）；
2. **重新标定 `ANNOTATION_MATCH_RADIUS`（当前 12.0 是工程惯例估值）**；
3. **补 INSERT/ARC/带宽度 LWPOLYLINE 的实体覆盖**（当前只认 LINE/POLYLINE）;
4. 以上完成后，`drawing_p1`（真实样本集）才可设召回率门槛并纳入验收。
