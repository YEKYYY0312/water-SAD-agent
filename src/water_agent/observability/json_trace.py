"""将 RAG 运行事件持久化为独立 JSON 文件。"""
from __future__ import annotations

import json
import time
from collections.abc import Sequence as _Seq
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from water_agent.domain.citation import CitationClaim
from water_agent.domain.models import Citation, RetrievedClause


class JsonTraceRecorder:
    """进程内收集一次运行事件，并在 finish 时原子写入独立 JSON 文件。"""

    def __init__(self, output_dir: str | Path = "data/traces") -> None:
        self.output_dir = Path(output_dir)
        self._document: dict[str, Any] | None = None
        self._started_monotonic = 0.0
        self._node_starts: dict[str, float] = {}

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _serialize(value: Any) -> Any:
        if isinstance(value, (RetrievedClause, Citation, CitationClaim)):
            return value.model_dump(mode="json") | {"source_id": value.source_id}
        if isinstance(value, list):
            return [JsonTraceRecorder._serialize(item) for item in value]
        if isinstance(value, dict):
            return {key: JsonTraceRecorder._serialize(item) for key, item in value.items()}
        return value

    def start(self, run_id: str, trace_id: str, query: str) -> None:
        self._started_monotonic = time.perf_counter()
        self._node_starts = {}
        self._document = {
            "run_id": run_id,
            "trace_id": trace_id,
            "query": query,
            "started_at": self._now(),
            "finished_at": None,
            "status": "running",
            "events": [],
        }
        self.record(node="graph", event="run_start", query=query, status="running")

    def record(
        self,
        node: str,
        event: str,
        *,
        query: str | None = None,
        context: list[RetrievedClause] | None = None,
        citations: _Seq[Citation | CitationClaim] | None = None,
        retry_count: int = 0,
        error: str | None = None,
        status: str = "running",
        duration_ms: float = 0.0,
        data: dict[str, Any] | None = None,
    ) -> None:
        if self._document is None:
            raise RuntimeError("trace recorder has not been started")
        event_id = uuid4().hex
        event_data = {
            "run_id": self._document["run_id"],
            "trace_id": self._document["trace_id"],
            "event_id": event_id,
            "event_type": event,
            "node_name": node,
            "timestamp": self._now(),
            "duration_ms": round(duration_ms, 3),
            "query": query if query is not None else self._document["query"],
            "retrieved_context": self._serialize(context or []),
            "citations": self._serialize(citations or []),
            "retry_count": retry_count,
            "error": error,
            "status": status,
        }
        if data:
            event_data["data"] = self._serialize(data)
        self._document["events"].append(event_data)

    def node_enter(self, node: str, **kwargs: Any) -> None:
        self._node_starts[node] = time.perf_counter()
        self.record(node=node, event="node_enter", **kwargs)

    def node_exit(self, node: str, **kwargs: Any) -> None:
        started = self._node_starts.pop(node, time.perf_counter())
        kwargs["duration_ms"] = (time.perf_counter() - started) * 1000
        self.record(node=node, event="node_exit", **kwargs)

    def finish(self, status: str, *, error: str | None = None, **kwargs: Any) -> Path:
        if self._document is None:
            raise RuntimeError("trace recorder has not been started")
        self.record(
            node="graph",
            event="run_end",
            status=status,
            error=error,
            duration_ms=(time.perf_counter() - self._started_monotonic) * 1000,
            **kwargs,
        )
        self._document["finished_at"] = self._now()
        self._document["status"] = status
        if error:
            self._document["error"] = error
        target = self.output_dir / f"{self._document['trace_id']}.json"
        temporary = target.with_suffix(".json.tmp")
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(self._document, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(target)
        except (OSError, TypeError, ValueError) as exc:
            raise OSError(f"trace write failed: {exc}") from exc
        return target

    @property
    def output_dir(self) -> Path:
        return self._output_dir

    @output_dir.setter
    def output_dir(self, value: str | Path) -> None:
        self._output_dir = Path(value)
