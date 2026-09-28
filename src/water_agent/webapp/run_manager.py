"""P5 Step 42: authorized Web Agent runs and resumable SSE event logs."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Condition, RLock
from typing import Any, Protocol, cast
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from water_agent.memory.contracts import MemoryError as DomainMemoryError
from water_agent.memory.contracts import MemoryStoreUnavailableError
from water_agent.memory.providers import MemoryProvider
from water_agent.memory.recall import MemoryRecallResult, MemoryRecallService
from water_agent.observability.json_trace import JsonTraceRecorder, resolve_trace_dir
from water_agent.runtime.agentic import RunCancelled, build_agentic_session
from water_agent.runtime.governance.contracts import Principal
from water_agent.storage.checkpoint import postgres_checkpointer
from water_agent.storage.database import Database
from water_agent.storage.repository import (
    ResourceNotFoundError,
    RunRepository,
    WebSessionRepository,
)
from water_agent.storage.scoped_checkpoint import ScopedPostgresSaver

RUN_EVENT_PAYLOAD_VERSION = "v1"
RUN_STATUSES = frozenset({"queued", "running", "succeeded", "failed", "blocked", "cancelled"})
TERMINAL_RUN_STATUSES = frozenset({"succeeded", "failed", "blocked", "cancelled"})
DEFAULT_TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")
MAX_SSE_TEXT = 16_000


class AgentSessionFactory(Protocol):
    def create(
        self,
        *,
        run_id: str,
        thread_id: str,
        user_id: UUID,
        principal: Principal,
        project_id: UUID | None,
        stack: ExitStack,
    ) -> Any: ...


class RunManagerError(RuntimeError):
    """Base class for safe Web run manager failures."""


class RunNotManagedError(RunManagerError):
    """The process has no event log for the requested run."""


@dataclass(frozen=True, slots=True)
class RunEvent:
    event_id: int
    event_type: str
    run_id: str
    thread_id: str
    time: str
    payload: dict[str, Any]
    payload_version: str = RUN_EVENT_PAYLOAD_VERSION

    def envelope(self) -> dict[str, Any]:
        return {
            "event_id": str(self.event_id),
            "type": self.event_type,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "time": self.time,
            "payload_version": self.payload_version,
            "payload": self.payload,
        }

    def sse(self) -> str:
        envelope = json.dumps(self.envelope(), ensure_ascii=False, separators=(",", ":"))
        return f"id: {self.event_id}\nevent: {self.event_type}\ndata: {envelope}\n\n"


@dataclass
class _RunHandle:
    run_id: str
    thread_id: str
    owner_user_id: UUID
    project_id: UUID | None
    condition: Condition = field(default_factory=Condition)
    events: list[RunEvent] = field(default_factory=list)
    status: str = "queued"
    cancel_requested: bool = False
    finalizing: bool = False
    finalized: bool = False
    session: Any | None = None
    seen_tool_calls: set[str] = field(default_factory=set)
    seen_tool_messages: set[str] = field(default_factory=set)
    calculations: list[dict[str, Any]] = field(default_factory=list)
    # P5-S48 导出专用，**不进 SSE**（前端事件契约不变）：最后一次 material_stats 的
    # 材料表，以及与 calc_velocity 结果按 tool_call_id 配对的数值输入参数。
    # 材料表取"最后一次"而不是累加：模型对同一批管段重复调用时，累加就是重复计量。
    material_table: dict[str, Any] | None = None
    calc_inputs: dict[str, float] = field(default_factory=dict)
    tool_call_args: dict[str, dict[str, Any]] = field(default_factory=dict)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_text(value: object, *, limit: int = MAX_SSE_TEXT) -> str:
    text = str(value) if value is not None else ""
    return JsonTraceRecorder._redact(text)[:limit]


def parse_last_event_id(value: str | None) -> int:
    if value in (None, ""):
        return 0
    try:
        cursor = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Last-Event-ID must be a non-negative integer") from exc
    if cursor < 0:
        raise ValueError("Last-Event-ID must be a non-negative integer")
    return cursor


class WebAgenticSessionFactory:
    """Build an authenticated AgenticSession using the shared assembly function."""

    def __init__(
        self,
        database: Database,
        sessions: WebSessionRepository,
        *,
        trace_dir: str | os.PathLike[str] | None = None,
        session_dir: str | os.PathLike[str] = "data/sessions",
        mock_generator: bool | None = None,
        agent_args: argparse.Namespace | None = None,
    ) -> None:
        self.database = database
        self.sessions = sessions
        # None ⇒ 交给 resolve_trace_dir 读 WATER_AGENT_TRACE_DIR；容器里 /app/data 只读，
        # 写死 "data/traces" 会让 recorder.finish() 必然 OSError（见 json_trace 模块文档）
        self.trace_dir = str(resolve_trace_dir(trace_dir))
        self.session_dir = str(session_dir)
        self.mock_generator = (
            os.getenv("WEB_MOCK_GENERATOR", "false").lower() == "true"
            if mock_generator is None
            else mock_generator
        )
        self.agent_args = agent_args

    def _args(self) -> argparse.Namespace:
        if self.agent_args is not None:
            return argparse.Namespace(**vars(self.agent_args))
        from water_agent.generation.local_model import local_model_configured, local_model_settings

        local_settings = local_model_settings() if local_model_configured() else {}
        return argparse.Namespace(
            db_path=None,
            dimensions=None,
            rerank="local",
            rerank_model=None,
            rerank_base_url=None,
            rerank_api_key=None,
            top_k=5,
            recall_top_k=20,
            hybrid_alpha=0.7,
            model=local_settings.get("model", "deepseek-chat"),
            base_url=local_settings.get("base_url", "https://api.deepseek.com"),
            api_key_env="DEEPSEEK_API_KEY",
            api_key=local_settings.get("api_key"),
            mock_generator=self.mock_generator,
            session_dir=self.session_dir,
            trace_dir=self.trace_dir,
            stream=False,
            drawing_root=None,
        )

    def create(
        self,
        *,
        run_id: str,
        thread_id: str,
        user_id: UUID,
        principal: Principal,
        project_id: UUID | None,
        stack: ExitStack,
    ) -> Any:
        saver = stack.enter_context(postgres_checkpointer(self.database))
        scoped_saver = ScopedPostgresSaver(saver, self.sessions, user_id)
        from water_agent.runtime.governance.contracts import RequestContext

        request_context = RequestContext(
            principal=principal,
            request_id=f"req-{run_id[:12]}",
            thread_id=thread_id,
        )
        return build_agentic_session(
            self._args(),
            stack=stack,
            thread_id=thread_id,
            request_context=request_context,
            checkpointer=scoped_saver,
        )


class RunManager:
    """Run state machine plus per-run, replayable in-process event logs.

    PostgreSQL owns the run row and status.  The manager owns the live event
    stream for the process lifetime; every stream request re-checks the
    repository authorization before replaying each event.  This covers client
    disconnect/reconnect without introducing a second queue or event backend.
    """

    def __init__(
        self,
        database: Database,
        *,
        session_factory: AgentSessionFactory | None = None,
        tenant_id: UUID = DEFAULT_TENANT_ID,
        memory_enabled: bool | None = None,
        memory_provider: MemoryProvider | None = None,
        uploads_enabled: bool | None = None,
        trace_dir: str | os.PathLike[str] | None = None,
        session_dir: str | os.PathLike[str] = "data/sessions",
        max_workers: int = 4,
    ) -> None:
        self.database = database
        self.run_repository = RunRepository(database)
        self.sessions = WebSessionRepository(database)
        self.tenant_id = tenant_id
        self.memory_enabled = (
            os.getenv("MEMORY_ENABLED", "true").lower() != "false"
            if memory_enabled is None
            else memory_enabled
        )
        self.memory_provider = memory_provider
        # 特性开关（AGENTS.md「未完成功能用特性开关护住」）：召回层可单独关掉而不用
        # 回退上传能力本身——文件照旧能上传和下载，只是不进提示词。
        self.uploads_enabled = (
            os.getenv("UPLOAD_RECALL_ENABLED", "true").lower() != "false"
            if uploads_enabled is None
            else uploads_enabled
        )
        self.session_factory = session_factory or WebAgenticSessionFactory(
            database,
            self.sessions,
            trace_dir=trace_dir,
            session_dir=session_dir,
        )
        self._handles: dict[str, _RunHandle] = {}
        self._lock = RLock()
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="water-agent-run")

    def start(
        self,
        *,
        run_id: UUID | str,
        thread_id: UUID | str,
        owner_user_id: UUID,
        principal: Principal,
        project_id: UUID | str | None,
        query: str,
    ) -> dict[str, str]:
        run_key = str(run_id)
        handle = _RunHandle(
            run_id=run_key,
            thread_id=str(thread_id),
            owner_user_id=owner_user_id,
            project_id=None if project_id is None else UUID(str(project_id)),
        )
        with self._lock:
            if run_key in self._handles:
                raise ValueError("run already exists")
            self._handles[run_key] = handle
        self._append(handle, "run_queued", {"status": "queued"})
        self._executor.submit(self._execute, handle, principal, query)
        return {"run_id": run_key, "thread_id": str(thread_id), "status": "queued"}

    def _append(self, handle: _RunHandle, event_type: str, payload: dict[str, Any]) -> RunEvent:
        with handle.condition:
            event = RunEvent(
                event_id=len(handle.events) + 1,
                event_type=event_type,
                run_id=handle.run_id,
                thread_id=handle.thread_id,
                time=_now(),
                payload=payload,
            )
            handle.events.append(event)
            handle.condition.notify_all()
            return event

    def _set_running(self, handle: _RunHandle) -> None:
        with handle.condition:
            if handle.finalized:
                return
            handle.status = "running"
        try:
            self.run_repository.set_status(
                handle.run_id,
                handle.owner_user_id,
                "running",
                expected_statuses=("queued",),
            )
        except (ResourceNotFoundError, SQLAlchemyError, OSError):
            self._finish(handle, status="blocked", payload={"error": "run state unavailable"})
            return
        self._append(handle, "run_started", {"status": "running"})

    def _execute(self, handle: _RunHandle, principal: Principal, query: str) -> None:
        self._set_running(handle)
        with handle.condition:
            if handle.finalized:
                return
            if handle.cancel_requested:
                self._finish(handle, status="cancelled", payload={"status": "cancelled"})
                return

        session: Any | None = None
        try:
            with ExitStack() as stack:
                session = self.session_factory.create(
                    run_id=handle.run_id,
                    thread_id=handle.thread_id,
                    user_id=handle.owner_user_id,
                    principal=principal,
                    project_id=handle.project_id,
                    stack=stack,
                )
                handle.session = session
                start_trace = getattr(session, "start_trace", None)
                if callable(start_trace):
                    start_trace(handle.run_id, query)

                memory_context: MemoryRecallResult | None = None
                if self.memory_enabled:
                    try:
                        trace = getattr(session, "recorder", None)
                        memory_context = MemoryRecallService(
                            self.database, trace=trace, provider=self.memory_provider
                        ).recall(
                            tenant_id=self.tenant_id,
                            actor_user_id=handle.owner_user_id,
                            project_id=handle.project_id,
                            query=query,
                            source_thread_id=handle.thread_id,
                            run_id=handle.run_id,
                        )
                        self._append(
                            handle,
                            "memory_recall",
                            {
                                "returned_count": len(memory_context.hits),
                                "candidate_count": memory_context.candidate_count,
                                "omitted_count": memory_context.omitted_count,
                                "character_budget": memory_context.character_budget,
                                "memory_ids": [str(hit.memory.memory_id) for hit in memory_context.hits],
                            },
                        )
                    except MemoryStoreUnavailableError:
                        self._record_trace_event(
                            session,
                            "memory_recall_degraded",
                            {"code": "MEMORY_RECALL_DEGRADED"},
                            status="running",
                        )
                        self._append(
                            handle,
                            "warning",
                            {
                                "code": "MEMORY_RECALL_DEGRADED",
                                "message": "Long-term memory was unavailable; continued without it",
                            },
                        )
                        memory_context = None
                    except DomainMemoryError:
                        self._record_trace_event(
                            session,
                            "memory_recall_blocked",
                            {"code": "MEMORY_POLICY_BLOCKED"},
                            status="blocked",
                        )
                        self._finish(
                            handle,
                            status="blocked",
                            payload={"error": "memory policy blocked this run"},
                        )
                        return
                    except (SQLAlchemyError, OSError, ValueError, TypeError):
                        self._record_trace_event(
                            session,
                            "memory_recall_blocked",
                            {"code": "MEMORY_DATA_UNAVAILABLE"},
                            status="blocked",
                        )
                        self._finish(
                            handle,
                            status="blocked",
                            payload={"error": "memory data could not be authorized"},
                        )
                        return

                upload_context = self._recall_uploads(handle, session, query)
                # 只在确有召回时才传 upload_context：AgentSessionFactory 是协议，现有实现
                # （harness/memory_eval.py 的评测会话、各测试夹具）的 aask 并不认识这个
                # 关键字。无条件传会让它们全部 TypeError → 被下方兜底改写成 run failed，
                # 即一个与上传无关的会话因为本步而静默失败。有召回却不支持时照旧响亮失败。
                extra: dict[str, Any] = (
                    {"upload_context": upload_context} if upload_context is not None else {}
                )

                result = asyncio.run(
                    session.aask(
                        query,
                        run_id=handle.run_id,
                        memory_context=memory_context,
                        on_token=lambda token: self._on_token(handle, token),
                        on_event=lambda mode, payload: self._on_agent_event(handle, mode, payload),
                        cancel_check=lambda: self._is_cancel_requested(handle),
                        **extra,
                    )
                )
            if self._is_cancel_requested(handle):
                self._finish(handle, status="cancelled", payload={"status": "cancelled"})
                return
            status = str(result.get("status") or "failed")
            if status not in RUN_STATUSES or status == "queued" or status == "running":
                status = "failed"
            self._finish(handle, status=status, payload=self._result_payload(result, handle=handle))
        except RunCancelled:
            self._finish(handle, status="cancelled", payload={"status": "cancelled"})
        except SQLAlchemyError:
            self._finish(handle, status="blocked", payload={"error": "run persistence unavailable"})
        except Exception as exc:  # noqa: BLE001 -- worker must convert failures to a terminal run state
            # 异常类型+消息+traceback 只进服务端 trace，不进 SSE：浏览器侧保持既有的
            # 通用文案（不泄漏内部结构），但服务端必须留下"到底炸在哪"的线索。
            import traceback
            self._record_trace_event(
                session,
                "run_failed",
                {
                    "exception_type": type(exc).__name__,
                    "exception_message": _safe_text(str(exc), limit=500),
                    "traceback": _safe_text(traceback.format_exc(), limit=2000),
                },
                status="failed",
                node="run",
            )
            self._finish(handle, status="failed", payload={"error": "agent run failed"})

    def _recall_uploads(self, handle: _RunHandle, session: Any, query: str) -> Any | None:
        """Recall uploaded project material for this query, or degrade loudly.

        Unlike memory recall this has **no blocked branch**: uploaded documents
        are project material the caller is already authorized for, not a
        cross-tenant surface, so an unavailable index is a degradation and not a
        reason to kill an otherwise valid question.  The degradation is still
        reported as a warning event -- silently answering without the document
        the user just uploaded is exactly the confusing case to avoid.
        """

        if not self.uploads_enabled:
            return None
        from water_agent.documents.retrieval import (
            UploadRecallUnavailableError,
            recall_uploads,
        )

        try:
            result = recall_uploads(
                self.database,
                tenant_id=self.tenant_id,
                actor_user_id=handle.owner_user_id,
                project_id=handle.project_id,
                query=query,
            )
        except (UploadRecallUnavailableError, ValueError, TypeError):
            self._record_trace_event(
                session,
                "upload_recall_degraded",
                {"code": "UPLOAD_RECALL_DEGRADED"},
                status="running",
                node="upload",
            )
            self._append(
                handle,
                "warning",
                {
                    "code": "UPLOAD_RECALL_DEGRADED",
                    "message": "Uploaded material could not be searched; continued without it",
                },
            )
            return None
        # 事件只带计数与 upload_id，**不带正文**：SSE 会进浏览器，上传正文属用户资料。
        self._append(
            handle,
            "upload_recall",
            {
                "returned_count": len(result.hits),
                "candidate_count": result.candidate_count,
                "omitted_count": result.omitted_count,
                "character_budget": result.character_budget,
                "upload_ids": result.upload_ids,
                "suspicious_count": sum(1 for hit in result.hits if hit.suspicious),
            },
        )
        return result if result.hits else None

    def _is_cancel_requested(self, handle: _RunHandle) -> bool:
        with handle.condition:
            return handle.cancel_requested

    def _on_token(self, handle: _RunHandle, token: str) -> None:
        self._append(handle, "token", {"text": _safe_text(token, limit=4000)})

    def _on_agent_event(self, handle: _RunHandle, mode: str, payload: Any) -> None:
        if mode == "values" and isinstance(payload, dict):
            self._observe_tool_messages(handle, payload.get("messages"))
            self._append(
                handle,
                "state",
                {
                    "status": str(payload.get("status") or "running"),
                    "citation_count": len(payload.get("citations") or []),
                    "error_count": len(payload.get("errors") or []),
                    "retry_count": int(payload.get("retry_count", 0) or 0),
                },
            )
            return
        if mode == "messages":
            chunk = payload[0] if isinstance(payload, tuple) and payload else payload
            text = getattr(chunk, "content", "")
            tool_calls = getattr(chunk, "tool_calls", None) or getattr(chunk, "tool_call_chunks", None)
            safe: dict[str, Any] = {}
            if isinstance(text, str) and text:
                safe["text"] = _safe_text(text, limit=4000)
            if tool_calls:
                names: list[str] = []
                for item in tool_calls:
                    name = str(item.get("name") or "") if isinstance(item, dict) else str(getattr(item, "name", "") or "")
                    call_id = str(item.get("id") or "") if isinstance(item, dict) else str(getattr(item, "id", "") or "")
                    if not name:
                        continue
                    names.append(name)
                    marker = call_id or name
                    if marker not in handle.seen_tool_calls:
                        handle.seen_tool_calls.add(marker)
                        self._append(
                            handle,
                            "tool_started",
                            {"tool_name": name, "status": "running"},
                        )
                if names:
                    safe["tool_names"] = names
            if safe:
                self._append(handle, "agent_message", safe)

    @staticmethod
    def _result_payload(result: dict[str, Any], *, handle: _RunHandle | None = None) -> dict[str, Any]:
        return {
            "status": str(result.get("status") or "failed"),
            "answer": _safe_text(result.get("answer", "")),
            "citations": [
                {"source_id": str(item.get("source_id") or "")}
                for item in result.get("citations", [])
                if isinstance(item, dict) and item.get("source_id")
            ],
            "errors": [_safe_text(item, limit=1000) for item in result.get("errors", [])],
            "warnings": [_safe_text(item, limit=1000) for item in result.get("warnings", [])],
            "calculations": list(handle.calculations) if handle is not None else [],
        }

    @staticmethod
    def _current_turn(messages: list[Any] | tuple[Any, ...]) -> list[Any]:
        """Messages produced by this run: everything after the last human turn.

        ``values`` events carry the **whole thread** state, which the
        checkpointer restores from previous runs.  Scanning all of it replayed
        turn N's tool cards and calculation results in turn N+1 (and fed them
        into the export snapshot, so a Word calculation book could carry numbers
        from an earlier question).  The current question is the last
        ``HumanMessage``; the runtime-context preamble is also a HumanMessage but
        is always inserted *before* the question, so taking the last one is right.
        """

        for index in range(len(messages) - 1, -1, -1):
            if getattr(messages[index], "type", None) == "human":
                return list(messages[index + 1 :])
        return list(messages)

    def _observe_tool_messages(self, handle: _RunHandle, messages: Any) -> None:
        """Project real ToolMessages into safe UI events.

        The agent graph remains the execution authority.  This observer only
        reads already-produced tool results and never computes or re-runs them.
        """

        if not isinstance(messages, (list, tuple)):
            return
        for message in self._current_turn(messages):
            for call in getattr(message, "tool_calls", None) or []:
                call_id = str(call.get("id") or "") if isinstance(call, dict) else ""
                args = call.get("args") if isinstance(call, dict) else None
                if call_id and isinstance(args, dict):
                    handle.tool_call_args[call_id] = args
            tool_name = getattr(message, "name", None)
            if not isinstance(tool_name, str) or not tool_name:
                continue
            # The citation validator uses a synthetic ToolMessage internally;
            # it is a graph boundary, not a user-callable tool.  When the
            # real session exposes a registry, only registered tools may
            # become a tool card.
            if tool_name == "citation_validator":
                continue
            registry = getattr(handle.session, "registry", None)
            if registry is not None and registry.get(tool_name) is None:
                continue
            tool_call_id = str(getattr(message, "tool_call_id", "") or "")
            marker = tool_call_id or f"{tool_name}:{len(handle.seen_tool_messages)}"
            if marker in handle.seen_tool_messages:
                continue
            handle.seen_tool_messages.add(marker)
            raw_content = getattr(message, "content", None)
            payload = self._tool_payload(raw_content)
            failed = bool(payload.get("error")) or (
                isinstance(raw_content, str) and bool(raw_content.strip()) and not payload
            )
            result_payload: dict[str, Any] = {
                "tool_name": tool_name,
                "status": "failed" if failed else "succeeded",
            }
            if failed and not payload:
                result_payload["code"] = "TOOL_RESULT_INVALID"
            if payload.get("code"):
                result_payload["code"] = _safe_text(payload["code"], limit=120)
            if payload.get("message"):
                result_payload["message"] = _safe_text(payload["message"], limit=500)
            calculation = self._calculation_payload(tool_name, payload)
            if calculation is not None:
                result_payload["calculation"] = calculation
                handle.calculations.append(calculation)
                # 输入只取调用参数里的数值：它们是模型传给程序化工具的原值，
                # 计算书把它们原样列出，不做任何换算。
                for key, value in handle.tool_call_args.get(tool_call_id, {}).items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        handle.calc_inputs[str(key)] = value
            if tool_name == "material_stats" and not failed:
                table = self._material_table(payload)
                if table is not None:
                    handle.material_table = table
            self._append(handle, "tool_result", result_payload)

    @staticmethod
    def _material_table(payload: dict[str, Any]) -> dict[str, Any] | None:
        """Keep the programmatic material table only when every row is well formed.

        A table with one malformed row is dropped whole rather than filtered:
        exporting "most of" a bill of quantities is a wrong bill of quantities.
        """

        rows = payload.get("rows")
        total = payload.get("total_length_m")
        if not isinstance(rows, list) or not rows:
            return None
        if isinstance(total, bool) or not isinstance(total, (int, float)):
            return None
        for row in rows:
            if not isinstance(row, dict) or not row.get("material"):
                return None
            quantity = row.get("quantity")
            if isinstance(quantity, bool) or not isinstance(quantity, (int, float)):
                return None
        return {
            "rows": [dict(row) for row in rows],
            "total_length_m": total,
            "assumptions": [str(item) for item in payload.get("assumptions") or []],
        }

    def export_snapshot(self, run_id: UUID | str) -> dict[str, Any]:
        """Export material for a finalized run of this process (P5-S48 Step 4).

        Same lifetime as :meth:`trace`: the run's result lives only in the
        in-process handle, so a run this process never executed (or executed
        before a restart) raises :class:`RunNotManagedError`.  A run that has not
        finalized yet also raises it -- exporting a half-written answer would
        produce a document that silently disagrees with what the user sees once
        the stream ends.

        Callers must authorize first; this method does no ownership check.
        """

        handle = self._get_handle(run_id)
        with handle.condition:
            if not handle.finalized:
                raise RunNotManagedError("run has not finalized")
            finalize = next(
                (event for event in reversed(handle.events) if event.event_type == "finalize"),
                None,
            )
            payload = dict(finalize.payload) if finalize is not None else {}
            return {
                "run_id": handle.run_id,
                "status": handle.status,
                "answer": str(payload.get("answer") or ""),
                "citations": list(payload.get("citations") or []),
                "calculations": list(handle.calculations),
                "calc_inputs": dict(handle.calc_inputs),
                "material_table": (
                    None if handle.material_table is None else dict(handle.material_table)
                ),
            }

    @staticmethod
    def _tool_payload(content: Any) -> dict[str, Any]:
        if isinstance(content, dict):
            return dict(content)
        if isinstance(content, str):
            try:
                decoded = json.loads(content)
            except (TypeError, ValueError):
                return {}
            return decoded if isinstance(decoded, dict) else {}
        return {}

    @staticmethod
    def _calculation_payload(tool_name: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Expose only the known programmatic calculation result shape."""

        if (
            tool_name != "calc_velocity"
            or isinstance(payload.get("velocity_mps"), bool)
            or not isinstance(payload.get("velocity_mps"), (int, float))
        ):
            return None
        return {
            "tool_name": tool_name,
            "programmatic": True,
            "values": {"velocity_mps": payload["velocity_mps"]},
            "units": {"velocity_mps": "m/s"},
            "assumptions": [
                _safe_text(item, limit=300)
                for item in payload.get("assumptions", [])
                if isinstance(item, str)
            ],
        }

    @staticmethod
    def _record_trace_event(
        session: Any,
        event_type: str,
        data: dict[str, Any],
        *,
        status: str,
        node: str = "memory",
    ) -> None:
        recorder = getattr(session, "recorder", None)
        if recorder is None or not getattr(recorder, "started", False):
            return
        try:
            recorder.record(node=node, event=event_type, query="", status=status, data=data)
        except Exception:  # noqa: BLE001 -- telemetry cannot alter run disposition
            return

    def _finish(self, handle: _RunHandle, *, status: str, payload: dict[str, Any]) -> None:
        if status not in TERMINAL_RUN_STATUSES:
            status = "failed"
        with handle.condition:
            if handle.finalized or handle.finalizing:
                return
            handle.finalizing = True
            handle.status = status
        try:
            self.run_repository.set_status(
                handle.run_id,
                handle.owner_user_id,
                status,
                expected_statuses=("queued", "running"),
            )
        except (ResourceNotFoundError, SQLAlchemyError, OSError):
            status = "blocked"
            with handle.condition:
                handle.status = status
            payload = {"status": status, "error": "run state persistence unavailable"}
        self._finish_trace(handle, status, payload.get("error"))
        final_payload = {"status": status, **payload}
        self._append(handle, "finalize", final_payload)
        with handle.condition:
            handle.finalizing = False
            handle.finalized = True
            handle.condition.notify_all()

    @staticmethod
    def _finish_trace(handle: _RunHandle, status: str, error: object | None) -> None:
        recorder = getattr(handle.session, "recorder", None)
        if recorder is None or not getattr(recorder, "started", False):
            return
        if getattr(recorder, "finished", False):
            return
        try:
            recorder.finish(status, error=None if error is None else _safe_text(error, limit=1000))
        except Exception:  # noqa: BLE001 -- trace failure must not duplicate finalization
            return

    def cancel(self, run_id: UUID | str, user_id: UUID) -> dict[str, str]:
        self.run_repository.get_owned_for_user(run_id, user_id)
        handle = self._get_handle(run_id)
        with handle.condition:
            if handle.finalized or handle.finalizing:
                return {"run_id": handle.run_id, "status": handle.status}
            handle.cancel_requested = True
            current = handle.status
        self._append(handle, "cancel_requested", {"status": "cancellation_requested"})
        return {"run_id": handle.run_id, "status": current if current == "cancelled" else "cancellation_requested"}

    def _get_handle(self, run_id: UUID | str) -> _RunHandle:
        with self._lock:
            handle = self._handles.get(str(run_id))
        if handle is None:
            raise RunNotManagedError("run event log not found")
        return handle

    def has_event_log(self, run_id: UUID | str) -> bool:
        """Return whether this process can serve the run's replay log."""

        self._get_handle(run_id)
        return True

    def get_visible_run(self, run_id: UUID | str, user_id: UUID) -> Any:
        return self.run_repository.get_for_user(run_id, user_id)

    def stream(
        self,
        run_id: UUID | str,
        *,
        last_event_id: str | None,
        is_authorized: Callable[[], bool],
    ):
        cursor = parse_last_event_id(last_event_id)
        # stream_end 是否该发，只取决于客户端**连上时**的游标：连上时就已越过
        # finalize 的重连不该再收到任何帧（test_web_sse 的空尾断言）。
        # 不能用"发完 pending 之后的 cursor"判断——finalize 恒为最后一条事件，
        # 发完之后 cursor 必然越过它，stream_end 于是永远不发（review 09-28 #4）。
        connect_cursor = cursor
        handle = self._get_handle(run_id)
        while True:
            if not self._authorization_check(is_authorized):
                return
            with handle.condition:
                pending = [event for event in handle.events if event.event_id > cursor]
                terminal = handle.finalized
                if not pending and not terminal:
                    handle.condition.wait(timeout=0.25)
                    continue
            for event in pending:
                if not self._authorization_check(is_authorized):
                    return
                cursor = event.event_id
                yield event.sse()
            if terminal:
                # 发送显式结束标记，让前端区分"正常结束"与"异常断开"。
                # 只发给尚未看到 finalize 的客户端：cursor 已越过 finalize 的重连
                # 不该再收到任何帧（语义与 tests/test_web_sse.py 的空尾断言一致）。
                finalize_seen_before_connect = any(
                    event.event_type == "finalize" and event.event_id <= connect_cursor
                    for event in handle.events
                )
                if not finalize_seen_before_connect:
                    yield RunEvent(
                        event_id=cursor + 1,
                        event_type="stream_end",
                        run_id=handle.run_id,
                        thread_id=handle.thread_id,
                        time=_now(),
                        payload={"status": handle.status},
                    ).sse()
                return

    @staticmethod
    def _authorization_check(is_authorized: Callable[[], bool]) -> bool:
        """Retry only transient DB-lock reads; authorization remains fail closed."""

        for attempt in range(3):
            try:
                return bool(is_authorized())
            except (SQLAlchemyError, OSError):
                if attempt == 2:
                    return False
                time.sleep(0.01)
        return False

    def trace(self, run_id: UUID | str) -> dict[str, Any]:
        handle = self._get_handle(run_id)
        recorder = getattr(handle.session, "recorder", None)
        snapshot = recorder.snapshot() if recorder is not None else None
        if snapshot is not None:
            return self._trace_projection(cast(dict[str, Any], snapshot))
        with handle.condition:
            return {
                "run_id": handle.run_id,
                "status": handle.status,
                "events": [],
            }

    @staticmethod
    def _trace_projection(snapshot: dict[str, Any]) -> dict[str, Any]:
        """Return the browser-safe trace view; omit query/context正文 by default."""

        projected_events: list[dict[str, Any]] = []
        for event in snapshot.get("events", []):
            if not isinstance(event, dict):
                continue
            projected: dict[str, Any] = {
                key: event[key]
                for key in (
                    "event_id",
                    "event_type",
                    "node_name",
                    "timestamp",
                    "duration_ms",
                    "retry_count",
                    "status",
                )
                if key in event
            }
            if event.get("error"):
                projected["error"] = _safe_text(event["error"], limit=1000)
            data = event.get("data")
            if isinstance(data, dict):
                projected["data"] = {
                    key: RunManager._trace_value(value)
                    for key, value in data.items()
                    if key not in {"query", "prompt", "messages", "content"}
                }
            citations = event.get("citations")
            if isinstance(citations, list):
                source_ids = [
                    str(item.get("source_id"))
                    for item in citations
                    if isinstance(item, dict) and item.get("source_id")
                ]
                if source_ids:
                    projected["citation_source_ids"] = source_ids
            context = event.get("retrieved_context")
            if isinstance(context, list):
                projected["retrieved_count"] = len(context)
            projected_events.append(projected)
        return {
            "run_id": snapshot.get("run_id", ""),
            "trace_id": snapshot.get("trace_id", ""),
            "status": snapshot.get("status", ""),
            "started_at": snapshot.get("started_at"),
            "finished_at": snapshot.get("finished_at"),
            "events": projected_events,
        }

    @staticmethod
    def _trace_value(value: Any) -> Any:
        if isinstance(value, str):
            return _safe_text(value, limit=1000)
        if isinstance(value, list):
            return [RunManager._trace_value(item) for item in value[:40]]
        if isinstance(value, dict):
            return {
                str(key): RunManager._trace_value(item)
                for key, item in list(value.items())[:40]
                if key not in {"content", "query", "prompt"}
            }
        return value

    def close(self, *, timeout_seconds: float = 20.0) -> dict[str, Any]:
        """Stop accepting work and wait a bounded time for in-flight runs (Step 45).

        The previous implementation called ``shutdown(wait=False)`` and returned
        immediately, so a SIGTERM during a run dropped it without ever writing a
        terminal event.  Every live run is now asked to cancel first (the agent
        loop observes ``cancel_check`` cooperatively), then the manager waits up
        to ``timeout_seconds`` for them to finalize.  Runs still alive at the
        deadline are reported, never silently dropped.
        """

        with self._lock:
            handles = list(self._handles.values())
        for handle in handles:
            with handle.condition:
                if not handle.finalized and not handle.finalizing:
                    handle.cancel_requested = True
                    handle.condition.notify_all()
        self._executor.shutdown(wait=False, cancel_futures=False)
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while time.monotonic() < deadline:
            if all(handle.finalized for handle in handles):
                break
            time.sleep(0.05)
        unfinalized = [handle.run_id for handle in handles if not handle.finalized]
        return {
            "runs": len(handles),
            "finalized": len(handles) - len(unfinalized),
            "unfinalized_run_ids": unfinalized,
            "graceful": not unfinalized,
        }


__all__ = [
    "DEFAULT_TENANT_ID",
    "AgentSessionFactory",
    "RunCancelled",
    "RunEvent",
    "RunManager",
    "RunManagerError",
    "RunNotManagedError",
    "WebAgenticSessionFactory",
    "parse_last_event_id",
]
