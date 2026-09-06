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

    @property
    def trace_id(self) -> str | None:
        return None if self._document is None else str(self._document.get("trace_id") or "")

    @property
    def started(self) -> bool:
        """是否已 ``start``（agentic 图用它决定是否在首个节点开启 trace）。"""
        return self._document is not None

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

    def record_llm_call(
        self,
        *,
        node: str = "llm",
        model: str = "",
        provider: str = "",
        prompt_preview: str = "",
        response_preview: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        cost_usd: float = 0.0,
        latency_ms: float = 0.0,
        status: str = "running",
        error: str | None = None,
        query: str | None = None,
    ) -> None:
        """记录一次 LLM 调用事件（Step 10：观测与成本）。

        ``prompt_preview`` / ``response_preview`` 只存摘要（默认截断到 200 字符），
        避免把完整 prompt/响应写入 trace（PII/密钥泄漏风险，见 OTel GenAI 约定）。
        """
        self.record(
            node=node,
            event="llm_call",
            query=query,
            retry_count=0,
            error=error,
            status=status,
            duration_ms=latency_ms,
            data={
                "model": model,
                "provider": provider,
                "prompt_preview": (prompt_preview or "")[:200],
                "response_preview": (response_preview or "")[:200],
                "input_tokens": int(input_tokens),
                "output_tokens": int(output_tokens),
                "total_tokens": int(total_tokens),
                "cost_usd": round(float(cost_usd), 8),
            },
        )

    def record_cache_event(
        self,
        *,
        node: str = "cache",
        cache_status: str,
        cache_key_hash: str = "",
        reason: str = "",
        entry_age_seconds: float | None = None,
        eligible_for_store: bool | None = None,
        saved_input_tokens: int | None = None,
        saved_output_tokens: int | None = None,
        saved_cost_usd: float | None = None,
        prompt_cache_support: str = "",
        prompt_cache_cached_tokens: int | None = None,
        query: str | None = None,
    ) -> None:
        """记录一次缓存裁定（P2.5 Step 13）。

        规格 §5 的落盘纪律：
        - 只写 ``cache_key_hash``，**永不写 cache key 原文**；
        - ``saved_*`` 仅在 provider usage 可得时填写，``None`` 表示不可知
          （**不估算** —— 与 ``UsageRecord.usage_known=False`` 同口径）；
        - ``prompt_cache_cached_tokens=None`` 表示 provider 未返回该字段，
          与"命中 0 token"是不同的事实。
        """
        data: dict[str, Any] = {
            "cache_status": cache_status,
            "cache_key_hash": cache_key_hash,
            "reason": reason,
        }
        if entry_age_seconds is not None:
            data["entry_age_seconds"] = round(float(entry_age_seconds), 6)
        if eligible_for_store is not None:
            data["eligible_for_store"] = bool(eligible_for_store)
        if saved_input_tokens is not None:
            data["saved_input_tokens"] = int(saved_input_tokens)
        if saved_output_tokens is not None:
            data["saved_output_tokens"] = int(saved_output_tokens)
        if saved_cost_usd is not None:
            data["saved_cost_usd"] = round(float(saved_cost_usd), 8)
        if prompt_cache_support:
            data["prompt_cache_support"] = prompt_cache_support
        if prompt_cache_cached_tokens is not None:
            data["prompt_cache_cached_tokens"] = int(prompt_cache_cached_tokens)
        self.record(node=node, event="cache", query=query, data=data)

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
