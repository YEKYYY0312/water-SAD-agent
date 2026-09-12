"""Transactional application of already-governed, explicitly confirmed candidates."""

from __future__ import annotations

from uuid import UUID

from water_agent.memory.contracts import (
    MemoryConflictRequiresConfirmationError,
    MemoryRevisionConflictError,
)
from water_agent.memory.service import MemoryService
from water_agent.memory.write_contracts import (
    CandidateDisposition,
    ConflictResolution,
    MemoryCandidate,
    MemoryConfirmation,
    MemoryWriteAction,
    MemoryWriteResult,
)
from water_agent.memory.write_guard import MemoryWriteGuard, normalize_memory_content


class ConfirmedCandidateWriter:
    """Map one confirmed preview to Step 39 transaction primitives."""

    def __init__(
        self,
        memory_service: MemoryService,
        *,
        guard: MemoryWriteGuard | None = None,
    ) -> None:
        self.memory_service = memory_service
        self.guard = guard if guard is not None else MemoryWriteGuard()

    def apply(
        self,
        candidate: MemoryCandidate,
        confirmation: MemoryConfirmation,
        *,
        tenant_id: UUID,
        actor_user_id: UUID,
    ) -> MemoryWriteResult:
        if candidate.disposition is CandidateDisposition.QUARANTINED:
            memory = self.memory_service.create_quarantined_memory(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                scope_type=candidate.scope_type,
                project_id=candidate.project_id,
                kind=candidate.kind,
                semantic_key=candidate.semantic_key,
                attempted_content_hash=candidate.content_hash,
                rule_ids=candidate.guard_rule_ids,
                origin=candidate.origin,
                source_thread_id=candidate.source_thread_id,
                source_message_id=candidate.source_message_id,
            )
            return MemoryWriteResult(
                action=MemoryWriteAction.QUARANTINED,
                memory=memory,
                content_hash=candidate.content_hash,
            )

        if candidate.disposition is CandidateDisposition.CONFLICT:
            return self._resolve_conflict(
                candidate,
                confirmation,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
            )

        if candidate.disposition is CandidateDisposition.CORRECTION:
            expected = self._confirmed_revision(candidate, confirmation)
            if candidate.target_memory_id is None:
                raise MemoryRevisionConflictError("correction target is missing")
            memory = self.memory_service.correct_memory(
                candidate.target_memory_id,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                expected_revision=expected,
                content=candidate.content,
                reason=f"explicit correction; basis={candidate.value_basis.value}",
            )
            return MemoryWriteResult(
                action=MemoryWriteAction.CORRECTED,
                memory=memory,
                content_hash=candidate.content_hash,
            )

        if candidate.disposition is CandidateDisposition.REAFFIRM:
            expected = self._confirmed_revision(candidate, confirmation)
            if candidate.target_memory_id is None:
                raise MemoryRevisionConflictError("reaffirmation target is missing")
            memory = self.memory_service.reaffirm_memory(
                candidate.target_memory_id,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                expected_revision=expected,
                source_thread_id=candidate.source_thread_id,
                source_message_id=candidate.source_message_id,
                reason="explicit reaffirmation",
            )
            return MemoryWriteResult(
                action=MemoryWriteAction.REAFFIRMED,
                memory=memory,
                content_hash=candidate.content_hash,
            )

        return self._create_or_reaffirm_after_race(
            candidate,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
        )

    def _create_or_reaffirm_after_race(
        self,
        candidate: MemoryCandidate,
        *,
        tenant_id: UUID,
        actor_user_id: UUID,
    ) -> MemoryWriteResult:
        existing = self.memory_service.find_active_by_key(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            scope_type=candidate.scope_type,
            project_id=candidate.project_id,
            kind=candidate.kind,
            semantic_key=candidate.semantic_key,
        )
        if existing is not None:
            if normalize_memory_content(existing.content) != candidate.content:
                raise MemoryConflictRequiresConfirmationError(
                    "active key changed after candidate preview; request a new conflict preview"
                )
            memory = self.memory_service.reaffirm_memory(
                existing.memory_id,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                expected_revision=existing.revision,
                source_thread_id=candidate.source_thread_id,
                source_message_id=candidate.source_message_id,
                reason="explicit reaffirmation after concurrent create",
            )
            return MemoryWriteResult(
                action=MemoryWriteAction.REAFFIRMED,
                memory=memory,
                content_hash=candidate.content_hash,
            )
        memory = self.memory_service.create_memory(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            scope_type=candidate.scope_type,
            project_id=candidate.project_id,
            kind=candidate.kind,
            semantic_key=candidate.semantic_key,
            content=candidate.content,
            origin=candidate.origin,
            source_thread_id=candidate.source_thread_id,
            source_message_id=candidate.source_message_id,
            expires_at=candidate.memory_expires_at,
            reason=f"explicit confirmation; basis={candidate.value_basis.value}",
        )
        return MemoryWriteResult(
            action=MemoryWriteAction.CREATED,
            memory=memory,
            content_hash=candidate.content_hash,
        )

    def _resolve_conflict(
        self,
        candidate: MemoryCandidate,
        confirmation: MemoryConfirmation,
        *,
        tenant_id: UUID,
        actor_user_id: UUID,
    ) -> MemoryWriteResult:
        preview = candidate.conflict
        if preview is None:
            raise MemoryConflictRequiresConfirmationError("conflict preview is missing")
        if confirmation.resolution is None:
            raise MemoryConflictRequiresConfirmationError(
                "conflict resolution must be explicitly selected"
            )
        if confirmation.resolution is ConflictResolution.CANCEL:
            return MemoryWriteResult(
                action=MemoryWriteAction.CANCELLED,
                content_hash=candidate.content_hash,
            )
        if confirmation.resolution is ConflictResolution.CORRECT_EXISTING:
            if confirmation.expected_revision != preview.revision:
                raise MemoryRevisionConflictError("conflict preview revision was not confirmed")
            memory = self.memory_service.correct_memory(
                preview.memory_id,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                expected_revision=preview.revision,
                content=candidate.content,
                reason=f"explicit conflict correction; basis={candidate.value_basis.value}",
            )
            return MemoryWriteResult(
                action=MemoryWriteAction.CORRECTED,
                memory=memory,
                content_hash=candidate.content_hash,
            )
        return self._keep_both(
            candidate,
            confirmation,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
        )

    def _keep_both(
        self,
        candidate: MemoryCandidate,
        confirmation: MemoryConfirmation,
        *,
        tenant_id: UUID,
        actor_user_id: UUID,
    ) -> MemoryWriteResult:
        alternative = confirmation.alternative_semantic_key
        if alternative is None:
            raise MemoryConflictRequiresConfirmationError(
                "keep_both requires an alternative semantic key"
            )
        alternative_key, key_guard_decision = self.guard.govern_semantic_key(alternative)
        if key_guard_decision.quarantined:
            memory = self.memory_service.create_quarantined_memory(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                scope_type=candidate.scope_type,
                project_id=candidate.project_id,
                kind=candidate.kind,
                semantic_key=alternative_key,
                attempted_content_hash=candidate.content_hash,
                rule_ids=tuple(
                    sorted(set(candidate.guard_rule_ids) | set(key_guard_decision.rule_ids))
                ),
                origin=candidate.origin,
                source_thread_id=candidate.source_thread_id,
                source_message_id=candidate.source_message_id,
            )
            return MemoryWriteResult(
                action=MemoryWriteAction.QUARANTINED,
                memory=memory,
                content_hash=candidate.content_hash,
            )
        if alternative_key == candidate.semantic_key:
            raise MemoryConflictRequiresConfirmationError(
                "keep_both requires a different semantic key"
            )
        other = self.memory_service.find_active_by_key(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            scope_type=candidate.scope_type,
            project_id=candidate.project_id,
            kind=candidate.kind,
            semantic_key=alternative_key,
        )
        if other is not None:
            raise MemoryConflictRequiresConfirmationError(
                "alternative semantic key already has an active memory"
            )
        memory = self.memory_service.create_memory(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            scope_type=candidate.scope_type,
            project_id=candidate.project_id,
            kind=candidate.kind,
            semantic_key=alternative_key,
            content=candidate.content,
            origin=candidate.origin,
            source_thread_id=candidate.source_thread_id,
            source_message_id=candidate.source_message_id,
            expires_at=candidate.memory_expires_at,
            reason=f"explicit keep-both conflict resolution; basis={candidate.value_basis.value}",
        )
        return MemoryWriteResult(
            action=MemoryWriteAction.CREATED,
            memory=memory,
            content_hash=candidate.content_hash,
        )

    @staticmethod
    def _confirmed_revision(candidate: MemoryCandidate, confirmation: MemoryConfirmation) -> int:
        expected = candidate.expected_revision
        if expected is None or confirmation.expected_revision != expected:
            raise MemoryRevisionConflictError("previewed revision was not explicitly confirmed")
        return expected


__all__ = ["ConfirmedCandidateWriter"]
