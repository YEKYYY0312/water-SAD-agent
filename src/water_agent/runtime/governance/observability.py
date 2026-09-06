"""P2.5 Step 15：治理指标聚合与本地结构化告警事件。

设计约束（规格 §7 + `observability-and-instrumentation` 的告警纪律）：

1. **不假装已发送 PagerDuty / Slack**。本项目没有配置这些通道，也没有值班轮换。
   谎称"已告警"比不告警更危险 —— 运维会以为有人被叫醒了。因此这里只产出
   **本地结构化告警事件** + operator 文档，投递通道留给真正部署时接。
2. **阈值来自配置，不硬编码在判据里**（:class:`AlertThresholds`，版本化）。
3. **只有两档严重度参与路由**：page / ticket。第三档会变成噪音。
4. **告警按症状，不按原因**：这里的症状是"某类失败信号的 trace 占比超阈值"，
   而不是"某个组件的内部计数器异常"。
5. **未知不装作已知**：无法计算的指标记 ``None``（而不是 0.0）。
   ``cache_hit_rate=None``（没有缓存事件）与 ``=0.0``（有事件但全 miss）
   是不同的事实 —— 这与 ``UsageRecord.usage_known=False`` 同口径。

本模块不发起远程调用、不写文件（写盘由调用方决定路径）。
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from water_agent.runtime.governance.detectors import (
    DETECTOR_SEVERITY,
    DETECTOR_VERSION,
    Severity,
    Signal,
    TagDistribution,
    TraceView,
)

#: 告警配置版本。**改任何阈值都要改它**（告警事件会带上它）。
ALERT_CONFIG_VERSION = "p2.5-step15-2026-09-06"


class AlertThresholds(BaseModel):
    """告警阈值（版本化配置）。

    默认值依据：`skill-failure-detector` 的"当日 trace 中 ≥5% 命中某模式即
    开工单"。本项目 trace 量小（单机单人），5% 在小样本下会因单条 trace 抖动，
    因此额外要求**绝对条数**下限 —— 1 条 trace 命中不该触发告警，
    除非它是 PAGE 级（安全相关，单条即需人看）。

    这些不是"生产调优值"：真实流量分布 unknown，阈值定位是**失控保护**。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(default=ALERT_CONFIG_VERSION, min_length=1)
    #: TICKET 级：命中占比阈值（0–1）。
    ticket_rate: float = Field(default=0.05, gt=0, le=1)
    #: TICKET 级：最少命中 trace 数（小样本抖动保护）。
    ticket_min_count: int = Field(default=2, ge=1)
    #: PAGE 级：命中占比阈值。安全相关信号阈值更低。
    page_rate: float = Field(default=0.01, gt=0, le=1)
    #: PAGE 级：最少命中 trace 数。**默认 1 —— 单条安全信号即需人看。**
    page_min_count: int = Field(default=1, ge=1)


DEFAULT_ALERT_THRESHOLDS = AlertThresholds()


class GovernanceMetrics(BaseModel):
    """一批 trace 的聚合治理指标。

    ``None`` 语义：该指标**不可计算**（没有相应事件），与 0.0 不同。
    读报告的人必须能区分"缓存全未命中"与"这批 trace 里根本没有缓存事件"。
    """

    model_config = ConfigDict(extra="forbid")

    traces: int = 0
    detector_version: str = DETECTOR_VERSION

    # 缓存
    cache_events: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    cache_bypass: int = 0
    cache_hit_rate: float | None = None

    # 护栏
    guardrail_events: int = 0
    guardrail_blocks: int = 0
    guardrail_redactions: int = 0
    #: 规则 ID → 命中次数（block/redact 均计）。规则 ID 是有界集合，可安全做标签。
    guardrail_rule_counts: dict[str, int] = Field(default_factory=dict)

    # 工具
    tool_events: int = 0
    tool_executed: int = 0
    tool_blocked: int = 0
    tool_failed: int = 0

    # 成本（token 与 USD）。usage 不可得时保持 None，**不估算**。
    llm_calls: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float | None = None
    cost_p50_usd: float | None = None
    cost_p95_usd: float | None = None
    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None

    # PII 脱敏：护栏事件里 category=pii/secret 的命中数
    pii_redactions: int = 0
    secret_redactions: int = 0


