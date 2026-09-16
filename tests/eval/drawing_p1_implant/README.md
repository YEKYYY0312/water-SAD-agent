# drawing_p1_implant —— 真实底图 + 植入缺陷（**只测植入段，不判底图**）

## 这是什么

| | |
|---|---|
| **是** | 在 `data/drawings/real_samples/` 真实 DXF 副本上程序化植入排水管段，验证 `drain.min_diameter` 在真实噪声下的逐条覆盖（召回）与合规植入零误报 |
| **不是** | 真实缺陷召回率证据。缺陷是合成植入的（`synthetic_implants=true`），且判分只认植入段 |

底图（P-01 平面图 / S-03 系统图）经 CAD 目视确认干净（`data/eval/drawing_p1/REVIEW.md`），
但 runner **不对底图做合规断言**：底图原生管段若被规则报出，不计入本套件判分
（实现上靠“gold 只含植入段 + 植入坐标远离原图包围盒”保证隔离；若底图漂移导致串扰，case 会失败而非静默通过）。

## 结构

```
tests/eval/drawing_p1_implant/
├── suite.json        # real_samples=true, synthetic_implants=true, accepts_recall_gate=false
├── README.md         # 本文件
├── build_implants.py # 唯一的生成入口（cases/*.dxf + cases/*.json 同次生成）
└── cases/            # *.json 入库；*.dxf 不入库（.gitignore，本地生成）
```

## 用例（5 条：3 缺陷 + 2 合规植入）

| case | 底图 | 植入 | gold |
|---|---|---|---|
| pi-001 | P-01 | DN200 钢筋混凝土 | 1 |
| pi-002 | P-01 | DN150 钢筋混凝土 + DN200 PVC-U | 2 |
| pi-003 | P-01 | DN400 钢筋混凝土（合规） | 0 |
| pi-004 | S-03 | DN200 钢筋混凝土 | 1 |
| pi-005 | S-03 | DN400 钢筋混凝土（合规） | 0 |

## 生成与运行

```bash
_venv/bin/python tests/eval/drawing_p1_implant/build_implants.py
_venv/bin/python -m water_agent.harness.drawing_eval tests/eval/drawing_p1_implant --report-path data/eval/reports/drawing_p1_implant_head.json
```

植入坐标：原图包围盒 max + 100 起，第二段再 +100（Y 向）；标注贴管旁 3 单位
（`ANNOTATION_MATCH_RADIUS=12` 之内）。gold handle 为保存后重读权威值。

## 门禁

本套件**不进 `ci_gate.sh`**：DXF cases 不入库，CI 无底图可跑。
门禁语义待定（`accepts_recall_gate=false`）；当前只做本地回归观测。
真实召回率验收仍 blocked，需带原生缺陷的真实图纸。
