"""P2.5 Step 15：基于 trace 的失败模式检测器。

设计约束（`docs/design/12-p2.5-production-governance.md` §7 +
`skill-failure-detector` 的三条 hard reject）：

1. **检测器是规则，不是 LLM 调用**。每条 trace 都调模型做判定，等于把观测成本
   与观测可用性绑到 provider 上：账单翻倍、provider 挂了就看不见故障，而且
   判定本身不可复现。LLM-as-judge 只用于**抽样人工复盘**，不进检测路径。
2. **不只在崩溃时打标**。绝大多数 Agent 故障产出的是**看起来正常的输出**
   （引用编造、静默丢上下文、声称完成但没做）。因此检测器读 trace 的
   **内容与状态签名**，不只看 ``status == "failed"``。
3. **检测器必须版本化**。检测器退化（规则写错、字段改名导致永不命中）会
   静默摧毁信号 —— 没有版本号就无法回答"这批数据是哪版规则跑出来的"。

**本模块只读 trace 文档，不发起任何远程调用，也不写文件。**

命名纪律：检测到的是**信号（signal）**，不是"确诊的 bug"。一条 trace 被打标
表示"值得人看"，不表示"一定错了"。误报率与已知局限写在每个检测器的 docstring
与 `docs/design/p2.5-step-15-decision.md`，不在这里假装精确。
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

#: 检测器规则集版本。**改任何判据都要改它**（聚合报告会记录它）。
DETECTOR_VERSION = "p2.5-step15-2026-09-06"


class Severity(str, Enum):
    """信号严重度。

    只有两档实际参与告警路由（见 `alerts.py`）：``PAGE`` 与 ``TICKET``。
    ``observability-and-instrumentation` 的告警纪律：第三档会变成噪音，
    训练人忽略全部告警。``INFO`` 只进报告不进告警。
    """

    INFO = "info"
    TICKET = "ticket"
    PAGE = "page"


@dataclass(frozen=True, slots=True)
class Signal:
    """一条检测信号。

    ``evidence`` 只放**已脱敏的结构化摘要**（事件下标、状态码、计数）。
    绝不放 trace 原文片段：失败样本恰恰承载最糟的内容（攻击载荷、PII），
    而信号会被聚合、打印、写盘（`skill-failure-detector` 的第三条 hard reject）。
    """

    detector: str
    severity: Severity
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)
    detector_version: str = DETECTOR_VERSION


@dataclass(frozen=True, slots=True)
class TraceView:
    """trace 文档的只读视图，屏蔽字段结构细节。

    存在的理由：检测器若各自 ``doc["events"][i]["data"].get(...)``，一次字段
    改名就会让所有检测器静默失效（返回空 = 看起来"没有故障"）。集中在这里，
    改名只需改一处，且 :meth:`schema_ok` 能显式报告"这份 trace 读不出事件"。
    """

    doc: dict[str, Any]

    @property
    def trace_id(self) -> str:
        return str(self.doc.get("trace_id") or "")

    @property
    def status(self) -> str:
        return str(self.doc.get("status") or "")

    @property
    def events(self) -> list[dict[str, Any]]:
        raw = self.doc.get("events")
        return [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []

    @property
    def schema_ok(self) -> bool:
        """这份文档是否具备可检测的最小结构。

        结构不对时**不能**当作"无故障"—— 那正是检测器静默失效的样子。
        调用方（tagger）会为此产出 ``trace_unreadable`` 信号。
        """
        return bool(self.doc.get("trace_id")) and isinstance(self.doc.get("events"), list)

    def events_of(self, event_type: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("event_type") == event_type]

    @staticmethod
    def data_of(event: dict[str, Any]) -> dict[str, Any]:
        raw = event.get("data")
        return raw if isinstance(raw, dict) else {}


# --------------------------------------------------------------------------
# 通用检测器（skill-failure-detector 的五个行业复现模式）
# --------------------------------------------------------------------------


def detect_tool_misuse(view: TraceView) -> list[Signal]:
    """工具误用：同一工具反复以非法参数被调用。

    判据：同名工具的 ``INVALID_ARGUMENTS`` 失败 ≥2 次。单次缺参是**正常**的
    澄清路径（工具本就该拒绝并让模型补参），所以阈值不是 1 —— 否则每次正常
    的缺参澄清都会告警，护栏信号会被噪音淹没。

    已知局限：同一工具两次不同原因的参数错误也会命中。这是刻意偏向召回：
    这是 TICKET 不是 PAGE，人工复盘成本低于漏掉真实的模型参数构造缺陷。
    """
    counts: dict[str, int] = {}
    for event in view.events_of("tool"):
        data = view.data_of(event)
        if data.get("code") == "INVALID_ARGUMENTS":
            name = str(data.get("tool_name") or "?")
            counts[name] = counts.get(name, 0) + 1
    return [
        Signal(
            detector="tool_misuse",
            severity=Severity.TICKET,
            summary=f"工具 {name!r} 连续 {count} 次参数非法",
            evidence={"tool_name": name, "invalid_argument_calls": count},
        )
        for name, count in sorted(counts.items())
        if count >= 2
    ]


def detect_cascading_errors(view: TraceView) -> list[Signal]:
    """级联错误：连续多个事件报错，说明失败没有被就地收敛。

    判据：连续 ≥3 个带 ``error`` 的事件。有界重试会产生 1–2 个连续错误事件
    （那是**设计如此**），3 个起才说明错误在链路里往下传播。
    """
    longest = 0
    current = 0
    for event in view.events:
        if event.get("error"):
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    if longest < 3:
        return []
    return [
        Signal(
            detector="cascading_errors",
            severity=Severity.TICKET,
            summary=f"连续 {longest} 个事件报错，错误未就地收敛",
            evidence={"longest_error_streak": longest},
        )
    ]


def detect_context_loss(view: TraceView) -> list[Signal]:
    """上下文丢失：检索到了条文，但最终回答零引用且仍报成功。

    这是**看起来正常的失败**的典型：状态是 succeeded，答案读起来通顺，
    但检索到的依据没进答案 —— 只看 status 永远发现不了。

    判据：有检索命中 + run_end 成功 + 全程无引用。
    """
    retrieved = any(event.get("retrieved_context") for event in view.events)
    cited = any(event.get("citations") for event in view.events)
    ends = view.events_of("run_end")
    succeeded = any(e.get("status") == "succeeded" for e in ends)
    if not (retrieved and succeeded and not cited):
        return []
    return [
        Signal(
            detector="context_loss",
            severity=Severity.TICKET,
            summary="检索到条文但最终回答零引用，且状态为成功",
            evidence={"retrieved": True, "cited": False, "status": "succeeded"},
        )
    ]


def detect_hallucinated_action(view: TraceView) -> list[Signal]:
    """幻觉动作：声明了引用，但该来源从未出现在检索命中里。

    这是铁律②（引用可追溯、禁止编造）的**事后可观测化**：Step 5 的
    ``validate_citations`` 在运行时拦截，这个检测器负责在 trace 层证明
    "拦截确实生效了"，并捕捉绕过校验器的新路径。

    判据：citations 里的 source_id 不在任何 retrieved_context 的 source_id 里。
    """
    retrieved_ids: set[str] = set()
    for event in view.events:
        raw = event.get("retrieved_context")
        if isinstance(raw, list):
            for clause in raw:
                if isinstance(clause, dict) and clause.get("source_id"):
                    retrieved_ids.add(str(clause["source_id"]))

    fabricated: set[str] = set()
    for event in view.events:
        raw = event.get("citations")
        if isinstance(raw, list):
            for citation in raw:
                if isinstance(citation, dict) and citation.get("source_id"):
                    source_id = str(citation["source_id"])
                    if source_id not in retrieved_ids:
                        fabricated.add(source_id)
    if not fabricated:
        return []
    return [
        Signal(
            detector="hallucinated_action",
            severity=Severity.PAGE,
            summary=f"{len(fabricated)} 个引用来源从未出现在检索命中中",
            evidence={
                "fabricated_count": len(fabricated),
                # source_id 是规范号+条款号（如 GB50015-3.2.1），不是用户内容，
                # 可安全入证据；工程师需要它才能定位问题。
                "fabricated_source_ids": sorted(fabricated)[:10],
            },
        )
    ]


def detect_success_hallucination(view: TraceView) -> list[Signal]:
    """成功幻觉：run_end 报成功，但链路里存在被阻断或失败的关键事件。

    规格 §6 明确禁止「把 blocked 伪装成业务回答成功」。这个检测器是那条
    禁令的**运行时证据**：即使将来有人在某条路径上把 blocked 咽掉并返回
    成功，trace 层仍会留下矛盾。

    判据：run_end 成功 + 存在 guardrail block 或 tool blocked 事件。
    """
    ends = view.events_of("run_end")
    if not any(e.get("status") == "succeeded" for e in ends):
        return []

    blocked_guardrails = [
        view.data_of(e).get("rule_id", "")
        for e in view.events_of("guardrail")
        if view.data_of(e).get("action") == "block"
    ]
    blocked_tools = [
        view.data_of(e).get("tool_name", "")
        for e in view.events_of("tool")
        if view.data_of(e).get("tool_status") == "blocked"
    ]
    if not (blocked_guardrails or blocked_tools):
        return []
    return [
        Signal(
            detector="success_hallucination",
            severity=Severity.PAGE,
            summary="run_end 报成功，但链路中存在被护栏阻断的调用",
            evidence={
                "blocked_guardrail_rules": sorted({r for r in blocked_guardrails if r})[:10],
                "blocked_tools": sorted({t for t in blocked_tools if t})[:10],
            },
        )
    ]


# --------------------------------------------------------------------------
# 本领域检测器（规格 §7 点名要求的三个）
# --------------------------------------------------------------------------


def detect_unverified_citation_after_cache(view: TraceView) -> list[Signal]:
    """缓存命中后出现未验证引用。

    规格 §5 的核心不变量：**只有已验证引用的回答才允许入缓存**，因此缓存
    命中的回答其引用必然已被验证过。若命中后仍出现未验证引用，说明缓存
    绕过了引用校验 —— 那是铁律②在缓存路径上的破口。

    判据：存在 cache HIT + 存在 citations 校验失败/未验证标记。
    """
    hits = [
        e
        for e in view.events_of("cache")
        if str(view.data_of(e).get("cache_status", "")).upper() == "HIT"
    ]
    if not hits:
        return []

    unverified: list[str] = []
    for event in view.events:
        raw = event.get("citations")
        if isinstance(raw, list):
            for citation in raw:
                if not isinstance(citation, dict):
                    continue
                # verified 显式为 False 才算未验证；字段缺失不算
                # （老 trace 没有这个字段，不能据此制造假信号）。
                if citation.get("verified") is False and citation.get("source_id"):
                    unverified.append(str(citation["source_id"]))
    if not unverified:
        return []
    return [
        Signal(
            detector="unverified_citation_after_cache",
            severity=Severity.PAGE,
            summary=f"缓存命中后出现 {len(unverified)} 个未验证引用",
            evidence={
                "cache_hits": len(hits),
                "unverified_source_ids": sorted(set(unverified))[:10],
            },
        )
    ]


def detect_budget_bypass(view: TraceView) -> list[Signal]:
    """预算绕过：LLM 调用产生了 token 用量，但没有对应的用量记账事件。

    判据：存在 ``llm_call`` 且 ``total_tokens > 0``，但该 trace 中
    ``usage``/``budget`` 类事件为零。

    已知局限（**重要，不得夸大本检测器**）：当前 gateway 的用量对账走
    ``on_usage`` 回调，而该回调**尚未统一写入 trace**。因此本检测器目前
    对生产 trace 会**系统性误报**，只在自带用量事件的合成 trace 上有意义。
    这是刻意保留的占位实现：判据本身是对的，缺的是接线。
    结论写在 `docs/design/p2.5-step-15-decision.md`，不得声称"预算绕过已可观测"。
    """
    billed = [
        e
        for e in view.events_of("llm_call")
        if int(view.data_of(e).get("total_tokens", 0) or 0) > 0
    ]
    if not billed:
        return []
    accounted = view.events_of("usage") + view.events_of("budget")
    if accounted:
        return []
    return [
        Signal(
            detector="budget_bypass",
            severity=Severity.TICKET,
            summary=f"{len(billed)} 次计费 LLM 调用没有对应的用量记账事件",
            evidence={
                "billed_llm_calls": len(billed),
                "usage_events": 0,
                # 显式声明本信号的已知局限，避免读报告的人误判为确诊缺陷
                "known_limitation": "gateway on_usage 回调尚未写入 trace，生产 trace 会误报",
            },
        )
    ]


def detect_guardrail_bypass(view: TraceView) -> list[Signal]:
    """护栏绕过：工具**真的执行了**，但没有配对的护栏裁定事件。

    这是 Step 14 决策记录 §5 第 6 项缺口的可观测化，也是本步最重要的检测器：
    Step 14 用测试保证了"护栏在 execute 之前"，但那只覆盖**已知**调用路径。
    将来有人新增一条绕过 ``ToolRegistry.invoke`` 的执行路径时，测试不会变红
    （新路径没有测试），而这个检测器会在 trace 层看见。

    判据：``tool`` 事件 ``executed=True`` 且其 ``invocation_id`` 不存在
    对应的 ``guardrail`` 事件（stage=tool）。用 ``invocation_id`` **精确配对**，
    不靠"事件时间相邻"猜测 —— 并发或重试会让相邻性判据给出错误结论。

    已知局限：``ToolRegistry(trace=None)`` 时两类事件都不写，本检测器
    静默返回空。**"没有信号" ≠ "没有绕过"**，这一点写在 operator 文档里。
    """
    guarded_ids = {
        str(view.data_of(e).get("invocation_id") or "")
        for e in view.events_of("guardrail")
        if view.data_of(e).get("stage") == "tool"
    }
    guarded_ids.discard("")

    unguarded: list[str] = []
    for event in view.events_of("tool"):
        data = view.data_of(event)
        if not data.get("executed"):
            continue  # 被拦在执行前的调用本来就不该有执行记录
        invocation_id = str(data.get("invocation_id") or "")
        if not invocation_id or invocation_id not in guarded_ids:
            unguarded.append(str(data.get("tool_name") or "?"))
    if not unguarded:
        return []
    return [
        Signal(
            detector="guardrail_bypass",
            severity=Severity.PAGE,
            summary=f"{len(unguarded)} 次工具执行缺少配对的护栏裁定事件",
            evidence={
                "executed_without_guardrail": len(unguarded),
                "tool_names": sorted(set(unguarded))[:10],
            },
        )
    ]


#: 全部检测器。顺序固定 → 信号顺序可复现（报告可 diff）。
DETECTORS: tuple[tuple[str, Any], ...] = (
    ("tool_misuse", detect_tool_misuse),
    ("cascading_errors", detect_cascading_errors),
    ("context_loss", detect_context_loss),
    ("hallucinated_action", detect_hallucinated_action),
    ("success_hallucination", detect_success_hallucination),
    ("unverified_citation_after_cache", detect_unverified_citation_after_cache),
    ("budget_bypass", detect_budget_bypass),
    ("guardrail_bypass", detect_guardrail_bypass),
)

#: 检测器名 → 严重度（供告警路由与文档核对；与各函数内的 Severity 一致）。
DETECTOR_SEVERITY: dict[str, Severity] = {
    "tool_misuse": Severity.TICKET,
    "cascading_errors": Severity.TICKET,
    "context_loss": Severity.TICKET,
    "hallucinated_action": Severity.PAGE,
    "success_hallucination": Severity.PAGE,
    "unverified_citation_after_cache": Severity.PAGE,
    "budget_bypass": Severity.TICKET,
    "guardrail_bypass": Severity.PAGE,
}


def tag_trace(doc: dict[str, Any]) -> list[Signal]:
    """对单份 trace 跑全部检测器，返回信号列表（顺序确定）。

    结构不可读时返回 ``trace_unreadable`` 而不是空列表 —— 把"读不出来"
    当成"没问题"正是检测器静默失效的表现。
    """
    view = TraceView(doc)
    if not view.schema_ok:
        return [
            Signal(
                detector="trace_unreadable",
                severity=Severity.TICKET,
                summary="trace 缺少 trace_id 或 events，无法检测（不等于无故障）",
                evidence={"has_trace_id": bool(doc.get("trace_id"))},
            )
        ]
    signals: list[Signal] = []
    for _name, detector in DETECTORS:
        signals.extend(detector(view))
    return signals


@dataclass(frozen=True, slots=True)
class TagDistribution:
    """一批 trace 的信号分布。"""

    traces: int
    signals: int
    by_detector: dict[str, int]
    by_severity: dict[str, int]
    tagged_traces: int
    detector_version: str = DETECTOR_VERSION

    def rate_for(self, detector: str) -> float:
        """该检测器命中的 trace 占比（0.0–1.0）。``traces==0`` 时为 0.0。"""
        if self.traces <= 0:
            return 0.0
        return self.by_detector.get(detector, 0) / self.traces


def tag_traces(docs: Iterable[dict[str, Any]]) -> tuple[list[list[Signal]], TagDistribution]:
    """对一批 trace 打标并汇总分布。

    返回逐 trace 的信号列表（与输入顺序对齐）+ 聚合分布。
    """
    per_trace: list[list[Signal]] = []
    by_detector: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    total_signals = 0
    tagged = 0

    for doc in docs:
        signals = tag_trace(doc)
        per_trace.append(signals)
        if signals:
            tagged += 1
        total_signals += len(signals)
        # by_detector 计的是"命中该检测器的 trace 数"（不是信号数）——
        # 告警阈值按 trace 占比定义，两者混用会让阈值失去意义。
        for name in {s.detector for s in signals}:
            by_detector[name] = by_detector.get(name, 0) + 1
        for severity in {s.severity.value for s in signals}:
            by_severity[severity] = by_severity.get(severity, 0) + 1

    return per_trace, TagDistribution(
        traces=len(per_trace),
        signals=total_signals,
        by_detector=by_detector,
        by_severity=by_severity,
        tagged_traces=tagged,
    )


def signals_to_payload(signals: Sequence[Signal]) -> list[dict[str, Any]]:
    """信号转可 JSON 序列化的结构（供报告与告警事件复用）。"""
    return [
        {
            "detector": s.detector,
            "severity": s.severity.value,
            "summary": s.summary,
            "evidence": s.evidence,
            "detector_version": s.detector_version,
        }
        for s in signals
    ]


__all__ = [
    "DETECTORS",
    "DETECTOR_SEVERITY",
    "DETECTOR_VERSION",
    "Severity",
    "Signal",
    "TagDistribution",
    "TraceView",
    "detect_budget_bypass",
    "detect_cascading_errors",
    "detect_context_loss",
    "detect_guardrail_bypass",
    "detect_hallucinated_action",
    "detect_success_hallucination",
    "detect_tool_misuse",
    "detect_unverified_citation_after_cache",
    "signals_to_payload",
    "tag_trace",
    "tag_traces",
]