def _percentile(values: Sequence[float], percent: float) -> float | None:
    """线性插值分位数。空序列返回 ``None``（不可计算 ≠ 0）。"""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 8)
    position = (len(ordered) - 1) * percent
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 8)


def aggregate_metrics(docs: Iterable[dict[str, Any]]) -> GovernanceMetrics:
    """从 trace 文档聚合治理指标。

    只读已存在的事件字段；缺字段一律按"不可计算"处理，绝不补默认值 ——
    补默认值会让"没接线"看起来像"值为 0"。
    """
    metrics = GovernanceMetrics()
    costs: list[float] = []
    latencies: list[float] = []
    rule_counts: dict[str, int] = {}
    saw_usage = False

    for doc in docs:
        view = TraceView(doc)
        if not view.schema_ok:
            continue
        metrics.traces += 1

        for event in view.events_of("cache"):
            data = view.data_of(event)
            metrics.cache_events += 1
            status = str(data.get("cache_status", "")).upper()
            if status == "HIT":
                metrics.cache_hits += 1
            elif status == "MISS":
                metrics.cache_misses += 1
            elif status == "BYPASS":
                # BYPASS 不计 miss（规格 §5：no_cache 不读但可写，且不计 miss）
                metrics.cache_bypass += 1

        for event in view.events_of("guardrail"):
            data = view.data_of(event)
            metrics.guardrail_events += 1
            action = str(data.get("action", ""))
            if action == "block":
                metrics.guardrail_blocks += 1
            elif action == "redact":
                metrics.guardrail_redactions += 1
            rule_id = str(data.get("rule_id") or "")
            if rule_id:
                rule_counts[rule_id] = rule_counts.get(rule_id, 0) + 1
            hits = data.get("hits")
            if isinstance(hits, list):
                for hit in hits:
                    if not isinstance(hit, dict):
                        continue
                    category = str(hit.get("category", ""))
                    count = int(hit.get("count", 1) or 1)
                    if category == "pii":
                        metrics.pii_redactions += count
                    elif category == "secret":
                        metrics.secret_redactions += count

        for event in view.events_of("tool"):
            data = view.data_of(event)
            metrics.tool_events += 1
            if data.get("executed"):
                metrics.tool_executed += 1
            status = str(data.get("tool_status", ""))
            if status == "blocked":
                metrics.tool_blocked += 1
            elif status == "failed":
                metrics.tool_failed += 1

        for event in view.events_of("llm_call"):
            data = view.data_of(event)
            metrics.llm_calls += 1
            metrics.total_input_tokens += int(data.get("input_tokens", 0) or 0)
            metrics.total_output_tokens += int(data.get("output_tokens", 0) or 0)
            cost = data.get("cost_usd")
            if cost is not None:
                saw_usage = True
                costs.append(float(cost))
            duration = event.get("duration_ms")
            if duration is not None:
                latencies.append(float(duration))

    if metrics.cache_events:
        # 分母排除 BYPASS：它既不算命中也不算未命中（否则"不读缓存"
        # 会把命中率稀释成一个没有意义的数）。
        denominator = metrics.cache_hits + metrics.cache_misses
        metrics.cache_hit_rate = (
            round(metrics.cache_hits / denominator, 6) if denominator else None
        )
    if saw_usage:
        metrics.total_cost_usd = round(sum(costs), 8)
        metrics.cost_p50_usd = _percentile(costs, 0.5)
        metrics.cost_p95_usd = _percentile(costs, 0.95)
    metrics.latency_p50_ms = _percentile(latencies, 0.5)
    metrics.latency_p95_ms = _percentile(latencies, 0.95)
    metrics.guardrail_rule_counts = dict(sorted(rule_counts.items()))
    return metrics


