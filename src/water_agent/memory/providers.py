"""Pluggable long-term-memory recall providers for P5 Step 46.

The native PostgreSQL FTS provider remains the default and the source of truth.
The pgvector provider is opt-in: it stores vectors in a separate table, never
copies memory bodies, applies the same tenant/user/project predicates, and is
allowed to fail back to native FTS.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from water_agent.memory.contracts import MemoryRecord, MemoryStatus
from water_agent.storage.database import Database

MAX_PGVECTOR_DIMENSIONS = 4096
PGVECTOR_TABLE = "memory_embeddings"


class MemoryProviderUnavailableError(RuntimeError):
    """A selectable provider cannot serve this request.

    The recall service catches this error only for an optional provider and
    retries with the native PostgreSQL FTS provider.  It is intentionally not
    a domain success/error code and never hides authorization failures from
    the native repository.
    """


@dataclass(frozen=True, slots=True)
class MemoryProviderRequest:
    database: Database
    tenant_id: UUID | str
    actor_user_id: UUID | str
    query: str
    project_id: UUID | str | None
    now: datetime
    candidate_limit: int = 32

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_limit, int) or self.candidate_limit <= 0:
            raise ValueError("memory provider candidate_limit must be a positive integer")


@dataclass(frozen=True, slots=True)
class MemoryProviderHit:
    memory: MemoryRecord
    score: float

    def __post_init__(self) -> None:
        if not math.isfinite(float(self.score)):
            raise ValueError("memory provider score must be finite")
        object.__setattr__(self, "score", float(self.score))


@runtime_checkable
class MemoryProvider(Protocol):
    """Read-only recall contract; providers never own authorization policy."""

    name: str

    def search(self, request: MemoryProviderRequest) -> Sequence[MemoryProviderHit]:
        """Return candidate records after applying hard scope predicates."""


class MemoryEmbeddingStore(Protocol):
    """Optional transaction hook used to remove vectors when memory is forgotten."""

    def delete_forget(self, session: Session, memory_id: str) -> None:
        """Delete all vector material for a memory inside the parent transaction."""


class PostgresFtsMemoryProvider:
    """The existing deterministic PostgreSQL/SQLite FTS implementation."""

    name = "postgres_fts"

    def search(self, request: MemoryProviderRequest) -> Sequence[MemoryProviderHit]:
        # Local import avoids making recall.py depend on a second copy of the
        # FTS query or creating an import cycle.  This class is the public
        # provider boundary; the query remains implemented once in recall.py.
        from water_agent.memory.recall import _fts_search_hits

        return _fts_search_hits(request)


def _as_uuid(value: UUID | str) -> str:
    return str(value if isinstance(value, UUID) else UUID(str(value)))


class PgVectorMemoryProvider:
    """Optional pgvector-backed semantic candidates.

    This provider does not become active merely because the module is
    importable.  The caller must explicitly select it and provision the
    ``vector`` extension/table with :meth:`ensure_schema`.
    """

    name = "pgvector"

    def __init__(
        self,
        embedding: Callable[[str], Sequence[float]],
        *,
        dimensions: int,
        candidate_limit: int = 32,
    ) -> None:
        self.embedding = embedding
        self.dimensions = dimensions
        self.candidate_limit = candidate_limit
        self._validate_dimensions(dimensions)
        if candidate_limit <= 0:
            raise ValueError("pgvector candidate_limit must be positive")

    @staticmethod
    def _validate_dimensions(dimensions: int) -> None:
        if (
            not isinstance(dimensions, int)
            or isinstance(dimensions, bool)
            or dimensions <= 0
            or dimensions > MAX_PGVECTOR_DIMENSIONS
        ):
            raise ValueError(
                f"pgvector dimensions must be an integer in 1..{MAX_PGVECTOR_DIMENSIONS}"
            )

    def _validate_vector(self, vector: Sequence[float]) -> tuple[float, ...]:
        if len(vector) != self.dimensions:
            raise ValueError(
                f"pgvector vector dimensions mismatch: expected {self.dimensions}, got {len(vector)}"
            )
        values = tuple(float(value) for value in vector)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("pgvector vector values must be finite")
        return values

    def _embed(self, value: str) -> tuple[float, ...]:
        try:
            return self._validate_vector(self.embedding(value))
        except MemoryProviderUnavailableError:
            raise
        except (TypeError, ValueError, OSError, RuntimeError) as exc:
            raise MemoryProviderUnavailableError("memory embedding provider unavailable") from exc

    @staticmethod
    def _vector_literal(vector: Sequence[float]) -> str:
        # Values are validated finite floats and are passed as one bound value;
        # memory text never enters this literal.
        return "[" + ",".join(format(float(value), ".17g") for value in vector) + "]"

    def ensure_schema(self, database: Database) -> None:
        """Provision the optional extension/table, never as a core migration."""

        if database.engine.dialect.name != "postgresql":
            raise MemoryProviderUnavailableError("pgvector requires PostgreSQL")
        table_sql = f"""
            CREATE TABLE IF NOT EXISTS {PGVECTOR_TABLE} (
                memory_id VARCHAR(36) PRIMARY KEY
                    REFERENCES memory_records(memory_id) ON DELETE CASCADE,
                embedding vector({self.dimensions}) NOT NULL,
                created_at TIMESTAMPTZ NOT NULL
            )
        """
        index_sql = f"""
            CREATE INDEX IF NOT EXISTS ix_{PGVECTOR_TABLE}_embedding_cosine
            ON {PGVECTOR_TABLE} USING hnsw (embedding vector_cosine_ops)
        """
        try:
            with database.transaction() as session:
                session.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                session.execute(text(table_sql))
                session.execute(text(index_sql))
        except (OSError, SQLAlchemyError) as exc:
            raise MemoryProviderUnavailableError("pgvector schema is unavailable") from exc

    def index_memory(self, database: Database, memory: MemoryRecord) -> None:
        """Explicitly index one active memory; no automatic P0 write hook."""

        if memory.status is not MemoryStatus.ACTIVE or not memory.content:
            raise ValueError("only active memory with正文 can be indexed")
        vector = self._embed(memory.content)
        try:
            with database.transaction() as session:
                session.execute(
                    text(
                        f"""
                        INSERT INTO {PGVECTOR_TABLE} (memory_id, embedding, created_at)
                        VALUES (:memory_id, CAST(:embedding AS vector), :created_at)
                        ON CONFLICT (memory_id) DO UPDATE SET
                            embedding = EXCLUDED.embedding,
                            created_at = EXCLUDED.created_at
                        """
                    ),
                    {
                        "memory_id": str(memory.memory_id),
                        "embedding": self._vector_literal(vector),
                        "created_at": memory.updated_at,
                    },
                )
        except (OSError, SQLAlchemyError) as exc:
            raise MemoryProviderUnavailableError("pgvector index is unavailable") from exc

    def delete_forget(self, session: Session, memory_id: str) -> None:
        """Delete vector material in the same transaction as the tombstone."""

        try:
            session.execute(
                text(f"DELETE FROM {PGVECTOR_TABLE} WHERE memory_id = :memory_id"),
                {"memory_id": memory_id},
            )
        except (OSError, SQLAlchemyError) as exc:
            raise MemoryProviderUnavailableError("pgvector cleanup is unavailable") from exc

    def search(self, request: MemoryProviderRequest) -> Sequence[MemoryProviderHit]:
        if request.database.engine.dialect.name != "postgresql":
            raise MemoryProviderUnavailableError("pgvector requires PostgreSQL")
        vector = self._embed(request.query)
        tenant_id = _as_uuid(request.tenant_id)
        actor_user_id = _as_uuid(request.actor_user_id)
        project_id = None if request.project_id is None else _as_uuid(request.project_id)
        statement = text(
            f"""
            SELECT
                mr.memory_id, mr.tenant_id, mr.owner_user_id, mr.scope_type,
                mr.project_id, mr.kind, mr.semantic_key, mr.content, mr.status,
                mr.origin, mr.source_thread_id, mr.source_message_id, mr.revision,
                mr.created_at, mr.updated_at, mr.expires_at, mr.tombstone,
                1.0 - (me.embedding <=> CAST(:query_embedding AS vector)) AS semantic_score
            FROM memory_records AS mr
            JOIN {PGVECTOR_TABLE} AS me ON me.memory_id = mr.memory_id
            WHERE mr.tenant_id = :tenant_id
              AND mr.status = 'active'
              AND mr.tombstone = FALSE
              AND (mr.expires_at IS NULL OR mr.expires_at > :now)
              AND EXISTS (
                  SELECT 1 FROM users AS authenticated_user
                  WHERE authenticated_user.user_id = :actor_user_id
                    AND authenticated_user.is_active = TRUE
              )
              AND (
                  (
                      mr.scope_type = 'user'
                      AND mr.owner_user_id = :actor_user_id
                      AND mr.project_id IS NULL
                  )
                  OR
                  (
                      mr.scope_type = 'project'
                      AND CAST(:project_id AS text) IS NOT NULL
                      AND mr.project_id = CAST(:project_id AS text)
                      AND EXISTS (
                          SELECT 1 FROM project_memberships AS membership
                          WHERE membership.project_id = mr.project_id
                            AND membership.user_id = :actor_user_id
                      )
                  )
              )
            ORDER BY me.embedding <=> CAST(:query_embedding AS vector), mr.memory_id
            LIMIT :candidate_limit
            """
        )
        try:
            with request.database.transaction() as session:
                rows = session.execute(
                    statement,
                    {
                        "query_embedding": self._vector_literal(vector),
                        "tenant_id": tenant_id,
                        "actor_user_id": actor_user_id,
                        "project_id": project_id,
                        "now": request.now,
                        "candidate_limit": min(request.candidate_limit, self.candidate_limit),
                    },
                ).mappings().all()
        except (OSError, SQLAlchemyError) as exc:
            raise MemoryProviderUnavailableError("pgvector search is unavailable") from exc

        from water_agent.memory.recall import _record_from_row

        return [
            MemoryProviderHit(
                memory=_record_from_row(row),
                score=float(row["semantic_score"]),
            )
            for row in rows
        ]


class MemoryMCPProvider:
    """Use a third-party Memory MCP only for ranked ID hints.

    The adapter never supplies active memory正文. Every returned ID is
    resolved through the native repository's authorization query, so a remote
    server cannot become the source of truth or widen user/project scope.
    """

    name = "memory_mcp"

    def __init__(self, adapter: Any, repository: Any) -> None:
        self.adapter = adapter
        self.repository = repository

    def search(self, request: MemoryProviderRequest) -> Sequence[MemoryProviderHit]:
        try:
            remote_hits = self.adapter.search_ids(
                query=request.query,
                tenant_id=request.tenant_id,
                actor_user_id=request.actor_user_id,
                project_id=request.project_id,
                limit=request.candidate_limit,
            )
        except MemoryProviderUnavailableError:
            raise
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            raise MemoryProviderUnavailableError("Memory MCP provider unavailable") from exc

        scores: dict[UUID, float] = {}
        for memory_id, score in remote_hits:
            try:
                parsed_id = memory_id if isinstance(memory_id, UUID) else UUID(str(memory_id))
                parsed_score = float(score)
            except (TypeError, ValueError) as exc:
                raise MemoryProviderUnavailableError("Memory MCP returned invalid hit") from exc
            if not math.isfinite(parsed_score):
                raise MemoryProviderUnavailableError("Memory MCP returned a non-finite score")
            scores[parsed_id] = parsed_score

        records = self.repository.list_active_by_ids(
            tuple(scores),
            tenant_id=request.tenant_id,
            actor_user_id=request.actor_user_id,
            now=request.now,
        )
        return [
            MemoryProviderHit(memory=record, score=scores[record.memory_id])
            for record in records
            if record.memory_id in scores
        ]


__all__ = [
    "MAX_PGVECTOR_DIMENSIONS",
    "PGVECTOR_TABLE",
    "MemoryEmbeddingStore",
    "MemoryMCPProvider",
    "MemoryProvider",
    "MemoryProviderHit",
    "MemoryProviderRequest",
    "MemoryProviderUnavailableError",
    "PgVectorMemoryProvider",
    "PostgresFtsMemoryProvider",
]
