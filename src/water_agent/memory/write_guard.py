"""P5 Step 40 guard for content that could persist across sessions."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from water_agent.memory.contracts import (
    MemoryInjectionBlockedError,
    MemoryKindInvalidError,
    MemoryPiiBlockedError,
    MemorySecretBlockedError,
    normalize_semantic_key,
)
from water_agent.runtime.governance.contracts import ContentEnvelope, SourceTag
from water_agent.runtime.governance.pipeline import GuardrailPipeline, default_guardrail_pipeline
from water_agent.runtime.governance.rules import (
    RuleCategory,
    RuleHit,
    decode_base64_segments,
    detect_pii,
    detect_secrets,
    strip_invisible,
)

_MEMORY_SECRET_ASSIGNMENT = re.compile(
    # Assignment form: a credential keyword, "=" or ":", then an opaque value.
    r"\b(?:api[_\- ]?key|secret[_\- ]?key|access[_\- ]?token|token|password"
    r"|passwd|pwd|private[_\- ]?key|client[_\- ]?secret|passphrase"
    r"|db[_\- ]?password|secret)\s*[=:]\s*['\"]?[^\s'\"]{8,}"
    # Bearer/authorization headers separate the credential with whitespace.
    r"|\bbearer\s+[A-Za-z0-9._\-]{8,}"
    # Natural language: "the db password is ...", "客户端 secret 为 ...".
    r"|\b(?:password|passwd|secret|token|api[_\- ]?key|passphrase)\s*"
    r"(?:is|为|=)\s*['\"]?\S{8,}",
    re.IGNORECASE,
)


def normalize_memory_content(value: str) -> str:
    """Normalize representation without hiding zero-width attack signals."""

    normalized = unicodedata.normalize("NFKC", value).replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.strip()
    if not normalized:
        raise MemoryKindInvalidError("memory content must not be empty")
    return normalized


def _detection_surfaces(text: str) -> tuple[str, ...]:
    cleaned, _ = strip_invisible(unicodedata.normalize("NFKC", text))
    surfaces = [text]
    if cleaned != text:
        surfaces.append(cleaned)
    surfaces.extend(decode_base64_segments(text))
    return tuple(surfaces)


def _secret_hits(text: str) -> tuple[RuleHit, ...]:
    """Detect exact, obfuscated, and encoded credential assignments."""

    by_rule: dict[str, RuleHit] = {}
    for surface in _detection_surfaces(text):
        for hit in detect_secrets(surface):
            by_rule.setdefault(hit.rule_id, hit)
        matches = _MEMORY_SECRET_ASSIGNMENT.findall(surface)
        if matches:
            by_rule.setdefault(
                "memory.secret_assignment",
                RuleHit(
                    rule_id="memory.secret_assignment",
                    category=RuleCategory.SECRET,
                    detail=f"命中记忆凭据赋值格式 ×{len(matches)}",
                    count=len(matches),
                ),
            )
    return tuple(by_rule[key] for key in sorted(by_rule))


def _pii_hits(text: str) -> tuple[RuleHit, ...]:
    by_rule: dict[str, RuleHit] = {}
    for surface in _detection_surfaces(text):
        for hit in detect_pii(surface):
            by_rule.setdefault(hit.rule_id, hit)
    return tuple(by_rule[key] for key in sorted(by_rule))


@dataclass(frozen=True, slots=True)
class MemoryWriteGuardDecision:
    quarantined: bool
    content_hash: str
    rule_ids: tuple[str, ...]
    ruleset_version: str


class MemoryWriteGuard:
    """Reuse the existing pipeline, then apply stricter persistence policy.

    P0 has no PII allow switch: detected PII is blocked, and detected credentials
    are always blocked.  Instruction-shaped content is eligible only for a
    metadata-only quarantined record after explicit confirmation.
    """

    def __init__(self, pipeline: GuardrailPipeline | None = None) -> None:
        self.pipeline = pipeline if pipeline is not None else default_guardrail_pipeline()

    @property
    def ruleset_version(self) -> str:
        return self.pipeline.ruleset_version

    def check(self, envelope: ContentEnvelope) -> MemoryWriteGuardDecision:
        if envelope.source_tag is not SourceTag.USER_MESSAGE:
            raise MemoryInjectionBlockedError("long-term memory writes require USER_MESSAGE source")

        pipeline_outcome = self.pipeline.check_memory_write(envelope)

        credential_hits = _secret_hits(envelope.text)
        if credential_hits:
            raise MemorySecretBlockedError(
                "credential-like content was blocked before persistence; "
                f"rules={','.join(hit.rule_id for hit in credential_hits)}"
            )

        pii_hits = _pii_hits(envelope.text)
        if pii_hits:
            raise MemoryPiiBlockedError(
                "PII was blocked by the default P0 memory policy; "
                f"rules={','.join(hit.rule_id for hit in pii_hits)}"
            )

        if not pipeline_outcome.allowed:
            rule_ids = tuple(sorted({hit.rule_id for hit in pipeline_outcome.hits}))
            if not rule_ids:
                raise MemoryInjectionBlockedError()
            return MemoryWriteGuardDecision(
                quarantined=True,
                content_hash=envelope.content_hash,
                rule_ids=rule_ids,
                ruleset_version=self.ruleset_version,
            )

        return MemoryWriteGuardDecision(
            quarantined=False,
            content_hash=envelope.content_hash,
            rule_ids=(),
            ruleset_version=self.ruleset_version,
        )

    def govern_semantic_key(self, value: str) -> tuple[str, MemoryWriteGuardDecision]:
        """Apply the memory-write policy to a semantic key as well as the body.

        Keys are metadata, but they are persisted and later used to select a
        memory.  Treating them as an ungoverned side channel would let a
        credential or directive bypass the body guard.  A quarantined key is
        replaced by a deterministic hash-derived surrogate; the original key
        is therefore never persisted.
        """

        normalized = normalize_semantic_key(value)
        decision = self.check(
            ContentEnvelope(source_tag=SourceTag.USER_MESSAGE, text=normalized)
        )
        if decision.quarantined:
            return f"quarantine-key-{decision.content_hash}", decision
        return normalized, decision


__all__ = [
    "MemoryWriteGuard",
    "MemoryWriteGuardDecision",
    "normalize_memory_content",
]