@dataclass(frozen=True, slots=True)
class AlertEvent:
    """一条本地结构化告警事件。

    **这不是"已投递的告警"**：``delivery`` 恒为 ``"local_event_only"``，
    表示事件已产出但**没有任何外部通道被调用**。字段名刻意直白，
    避免读日志的人以为有人被叫醒了。
    """

    detector: str
    severity: str
    triggered: bool
    tagged_traces: int
    total_traces: int
    rate: float
    threshold_rate: float
    threshold_min_count: int
    summary: str
    runbook: str
    config_version: str = ALERT_CONFIG_VERSION
    detector_version: str = DETECTOR_VERSION
    delivery: str = "local_event_only"
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )

    def to_payload(self) -> dict[str, Any]:
        return {
            "detector": self.detector,
            "severity": self.severity,
            "triggered": self.triggered,
            "tagged_traces": self.tagged_traces,
            "total_traces": self.total_traces,
            "rate": self.rate,
            "threshold_rate": self.threshold_rate,
            "threshold_min_count": self.threshold_min_count,
            "summary": self.summary,
            "runbook": self.runbook,
            "config_version": self.config_version,
            "detector_version": self.detector_version,
            "delivery": self.delivery,
            "created_at": self.created_at,
        }


#: 每个检测器的 runbook 指引（`observability-and-instrumentation`：告警必须
#: 链到 runbook，哪怕只有三行 —— 含义、第一条要跑的命令、升级路径）。
RUNBOOKS: dict[str, str] = {
    "tool_misuse": (
        "模型反复以非法参数调用同一工具。查 trace 的 tool 事件与工具 description；"
        "多为工具参数语义描述不清。先跑 pytest tests/test_tools.py 确认工具契约未坏。"
    ),
    "cascading_errors": (
        "错误在链路里往下传播而未就地收敛。查最长错误链的首个事件；"
        "确认是否 provider 故障（应由 gateway 熔断吸收）还是节点缺少错误处理。"
    ),
    "context_loss": (
        "检索到条文但答案零引用且报成功。查 citation_check 节点与 validate_citations 回流；"
        "这是铁律②的可用性侧失败，需人工看答案是否实际无依据。"
    ),
    "hallucinated_action": (
        "引用了从未检索到的来源（疑似编造）。**立即**核对 validate_citations 是否被绕过；"
        "跑 pytest tests/test_citation_contract.py。这是铁律②的直接违反。"
    ),
    "success_hallucination": (
        "报成功但链路里有被阻断的调用。规格 §6 禁止把 blocked 伪装成成功；"
        "查阻断事件的 rule_id 与最终 status 的产生位置。"
    ),
    "unverified_citation_after_cache": (
        "缓存命中后出现未验证引用，说明缓存绕过了引用校验。"
        "**立即**停用缓存写入（no_store）并跑 pytest tests/test_answer_cache.py。"
    ),
    "budget_bypass": (
        "计费 LLM 调用缺少用量记账事件。注意：当前 gateway on_usage 未写 trace，"
        "生产 trace 会系统性误报 —— 先确认是否为该已知缺口，再查真实绕过。"
    ),
    "guardrail_bypass": (
        "工具执行缺少配对的护栏裁定事件。查是否新增了绕过 ToolRegistry.invoke 的"
        "执行路径；跑 pytest tests/test_tool_policy.py::TestNoBypass。"
        "注意：ToolRegistry(trace=None) 时本信号恒空，无信号 ≠ 无绕过。"
    ),
    "trace_unreadable": (
        "trace 缺少 trace_id 或 events，检测器无法工作。查落盘逻辑与 schema 变更；"
        "读不出来不等于无故障。"
    ),
}


