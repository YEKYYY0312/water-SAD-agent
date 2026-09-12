"""SQLAlchemy metadata for Step 38 business records.

Long-term memory tables intentionally do not appear here; they belong to
Step 39.  Checkpoint tables are included because Alembic is the sole schema
change entrypoint for both business and LangGraph persistence.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, MetaData, String, Text
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
    "Project",
    "ProjectMembership",
    "Run",
    "User",
    "WebSession",
]
