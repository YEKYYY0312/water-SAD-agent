"""P2.5 治理 trace 后处理入口（review 修复：把检测器/聚合/告警接入可执行入口）。

读取一个目录下的所有 trace JSON（默认 ``data/traces``），运行：
1. ``tag_traces``：对每条 trace 跑 8 个失败模式检测器；
2. ``build_governance_report``：聚合治理指标 + 本地结构化告警；
3. ``write_alert_events``：把**触发**的告警追加写进 JSONL。

**告警投递边界**：``Alert.delivery="local_event_only"``——只产出本地结构化事件，
**不假装已发送 PagerDuty / Slack**（规格 §7）。外部投递通道待单独决策。

用法：
    _venv/bin/python scripts/governance_report.py [--trace-dir data/traces] [--alert-file data/eval/reports/alerts.jsonl]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from water_agent.runtime.governance.detectors import tag_traces
from water_agent.runtime.governance.observability import (
    build_governance_report,
    evaluate_alerts,
    write_alert_events,
)


def _load_traces(trace_dir: Path) -> list[dict]:
    docs: list[dict] = []
    if not trace_dir.exists():
        return docs
    for path in sorted(trace_dir.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(doc, dict):
            docs.append(doc)
    return docs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P2.5 治理 trace 后处理")
    parser.add_argument("--trace-dir", default="data/traces")
    parser.add_argument("--alert-file", default="data/eval/reports/governance_alerts.jsonl")
    args = parser.parse_args(argv)

    docs = _load_traces(Path(args.trace_dir))
    per_trace, distribution = tag_traces(docs)
    report = build_governance_report(
        docs, distribution=distribution, per_trace_signals=per_trace
    )
    triggered = evaluate_alerts(distribution, include_untriggered=False)
    written = write_alert_events(triggered, args.alert_file)
    report["alert_events_written"] = written
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
