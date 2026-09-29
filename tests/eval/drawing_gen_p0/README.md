# drawing_gen_p0 —— 参数化给水出图机制层评测集（**合成参数，非真实工程**）

## 是什么、不是什么

| | |
|---|---|
| **是** | P6-S1 出图链路的回归基线：输入校验 → 流速程序化校核 → 三张 DXF 的图层 / 管段数 / 管径读回 → 每层一根立管 → S50 复审跑通且不写成"审查通过" |
| **不是** | 图纸质量或"可施工"的证据。布置规则只有三种敷设方式（沿墙 / 吊顶 / 柱网），未考虑结构梁高与其他专业管线；没有在 AutoCAD / 天正里打开验证 |

## 结构

```
tests/eval/drawing_gen_p0/
├── suite.json        # 套件元数据（real_cad_verified=false）
├── README.md         # 本文件
└── cases/            # 14 条：住宅 / 办公 / 商业各 3 条合法参数 + 5 条非法参数
```

用例是纯 JSON（请求参数 + 期望），不含图纸文件：图纸由被测的生成器现场产出，
评测只判它读回来的结果。

## 用例格式

```json
{
  "case_id": "gen-res-01-spec-example",
  "request": { "...": "GenerationRequest 原样" },
  "expect": {
    "ok": true,
    "main_velocity_mps": 1.2732,          // 独立按 v = Q / (π d² / 4) 算出，不取被测代码
    "main_advisory": "above",             // 规则表 ws.pipe_velocity 的判定
    "warnings_contains": ["超出建议区间"],
    "missing_inputs_contains": ["branch_flow_rate_lps"],
    "sheets": {"plan": {"pipe_runs": 5, "layers": ["WS-PIPE-MAIN", "..."], "min_entities": 40}, "...": {}},
    "all_pipe_dn_matched": true,          // WS-PIPE-* 上每条管段读回的 dn_mm == 规划管径
    "riser_segments": 6,                  // 系统图立管段数 = 层数（每层一根）
    "branch_groups": 6,                   // 系统图支管组数 = 层数
    "review": {"min_not_checked": 1, "summary_must_not_contain": ["均未发现问题", "审查通过", "0 缺陷"]}
  }
}
```

`expect.ok = false` 的用例只判 `error_code` 与 `error_fields`（字段路径），不判提示文案。

## 覆盖

| case | 覆盖点 |
|---|---|
| gen-res-01 | spec D1 原例；主管流速 1.27 m/s 高于常规区间 → 只提示不阻断；支管缺流量 → 不校核 |
| gen-res-02 | 上限：33 层 × 8 户，管道井立管，钢塑复合（审查器不识别该材质，已知缺口） |
| gen-res-03 | 下限：1 层 1 户；立管位置缺省取规则默认；勾选实验性 DWG |
| gen-off-01..03 | 核心筒立管、走廊吊顶内敷设、楼层水表；低流速提示；层高 4.2 m |
| gen-com-01..03 | 边角立管、沿柱网敷设、商铺独立计量、泵房接管大样；流速大于上限约 2.0 m/s 仍生成 |
| gen-inv-01..05 | 排水系统、越界数值、白名单外取值与未知字段、住宅选核心筒、支管大于主管 |

## 运行与门禁（harness 与门禁在 Step 7 接入；Step 2 先由 `tests/test_drawing_generation_models.py` 跑非法参数那一半）

```bash
_venv/bin/python -m water_agent.harness.drawing_gen_eval tests/eval/drawing_gen_p0
```

`scripts/ci_gate.sh` 用 `scripts/compare_drawing_gen_p0.py` 对比 HEAD 与入仓基线
`data/eval/drawing_gen_p0_main.json`：基线通过的用例在 HEAD 失败即红灯。
