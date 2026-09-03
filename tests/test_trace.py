"""Tests for local JSON trace recording and graph integration."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from water_agent.domain.models import TraceEvent
from water_agent.domain.state import STATUS_FAILED, STATUS_SUCCEEDED, default_state
from water_agent.generation.mock import MockGenerator
from water_agent.graphs.rag import build_rag_graph
from water_agent.observability.json_trace import JsonTraceRecorder
from water_agent.retrieval.in_memory import InMemoryRetriever
from water_agent.retrieval.loader import load_clauses_jsonl

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "data" / "fixtures" / "rag_fixture.jsonl"


@pytest.fixture
def clauses():
    return load_clauses_jsonl(FIXTURE_PATH)


@pytest.fixture
def recorder(tmp_path: Path) -> JsonTraceRecorder:
    return JsonTraceRecorder(tmp_path / "traces")


@pytest.fixture
def graph(recorder: JsonTraceRecorder, clauses):
    return build_rag_graph(InMemoryRetriever(clauses), MockGenerator(), trace=recorder)


def read_trace(recorder: JsonTraceRecorder) -> dict:
    files = list(recorder.output_dir.glob("*.json"))
    assert len(files) == 1
    return json.loads(files[0].read_text(encoding="utf-8"))


class TestJsonTraceRecorder:
    def test_event_is_structured(self) -> None:
        event = TraceEvent(run_id="run-1", trace_id="trace-1", node="router", event="enter")
        assert event.model_dump(mode="json")["run_id"] == "run-1"

    def test_flush_writes_valid_json(self, recorder: JsonTraceRecorder) -> None:
        recorder.start(run_id="run-1", trace_id="trace-1", query="测试")
        recorder.record(node="router", event="enter", status="running")
        path = recorder.finish(status="succeeded")
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["run_id"] == "run-1"
        assert document["trace_id"] == "trace-1"
        assert document["events"]
        assert document["status"] == "succeeded"

    def test_write_failure_is_structured(self, tmp_path: Path) -> None:
        recorder = JsonTraceRecorder(tmp_path / "traces")
        recorder.start(run_id="run-1", trace_id="trace-1", query="测试")
        recorder.output_dir = tmp_path / "a-file"
        recorder.output_dir.write_text("not a directory", encoding="utf-8")
        with pytest.raises(OSError, match="trace write failed"):
            recorder.finish(status="failed")


class TestGraphTrace:
    def test_success_trace_has_required_fields(self, graph, recorder: JsonTraceRecorder) -> None:
        result = graph.invoke(default_state(query="给水管道的设计秒流量"))
        assert result["status"] == STATUS_SUCCEEDED
        document = read_trace(recorder)
        required = {"run_id", "trace_id", "events", "query", "status", "started_at", "finished_at"}
        assert required <= document.keys()
        event_types = {(e["node_name"], e["event_type"]) for e in document["events"]}
        assert ("router", "node_enter") in event_types
        assert ("router", "node_exit") in event_types
        assert ("retrieve", "retrieve_complete") in event_types
        assert ("generate", "generate_complete") in event_types
        assert ("citation_check", "citation_check_complete") in event_types
        assert ("graph", "run_start") in event_types
        assert ("graph", "run_end") in event_types

        retrieve_events = [e for e in document["events"] if e["event_type"] == "retrieve_complete"]
        assert retrieve_events[0]["retrieved_context"]
        citation_events = [e for e in document["events"] if e["event_type"] == "citation_check_complete"]
        assert citation_events[0]["citations"]

    def test_failed_trace_records_error(self, recorder: JsonTraceRecorder, clauses) -> None:
        app = build_rag_graph(InMemoryRetriever(clauses), MockGenerator(), trace=recorder)
        result = app.invoke(default_state(query="量子纠缠与宇宙常数"))
        assert result["status"] == STATUS_FAILED
        document = read_trace(recorder)
        assert document["status"] == "failed"
        assert any(e["event_type"] == "error" for e in document["events"])
        assert any(e["error"] for e in document["events"] if e["event_type"] == "error")

    def test_retry_event_is_recorded(self, recorder: JsonTraceRecorder, clauses) -> None:
        from water_agent.domain.models import Citation, GeneratedAnswer
        from water_agent.generation.base import Generator

        class BadCitationGenerator(Generator):
            def generate(self, query: str, context):
                return GeneratedAnswer(
                    answer="包含编造引用的回答。",
                    citations=[Citation(spec_id="FAKE", article="99.99")],
                )

        app = build_rag_graph(InMemoryRetriever(clauses), BadCitationGenerator(), trace=recorder)
        result = app.invoke(default_state(query="给水管道的设计秒流量"))
        assert result["status"] == STATUS_FAILED
        document = read_trace(recorder)
        assert any(e["event_type"] == "retry" for e in document["events"])
        assert all("retry_count" in e for e in document["events"])

    def test_multiple_runs_do_not_overwrite(self, clauses, tmp_path: Path) -> None:
        recorder = JsonTraceRecorder(tmp_path / "traces")
        app = build_rag_graph(InMemoryRetriever(clauses), MockGenerator(), trace=recorder)
        app.invoke(default_state(query="给水管道"))
        app.invoke(default_state(query="排水管道坡度"))
        assert len(list((tmp_path / "traces").glob("*.json"))) == 2

    def test_trace_schema_fields_are_present_on_events(self, graph, recorder: JsonTraceRecorder) -> None:
        graph.invoke(default_state(query="水泵扬程"))
        document = read_trace(recorder)
        event_fields = {
            "run_id", "trace_id", "event_id", "event_type", "node_name", "timestamp",
            "duration_ms", "query", "retrieved_context", "citations", "retry_count", "error", "status",
        }
        assert all(event_fields <= event.keys() for event in document["events"])


def test_legacy_trace_records_llm_call_event(tmp_path: Path, clauses) -> None:
    """Step 10 观测：generate 节点每次生成写一条 llm_call（mock 生成器记 0 token、provider=mock）。"""
    from water_agent.domain.state import default_state
    from water_agent.generation.mock import MockGenerator
    from water_agent.graphs.rag import build_rag_graph
    from water_agent.retrieval.in_memory import InMemoryRetriever

    recorder = JsonTraceRecorder(tmp_path)
    app = build_rag_graph(InMemoryRetriever(clauses), MockGenerator(), trace=recorder)
    result = app.invoke(default_state(query="给水管道的设计秒流量", run_id="r", trace_id="t"))
    document = json.loads((tmp_path / f"{result['trace_id']}.json").read_text(encoding="utf-8"))
    llm_calls = [e for e in document["events"] if e["event_type"] == "llm_call"]
    assert len(llm_calls) == 1
    data = llm_calls[0]["data"]
    assert data["provider"] == "mock" and data["model"] == "MockGenerator"
    assert data["input_tokens"] == 0 and data["cost_usd"] == 0.0
    assert data["response_preview"] and llm_calls[0]["duration_ms"] >= 0
