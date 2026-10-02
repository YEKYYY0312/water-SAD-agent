# multi_agent_p0 — 合成机制层评测

- 入口：`water_agent.harness.multi_agent_eval tests/eval/multi_agent_p0 --out data/eval/reports/multi_agent_p0_head.json`
- 基准：`data/eval/multi_agent_p0_main.json`（机制层全绿快照）
- 对比：`python scripts/compare_multi_agent_p0.py --head data/eval/reports/multi_agent_p0_head.json --base data/eval/multi_agent_p0_main.json`
- 套件声明：`suite.json: flow: "multi-agent"` 显式启用，多 Agent 链路仅在该 flow 下执行
- 14 用例：前 12 条覆盖任务书 §15.2 逐字条目；ma-p0-13/14（2026-09-30）覆盖"带条件进入人审"口径——P3 缺陷成为人工问题停在 waiting、P3 覆盖缺口与水力越界提示写进审批请求；全部 offline/deterministic，无网络/无真实CAD，同 suite 100 次同指标
- 特性开关：CLI `--flow multi-agent` 与 `WATER_AGENT_FLOW=multi-agent` 等价，未启用时旧路径不变
