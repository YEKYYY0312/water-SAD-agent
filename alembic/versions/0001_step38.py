"""Step 38 PostgreSQL business and LangGraph checkpoint baseline."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0001_step38"
down_revision = None
branch_labels = None
depends_on = None


def _table(name: str, *columns: sa.Column) -> None:
    op.create_table(name, *columns)


def upgrade() -> None:
    now = sa.DateTime(timezone=True)
    checkpoint_json = sa.JSON().with_variant(JSONB(), "postgresql")
    _table(
        "users",
        sa.Column("user_id", sa.String(36), primary_key=True),
        sa.Column("username", sa.String(128), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", now, nullable=False),
        sa.Column("updated_at", now, nullable=False),
    )
    _table(
        "auth_sessions",
        sa.Column("session_id", sa.String(128), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("csrf_token", sa.String(128), nullable=False),
        sa.Column("created_at", now, nullable=False),
        sa.Column("expires_at", now, nullable=False),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    _table(
        "projects",
        sa.Column("project_id", sa.String(36), primary_key=True),
        sa.Column("owner_user_id", sa.String(36), sa.ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("created_at", now, nullable=False),
        sa.Column("updated_at", now, nullable=False),
    )
    op.create_index("ix_projects_owner_user_id", "projects", ["owner_user_id"])
    _table(
        "project_memberships",
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.project_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("created_at", now, nullable=False),
    )
    op.create_index("ix_project_memberships_user_id", "project_memberships", ["user_id"])
    _table(
        "web_sessions",
        sa.Column("thread_id", sa.String(36), primary_key=True),
        sa.Column("owner_user_id", sa.String(36), sa.ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=True),
        sa.Column("title", sa.String(240), nullable=False, server_default=""),
        sa.Column("created_at", now, nullable=False),
        sa.Column("updated_at", now, nullable=False),
    )
    op.create_index("ix_web_sessions_owner_user_id", "web_sessions", ["owner_user_id"])
    op.create_index("ix_web_sessions_project_id", "web_sessions", ["project_id"])
    _table(
        "runs",
        sa.Column("run_id", sa.String(36), primary_key=True),
        sa.Column("thread_id", sa.String(36), sa.ForeignKey("web_sessions.thread_id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_user_id", sa.String(36), sa.ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("created_at", now, nullable=False),
        sa.Column("updated_at", now, nullable=False),
    )
    op.create_index("ix_runs_thread_id", "runs", ["thread_id"])
    op.create_index("ix_runs_owner_user_id", "runs", ["owner_user_id"])

    # LangGraph's official PostgresSaver uses these tables.  They are created
    # by this migration so runtime startup never mutates the schema.
    _table("checkpoint_migrations", sa.Column("v", sa.Integer(), primary_key=True))
    _table(
        "checkpoints",
        sa.Column("thread_id", sa.Text(), primary_key=True),
        sa.Column("checkpoint_ns", sa.Text(), primary_key=True, server_default=""),
        sa.Column("checkpoint_id", sa.Text(), primary_key=True),
        sa.Column("parent_checkpoint_id", sa.Text()),
        sa.Column("type", sa.Text()),
        sa.Column("checkpoint", checkpoint_json, nullable=False),
        sa.Column("metadata", checkpoint_json, nullable=False, server_default="{}"),
    )
    op.create_index("checkpoints_thread_id_idx", "checkpoints", ["thread_id"])
    _table(
        "checkpoint_blobs",
        sa.Column("thread_id", sa.Text(), primary_key=True),
        sa.Column("checkpoint_ns", sa.Text(), primary_key=True, server_default=""),
        sa.Column("channel", sa.Text(), primary_key=True),
        sa.Column("version", sa.Text(), primary_key=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("blob", sa.LargeBinary()),
    )
    op.create_index("checkpoint_blobs_thread_id_idx", "checkpoint_blobs", ["thread_id"])
    _table(
        "checkpoint_writes",
        sa.Column("thread_id", sa.Text(), primary_key=True),
        sa.Column("checkpoint_ns", sa.Text(), primary_key=True, server_default=""),
        sa.Column("checkpoint_id", sa.Text(), primary_key=True),
        sa.Column("task_id", sa.Text(), primary_key=True),
        sa.Column("idx", sa.Integer(), primary_key=True),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("type", sa.Text()),
        sa.Column("blob", sa.LargeBinary(), nullable=False),
        sa.Column("task_path", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("checkpoint_writes_thread_id_idx", "checkpoint_writes", ["thread_id"])
    # 3.1.2 ships ten migrations (0..9); marking the installed package head
    # prevents PostgresSaver.setup from becoming a second schema authority.
    op.execute(sa.text("INSERT INTO checkpoint_migrations (v) VALUES (9)"))


def downgrade() -> None:
    raise RuntimeError("Step 38 migrations are forward-only; restore a database backup to roll back")
