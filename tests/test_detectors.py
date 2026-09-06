"""P2.5 Step 15 测试：基于 trace 的失败模式检测器。

覆盖 `skill-failure-detector` 的三条 hard reject 与八个检测器的正反例：

- 每个检测器都有**正例**（该命中）与**反例**（不该命中）。只有正例的检测器
  无法证明它不是"永远命中"；只有反例的无法证明它不是"永远不命中"。
- 检测器**不得**依赖 LLM 调用（静态断言源码无 provider 导入）。
- 检测器**不得**只在崩溃时打标（多个正例的 trace status 都是 succeeded）。
- 信号证据**不得**含攻击载荷原文。

全部离线，不读磁盘、不发起远程调用。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from water_agent.runtime.governance.detectors import (
    DETECTOR_SEVERITY,
    DETECTOR_VERSION,
    DETECTORS,
    Severity,
    TraceView,
    detect_budget_bypass,
    detect_cascading_errors,
    detect_context_loss,
    detect_guardrail_bypass,
    detect_hallucinated_action,
    detect_success_hallucination,
    detect_tool_misuse,
    detect_unverified_citation_after_cache,
    signals_to_payload,
    tag_trace,
    tag_traces,
)


def _doc(events: list[dict[str, Any]], *, status: str = "succeeded") -> dict[str, Any]:
    """构造最小可检测 trace 文档。"""
    return {"trace_id": "t-test", "status": status, "events": events}


def _tool_event(
    *,
    name: str = "calc_velocity",
    status: str = "succeeded",
    executed: bool = True,
    invocation_id: str = "inv-1",
    code: str | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tool_name": name,
        "tool_status": status,
        "executed": executed,
        "invocation_id": invocation_id,
    }
    if code:
        data["code"] = code
    return {"event_type": "tool", "data": data}


def _guardrail_event(
    *, stage: str = "tool", action: str = "allow", invocation_id: str = "inv-1"
) -> dict[str, Any]:
    return {
        "event_type": "guardrail",
        "data": {"stage": stage, "action": action, "invocation_id": invocation_id},
    }


class TestDetectorDiscipline:
    """检测器自身必须满足的纪律（`skill-failure-detector` 的 hard rejects）。"""

    def test_detectors_never_call_llm(self) -> None:
        """检测器不得导入 provider SDK 或发起出站调用。

        每条 trace 调模型做判定会把观测成本与可用性绑到 provider 上：
        账单翻倍、provider 挂了就看不见故障，且判定不可复现。

        用 AST 查**真实 import**而非裸词匹配：docstring 里写"不依赖 openai"
        是正确的说明，裸词断言会把诚实的免责声明也判为违规
        （`test_governance_observability.py` 的同类测试第一版踩过这个坑）。
        """
        import ast

        import water_agent.runtime.governance.detectors as module

        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        forbidden = {
            "openai",
            "anthropic",
            "langchain",
            "langchain_openai",
            "langgraph",
            "requests",
            "httpx",
            "urllib",
            "socket",
        }
        assert not (imported & forbidden), f"检测器不得依赖 {imported & forbidden}"

    def test_detector_version_is_declared(self) -> None:
        """检测器必须版本化：退化会静默摧毁信号，无版本号无法回溯。"""
        assert DETECTOR_VERSION
        assert DETECTOR_VERSION.startswith("p2.5-")

    def test_every_detector_has_declared_severity(self) -> None:
        """每个检测器都必须登记严重度，否则告警路由会静默降级。"""
        for name, _fn in DETECTORS:
            assert name in DETECTOR_SEVERITY, f"检测器 {name} 未登记严重度"

    def test_only_two_alerting_severities(self) -> None:
        """只有 page/ticket 参与告警：第三档会变成噪音，训练人忽略全部告警。"""
        assert set(DETECTOR_SEVERITY.values()) <= {Severity.PAGE, Severity.TICKET}

    def test_detects_on_successful_traces_not_only_crashes(self) -> None:
        """不得只在崩溃时打标：绝大多数 Agent 故障产出看起来正常的输出。"""
        # status=succeeded 但存在护栏阻断 → 必须被 success_hallucination 命中
        doc = _doc(
            [
                _guardrail_event(stage="input", action="block"),
                {"event_type": "run_end", "status": "succeeded"},
            ]
        )
        assert detect_success_hallucination(TraceView(doc))

    def test_unreadable_trace_is_not_silently_clean(self) -> None:
        """结构不可读 → 报 trace_unreadable，而不是空列表（=看起来没问题）。"""
        signals = tag_trace({"nonsense": True})
        assert [s.detector for s in signals] == ["trace_unreadable"]


class TestGuardrailBypass:
    """最重要的检测器：Step 14 缺口第 6 项的可观测化。"""

    def test_executed_without_guardrail_event_is_page(self) -> None:
        doc = _doc([_tool_event(executed=True)])
        signals = detect_guardrail_bypass(TraceView(doc))
        assert len(signals) == 1
        assert signals[0].severity is Severity.PAGE

    def test_paired_execution_is_clean(self) -> None:
        """有配对护栏事件 → 不报（否则正常执行会被淹没在假信号里）。"""
        doc = _doc([_guardrail_event(invocation_id="inv-9"), _tool_event(invocation_id="inv-9")])
        assert detect_guardrail_bypass(TraceView(doc)) == []

    def test_blocked_call_not_flagged(self) -> None:
        """被拦在执行前的调用本来就不该有执行记录，不算绕过。"""
        doc = _doc([_tool_event(status="blocked", executed=False, invocation_id="inv-2")])
        assert detect_guardrail_bypass(TraceView(doc)) == []

    def test_pairing_is_by_id_not_adjacency(self) -> None:
        """配对靠 invocation_id，不靠事件相邻 —— 并发/重试会让相邻性给出错误结论。

        构造：护栏事件的 id 与工具事件不同（相邻但不配对）→ 必须报绕过。
        """
        doc = _doc(
            [_guardrail_event(invocation_id="other"), _tool_event(invocation_id="inv-1")]
        )
        assert detect_guardrail_bypass(TraceView(doc))

    def test_non_tool_stage_guardrail_does_not_count_as_pairing(self) -> None:
        """input 阶段的护栏事件不能顶替 tool 阶段的裁定。"""
        doc = _doc(
            [
                _guardrail_event(stage="input", invocation_id="inv-1"),
                _tool_event(invocation_id="inv-1"),
            ]
        )
        assert detect_guardrail_bypass(TraceView(doc))


class TestToolMisuse:
    def test_two_invalid_argument_calls_flagged(self) -> None:
        doc = _doc(
            [
                _tool_event(status="failed", code="INVALID_ARGUMENTS", invocation_id="a"),
                _tool_event(status="failed", code="INVALID_ARGUMENTS", invocation_id="b"),
            ]
        )
        signals = detect_tool_misuse(TraceView(doc))
        assert len(signals) == 1
        assert signals[0].evidence["invalid_argument_calls"] == 2

    def test_single_missing_arg_is_not_flagged(self) -> None:
        """单次缺参是正常的澄清路径；阈值为 1 会让护栏信号被噪音淹没。"""
        doc = _doc([_tool_event(status="failed", code="INVALID_ARGUMENTS")])
        assert detect_tool_misuse(TraceView(doc)) == []

    def test_successful_calls_not_flagged(self) -> None:
        doc = _doc([_tool_event(), _tool_event(invocation_id="b")])
        assert detect_tool_misuse(TraceView(doc)) == []


class TestCascadingErrors:
    def test_three_consecutive_errors_flagged(self) -> None:
        doc = _doc([{"event_type": "x", "error": "boom"} for _ in range(3)])
        assert detect_cascading_errors(TraceView(doc))

    def test_two_errors_within_bounded_retry_not_flagged(self) -> None:
        """有界重试本就会产生 1–2 个连续错误事件，那是设计如此。"""
        doc = _doc([{"event_type": "x", "error": "boom"} for _ in range(2)])
        assert detect_cascading_errors(TraceView(doc)) == []

    def test_streak_broken_by_success_not_flagged(self) -> None:
        events: list[dict[str, Any]] = [
            {"event_type": "x", "error": "boom"},
            {"event_type": "x", "error": "boom"},
            {"event_type": "x"},
            {"event_type": "x", "error": "boom"},
        ]
        assert detect_cascading_errors(TraceView(_doc(events))) == []


class TestContextLoss:
    def test_retrieved_but_no_citation_on_success_flagged(self) -> None:
        doc = _doc(
            [
                {"event_type": "retrieve", "retrieved_context": [{"source_id": "GB50015-3.2.1"}]},
                {"event_type": "run_end", "status": "succeeded"},
            ]
        )
        assert detect_context_loss(TraceView(doc))

    def test_retrieved_and_cited_not_flagged(self) -> None:
        doc = _doc(
            [
                {"event_type": "retrieve", "retrieved_context": [{"source_id": "GB50015-3.2.1"}]},
                {"event_type": "cite", "citations": [{"source_id": "GB50015-3.2.1"}]},
                {"event_type": "run_end", "status": "succeeded"},
            ]
        )
        assert detect_context_loss(TraceView(doc)) == []

    def test_no_retrieval_not_flagged(self) -> None:
        """纯计算类请求没有检索，零引用是正常的。"""
        doc = _doc([{"event_type": "run_end", "status": "succeeded"}])
        assert detect_context_loss(TraceView(doc)) == []


class TestHallucinatedAction:
    def test_citation_absent_from_retrieval_is_page(self) -> None:
        doc = _doc(
            [
                {"event_type": "retrieve", "retrieved_context": [{"source_id": "GB50015-3.2.1"}]},
                {"event_type": "cite", "citations": [{"source_id": "GB50099-9.9.9"}]},
            ]
        )
        signals = detect_hallucinated_action(TraceView(doc))
        assert len(signals) == 1
        assert signals[0].severity is Severity.PAGE
        assert "GB50099-9.9.9" in signals[0].evidence["fabricated_source_ids"]

    def test_citation_present_in_retrieval_not_flagged(self) -> None:
        doc = _doc(
            [
                {"event_type": "retrieve", "retrieved_context": [{"source_id": "GB50015-3.2.1"}]},
                {"event_type": "cite", "citations": [{"source_id": "GB50015-3.2.1"}]},
            ]
        )
        assert detect_hallucinated_action(TraceView(doc)) == []


class TestSuccessHallucination:
    def test_success_with_blocked_guardrail_is_page(self) -> None:
        """规格 §6 禁止把 blocked 伪装成业务回答成功。"""
        doc = _doc(
            [
                _guardrail_event(stage="input", action="block"),
                {"event_type": "run_end", "status": "succeeded"},
            ]
        )
        signals = detect_success_hallucination(TraceView(doc))
        assert len(signals) == 1
        assert signals[0].severity is Severity.PAGE

    def test_blocked_run_reporting_failure_not_flagged(self) -> None:
        """阻断且如实报失败 → 正确行为，不该告警。"""
        doc = _doc(
            [
                _guardrail_event(stage="input", action="block"),
                {"event_type": "run_end", "status": "failed"},
            ],
            status="failed",
        )
        assert detect_success_hallucination(TraceView(doc)) == []

    def test_clean_success_not_flagged(self) -> None:
        doc = _doc([_guardrail_event(action="allow"), {"event_type": "run_end", "status": "succeeded"}])
        assert detect_success_hallucination(TraceView(doc)) == []


class TestUnverifiedCitationAfterCache:
    def test_cache_hit_with_unverified_citation_is_page(self) -> None:
        doc = _doc(
            [
                {"event_type": "cache", "data": {"cache_status": "HIT"}},
                {"event_type": "cite", "citations": [{"source_id": "X-1", "verified": False}]},
            ]
        )
        signals = detect_unverified_citation_after_cache(TraceView(doc))
        assert len(signals) == 1
        assert signals[0].severity is Severity.PAGE

    def test_cache_miss_not_flagged(self) -> None:
        doc = _doc(
            [
                {"event_type": "cache", "data": {"cache_status": "MISS"}},
                {"event_type": "cite", "citations": [{"source_id": "X-1", "verified": False}]},
            ]
        )
        assert detect_unverified_citation_after_cache(TraceView(doc)) == []

    def test_missing_verified_field_is_not_treated_as_unverified(self) -> None:
        """字段缺失 ≠ 未验证：老 trace 没有该字段，不能据此制造假信号。"""
        doc = _doc(
            [
                {"event_type": "cache", "data": {"cache_status": "HIT"}},
                {"event_type": "cite", "citations": [{"source_id": "X-1"}]},
            ]
        )
        assert detect_unverified_citation_after_cache(TraceView(doc)) == []


class TestBudgetBypass:
    def test_billed_call_without_usage_event_flagged(self) -> None:
        doc = _doc([{"event_type": "llm_call", "data": {"total_tokens": 100}}])
        signals = detect_budget_bypass(TraceView(doc))
        assert len(signals) == 1

    def test_known_limitation_is_declared_in_evidence(self) -> None:
        """本检测器当前会系统性误报，证据里必须写明，避免读报告的人误判为确诊缺陷。"""
        doc = _doc([{"event_type": "llm_call", "data": {"total_tokens": 100}}])
        signals = detect_budget_bypass(TraceView(doc))
        assert "known_limitation" in signals[0].evidence

    def test_zero_token_call_not_flagged(self) -> None:
        doc = _doc([{"event_type": "llm_call", "data": {"total_tokens": 0}}])
        assert detect_budget_bypass(TraceView(doc)) == []

    def test_with_usage_event_not_flagged(self) -> None:
        doc = _doc(
            [
                {"event_type": "llm_call", "data": {"total_tokens": 100}},
                {"event_type": "usage", "data": {"input_tokens": 60}},
            ]
        )
        assert detect_budget_bypass(TraceView(doc)) == []


class TestEvidenceCarriesNoPayload:
    """失败样本承载最糟的内容；信号会被聚合、打印、写盘。"""

    def test_signal_evidence_has_no_attack_payload(self) -> None:
        doc = _doc(
            [
                {
                    "event_type": "guardrail",
                    "data": {
                        "stage": "input",
                        "action": "block",
                        "rule_id": "injection.override",
                    },
                },
                {"event_type": "run_end", "status": "succeeded"},
            ]
        )
        signals = detect_success_hallucination(TraceView(doc))
        blob = repr(signals_to_payload(signals))
        # 规则 ID 可以在（排障需要），载荷不可以
        assert "injection.override" in blob
        for token in ("忽略以上", "PWNED", "ignore all previous"):
            assert token not in blob


class TestTagging:
    def test_tag_traces_counts_traces_not_signals(self) -> None:
        """by_detector 计"命中该检测器的 trace 数"；混用信号数会让阈值失去意义。"""
        doc = _doc(
            [
                _tool_event(status="failed", code="INVALID_ARGUMENTS", invocation_id="a"),
                _tool_event(status="failed", code="INVALID_ARGUMENTS", invocation_id="b"),
                _guardrail_event(invocation_id="a"),
                _guardrail_event(invocation_id="b"),
            ]
        )
        _per_trace, dist = tag_traces([doc, doc])
        assert dist.traces == 2
        assert dist.by_detector["tool_misuse"] == 2  # 2 条 trace，不是 4 条信号

    def test_rate_for_is_trace_fraction(self) -> None:
        clean = _doc([_guardrail_event(), _tool_event()])
        dirty = _doc([_tool_event(invocation_id="unpaired")])
        _per_trace, dist = tag_traces([clean, dirty])
        assert dist.rate_for("guardrail_bypass") == 0.5

    def test_rate_for_empty_batch_is_zero_not_error(self) -> None:
        _per_trace, dist = tag_traces([])
        assert dist.rate_for("guardrail_bypass") == 0.0

    def test_clean_trace_yields_no_signals(self) -> None:
        """完全正常的 trace 不得产生任何信号（否则信号失去意义）。"""
        doc = _doc(
            [
                {"event_type": "retrieve", "retrieved_context": [{"source_id": "GB50015-3.2.1"}]},
                {"event_type": "cite", "citations": [{"source_id": "GB50015-3.2.1"}]},
                _guardrail_event(action="allow"),
                _tool_event(),
                {"event_type": "run_end", "status": "succeeded"},
            ]
        )
        assert tag_trace(doc) == []

    def test_signal_order_is_deterministic(self) -> None:
        """信号顺序固定 → 报告可 diff。"""
        doc = _doc(
            [
                _tool_event(invocation_id="unpaired"),
                {"event_type": "run_end", "status": "succeeded"},
                _guardrail_event(stage="input", action="block"),
            ]
        )
        first = [s.detector for s in tag_trace(doc)]
        second = [s.detector for s in tag_trace(doc)]
        assert first == second
        assert len(first) >= 2
