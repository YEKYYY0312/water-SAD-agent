"""SQLAlchemy metadata for P5 business and memory records.

Checkpoint and long-term memory tables are included because Alembic is the
sole schema change entrypoint for both business and LangGraph persistence.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    metadata = MetaData()


_CHECKPOINT_JSON = JSON().with_variant(JSONB(), "postgresql")


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False)
    csrf_token: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_auth_sessions_user_id", "user_id"),)


class Project(Base):
    __tablename__ = "projects"

    project_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_projects_owner_user_id", "owner_user_id"),)


class ProjectMembership(Base):
    __tablename__ = "project_memberships"

    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("projects.project_id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_project_memberships_user_id", "user_id"),)


class WebSession(Base):
    __tablename__ = "web_sessions"

    thread_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=True)
    title: Mapped[str] = mapped_column(String(240), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_web_sessions_owner_user_id", "owner_user_id"),
        Index("ix_web_sessions_project_id", "project_id"),
    )


class Run(Base):
    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(36), ForeignKey("web_sessions.thread_id", ondelete="CASCADE"), nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_runs_thread_id", "thread_id"),
        Index("ix_runs_owner_user_id", "owner_user_id"),
    )


class MemoryRecordRow(Base):
    __tablename__ = "memory_records"

    memory_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    owner_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False
    )
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    semantic_key: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    origin: Mapped[str] = mapped_column(String(32), nullable=False)
    source_thread_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tombstone: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

    __table_args__ = (
        CheckConstraint(
            "scope_type IN ('user', 'project')",
            name="ck_memory_records_scope_type",
        ),
        CheckConstraint(
            "(scope_type = 'user' AND project_id IS NULL) OR "
            "(scope_type = 'project' AND project_id IS NOT NULL)",
            name="ck_memory_records_scope_project",
        ),
        CheckConstraint(
            "kind IN ('user_preference', 'project_fact', 'project_decision', 'workflow_note')",
            name="ck_memory_records_kind",
        ),
        CheckConstraint(
            "(kind = 'user_preference' AND scope_type = 'user') OR "
            "(kind IN ('project_fact', 'project_decision') AND scope_type = 'project') OR "
            "kind = 'workflow_note'",
            name="ck_memory_records_kind_scope",
        ),
        CheckConstraint(
            "status IN ('active', 'superseded', 'expired', 'forgotten', 'quarantined')",
            name="ck_memory_records_status",
        ),
        CheckConstraint(
            "origin IN ('explicit_user', 'approved_import')",
            name="ck_memory_records_origin",
        ),
        CheckConstraint("revision > 0", name="ck_memory_records_revision_positive"),
        CheckConstraint(
            "(status = 'forgotten' AND tombstone = TRUE) OR "
            "(status <> 'forgotten' AND tombstone = FALSE)",
            name="ck_memory_records_tombstone_status",
        ),
        CheckConstraint(
            "(status IN ('active', 'superseded', 'expired') AND content IS NOT NULL "
            "AND length(trim(content)) > 0) OR "
            "(status IN ('forgotten', 'quarantined') AND (content IS NULL OR content = ''))",
            name="ck_memory_records_body_status",
        ),
        Index("ix_memory_records_tenant_id", "tenant_id"),
        Index("ix_memory_records_project_id", "project_id"),
        Index(
            "uq_memory_records_active_user_key",
            "tenant_id",
            "owner_user_id",
            "kind",
            "semantic_key",
            unique=True,
            sqlite_where=text("scope_type = 'user' AND status = 'active'"),
            postgresql_where=text("scope_type = 'user' AND status = 'active'"),
        ),
        Index(
            "uq_memory_records_active_project_key",
            "tenant_id",
            "project_id",
            "kind",
            "semantic_key",
            unique=True,
            sqlite_where=text("scope_type = 'project' AND status = 'active'"),
            postgresql_where=text("scope_type = 'project' AND status = 'active'"),
        ),
    )


class MemoryEventRow(Base):
    __tablename__ = "memory_events"

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)
    memory_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("memory_records.memory_id", ondelete="CASCADE"), nullable=False
    )
    actor_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    before_content_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    after_content_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_thread_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reason: Mapped[str] = mapped_column(String(256), nullable=False, default="")

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('created', 'reaffirmed', 'corrected', 'superseded', "
            "'expired', 'forgotten', 'quarantined', 'recalled')",
            name="ck_memory_events_event_type",
        ),
        CheckConstraint("revision > 0", name="ck_memory_events_revision_positive"),
        Index("ix_memory_events_memory_id", "memory_id"),
    )


class MemoryUsageRow(Base):
    __tablename__ = "memory_usage"

    usage_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    memory_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("memory_records.memory_id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False
    )
    source_thread_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_memory_usage_memory_id", "memory_id"),
        Index("ix_memory_usage_tenant_user", "tenant_id", "user_id"),
    )


class CheckpointMigration(Base):
    __tablename__ = "checkpoint_migrations"

    v: Mapped[int] = mapped_column(Integer, primary_key=True)


class Checkpoint(Base):
    __tablename__ = "checkpoints"

    thread_id: Mapped[str] = mapped_column(Text, primary_key=True)
    checkpoint_ns: Mapped[str] = mapped_column(Text, primary_key=True, default="")
    checkpoint_id: Mapped[str] = mapped_column(Text, primary_key=True)
    parent_checkpoint_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    type: Mapped[str | None] = mapped_column(Text, nullable=True)
    checkpoint: Mapped[dict] = mapped_column(_CHECKPOINT_JSON, nullable=False)
    metadata_json: Mapped[dict] = mapped_column("metadata", _CHECKPOINT_JSON, nullable=False, default=dict)

    __table_args__ = (Index("checkpoints_thread_id_idx", "thread_id"),)


class CheckpointBlob(Base):
    __tablename__ = "checkpoint_blobs"

    thread_id: Mapped[str] = mapped_column(Text, primary_key=True)
    checkpoint_ns: Mapped[str] = mapped_column(Text, primary_key=True, default="")
    channel: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[str] = mapped_column(Text, primary_key=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    blob: Mapped[bytes | None] = mapped_column(nullable=True)

    __table_args__ = (Index("checkpoint_blobs_thread_id_idx", "thread_id"),)


class CheckpointWrite(Base):
    __tablename__ = "checkpoint_writes"

    thread_id: Mapped[str] = mapped_column(Text, primary_key=True)
    checkpoint_ns: Mapped[str] = mapped_column(Text, primary_key=True, default="")
    checkpoint_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(Text, primary_key=True)
    idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str | None] = mapped_column(Text, nullable=True)
    blob: Mapped[bytes] = mapped_column(nullable=False)
    task_path: Mapped[str] = mapped_column(Text, nullable=False, default="")

    __table_args__ = (Index("checkpoint_writes_thread_id_idx", "thread_id"),)


__all__ = [
    "AuthSession",
    "Base",
    "Checkpoint",
    "CheckpointBlob",
    "CheckpointMigration",
    "CheckpointWrite",
    "MemoryEventRow",
    "MemoryRecordRow",
    "MemoryUsageRow",
    "Project",
    "ProjectMembership",
    "Run",
    "User",
    "WebSession",
]
