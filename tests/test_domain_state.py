"""Tests for domain models and WaterAgentState."""
from __future__ import annotations

import json
from datetime import datetime

from water_agent.domain.models import (
    Citation,
    EvalCase,
    EvalReport,
    EvalResult,
    GeneratedAnswer,
    RetrievedClause,
    TraceEvent,
    make_source_id,
)
from water_agent.domain.state import (
    MAX_CITATION_RETRIES,
    MAX_TOTAL_STEPS,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    WaterAgentState,
    default_state,
)


class TestMakeSourceId:
    def test_format(self) -> None:
        assert make_source_id("GB50014", "1.0.3") == "GB50014 1.0.3"
        assert make_source_id("FIX", "1.1.1") == "FIX 1.1.1"

    def test_handles_empty(self) -> None:
        assert make_source_id("", "") == ""


class TestRetrievedClause:
    def test_minimal(self) -> None:
        c = RetrievedClause(spec_id="FIX", article="1.1.1", text="test")
        assert c.source_id == "FIX 1.1.1"
        assert c.title == ""
        assert c.score is None

    def test_full(self) -> None:
        c = RetrievedClause(
            spec_id="GB50014",
            chapter="1",
            section="1.0",
            article="1.0.3",
            title="排水工程设计",
            text="排水工程设计应以...",
            score=0.95,
        )
        assert c.source_id == "GB50014 1.0.3"
        assert c.score == 0.95

    def test_json_serializable(self) -> None:
        c = RetrievedClause(spec_id="FIX", article="1.1", title="测试", text="内容")
        d = json.loads(c.model_dump_json())
        assert d["spec_id"] == "FIX"
        assert d["article"] == "1.1"
        # source_id 是计算属性，不存储在 JSON 中
        assert "source_id" not in d

    def test_serialize_with_none(self) -> None:
        c = RetrievedClause(spec_id="FIX", article="1.1", text="test")
        d = json.loads(c.model_dump_json())
        assert d["score"] is None


class TestCitation:
    def test_source_id(self) -> None:
        cit = Citation(spec_id="GB50015", article="4.4.1", title="管径规定")
        assert cit.source_id == "GB50015 4.4.1"

    def test_defaults(self) -> None:
        cit = Citation(spec_id="FIX", article="1.1")
        assert cit.title == ""
        assert cit.text_snippet == ""


class TestGeneratedAnswer:
    def test_defaults(self) -> None:
        ans = GeneratedAnswer(answer="测试回答")
        assert ans.citations == []
        assert ans.intent == "rag"

    def test_with_citations(self) -> None:
        cit = Citation(spec_id="FIX", article="1.1")
        ans = GeneratedAnswer(answer="回答", citations=[cit])
        assert len(ans.citations) == 1


class TestTraceEvent:
    def test_minimal(self) -> None:
        e = TraceEvent(run_id="r1", trace_id="t1", node="router", event="enter")
        assert e.node == "router"
        assert isinstance(e.ts, datetime)
        assert e.data == {}

    def test_with_data(self) -> None:
        e = TraceEvent(run_id="r1", trace_id="t1", node="retrieve", event="exit", data={"hits": 3})
        assert e.data["hits"] == 3


class TestEvalCase:
    def test_minimal(self) -> None:
        ec = EvalCase(id="rag-p0-0001", query="测试问题")
        assert ec.type == "rag"
        assert ec.expected_status == "succeeded"
        assert ec.eval == "citation_exists"

    def test_with_gold_articles(self) -> None:
        ec = EvalCase(id="rag-p0-0010", query="给水管道", gold_articles_any=["FIX 1.1.1"])
        assert "FIX 1.1.1" in ec.gold_articles_any

    def test_json_roundtrip(self) -> None:
        ec = EvalCase(id="t1", query="q", gold_articles_any=["FIX 1.1"], forbidden_articles=["FIX 1.2"])
        d = json.loads(ec.model_dump_json())
        assert d["type"] == "rag"
        assert d["forbidden_articles"] == ["FIX 1.2"]


class TestEvalResult:
    def test_defaults(self) -> None:
        r = EvalResult(
            case_id="rag-p0-0001",
            passed=True,
            expected_status="succeeded",
            actual_status="succeeded",
        )
        assert r.actual_citations == []
        assert r.failure_reason == ""
        assert r.latency_ms == 0.0
        assert r.trace_id == ""


class TestEvalReport:
    def test_defaults(self) -> None:
        report = EvalReport(total=0, passed=0, failed=0, pass_rate=0.0)
        assert report.cases == []
        assert report.input_path == ""
        assert report.output_path == ""


class TestWaterAgentState:
    def test_default_state_has_all_keys(self) -> None:
        s = default_state()
        assert isinstance(s, dict)
        assert s["status"] == STATUS_RUNNING
        assert s["messages"] == []
        assert s["context"] == []
        assert s["citations"] == []
        assert s["errors"] == []
        assert s["retry_count"] == 0
        assert s["step_count"] == 0
        assert s["run_id"] == ""
        assert s["trace_id"] == ""
        assert s["citation_ok"] is True

    def test_default_state_override(self) -> None:
        s = default_state(query="给水管道", run_id="my-run")
        assert s["query"] == "给水管道"
        assert s["run_id"] == "my-run"
        assert s["status"] == STATUS_RUNNING  # other defaults remain

    def test_state_keys_subset(self) -> None:
        """Ensure all required P0 fields exist."""
        s = default_state()
        required = {
            "query", "intent", "context", "answer", "citations",
            "errors", "run_id", "trace_id", "retry_count", "step_count",
            "status", "missing_inputs", "assumptions",
        }
        for key in required:
            assert key in s, f"Missing required state key: {key}"


class TestConstants:
    def test_retry_limits_positive(self) -> None:
        assert MAX_TOTAL_STEPS >= 1
        assert MAX_CITATION_RETRIES >= 1
        assert MAX_CITATION_RETRIES < MAX_TOTAL_STEPS  # retries should be ≤ total steps

    def test_status_values(self) -> None:
        assert STATUS_RUNNING == "running"
        assert STATUS_SUCCEEDED == "succeeded"
        assert STATUS_FAILED == "failed"