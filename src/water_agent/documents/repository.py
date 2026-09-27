"""Authorized persistence for uploaded documents (P5-S48).

Same discipline as ``storage/repository.py``: the authenticated user id is a
required argument on every lookup, and a resource id is only a selector -- it
never grants access on its own.

Visibility mirrors the existing session/run rules:

- personal upload (``project_id IS NULL``) -- owner only;
- project upload -- any project member (a design team shares project material);
- delete -- owner only (membership makes material readable, not removable).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, exists, or_, select

from water_agent.documents.chunking import UploadChunk
from water_agent.storage.database import Database
from water_agent.storage.models import ProjectMembership, UploadChunkRow, UploadRow
from water_agent.storage.repository import ResourceNotFoundError


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class UploadRecord:
    """Detached projection of an upload row (never carries document body)."""

    upload_id: str
    tenant_id: str
    owner_user_id: str
    project_id: str | None
    filename: str
    safe_filename: str
    suffix: str
    size_bytes: int
    content_hash: str
    extracted_chars: int
    page_count: int | None
    extract_engine: str
    suspicious: bool
    redacted: bool
    created_at: datetime
    chunk_count: int = 0

    @classmethod
    def of(cls, row: UploadRow, *, chunk_count: int = 0) -> UploadRecord:
        return cls(
            upload_id=row.upload_id,
            tenant_id=row.tenant_id,
            owner_user_id=row.owner_user_id,
            project_id=row.project_id,
            filename=row.filename,
            safe_filename=row.safe_filename,
            suffix=row.suffix,
            size_bytes=row.size_bytes,
            content_hash=row.content_hash,
            extracted_chars=row.extracted_chars,
            page_count=row.page_count,
            extract_engine=row.extract_engine,
            suspicious=row.suspicious,
            redacted=row.redacted,
            created_at=row.created_at,
            chunk_count=chunk_count,
        )


def _visible_to(user_key: str):
    """Row-level visibility predicate shared by every read path."""

    return or_(
        (UploadRow.project_id.is_(None)) & (UploadRow.owner_user_id == user_key),
        exists(
            select(ProjectMembership.project_id).where(
                ProjectMembership.project_id == UploadRow.project_id,
                ProjectMembership.user_id == user_key,
            )
        ),
    )


class UploadRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        *,
        upload_id: str,
        tenant_id: UUID | str,
        owner_user_id: UUID | str,
        project_id: UUID | str | None,
        filename: str,
        safe_filename: str,
        suffix: str,
        size_bytes: int,
        content_hash: str,
        extracted_chars: int,
        page_count: int | None,
        extract_engine: str,
        suspicious: bool,
        redacted: bool,
        chunks: list[UploadChunk],
    ) -> UploadRecord:
        """Write the upload row and all its chunks in one transaction.

        One transaction on purpose: an upload row without chunks would be a
        document the workbench lists but retrieval can never see, and the
        operator would have no signal that anything went wrong.
        """

        row = UploadRow(
            upload_id=upload_id,
            tenant_id=str(tenant_id),
            owner_user_id=str(owner_user_id),
            project_id=None if project_id is None else str(project_id),
            filename=filename,
            safe_filename=safe_filename,
            suffix=suffix,
            size_bytes=size_bytes,
            content_hash=content_hash,
            extracted_chars=extracted_chars,
            page_count=page_count,
            extract_engine=extract_engine,
            suspicious=suspicious,
            redacted=redacted,
            created_at=_now(),
        )
        with self.database.transaction() as session:
            session.add(row)
            # flush() 不是多余的一步：这两张表之间**没有** relationship()，所以
            # SQLAlchemy 的 unit of work 不按外键做拓扑排序，而是按表名排序 ——
            # 而 "upload_chunks" 恰好排在 "uploads" 之前。于是 add 顺序正确也不管用，
            # chunk 会先 INSERT 并在开了 PRAGMA foreign_keys 的 SQLite 上直接
            # IntegrityError（实测）。显式 flush 把父行先写进事务，与名字顺序无关。
            session.flush()
            for chunk in chunks:
                session.add(
                    UploadChunkRow(
                        chunk_id=str(uuid4()),
                        upload_id=upload_id,
                        ordinal=chunk.ordinal,
                        page=chunk.page,
                        content=chunk.content,
                    )
                )
        return UploadRecord.of(row, chunk_count=len(chunks))

    def get_for_user(self, upload_id: str, user_id: UUID | str) -> UploadRecord:
        with self.database.transaction() as session:
            row = session.execute(
                select(UploadRow).where(
                    UploadRow.upload_id == str(upload_id),
                    _visible_to(str(user_id)),
                )
            ).scalar_one_or_none()
            if row is None:
                raise ResourceNotFoundError("upload not found")
            count = (
                session.execute(
                    select(UploadChunkRow.chunk_id).where(
                        UploadChunkRow.upload_id == row.upload_id
                    )
                )
                .scalars()
                .all()
            )
            return UploadRecord.of(row, chunk_count=len(count))

    def list_for_project(
        self, project_id: UUID | str, user_id: UUID | str, *, limit: int = 100
    ) -> list[UploadRecord]:
        with self.database.transaction() as session:
            rows = (
                session.execute(
                    select(UploadRow)
                    .where(
                        UploadRow.project_id == str(project_id),
                        _visible_to(str(user_id)),
                    )
                    .order_by(UploadRow.created_at.desc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [UploadRecord.of(row) for row in rows]

    def list_personal(self, user_id: UUID | str, *, limit: int = 100) -> list[UploadRecord]:
        with self.database.transaction() as session:
            rows = (
                session.execute(
                    select(UploadRow)
                    .where(
                        UploadRow.project_id.is_(None),
                        UploadRow.owner_user_id == str(user_id),
                    )
                    .order_by(UploadRow.created_at.desc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [UploadRecord.of(row) for row in rows]

    def delete_owned(self, upload_id: str, user_id: UUID | str) -> UploadRecord:
        """Delete an upload the caller owns; chunks go with it.

        Chunks are removed explicitly rather than relying on the FK cascade:
        SQLite only enforces ``ON DELETE CASCADE`` when ``PRAGMA foreign_keys``
        is on, which is a connection-level setting the application does not own
        everywhere.  Deleting both here makes removal independent of that.
        """

        with self.database.transaction() as session:
            row = session.execute(
                select(UploadRow).where(
                    UploadRow.upload_id == str(upload_id),
                    UploadRow.owner_user_id == str(user_id),
                )
            ).scalar_one_or_none()
            if row is None:
                raise ResourceNotFoundError("upload not found")
            record = UploadRecord.of(row)
            session.execute(
                delete(UploadChunkRow).where(UploadChunkRow.upload_id == row.upload_id)
            )
            session.execute(delete(UploadRow).where(UploadRow.upload_id == row.upload_id))
            return record


__all__ = ["UploadRecord", "UploadRepository"]
