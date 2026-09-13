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
    def _redact(value: str) -> str:
        """落盘前脱敏（P2.5 Step 14）。

        规格 §6：trace 里不得留 PII 与凭据原文。**无条件执行**，不看 `pii_mode` ——
        `pii_mode=allow` 只授权 PII 进本次上下文，磁盘长期留存是另一件事。

        导入放在函数内：`observability` 是底层模块，模块级导入治理层会形成环
        （治理层的 gateway 需要 trace）。脱敏本身是纯函数，无状态开销。
        """
        from water_agent.runtime.governance.rules import redact_text

        return redact_text(value)[0]

    @staticmethod
    def _serialize(value: Any) -> Any:
        if isinstance(value, (RetrievedClause, Citation, CitationClaim)):
            dumped = value.model_dump(mode="json") | {"source_id": value.source_id}
            return JsonTraceRecorder._serialize(dumped)
        if isinstance(value, list):
            return [JsonTraceRecorder._serialize(item) for item in value]
        if isinstance(value, dict):
            return {key: JsonTraceRecorder._serialize(item) for key, item in value.items()}
        if isinstance(value, str):
            return JsonTraceRecorder._redact(value)
        return value

    def start(self, run_id: str, trace_id: str, query: str) -> None:
        self._started_monotonic = time.perf_counter()
        self._node_starts = {}
        self._document = {
            "run_id": run_id,
            "trace_id": trace_id,
            "query": self._redact(query),
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
            "query": self._redact(
                query if query is not None else str(self._document["query"])
            ),
            "retrieved_context": self._serialize(context or []),
            "citations": self._serialize(citations or []),
            "retry_count": retry_count,
            "error": None if error is None else self._redact(error),
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
                "prompt_preview": self._redact((prompt_preview or "")[:200]),
                "response_preview": self._redact((response_preview or "")[:200]),
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

    def record_tool_event(
        self,
        *,
        node: str = "tool",
        tool_name: str,
        status: str,
        code: str | None = None,
        invocation_id: str = "",
        args_hash: str = "",
        executed: bool = False,
        duration_ms: float = 0.0,
    ) -> None:
        """记录一次工具调用裁定与执行（P2.5 Step 15）。

        存在的理由是**可观测化 Step 14 的第 6 项缺口**：Step 14 保证了
        "护栏在 execute 之前"，但 trace 里没有工具事件，所以无法事后证明
        某次执行**确实**经过了护栏。本事件与 :meth:`record_guardrail_event`
        共享 ``invocation_id``，`guardrail_bypass` 检测器据此做精确配对
        （而不是靠时间相邻猜测）。

        ``executed`` 是关键字段：``False`` 表示被护栏/schema 拦在执行之前，
        ``True`` 表示 ``spec.execute`` 真的跑了。检测器只对 ``executed=True``
        且缺少配对护栏事件的情况告警 —— 被拦住的调用本来就不该有执行记录。

        不写参数原文，只写 ``args_hash``：工具参数可能含 PII 或注入载荷。
        """
        data: dict[str, Any] = {
            "tool_name": tool_name,
            "tool_status": status,
            "executed": bool(executed),
        }
        if code:
            data["code"] = code
        if invocation_id:
            data["invocation_id"] = invocation_id
        if args_hash:
            data["args_hash"] = args_hash
        # query 显式传空串：工具事件不承载用户输入，只留 args_hash
        self.record(
            node=node, event="tool", query="", data=data, duration_ms=duration_ms
        )

    def record_guardrail_event(
        self,
        *,
        node: str = "guardrail",
        stage: str,
        action: str,
        rule_id: str = "",
        ruleset_version: str = "",
        code: str | None = None,
        hits: _Seq[dict[str, Any]] | None = None,
        input_hash: str = "",
        invocation_id: str = "",
        duration_ms: float = 0.0,
    ) -> None:
        """记录一次护栏裁定（P2.5 Step 14）。

        规格 §6 的落盘纪律：**只写输入 hash、规则 ID、动作、耗时与脱敏摘要**，
        绝不写攻击原文 —— 攻击载荷本身就是注入内容，永久留存等于给后续读取者
        埋一颗定时炸弹（trace 会被人工与工具读取）。

        ``hits`` 只接受 ``{"rule_id", "category", "count"}`` 三键的摘要
        （即 ``GuardrailOutcome.trace_payload()["hits"]`` 的形态），
        多余键一律丢弃 —— 不给"顺手把命中文本塞进来"留缝。

        Args:
            stage: 护栏阶段（``input`` / ``retrieved`` / ``tool`` / ``output`` / ``memory``）。
            action: ``allow`` / ``redact`` / ``block``。
            input_hash: 输入内容哈希（不是原文），用于跨事件关联同一输入。
            invocation_id: 与 :meth:`record_tool_event` 配对的调用 ID（Step 15）。
                `guardrail_bypass` 检测器靠它精确配对，不靠事件时间相邻猜测。
        """
        allowed_hit_keys = ("rule_id", "category", "count")
        data: dict[str, Any] = {
            "stage": stage,
            "action": action,
            "rule_id": rule_id,
            "ruleset_version": ruleset_version,
            "hits": [
                {key: hit[key] for key in allowed_hit_keys if key in hit}
                for hit in (hits or [])
            ],
        }
        if code:
            data["code"] = code
        if input_hash:
            data["input_hash"] = input_hash
        if invocation_id:
            data["invocation_id"] = invocation_id
        # query 显式传空串：护栏事件不重复承载输入内容，只留 input_hash
        self.record(
            node=node, event="guardrail", query="", data=data, duration_ms=duration_ms
        )

    def record_memory_write(
        self,
        *,
        action: str,
        status: str,
        memory_id: str = "",
        content_hash: str = "",
        kind: str = "",
        scope_type: str = "",
        revision: int | None = None,
        ruleset_version: str = "",
        rule_ids: _Seq[str] = (),
        code: str | None = None,
        project_id_hash: str = "",
    ) -> None:
        """Record a P5 memory mutation using a fixed metadata-only schema.

        The method deliberately has no content, semantic-key, actor, reason, or
        free-form data parameter.  Callers therefore cannot accidentally copy
        a password, token, PII value, or complete memory body into the trace.
        """

        data: dict[str, Any] = {
            "action": action,
            "status": status,
            "content_hash": content_hash,
            "rule_ids": sorted({str(rule_id) for rule_id in rule_ids}),
        }
        if memory_id:
            data["memory_id"] = memory_id
        if kind:
            data["kind"] = kind
        if scope_type:
            data["scope_type"] = scope_type
        if revision is not None:
            data["revision"] = int(revision)
        if ruleset_version:
            data["ruleset_version"] = ruleset_version
        if code:
            data["code"] = code
        if project_id_hash:
            data["project_id_hash"] = project_id_hash
        self.record(node="memory", event="memory_write", query="", data=data)

    def record_memory_recall(
        self,
        *,
        query_hash: str,
        candidate_count: int,
        returned_count: int,
        omitted_count: int,
        used_characters: int,
        character_budget: int,
        item_limit: int,
        hits: _Seq[dict[str, Any]] = (),
        reason: str | None = None,
    ) -> None:
        """Record bounded recall metadata without copying memory正文.

        The fixed hit shape is intentionally narrower than ``record``'s
        general-purpose data argument: recall trace consumers get IDs, hashes,
        scores and budget facts, never the memory text or query itself.
        """

        safe_hits = []
        for hit in hits:
            safe_hits.append(
                {
                    "memory_id": str(hit.get("memory_id", "")),
                    "kind": str(hit.get("kind", "")),
                    "scope": str(hit.get("scope", "")),
                    "content_hash": str(hit.get("content_hash", "")),
                    "score": hit.get("score"),
                    "injected": bool(hit.get("injected", False)),
                }
            )
        data: dict[str, Any] = {
            "query_hash": query_hash,
            "candidate_count": int(candidate_count),
            "returned_count": int(returned_count),
            "omitted_count": int(omitted_count),
            "used_characters": int(used_characters),
            "character_budget": int(character_budget),
            "item_limit": int(item_limit),
            "hits": safe_hits,
        }
        if reason:
            data["reason"] = reason
        self.record(node="memory", event="memory_recall", query="", data=data)

    def record_provider_event(self, record: Any) -> None:
        """记录一次 provider 治理事件（P2.5 Step 15 review 修复：gateway → trace 接线）。

        由 ``ProviderGateway.on_usage`` 回调调用。``UsageRecord`` 只含
        ``principal_hash``（非 subject）、模型、分类、token/cost 与结果状态，
        **不含 API Key / 原始输入 / PII**。

        事件类型由 ``result_status`` 映射：
        - ``ok`` / ``USAGE_UNKNOWN`` → ``usage``（usage_known=False 时 token 为 None，不写成 0）；
        - ``RATE_LIMITED`` → ``rate_limit``；``BUDGET_EXCEEDED`` → ``budget``；
        - ``CIRCUIT_OPEN`` → ``breaker``；其余失败 → ``failure``。

        观测失败绝不影响主流程（gateway 的 ``_emit`` 已兜底）。
        """
        try:
            data = record.model_dump(mode="json") if hasattr(record, "model_dump") else dict(record)
        except Exception:  # noqa: BLE001
            return
        result_status = str(data.get("result_status", "ok") or "ok")
        if result_status in ("ok", "USAGE_UNKNOWN"):
            event_type = "usage"
        elif result_status == "RATE_LIMITED":
            event_type = "rate_limit"
        elif result_status == "BUDGET_EXCEEDED":
            event_type = "budget"
        elif result_status == "CIRCUIT_OPEN":
            event_type = "breaker"
        else:
            event_type = "failure"
        self.record(
            node="provider",
            event=event_type,
            query="",
            duration_ms=float(data.get("latency_ms", 0.0) or 0.0),
            data=data,
        )

    @staticmethod
    def hash_input(text: str) -> str:
        """输入内容的稳定哈希，供 ``record_guardrail_event(input_hash=...)`` 使用。

        `skill-guardrail-patterns` 的监控要求：记录 input hash 而非明文。
        """
        import hashlib

        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

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
