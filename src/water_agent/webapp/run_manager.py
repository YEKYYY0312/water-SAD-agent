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
from water_agent.memory.recall import MemoryRecallResult, MemoryRecallService
from water_agent.observability.json_trace import JsonTraceRecorder
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
        trace_dir: str | os.PathLike[str] = "data/traces",
        session_dir: str | os.PathLike[str] = "data/sessions",
        mock_generator: bool | None = None,
        agent_args: argparse.Namespace | None = None,
    ) -> None:
        self.database = database
        self.sessions = sessions
        self.trace_dir = str(trace_dir)
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
            model="deepseek-chat",
            base_url="https://api.deepseek.com",
            api_key_env="DEEPSEEK_API_KEY",
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
        trace_dir: str | os.PathLike[str] = "data/traces",
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
                            self.database, trace=trace
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

                result = asyncio.run(
                    session.aask(
                        query,
                        run_id=handle.run_id,
                        project_id=handle.project_id,
                        memory_context=memory_context,
                        on_token=lambda token: self._on_token(handle, token),
                        on_event=lambda mode, payload: self._on_agent_event(handle, mode, payload),
                        cancel_check=lambda: self._is_cancel_requested(handle),
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
        except Exception:  # noqa: BLE001 -- worker must convert failures to a terminal run state
            self._finish(handle, status="failed", payload={"error": "agent run failed"})

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

    def _observe_tool_messages(self, handle: _RunHandle, messages: Any) -> None:
        """Project real ToolMessages into safe UI events.

        The agent graph remains the execution authority.  This observer only
        reads already-produced tool results and never computes or re-runs them.
        """

        if not isinstance(messages, (list, tuple)):
            return
        for message in messages:
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
            self._append(handle, "tool_result", result_payload)

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
    ) -> None:
        recorder = getattr(session, "recorder", None)
        if recorder is None or not getattr(recorder, "started", False):
            return
        try:
            recorder.record(node="memory", event=event_type, query="", status=status, data=data)
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

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=False)


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
