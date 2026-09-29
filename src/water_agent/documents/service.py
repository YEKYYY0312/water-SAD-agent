"""Upload ingest pipeline (P5-S48 Step 1).

One function owns the whole order of operations, because the order *is* the
security property:

1. name / extension / magic validation -- before any parser touches the bytes;
2. text extraction (no OCR, no images);
3. **redact the whole extracted text once** (PII + credentials) -- this is what
   reaches both the disk copy and every chunk, so a credential that straddles a
   chunk boundary is still caught (per-chunk redaction would miss it);
4. chunk the already-redacted text;
5. run the indirect-injection check **per chunk**, because a chunk is what
   actually enters the prompt, and the guardrail's whole-content byte ceiling
   (``MAX_CONTENT_BYTES`` = 200 KB) would otherwise reject a perfectly ordinary
   large design document outright;
6. write file + ``extracted.txt`` to the uploads volume;
7. persist the row and chunks in one transaction.

An injection hit **never blocks** the upload: uploaded text is data, not
instruction, and the actual isolation is ``SourceTag.RETRIEVED_FILE`` not being
in ``INSTRUCTION_BEARING_SOURCES``.  The hit is recorded as ``suspicious`` so an
answer built on a poisoned document can be recognised as such -- dropping the
document instead would just make a poisoned upload look like a broken one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from water_agent.documents.chunking import UploadChunk, chunk_upload_text
from water_agent.documents.errors import (
    CODE_UPLOAD_INJECTION_BLOCKED,
    CODE_UPLOAD_TOO_LARGE,
    UploadError,
)
from water_agent.documents.extractors import extract_text, safe_filename, suffix_of
from water_agent.documents.repository import UploadRecord, UploadRepository
from water_agent.documents.storage import resolve_upload_dir, write_upload_files
from water_agent.domain.models import content_hash_of
from water_agent.runtime.governance.contracts import (
    CODE_INPUT_TOO_LARGE,
    GovernanceError,
    SourceTag,
)
from water_agent.runtime.governance.pipeline import GuardrailPipeline, default_guardrail_pipeline
from water_agent.storage.database import Database

#: Response preview length (spec D1).  A preview is a convenience, not the
#: document: the full body is only reachable through authorized chunk retrieval.
PREVIEW_CHARACTERS = 200


@dataclass(frozen=True, slots=True)
class UploadIngestResult:
    record: UploadRecord
    preview: str
    chunks: tuple[UploadChunk, ...]

    def response_payload(self) -> dict[str, object]:
        """Spec D1 response shape."""

        return {
            "upload_id": self.record.upload_id,
            "filename": self.record.safe_filename,
            "size_bytes": self.record.size_bytes,
            "extracted_text_length": self.record.extracted_chars,
            "extracted_text_preview": self.preview,
            "page_count": self.record.page_count,
            "chunk_count": self.record.chunk_count,
            # suspicious = 注入检测命中（内容照旧可用，但据此作答要能被识别）；
            # redacted   = PII/凭据已被剥离（用户上传的原始文件仍完整保留在卷上）。
            # 两件事分开报：混成一个字段会让"这份文件被脱敏过"和"这份文件可疑"
            # 无法区分，而它们的处置完全不同。
            "suspicious": self.record.suspicious,
            "redacted": self.record.redacted,
            "project_id": self.record.project_id,
            "created_at": self.record.created_at.isoformat(),
            "suffix": self.record.suffix,
        }


def _guard_chunks(
    pipeline: GuardrailPipeline, upload_id: str, chunks: list[UploadChunk]
) -> tuple[list[UploadChunk], bool]:
    """Run the indirect-injection check per chunk; return (chunks, suspicious)."""

    checked: list[UploadChunk] = []
    suspicious = False
    for chunk in chunks:
        envelope = pipeline.wrap_untrusted(
            chunk.content,
            source_tag=SourceTag.RETRIEVED_FILE,
            reference_id=f"{upload_id}#{chunk.ordinal}",
        )
        outcome = pipeline.check_retrieved_content(envelope)
        if not outcome.allowed:
            # The only reachable BLOCK is the byte ceiling; RETRIEVED_FILE can
            # never be instruction-bearing.  Both map to a stable upload code
            # rather than leaking the governance code into the HTTP surface.
            code = (
                CODE_UPLOAD_TOO_LARGE
                if outcome.decision.code == CODE_INPUT_TOO_LARGE
                else CODE_UPLOAD_INJECTION_BLOCKED
            )
            raise UploadError(code, "上传内容未通过护栏检查（文本块过大或来源标记非法）")
        if outcome.redacted:
            suspicious = True
        checked.append(
            UploadChunk(ordinal=chunk.ordinal, page=chunk.page, content=outcome.content)
        )
    return checked, suspicious


def ingest_upload(
    *,
    database: Database,
    tenant_id: UUID | str,
    owner_user_id: UUID | str,
    project_id: UUID | str | None,
    filename: str,
    payload: bytes,
    upload_root: Path | None = None,
    pipeline: GuardrailPipeline | None = None,
) -> UploadIngestResult:
    """Validate, extract, guard, persist one upload.

    Raises:
        UploadError: any stable upload failure (type / magic / size / extraction
            / guardrail block).
    """

    if not payload:
        raise UploadError("UPLOAD_EXTRACTION_FAILED", "文件内容为空")
    guardrails = pipeline if pipeline is not None else default_guardrail_pipeline()
    suffix = suffix_of(filename)
    stored_name = safe_filename(filename)
    extracted = extract_text(payload, suffix, display_name=stored_name)

    try:
        persisted_text, redaction_hits = guardrails.sanitize_for_persistence(extracted.text)
    except GovernanceError as exc:  # pragma: no cover -- defensive
        raise UploadError(CODE_UPLOAD_INJECTION_BLOCKED, "上传内容未通过脱敏检查") from exc

    upload_id = str(uuid4())
    chunks, suspicious = _guard_chunks(guardrails, upload_id, chunk_upload_text(persisted_text))

    root = resolve_upload_dir(upload_root)
    write_upload_files(
        root,
        upload_id,
        filename=stored_name,
        payload=payload,
        extracted_text=persisted_text,
    )

    repository = UploadRepository(database)
    record = repository.create(
        upload_id=upload_id,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        project_id=project_id,
        filename=filename[:255],
        safe_filename=stored_name,
        suffix=suffix,
        size_bytes=len(payload),
        content_hash=content_hash_of(persisted_text),
        extracted_chars=len(persisted_text),
        page_count=extracted.page_count,
        extract_engine=extracted.engine,
        suspicious=suspicious or extracted.suspicious,
        redacted=bool(redaction_hits),
        chunks=chunks,
    )
    return UploadIngestResult(
        record=record,
        preview=persisted_text[:PREVIEW_CHARACTERS],
        chunks=tuple(chunks),
    )


__all__ = ["PREVIEW_CHARACTERS", "UploadIngestResult", "ingest_upload"]
