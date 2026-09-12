"""P5 Step 40 application service for explicit, confirmed memory mutations."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from water_agent.domain.models import content_hash_of
from water_agent.memory.commands import adapt_remember_command
from water_agent.memory.contracts import (
    MemoryConflictRequiresConfirmationError,
    MemoryError,
    MemoryKind,
    MemoryOrigin,
    MemoryRevisionConflictError,
    MemoryScopeDeniedError,
    MemoryScopeType,
    content_hash,
)
from water_agent.memory.pending_candidates import PendingCandidate, PendingCandidateStore
from water_agent.memory.service import MemoryService
from water_agent.memory.write_commit import ConfirmedCandidateWriter
from water_agent.memory.write_contracts import (
    CandidateDisposition,
    CandidateOperation,
    ConflictPreview,
    MemoryCandidate,
    MemoryConfirmation,
    MemoryValueBasis,
    MemoryWriteAction,
    MemoryWriteResult,
)
from water_agent.memory.write_guard import MemoryWriteGuard, normalize_memory_content
from water_agent.memory.write_validation import (
    as_uuid,
    coerce_kind,
    coerce_scope,
    validate_candidate_schema,
)
from water_agent.runtime.governance.contracts import ContentEnvelope, SourceTag


class MemoryWriteTrace(Protocol):
    @property
    def started(self) -> bool: ...

    def record_memory_write(self, **kwargs: Any) -> None: ...


class ExplicitMemoryWriteService:
    """Single sanctioned application boundary for P5 long-term mutations."""

    def __init__(
        self,
        memory_service: MemoryService,
        *,
        guard: MemoryWriteGuard | None = None,
        trace: MemoryWriteTrace | None = None,
    ) -> None:
        self.memory_service = memory_service
        self.guard = guard if guard is not None else MemoryWriteGuard()
        self.trace = trace
        self._pending = PendingCandidateStore()
        self._writer = ConfirmedCandidateWriter(memory_service, guard=self.guard)

    def candidate(
        self,
        *,
        tenant_id: UUID | str,
        actor_user_id: UUID | str,
        scope_type: MemoryScopeType | str,
        kind: MemoryKind | str,
        semantic_key: str,
        content: str,
        project_id: UUID | str | None = None,
        origin: MemoryOrigin = MemoryOrigin.EXPLICIT_USER,
        source_thread_id: UUID | str | None = None,
        source_message_id: UUID | str | None = None,
        expires_at: datetime | None = None,
        value_basis: MemoryValueBasis = MemoryValueBasis.USER_STATEMENT,
    ) -> MemoryCandidate:
        """Authorize, normalize, govern, validate, and preview a proposal."""

        tenant = as_uuid(tenant_id)
        actor = as_uuid(actor_user_id)
        scope = coerce_scope(scope_type)
        project = as_uuid(project_id) if project_id is not None else None

        # Authorization precedes all candidate content processing.
        self.memory_service.authorize_create_scope(
            actor_user_id=actor,
            scope_type=scope,
            project_id=project,
        )

        normalized_content = normalize_memory_content(content)
        envelope = ContentEnvelope(source_tag=SourceTag.USER_MESSAGE, text=normalized_content)
        try:
            guard_decision = self.guard.check(envelope)
        except MemoryError as exc:
            self._emit(
                action="blocked",
                status="blocked",
                content_hash=envelope.content_hash,
                code=exc.code,
                ruleset_version=self.guard.ruleset_version,
            )
            raise
        try:
            normalized_key, key_guard_decision = self.guard.govern_semantic_key(semantic_key)
        except MemoryError as exc:
            self._emit(
                action="blocked",
                status="blocked",
                content_hash=content_hash_of(str(semantic_key)),
                code=exc.code,
                ruleset_version=self.guard.ruleset_version,
            )
            raise

        memory_kind = coerce_kind(kind)
        memory_origin = MemoryOrigin(origin)
        if memory_origin is not MemoryOrigin.EXPLICIT_USER:
            raise MemoryScopeDeniedError(
                "approved import requires a separate server-side administrator approval adapter"
            )
        basis = MemoryValueBasis(value_basis)
        validate_candidate_schema(
            scope_type=scope,
            kind=memory_kind,
            content=normalized_content,
            value_basis=basis,
        )

        all_rule_ids = tuple(
            sorted(set(guard_decision.rule_ids) | set(key_guard_decision.rule_ids))
        )
        is_quarantined = guard_decision.quarantined or key_guard_decision.quarantined
        existing = None
        if not is_quarantined:
            existing = self.memory_service.find_active_by_key(
                tenant_id=tenant,
                actor_user_id=actor,
                scope_type=scope,
                project_id=project,
                kind=memory_kind,
                semantic_key=normalized_key,
            )
        disposition = CandidateDisposition.READY
        conflict: ConflictPreview | None = None
        target_memory_id: UUID | None = None
        expected_revision: int | None = None
        retained_content = normalized_content
        if is_quarantined:
            disposition = CandidateDisposition.QUARANTINED
            retained_content = ""
        elif existing is not None:
            target_memory_id = existing.memory_id
            expected_revision = existing.revision
            if normalize_memory_content(existing.content) == normalized_content:
                disposition = CandidateDisposition.REAFFIRM
            else:
                disposition = CandidateDisposition.CONFLICT
                conflict = ConflictPreview(
                    memory_id=existing.memory_id,
                    revision=existing.revision,
                    current_content=existing.content,
                    current_content_hash=content_hash(existing.content) or "",
                    proposed_content=normalized_content,
                    proposed_content_hash=guard_decision.content_hash,
                )

        candidate = MemoryCandidate(
            candidate_id=uuid4(),
            operation=CandidateOperation.CREATE,
            disposition=disposition,
            scope_type=scope,
            project_id=project,
            kind=memory_kind,
            semantic_key=normalized_key,
            content=retained_content,
            content_hash=guard_decision.content_hash,
            origin=memory_origin,
            source_thread_id=(as_uuid(source_thread_id) if source_thread_id is not None else None),
            source_message_id=(
                as_uuid(source_message_id) if source_message_id is not None else None
            ),
            memory_expires_at=expires_at,
            value_basis=basis,
            target_memory_id=target_memory_id,
            expected_revision=expected_revision,
            conflict=conflict,
            guard_rule_ids=all_rule_ids,
        )
        self._pending.put(candidate, tenant_id=tenant, actor_user_id=actor)
        self._emit_candidate(candidate)
        return candidate

    def candidate_from_remember(self, command: str, **kwargs: Any) -> MemoryCandidate:
        parsed = adapt_remember_command(command)
        if parsed is None:
            raise ValueError("message is not a /remember command")
        return self.candidate(content=parsed.content, **kwargs)

    def correct(
        self,
        memory_id: UUID | str,
        *,
        tenant_id: UUID | str,
        actor_user_id: UUID | str,
        expected_revision: int,
        content: str,
        value_basis: MemoryValueBasis = MemoryValueBasis.USER_STATEMENT,
    ) -> MemoryCandidate:
        """Build a governed correction candidate; no mutation occurs here."""

        tenant = as_uuid(tenant_id)
        actor = as_uuid(actor_user_id)
        current = self.memory_service.get_memory_for_write(
            memory_id,
            tenant_id=tenant,
            actor_user_id=actor,
        )
        normalized_content = normalize_memory_content(content)
        envelope = ContentEnvelope(source_tag=SourceTag.USER_MESSAGE, text=normalized_content)
        try:
            guard_decision = self.guard.check(envelope)
        except MemoryError as exc:
            self._emit(
                action="blocked",
                status="blocked",
                memory_id=current.memory_id,
                content_hash=envelope.content_hash,
                code=exc.code,
                ruleset_version=self.guard.ruleset_version,
            )
            raise
        try:
            governed_key, key_guard_decision = self.guard.govern_semantic_key(
                current.semantic_key
            )
        except MemoryError as exc:
            self._emit(
                action="blocked",
                status="blocked",
                memory_id=current.memory_id,
                content_hash=content_hash_of(current.semantic_key),
                code=exc.code,
                ruleset_version=self.guard.ruleset_version,
            )
            raise
        basis = MemoryValueBasis(value_basis)
        validate_candidate_schema(
            scope_type=current.scope_type,
            kind=current.kind,
            content=normalized_content,
            value_basis=basis,
        )

        all_rule_ids = tuple(
            sorted(set(guard_decision.rule_ids) | set(key_guard_decision.rule_ids))
        )
        if guard_decision.quarantined or key_guard_decision.quarantined:
            operation = CandidateOperation.CREATE
            disposition = CandidateDisposition.QUARANTINED
            retained_content = ""
            target_memory_id = None
            candidate_revision = None
        else:
            operation = CandidateOperation.CORRECT
            disposition = (
                CandidateDisposition.REAFFIRM
                if normalize_memory_content(current.content) == normalized_content
                else CandidateDisposition.CORRECTION
            )
            retained_content = normalized_content
            target_memory_id = current.memory_id
            candidate_revision = expected_revision
        candidate = MemoryCandidate(
            candidate_id=uuid4(),
            operation=operation,
            disposition=disposition,
            scope_type=current.scope_type,
            project_id=current.project_id,
            kind=current.kind,
            semantic_key=governed_key,
            content=retained_content,
            content_hash=guard_decision.content_hash,
            origin=current.origin,
            source_thread_id=current.source_thread_id,
            source_message_id=current.source_message_id,
            memory_expires_at=current.expires_at,
            value_basis=basis,
            target_memory_id=target_memory_id,
            expected_revision=candidate_revision,
            guard_rule_ids=all_rule_ids,
        )
        self._pending.put(candidate, tenant_id=tenant, actor_user_id=actor)
        self._emit_candidate(candidate)
        return candidate

    def confirm(
        self,
        candidate_id: UUID | str,
        *,
        tenant_id: UUID | str,
        actor_user_id: UUID | str,
        confirmation: MemoryConfirmation,
    ) -> MemoryWriteResult:
        """Apply exactly the candidate the same user previewed and confirmed."""

        pending = self._authorized_pending(candidate_id, tenant_id, actor_user_id)
        candidate = pending.candidate
        if confirmation.content_hash != candidate.content_hash:
            raise MemoryRevisionConflictError("candidate content hash does not match preview")
        if confirmation.confirmed is not True:
            self._pending.pop(candidate.candidate_id)
            self._emit_candidate(candidate, action="cancelled", status="cancelled")
            return MemoryWriteResult(
                action=MemoryWriteAction.CANCELLED,
                content_hash=candidate.content_hash,
            )

        result = self._writer.apply(
            candidate,
            confirmation,
            tenant_id=pending.tenant_id,
            actor_user_id=pending.actor_user_id,
        )
        self._pending.pop(candidate.candidate_id)
        self._emit(
            action=result.action.value,
            status=result.memory.status.value if result.memory is not None else "cancelled",
            memory_id=result.memory.memory_id if result.memory is not None else None,
            content_hash=result.content_hash,
            kind=candidate.kind.value,
            scope_type=candidate.scope_type.value,
            revision=result.memory.revision if result.memory is not None else None,
            ruleset_version=self.guard.ruleset_version,
            rule_ids=candidate.guard_rule_ids,
            project_id=candidate.project_id,
        )
        return result

    def reaffirm(
        self,
        candidate_id: UUID | str,
        *,
        tenant_id: UUID | str,
        actor_user_id: UUID | str,
        confirmation: MemoryConfirmation,
    ) -> MemoryWriteResult:
        pending = self._authorized_pending(candidate_id, tenant_id, actor_user_id)
        if pending.candidate.disposition is not CandidateDisposition.REAFFIRM:
            raise MemoryConflictRequiresConfirmationError("candidate is not a reaffirmation")
        return self.confirm(
            candidate_id,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            confirmation=confirmation,
        )

    def forget(
        self,
        memory_id: UUID | str,
        *,
        tenant_id: UUID | str,
        actor_user_id: UUID | str,
        expected_revision: int,
        confirmed: bool,
    ) -> MemoryWriteResult:
        """Forget only after resource authorization and an explicit true decision."""

        tenant = as_uuid(tenant_id)
        actor = as_uuid(actor_user_id)
        current = self.memory_service.get_memory_for_write(
            memory_id,
            tenant_id=tenant,
            actor_user_id=actor,
        )
        digest = content_hash(current.content) or ""
        if confirmed is not True:
            self._emit(
                action="cancelled",
                status="cancelled",
                memory_id=current.memory_id,
                content_hash=digest,
                kind=current.kind.value,
                scope_type=current.scope_type.value,
                revision=current.revision,
                ruleset_version=self.guard.ruleset_version,
                project_id=current.project_id,
            )
            return MemoryWriteResult(
                action=MemoryWriteAction.CANCELLED,
                content_hash=digest,
            )
        forgotten = self.memory_service.forget_memory(
            current.memory_id,
            tenant_id=tenant,
            actor_user_id=actor,
            expected_revision=expected_revision,
            reason="explicit user forget",
        )
        self._emit(
            action=MemoryWriteAction.FORGOTTEN.value,
            status=forgotten.status.value,
            memory_id=forgotten.memory_id,
            content_hash=digest,
            kind=forgotten.kind.value,
            scope_type=forgotten.scope_type.value,
            revision=forgotten.revision,
            ruleset_version=self.guard.ruleset_version,
            project_id=forgotten.project_id,
        )
        return MemoryWriteResult(
            action=MemoryWriteAction.FORGOTTEN,
            memory=forgotten,
            content_hash=digest,
        )

    def _authorized_pending(
        self,
        candidate_id: UUID | str,
        tenant_id: UUID | str,
        actor_user_id: UUID | str,
    ) -> PendingCandidate:
        pending = self._pending.get(as_uuid(candidate_id))
        if pending.tenant_id != as_uuid(tenant_id) or pending.actor_user_id != as_uuid(
            actor_user_id
        ):
            raise MemoryScopeDeniedError("memory candidate is not authorized")
        return pending

    def _emit_candidate(
        self,
        candidate: MemoryCandidate,
        *,
        action: str = "candidate",
        status: str | None = None,
    ) -> None:
        self._emit(
            action=action,
            status=status or candidate.disposition.value,
            content_hash=candidate.content_hash,
            kind=candidate.kind.value,
            scope_type=candidate.scope_type.value,
            ruleset_version=self.guard.ruleset_version,
            rule_ids=candidate.guard_rule_ids,
            project_id=candidate.project_id,
        )

    def _emit(
        self,
        *,
        action: str,
        status: str,
        content_hash: str,
        memory_id: UUID | None = None,
        kind: str = "",
        scope_type: str = "",
        revision: int | None = None,
        ruleset_version: str = "",
        rule_ids: tuple[str, ...] = (),
        code: str | None = None,
        project_id: UUID | None = None,
    ) -> None:
        if self.trace is None or not self.trace.started:
            return
        try:
            self.trace.record_memory_write(
                action=action,
                status=status,
                memory_id=str(memory_id) if memory_id is not None else "",
                content_hash=content_hash,
                kind=kind,
                scope_type=scope_type,
                revision=revision,
                ruleset_version=ruleset_version,
                rule_ids=rule_ids,
                code=code,
                project_id_hash=(
                    content_hash_of(str(project_id)) if project_id is not None else ""
                ),
            )
        except Exception:  # noqa: BLE001 -- telemetry cannot change write semantics
            return


__all__ = ["ExplicitMemoryWriteService"]