def evaluate_alerts(
    distribution: TagDistribution,
    *,
    thresholds: AlertThresholds = DEFAULT_ALERT_THRESHOLDS,
    include_untriggered: bool = False,
) -> list[AlertEvent]:
    """按阈值把信号分布转成本地告警事件。

    Args:
        distribution: :func:`tag_traces` 的分布结果。
        thresholds: 版本化阈值配置。
        include_untriggered: 是否也返回未触发的检测器（供报告展示全量口径）。

    Returns:
        告警事件列表（按 severity 降序、detector 名升序，顺序确定）。
    """
    events: list[AlertEvent] = []
    names = sorted(set(DETECTOR_SEVERITY) | set(distribution.by_detector))
    for name in names:
        count = distribution.by_detector.get(name, 0)
        severity = DETECTOR_SEVERITY.get(name, Severity.TICKET)
        if severity is Severity.PAGE:
            rate_threshold = thresholds.page_rate
            min_count = thresholds.page_min_count
        else:
            rate_threshold = thresholds.ticket_rate
            min_count = thresholds.ticket_min_count
        rate = distribution.rate_for(name)
        triggered = count >= min_count and rate >= rate_threshold
        if not triggered and not include_untriggered:
            continue
        events.append(
            AlertEvent(
                detector=name,
                severity=severity.value,
                triggered=triggered,
                tagged_traces=count,
                total_traces=distribution.traces,
                rate=round(rate, 6),
                threshold_rate=rate_threshold,
                threshold_min_count=min_count,
                summary=(
                    f"{name}: {count}/{distribution.traces} trace 命中"
                    f"（{rate:.1%}），阈值 {rate_threshold:.1%} 且 ≥{min_count} 条"
                ),
                runbook=RUNBOOKS.get(name, "无 runbook（新增检测器必须补）"),
            )
        )
    # PAGE 排前面：读的人先看到要立刻处理的
    events.sort(key=lambda e: (e.severity != "page", e.detector))
    return events


def build_governance_report(
    docs: Sequence[dict[str, Any]],
    *,
    distribution: TagDistribution,
    per_trace_signals: Sequence[Sequence[Signal]],
    thresholds: AlertThresholds = DEFAULT_ALERT_THRESHOLDS,
) -> dict[str, Any]:
    """组装完整治理观测报告（可 JSON 序列化）。

    报告只含**聚合数与已脱敏证据**，不含 trace 原文。
    """
    from water_agent.runtime.governance.detectors import signals_to_payload

    metrics = aggregate_metrics(docs)
    alerts = evaluate_alerts(distribution, thresholds=thresholds, include_untriggered=True)
    return {
        "detector_version": DETECTOR_VERSION,
        "alert_config_version": thresholds.version,
        "created_at": datetime.now(UTC).isoformat(),
        "traces": distribution.traces,
        "tagged_traces": distribution.tagged_traces,
        "signals": distribution.signals,
        "by_detector": dict(sorted(distribution.by_detector.items())),
        "by_severity": dict(sorted(distribution.by_severity.items())),
        "metrics": metrics.model_dump(),
        "alerts": [event.to_payload() for event in alerts],
        "triggered_alerts": [e.to_payload() for e in alerts if e.triggered],
        "per_trace_signals": [
            signals_to_payload(signals) for signals in per_trace_signals
        ],
    }


def write_alert_events(events: Sequence[AlertEvent], target: Any) -> int:
    """把告警事件按 JSONL 追加写入 ``target`` 路径，返回写入条数。

    只写触发的事件（未触发的属报告内容，不是告警）。
    """
    from pathlib import Path

    triggered = [e for e in events if e.triggered]
    if not triggered:
        return 0
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for event in triggered:
            handle.write(json.dumps(event.to_payload(), ensure_ascii=False) + "\n")
    return len(triggered)


__all__ = [
    "ALERT_CONFIG_VERSION",
    "DEFAULT_ALERT_THRESHOLDS",
    "RUNBOOKS",
    "AlertEvent",
    "AlertThresholds",
    "GovernanceMetrics",
    "aggregate_metrics",
    "build_governance_report",
    "evaluate_alerts",
    "write_alert_events",
]
