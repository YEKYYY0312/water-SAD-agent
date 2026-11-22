"""Trusted Web adapter. Repositories authorize every boundary; model messages are never inputs."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from water_agent.multi_agent.contracts import compute_payload_hash
from water_agent.multi_agent.project_calculation_contracts import (
    ProjectCalculationPlan,
    ProjectExecutionContext,
)
from water_agent.multi_agent.project_calculation_service import Authorization, CalculationService
from water_agent.multi_agent.project_publication import DocumentBackend, ProjectPublisher
from water_agent.runtime.governance.contracts import Capability
from water_agent.storage.models import Run
from water_agent.storage.repository import ProjectRepository, ResourceNotFoundError
from water_agent.webapp.auth.db_store import PostgresUserStore
from water_agent.webapp.auth.dependencies import _build_principal
from water_agent.webapp.auth.models import UserRole


class WebProjectAuthority:
    def __init__(self, runtime):
        self.runtime = runtime

    def query(self, ctx: ProjectExecutionContext) -> Authorization:
        runtime = self.runtime
        _, principal, session = runtime.authorize_session(ctx.owner_user_id, ctx.thread_id)
        run = runtime.manager.run_repository.get_owned_for_user(ctx.run_id, ctx.owner_user_id)
        if str(run.thread_id) != ctx.thread_id or str(session.project_id) != ctx.project_id:
            raise ValueError("PROJECT_SCOPE_MISMATCH")
        # Epoch is a local identity configuration version, never a substitute for repository checks.
        return Authorization(principal, 1)


class ProjectCalculationRuntime:
    def __init__(self, manager, root: Path):
        self.manager = manager
        self.documents = DocumentBackend(root / "documents")
        self.service = CalculationService(
            root / "checkpoints", WebProjectAuthority(self), document_backend=self.documents
        )
        self.publisher = ProjectPublisher(
            self.service, manager.export_store, self.documents, set_succeeded=self._succeeded
        )

    def authorize_session(self, owner, thread):
        user = PostgresUserStore(self.manager.database).get_by_id(UUID(str(owner)))
        session = self.manager.sessions.get_for_user(thread, owner)
        if (
            not user.is_active
            or user.must_change_password
            or str(session.owner_user_id) != str(owner)
            or session.project_id is None
        ):
            raise ResourceNotFoundError("project scope unavailable")
        role = ProjectRepository(self.manager.database).membership_role(session.project_id, owner)
        if user.role is UserRole.VIEWER or role not in ("owner", "member"):
            raise PermissionError("project write permission required")
        principal = _build_principal(user).model_copy(
            update={"capabilities": frozenset({Capability.CALCULATE})}
        )
        return user, principal, session

    def selector(self, owner, thread, run_id=None) -> ProjectExecutionContext:
        _, principal, session = self.authorize_session(owner, thread)
        return ProjectExecutionContext(
            owner_user_id=str(owner),
            thread_id=str(thread),
            run_id=str(run_id or uuid4()),
            project_id=str(session.project_id),
            plan_id="project-calculation",
            branch_id="main",
            plan_revision=1,
            generation=1,
            authorization_epoch=1,
            checkpoint_namespace="t27-v1",
            principal=principal,
            cancel_check=lambda: False,
        )

    def _row_identity(self, run_id):
        with self.manager.database.transaction() as session:
            row = session.get(Run, str(UUID(str(run_id))))
            if row is None:
                raise ResourceNotFoundError("run unavailable")
            return row.owner_user_id, row.thread_id

    def context(self, run_id, owner=None) -> ProjectExecutionContext:
        row_owner, thread = self._row_identity(run_id)
        if owner is not None and str(owner) != str(row_owner):
            raise ResourceNotFoundError("run unavailable")
        ctx = self.selector(row_owner, thread, run_id)
        with self.service._locked(ctx):
            state = self.service._load(ctx)
            run = state["runs"].get(str(run_id))
            if run is None:
                raise ResourceNotFoundError("project run unavailable")
            return ProjectExecutionContext(
                **run["context"], principal=ctx.principal, cancel_check=lambda: False
            )

    def owns_run(self, run_id) -> bool:
        # Detection only. Authorization is mandatory before reading any project result.
        try:
            owner, thread = self._row_identity(run_id)
            session = self.manager.sessions.get_for_user(thread, owner)
            if session.project_id is None:
                return False
            key = compute_payload_hash(
                {
                    "owner_user_id": str(owner),
                    "project_id": str(session.project_id),
                    "thread_id": str(thread),
                }
            )
            path = self.service.root / (key + ".sqlite")
            if not path.exists():
                return False
            # Read the scope marker without requiring current write rights. Revocation must not
            # accidentally send an existing project run down the legacy export path.
            from water_agent.runtime.governance.contracts import Principal

            ctx = ProjectExecutionContext(
                owner_user_id=str(owner),
                thread_id=str(thread),
                run_id=str(run_id),
                project_id=str(session.project_id),
                plan_id="selector",
                branch_id="main",
                plan_revision=1,
                generation=1,
                authorization_epoch=1,
                checkpoint_namespace="t27-v1",
                principal=Principal.local("scope-marker"),
                cancel_check=lambda: False,
            )
            with self.service._locked(ctx):
                return str(run_id) in self.service._load(ctx)["runs"]
        except (ValueError, OSError, KeyError):
            return True
        except ResourceNotFoundError:
            return False

    def preview(self, owner, thread, raw: dict, base_revision: int, generation: int) -> dict:
        ctx = self.selector(owner, thread)
        s = self.service
        with s._locked(ctx):
            self.authorize_session(owner, thread)
            state = s._load(ctx)
            head = state["runs"].get(state["active_run"])
            if (
                base_revision != (head["plan"]["revision"] if head else 0)
                or generation != state["generation"]
            ):
                raise ValueError("PROJECT_REVISION_CONFLICT")
            if raw.get("project_id") != ctx.project_id:
                raise ResourceNotFoundError("project scope unavailable")
            plan_data = {
                **raw,
                "revision": state["global_revision_counter"] + 1,
                "base_revision": base_revision,
                "supersedes_plan_hash": head["plan_hash"] if head else None,
            }
            plan = ProjectCalculationPlan.model_validate(plan_data)
            from water_agent.multi_agent.project_calculation import _json
            from water_agent.multi_agent.project_publication import check_output

            if len(_json(plan.model_dump()).encode("utf-8")) > 32768:
                raise ValueError("PROJECT_BUDGET_EXCEEDED")
            if not check_output(_json(plan.model_dump())):
                raise ValueError("PROJECT_OUTPUT_BLOCKED")

            if plan.branch_id != state["active_branch_id"] and plan.branch_id in state["branches"]:
                raise ValueError("PROJECT_REVISION_CONFLICT")
            now = s.now()
            previews = {
                k: v
                for k, v in state.get("previews", {}).items()
                if datetime.fromisoformat(v["expires_at"]) > now and not v["consumed"]
            }
            if len(previews) >= 32:
                raise ValueError("PROJECT_BUDGET_EXCEEDED")
            confirmation_id = str(uuid4())
            entry = {
                "confirmation_id": confirmation_id,
                "plan": plan.model_dump(),
                "plan_hash": compute_payload_hash(plan.model_dump()),
                "base_revision": base_revision,
                "generation": generation,
                "owner_user_id": ctx.owner_user_id,
                "thread_id": ctx.thread_id,
                "project_id": ctx.project_id,
                "expires_at": (now + timedelta(minutes=15)).isoformat(),
                "consumed": False,
            }
            previews[confirmation_id] = entry
            state["previews"] = previews
            s._save(ctx, state)
            return {
                k: v
                for k, v in entry.items()
                if k not in ("consumed", "owner_user_id", "thread_id")
            }

    def execute(self, owner, thread, confirmation_id, expected_hash) -> dict:
        with self.manager._lock:
            from water_agent.webapp.run_manager import ThreadBusyError, _RunHandle

            selector = self.selector(owner, thread)
            s = self.service
            if self.manager.thread_busy(thread):
                raise ThreadBusyError("thread busy")
            with s._locked(selector):
                self.authorize_session(owner, thread)
                state = s._load(selector)
                entry = state.get("previews", {}).get(confirmation_id)
                if (
                    not entry
                    or entry["consumed"]
                    or entry["plan_hash"] != expected_hash
                    or entry["owner_user_id"] != str(owner)
                    or entry["thread_id"] != str(thread)
                    or s.now() >= datetime.fromisoformat(entry["expires_at"])
                ):
                    raise ValueError("PROJECT_CONFIRMATION_STALE")
                head = state["runs"].get(state["active_run"])
                if (
                    entry["base_revision"] != (head["plan"]["revision"] if head else 0)
                    or entry["generation"] != state["generation"]
                ):
                    raise ValueError("PROJECT_REVISION_CONFLICT")
                entry["consumed"] = True
                s._save(selector, state)
            row = self.manager.run_repository.create(thread, owner)
            ctx = replace(
                selector,
                run_id=str(row.run_id),
                plan_id=entry["plan"]["plan_id"],
                branch_id=entry["plan"]["branch_id"],
            )
            try:
                confirmed = s.confirm(
                    entry["plan"],
                    ctx,
                    base_revision=entry["base_revision"],
                    generation=entry["generation"],
                    branch=entry["plan"]["branch_id"],
                )
                if confirmed["plan_hash"] != expected_hash:
                    raise ValueError("PROJECT_CONFIRMATION_STALE")
                with s._locked(ctx):
                    state = s._load(ctx)
                    state["runs"][ctx.run_id]["confirmation_expires_at"] = entry["expires_at"]
                    s._save(ctx, state)
                ctx = ProjectExecutionContext(
                    **confirmed["context"], principal=ctx.principal, cancel_check=lambda: False
                )
                handle = _RunHandle(
                    run_id=ctx.run_id,
                    thread_id=ctx.thread_id,
                    owner_user_id=UUID(str(owner)),
                    project_id=UUID(ctx.project_id),
                    status="running",
                )
                with self.manager._lock:
                    if self.manager._thread_busy_locked(ctx.thread_id):
                        raise ThreadBusyError("thread busy")
                    self.manager._handles[ctx.run_id] = handle
                    self.manager._active_by_thread[ctx.thread_id] = ctx.run_id
                self.manager.run_repository.set_status(
                    ctx.run_id, owner, "running", expected_statuses=("queued",)
                )
                self.manager._executor.submit(self._execute, ctx, handle)
                return {"run_id": ctx.run_id, "thread_id": ctx.thread_id, "status": "running"}
            except Exception:
                self.manager.run_repository.set_status(
                    ctx.run_id, owner, "blocked", expected_statuses=("queued", "running")
                )
                raise

    def _mark_waiting(self, ctx) -> str:
        # The repository status write must not overtake a publication commit.
        with self.service._locked(ctx):
            self.service._auth(ctx)
            state = self.service._load(ctx)
            run = self.service._run(state, ctx)
            if state.get("run_publications", {}).get(ctx.run_id, {}).get("state") == "committed":
                return "succeeded"
            terminal = state.get("project_terminal_events", {}).get(ctx.run_id, [])
            known: str | None = next(
                (e["payload"]["status"] for e in terminal if e["type"] == "finalize"), None
            )
            if known is not None:
                return known
            if run["status"] != "waiting_approval" or state["active_run"] != ctx.run_id:
                return "cancelled" if run["status"] == "cancelled" else "blocked"
            row = self.manager.run_repository.get_owned_for_user(ctx.run_id, ctx.owner_user_id)
            if row.status != "waiting_approval":
                self.manager.run_repository.set_status(
                    ctx.run_id,
                    ctx.owner_user_id,
                    "waiting_approval",
                    expected_statuses=("running",),
                )
            return "waiting_approval"

    def _execute(self, ctx, handle):
        try:
            run = self.service.execute(ctx)
            if handle.finalized:
                return
            if run["status"] == "waiting_approval":
                status = self._mark_waiting(ctx)
                if status == "blocked":
                    self._terminal(ctx, status, "PROJECT_STALE_SOURCE")
            else:
                status = (
                    run["status"]
                    if run["status"] in ("cancelled", "failed", "blocked")
                    else "blocked"
                )
                self._terminal(ctx, status, run.get("error") or "PROJECT_EXECUTION_FAILED")
        except Exception:  # noqa: BLE001 - background failure must stop publication
            status = "blocked"
            self._terminal(ctx, status, "PROJECT_PERSISTENCE_FAILED")
        finally:
            with handle.condition:
                if not handle.finalized:
                    handle.status = locals().get("status", "blocked")
                    handle.suspended = handle.status == "waiting_approval"
                    handle.finalized = not handle.suspended
                    handle.condition.notify_all()
            with self.manager._lock:
                if self.manager._active_by_thread.get(ctx.thread_id) == ctx.run_id:
                    self.manager._active_by_thread.pop(ctx.thread_id, None)

    def currentness(self, run_id) -> dict:
        from water_agent.multi_agent.project_calculation_service import algorithm_version

        ctx = self.context(run_id)
        with self.service._locked(ctx):
            self.service._auth(ctx)
            state = self.service._load(ctx)
            run = self.service._run(state, ctx)
            current = (
                state["active_run"] == ctx.run_id
                and state["generation"] == run["generation"]
                and state["active_branch_id"] == run["plan"]["branch_id"]
                and run["algorithm_version"] == algorithm_version()
                and run["status"] == "published"
            )
            return {
                "is_current": current,
                "historical": not current,
                "revision": run["plan"]["revision"],
            }

    def head(self, owner, thread) -> dict:
        ctx = self.selector(owner, thread)
        with self.service._locked(ctx):
            self.authorize_session(owner, thread)
            state = self.service._load(ctx)
            run = state["runs"].get(state["active_run"])
            published_runs = sorted(
                (
                    {"run_id": run_id, "revision": publication["revision"]}
                    for run_id, publication in state.get("run_publications", {}).items()
                    if publication["state"] == "committed"
                ),
                key=lambda item: int(item["revision"]),
                reverse=True,
            )
            return {
                "base_revision": run["plan"]["revision"] if run else 0,
                "generation": state["generation"],
                "active_branch_id": state["active_branch_id"],
                "active_run_id": state["active_run"],
                "active_plan": run["plan"] if run else None,
                "published_runs": published_runs,
            }

    def approval_metadata(self, run_id, owner) -> dict:
        ctx = self.context(run_id, owner)
        record = self.publisher.read(ctx)
        with self.service._locked(ctx):
            self.service._auth(ctx)
            state = self.service._load(ctx)
            approval = self.service._run(state, ctx)["approval"]
            return {
                "run_id": ctx.run_id,
                "revision": record["snapshot"]["project_binding"]["revision"],
                "approved_by_user_id": approval["approved_by_user_id"],
                "release_manifest_hash": approval["release_manifest_hash"],
                "record": approval["record"],
            }

    def ensure_events(self, run_id) -> None:
        ctx = self.context(run_id)
        run = self.service.recover(ctx)
        with self.service._locked(ctx):
            state = self.service._load(ctx)
            publication = state.get("run_publications", {}).get(ctx.run_id)
            terminal = state.get("project_terminal_events", {}).get(ctx.run_id)
        if publication and publication["state"] == "committed":
            self.publisher.read(ctx)
        elif terminal:
            saved = next(e for e in terminal if e["type"] == "finalize")
            self._terminal(
                ctx,
                saved["payload"]["status"],
                saved["payload"].get("error", "PROJECT_EXECUTION_FAILED"),
            )
            return
        elif publication:
            raise ValueError("PROJECT_PERSISTENCE_FAILED")
        elif run["status"] in ("indeterminate", "stale", "failed", "blocked", "cancelled"):
            status = (
                "cancelled"
                if run["status"] == "cancelled"
                else "failed"
                if run["status"] == "failed"
                else "blocked"
            )
            self._terminal(ctx, status, run.get("error") or "PROJECT_STALE_SOURCE")
            self._finish_handle(ctx, status)
        elif run["status"] == "confirmed":
            handle = self.manager._handles.get(ctx.run_id)
            if handle is None or handle.finalized or handle.status not in ("queued", "running"):
                raise ValueError("PROJECT_PERSISTENCE_FAILED")
        elif run["status"] not in ("running", "waiting_approval"):
            raise ValueError("PROJECT_PERSISTENCE_FAILED")

    def state(self, run_id, owner) -> dict:
        ctx = self.context(run_id, owner)
        run = self.service.recover(ctx)
        if self._committed(ctx):
            from water_agent.graphs.project_calculation_answer import project_calculation_projection

            record = self.publisher.read(ctx)
            return {
                "run_id": ctx.run_id,
                "status": "succeeded",
                "candidate": project_calculation_projection(
                    {"result": record["snapshot"]["project_calculation"]}
                ),
                "revision": run["plan"]["revision"],
                "export_formats": ["docx"],
                **self.currentness(run_id),
            }
        if run["status"] == "indeterminate":
            self._terminal(ctx, "blocked", "PROJECT_EXECUTION_INDETERMINATE")
        with self.service._locked(ctx):
            state = self.service._load(ctx)
            publication = state.get("run_publications", {}).get(ctx.run_id)
            terminal = state.get("project_terminal_events", {}).get(ctx.run_id, [])
            known = next(
                (event["payload"]["status"] for event in terminal if event["type"] == "finalize"),
                None,
            )
            if known is None and (publication is not None or run["status"] == "published"):
                raise ValueError("PROJECT_PERSISTENCE_FAILED")
        data = {
            "run_id": ctx.run_id,
            "status": known or ("blocked" if run["status"] == "indeterminate" else run["status"]),
            "revision": run["plan"]["revision"],
            "plan_hash": run["plan_hash"],
            "export_formats": [],
        }
        if run["status"] == "waiting_approval":
            from water_agent.graphs.project_calculation_answer import project_calculation_projection

            data["candidate"] = project_calculation_projection(run)
            data["release_manifest_hash"] = run["release_manifest_hash"]
        return data

    def approve(self, run_id, owner, decision, reason) -> dict:
        ctx = self.context(run_id, owner)
        if self._committed(ctx) or self._mark_waiting(ctx) != "waiting_approval":
            raise ValueError("PROJECT_APPROVAL_STALE")
        from water_agent.documents.storage import ExportSnapshotStore

        run = self.service.approve(ctx, decision, ExportSnapshotStore._sanitize(reason))
        if run["status"] == "approved":
            try:
                self.publisher.publish(ctx)
            except (OSError, ValueError) as exc:
                if not self._committed(ctx):
                    with self.service._locked(ctx):
                        state = self.service._load(ctx)
                        state["runs"][ctx.run_id]["status"] = "blocked"
                        publication = state.get("run_publications", {}).get(ctx.run_id)
                        if publication is not None and publication["state"] == "prepared":
                            publication["state"] = "aborted"
                        self.service._save(ctx, state)
                    self._terminal(
                        ctx,
                        "blocked",
                        "PROJECT_OUTPUT_BLOCKED"
                        if str(exc) == "PROJECT_OUTPUT_BLOCKED"
                        else "PROJECT_PERSISTENCE_FAILED",
                    )
                    self._finish_handle(ctx, "blocked")
                raise
            self._finish_handle(ctx, "succeeded")
            return {"run_id": ctx.run_id, "status": "succeeded", "export_formats": ["docx"]}
        if run["status"] == "failed":
            self._terminal(ctx, "failed", "PROJECT_APPROVAL_REJECTED")
            self._finish_handle(ctx, "failed")
        return {"run_id": ctx.run_id, "status": run["status"], "export_formats": []}

    def _finish_handle(self, ctx, status):
        handle = self.manager._handles.get(ctx.run_id)
        if handle:
            with handle.condition:
                handle.status = status
                handle.finalized = True
                handle.suspended = False
                handle.condition.notify_all()

    def _succeeded(self, ctx):
        row = self.manager.run_repository.get_owned_for_user(ctx.run_id, ctx.owner_user_id)
        if row.status != "succeeded":
            self.manager.run_repository.set_status(
                ctx.run_id,
                ctx.owner_user_id,
                "succeeded",
                expected_statuses=("queued", "running", "waiting_approval"),
            )

    def _committed(self, ctx):
        with self.service._locked(ctx):
            state = self.service._load(ctx)
            return state.get("run_publications", {}).get(ctx.run_id, {}).get("state") == "committed"

    def _terminal(self, ctx, status, error):
        with self.service._locked(ctx):
            state = self.service._load(ctx)
            if state.get("run_publications", {}).get(ctx.run_id, {}).get("state") == "committed":
                return
            events = state.setdefault("project_terminal_events", {})
            existing = events.get(ctx.run_id)
            if existing is None:
                run = self.service._run(state, ctx)
                if run["status"] not in (
                    "stale",
                    "indeterminate",
                    "cancelled",
                    "failed",
                    "blocked",
                ):
                    run["status"] = status
                final = {
                    "event_id": "2",
                    "type": "finalize",
                    "run_id": ctx.run_id,
                    "thread_id": ctx.thread_id,
                    "time": self.service.now().isoformat(),
                    "payload_version": "v1",
                    "payload": {"status": status, "error": error, "export_formats": []},
                }
                retract = {
                    **final,
                    "event_id": "1",
                    "type": "draft_retract",
                    "payload": {
                        "reason": "guardrail" if error == "PROJECT_OUTPUT_BLOCKED" else "run_ended"
                    },
                }
                events[ctx.run_id] = ([retract] if status == "blocked" else []) + [
                    final,
                    {**final, "event_id": "3", "type": "stream_end", "payload": {"status": status}},
                ]
                self.service._save(ctx, state)
            else:
                status = next(e["payload"]["status"] for e in existing if e["type"] == "finalize")
            self.manager.run_repository.set_status(
                ctx.run_id,
                ctx.owner_user_id,
                status,
                expected_statuses=("queued", "running", "waiting_approval", status),
            )

    def cancel(self, run_id, owner):
        ctx = self.context(run_id, owner)
        if self._committed(ctx):
            return {"run_id": ctx.run_id, "status": "succeeded"}
        result = self.service.cancel(ctx)
        if self._committed(ctx):
            self.publisher.read(ctx)
            return {"run_id": ctx.run_id, "status": "succeeded"}
        with self.service._locked(ctx):
            state = self.service._load(ctx)
            terminal = state.get("project_terminal_events", {}).get(ctx.run_id, [])
            known = next(
                (event["payload"]["status"] for event in terminal if event["type"] == "finalize"),
                None,
            )
        if known is not None:
            return {"run_id": ctx.run_id, "status": known}
        if result["status"] in ("failed", "blocked", "indeterminate"):
            return {
                "run_id": ctx.run_id,
                "status": "blocked" if result["status"] == "indeterminate" else result["status"],
            }
        self._terminal(ctx, "cancelled", "PROJECT_CANCELLED")
        self._finish_handle(ctx, "cancelled")
        return {"run_id": ctx.run_id, "status": "cancelled"}

    def saved(self, run_id):
        return self.publisher.read(self.context(run_id))

    def document(self, run_id):
        return self.publisher.document(self.context(run_id))

    def stream(self, run_id, last_event_id, is_authorized):
        from water_agent.webapp.run_manager import parse_last_event_id

        cursor = parse_last_event_id(last_event_id)
        if not is_authorized():
            return
        ctx = self.context(run_id)
        # Wait on the existing RunManager handle, not a second worker/queue.
        handle = self.manager._handles.get(str(run_id))
        if handle:
            with handle.condition:
                while handle.status in ("queued", "running"):
                    if not is_authorized():
                        return
                    handle.condition.wait(0.1)
        if not is_authorized():
            return
        with self.service._locked(ctx):
            self.service._auth(ctx)
            state = self.service._load(ctx)
            run = self.service._run(state, ctx)
            pub = state.get("run_publications", {}).get(ctx.run_id)
            terminals = state.get("project_terminal_events", {}).get(ctx.run_id)
        if pub and pub["state"] == "committed":
            record = self.publisher.read(ctx)
            events = [record["finalize"], state["publication_stream_ends"][ctx.run_id]]
        elif terminals:
            events = terminals
        elif run["status"] == "waiting_approval":
            events = [
                {
                    "event_id": "1",
                    "type": "approval_required",
                    "run_id": ctx.run_id,
                    "thread_id": ctx.thread_id,
                    "time": run["updated_at"],
                    "payload_version": "v1",
                    "payload": {"status": "waiting_approval"},
                }
            ]
        else:
            raise ValueError("PROJECT_PERSISTENCE_FAILED")
        # As in the legacy stream, reconnect after finalize yields no new stream_end.
        if any(
            event["type"] == "finalize" and cursor >= int(event["event_id"]) for event in events
        ):
            return
        for event in events:
            if int(event["event_id"]) > cursor and is_authorized():
                yield f"id: {event['event_id']}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"
