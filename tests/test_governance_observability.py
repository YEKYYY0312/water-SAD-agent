"""P2.5 Step 15 测试：治理指标聚合与本地结构化告警。

覆盖规格 §7 的告警纪律与 `observability-and-instrumentation` 的要求：

- **不可计算的指标记 ``None``，不记 0.0**（"没接线"不得看起来像"值为 0"）；
- 告警阈值来自版本化配置，不硬编码；
- **不假装已发送 PagerDuty/Slack**（``delivery`` 恒为 local_event_only）；
- 每条告警都有 runbook（`observability` 要求：无 runbook 的告警等于噪音）；
- 只有 page/ticket 两档参与路由。

全部离线，写盘仅用 tmp_path。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from water_agent.runtime.governance.detectors import DETECTOR_SEVERITY, tag_traces
from water_agent.runtime.governance.observability import (
    ALERT_CONFIG_VERSION,
    DEFAULT_ALERT_THRESHOLDS,
    RUNBOOKS,
    AlertThresholds,
    aggregate_metrics,
    build_governance_report,
    evaluate_alerts,
    write_alert_events,
)


def _doc(events: list[dict[str, Any]], *, status: str = "succeeded") -> dict[str, Any]:
    return {"trace_id": "t", "status": status, "events": events}


class TestUnknownIsNotZero:
    """不可计算 ≠ 0：这是本项目贯穿始终的口径（同 UsageRecord.usage_known）。"""

    def test_no_cache_events_gives_none_hit_rate(self) -> None:
        """没有缓存事件 → hit_rate=None，而不是 0.0。

        0.0 会让人以为"缓存在跑但一次没命中"，而事实是"根本没接缓存"。
        """
        metrics = aggregate_metrics([_doc([])])
        assert metrics.cache_hit_rate is None

    def test_all_miss_gives_zero_not_none(self) -> None:
        """有事件但全未命中 → 0.0（这是真实的 0，与不可计算不同）。"""
        metrics = aggregate_metrics(
            [_doc([{"event_type": "cache", "data": {"cache_status": "MISS"}}])]
        )
        assert metrics.cache_hit_rate == 0.0

    def test_no_usage_gives_none_cost(self) -> None:
        """provider 未返回 usage → 成本为 None，**不估算**。"""
        metrics = aggregate_metrics([_doc([{"event_type": "llm_call", "data": {}}])])
        assert metrics.total_cost_usd is None
        assert metrics.cost_p50_usd is None

    def test_bypass_excluded_from_hit_rate_denominator(self) -> None:
        """BYPASS 既非命中也非未命中，不得稀释命中率（规格 §5：不计 miss）。"""
        metrics = aggregate_metrics(
            [
                _doc(
                    [
                        {"event_type": "cache", "data": {"cache_status": "HIT"}},
                        {"event_type": "cache", "data": {"cache_status": "BYPASS"}},
                    ]
                )
            ]
        )
        assert metrics.cache_bypass == 1
        assert metrics.cache_hit_rate == 1.0  # 1 命中 / (1 命中 + 0 未命中)


class TestMetricAggregation:
    def test_counts_guardrail_actions(self) -> None:
        metrics = aggregate_metrics(
            [
                _doc(
                    [
                        {
                            "event_type": "guardrail",
                            "data": {"stage": "input", "action": "block", "rule_id": "inj.a"},
                        },
                        {
                            "event_type": "guardrail",
                            "data": {"stage": "input", "action": "redact", "rule_id": "pii.b"},
                        },
                    ]
                )
            ]
        )
        assert metrics.guardrail_blocks == 1
        assert metrics.guardrail_redactions == 1
        assert metrics.guardrail_rule_counts == {"inj.a": 1, "pii.b": 1}

    def test_counts_pii_and_secret_redactions_separately(self) -> None:
        """PII 与凭据是不同量级的事故，指标必须分开。"""
        metrics = aggregate_metrics(
            [
                _doc(
                    [
                        {
                            "event_type": "guardrail",
                            "data": {
                                "stage": "input",
                                "action": "redact",
                                "hits": [
                                    {"rule_id": "pii.phone", "category": "pii", "count": 2},
                                    {"rule_id": "sec.key", "category": "secret", "count": 1},
                                ],
                            },
                        }
                    ]
                )
            ]
        )
        assert metrics.pii_redactions == 2
        assert metrics.secret_redactions == 1

    def test_counts_tool_execution_and_blocks(self) -> None:
        metrics = aggregate_metrics(
            [
                _doc(
                    [
                        {
                            "event_type": "tool",
                            "data": {"tool_name": "a", "tool_status": "succeeded", "executed": True},
                        },
                        {
                            "event_type": "tool",
                            "data": {"tool_name": "b", "tool_status": "blocked", "executed": False},
                        },
                    ]
                )
            ]
        )
        assert metrics.tool_events == 2
        assert metrics.tool_executed == 1
        assert metrics.tool_blocked == 1

    def test_percentiles_use_percentiles_not_average(self) -> None:
        """`observability`：平均值会藏住 1% 用户的糟糕体验，必须用分位数。"""
        docs = [
            _doc(
                [
                    {"event_type": "llm_call", "data": {"cost_usd": c}, "duration_ms": c * 1000}
                    for c in (0.001, 0.002, 0.100)
                ]
            )
        ]
        metrics = aggregate_metrics(docs)
        assert metrics.cost_p50_usd == 0.002
        assert metrics.cost_p95_usd is not None
        assert metrics.cost_p95_usd > metrics.cost_p50_usd

    def test_unreadable_docs_excluded_from_trace_count(self) -> None:
        metrics = aggregate_metrics([_doc([]), {"garbage": 1}])
        assert metrics.traces == 1


class TestAlertThresholds:
    def test_config_is_versioned(self) -> None:
        """改阈值必须改版本号，否则无法回答"这批告警是哪版阈值产生的"。"""
        assert DEFAULT_ALERT_THRESHOLDS.version == ALERT_CONFIG_VERSION
        assert ALERT_CONFIG_VERSION.startswith("p2.5-")

    def test_thresholds_reject_extra_fields(self) -> None:
        """拼错的阈值字段必须响亮失败，而不是被静默丢弃后按默认放行。"""
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            AlertThresholds(ticket_ratee=0.5)  # type: ignore[call-arg]

    def test_page_threshold_is_stricter_than_ticket(self) -> None:
        """安全相关信号阈值必须更低：单条即需人看。"""
        t = DEFAULT_ALERT_THRESHOLDS
        assert t.page_rate <= t.ticket_rate
        assert t.page_min_count <= t.ticket_min_count


class TestAlertEvaluation:
    def _dirty_batch(self, dirty: int, clean: int) -> Any:
        """构造 dirty 条含 guardrail_bypass 的 trace + clean 条干净 trace。"""
        bad = _doc(
            [{"event_type": "tool", "data": {"tool_name": "x", "executed": True}}]
        )
        good = _doc(
            [
                {
                    "event_type": "guardrail",
                    "data": {"stage": "tool", "action": "allow", "invocation_id": "i"},
                },
                {
                    "event_type": "tool",
                    "data": {"tool_name": "x", "executed": True, "invocation_id": "i"},
                },
            ]
        )
        _per, dist = tag_traces([bad] * dirty + [good] * clean)
        return dist

    def test_single_page_signal_triggers(self) -> None:
        """PAGE 级 min_count=1：单条安全信号即触发。"""
        alerts = evaluate_alerts(self._dirty_batch(1, 0))
        triggered = [a for a in alerts if a.detector == "guardrail_bypass"]
        assert triggered and triggered[0].triggered

    def test_below_threshold_does_not_trigger(self) -> None:
        """占比低于阈值不触发（避免噪音训练人忽略告警）。"""
        # 1 脏 / 500 总 = 0.2% < page_rate 1%
        alerts = evaluate_alerts(self._dirty_batch(1, 499))
        triggered = [a for a in alerts if a.triggered]
        assert not triggered

    def test_clean_batch_triggers_nothing(self) -> None:
        assert evaluate_alerts(self._dirty_batch(0, 10)) == []

    def test_include_untriggered_shows_full_picture(self) -> None:
        alerts = evaluate_alerts(self._dirty_batch(0, 5), include_untriggered=True)
        assert len(alerts) >= len(DETECTOR_SEVERITY)
        assert all(not a.triggered for a in alerts)

    def test_page_alerts_sort_first(self) -> None:
        """读的人先看到要立刻处理的。"""
        alerts = evaluate_alerts(self._dirty_batch(2, 0), include_untriggered=True)
        severities = [a.severity for a in alerts]
        assert severities == sorted(severities, key=lambda s: s != "page")

    def test_custom_thresholds_are_honored(self) -> None:
        """阈值来自配置，不硬编码在判据里。"""
        strict = AlertThresholds(ticket_rate=1.0, ticket_min_count=999)
        dist = self._dirty_batch(2, 0)
        alerts = evaluate_alerts(dist, thresholds=strict)
        assert all(a.severity == "page" for a in alerts if a.triggered)


class TestNoFakeDelivery:
    """不假装已投递：谎称"已告警"比不告警更危险。"""

    def test_delivery_is_local_event_only(self) -> None:
        alerts = evaluate_alerts(
            tag_traces([_doc([{"event_type": "tool", "data": {"tool_name": "x", "executed": True}}])])[1]
        )
        assert alerts
        for alert in alerts:
            assert alert.delivery == "local_event_only"

    def test_no_external_alert_channel_wired(self) -> None:
        """源码不得真的接入外部告警通道（没有配置就不许声称已投递）。

        用 AST 查**真实的 import 与 URL 字面量**，而不是裸词匹配 ——
        模块 docstring 里写"不假装已发送 PagerDuty/Slack"是正确的说明，
        裸词断言会把这句诚实的免责声明也判为违规（本测试第一版就踩了这个坑）。
        """
        import ast

        import water_agent.runtime.governance.observability as module

        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))

        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        forbidden_modules = {
            "requests",
            "httpx",
            "urllib",
            "http",
            "smtplib",
            "slack_sdk",
            "pdpyras",
            "socket",
        }
        assert not (imported & forbidden_modules), (
            f"不得接入外部投递通道：{imported & forbidden_modules}"
        )

        # 也不得藏着任何出站 URL
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert "http://" not in node.value and "https://" not in node.value, (
                    f"源码不得含出站 URL：{node.value[:60]}"
                )

    def test_every_detector_has_runbook(self) -> None:
        """`observability`：无 runbook 的告警等于噪音。"""
        for name in DETECTOR_SEVERITY:
            assert name in RUNBOOKS, f"检测器 {name} 缺 runbook"
            assert len(RUNBOOKS[name]) > 20

    def test_runbook_reaches_alert_payload(self) -> None:
        alerts = evaluate_alerts(
            tag_traces([_doc([{"event_type": "tool", "data": {"tool_name": "x", "executed": True}}])])[1]
        )
        assert alerts[0].to_payload()["runbook"]


class TestReportAndPersistence:
    def test_report_is_json_serializable(self) -> None:
        docs = [_doc([{"event_type": "tool", "data": {"tool_name": "x", "executed": True}}])]
        per_trace, dist = tag_traces(docs)
        report = build_governance_report(docs, distribution=dist, per_trace_signals=per_trace)
        json.dumps(report, ensure_ascii=False)  # 不抛即通过
        assert report["detector_version"]
        assert report["alert_config_version"]

    def test_report_carries_no_trace_payload(self) -> None:
        """报告只含聚合数与已脱敏证据，不含 trace 原文。"""
        docs = [
            _doc(
                [
                    {
                        "event_type": "guardrail",
                        "data": {"stage": "input", "action": "block", "rule_id": "inj.a"},
                    },
                    {"event_type": "run_end", "status": "succeeded"},
                ]
            )
        ]
        per_trace, dist = tag_traces(docs)
        blob = json.dumps(
            build_governance_report(docs, distribution=dist, per_trace_signals=per_trace),
            ensure_ascii=False,
        )
        for token in ("忽略以上", "PWNED", "13800138000", "sk-"):
            assert token not in blob

    def test_write_alert_events_only_writes_triggered(self, tmp_path: Path) -> None:
        docs = [_doc([{"event_type": "tool", "data": {"tool_name": "x", "executed": True}}])]
        _per, dist = tag_traces(docs)
        alerts = evaluate_alerts(dist, include_untriggered=True)
        target = tmp_path / "alerts.jsonl"
        written = write_alert_events(alerts, target)
        assert written == len([a for a in alerts if a.triggered])
        lines = target.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == written
        for line in lines:
            assert json.loads(line)["triggered"] is True

    def test_write_alert_events_no_file_when_nothing_triggered(self, tmp_path: Path) -> None:
        clean = _doc(
            [
                {
                    "event_type": "guardrail",
                    "data": {"stage": "tool", "action": "allow", "invocation_id": "i"},
                },
                {"event_type": "tool", "data": {"tool_name": "x", "executed": True, "invocation_id": "i"}},
            ]
        )
        _per, dist = tag_traces([clean])
        target = tmp_path / "alerts.jsonl"
        assert write_alert_events(evaluate_alerts(dist), target) == 0
        assert not target.exists()
