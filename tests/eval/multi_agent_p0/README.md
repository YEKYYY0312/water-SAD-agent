# multi_agent_p0 — 合成机制层评测

- 入口：`water_agent.harness.multi_agent_eval tests/eval/multi_agent_p0 --out data/eval/reports/multi_agent_p0_head.json`
- 基准：`data/eval/multi_agent_p0_main.json`（机制层全绿快照）
- 对比：`python scripts/compare_multi_agent_p0.py --head data/eval/reports/multi_agent_p0_head.json --base data/eval/multi_agent_p0_main.json`
- 套件声明：`suite.json: flow: "multi-agent"` 显式启用，多 Agent 链路仅在该 flow 下执行
- 12 用例覆盖任务书 §15.2 逐字条目；全部 offline/deterministic，无网络/无真实CAD，同 suite 100 次同指标
- 特性开关：CLI `--flow multi-agent` 与 `WATER_AGENT_FLOW=multi-agent` 等价，未启用时旧路径不变
