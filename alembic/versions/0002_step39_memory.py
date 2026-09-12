"""P5 Step 39 scoped long-term memory tables."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002_step39_memory"
down_revision = "0001_step38"
branch_labels = None
depends_on = None


def upgrade() -> None:
    now = sa.DateTime(timezone=True)
    op.create_table(
        "memory_records",
        sa.Column("memory_id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column(
            "owner_user_id",
            sa.String(36),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scope_type", sa.String(16), nullable=False),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.project_id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("semantic_key", sa.String(255), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("origin", sa.String(32), nullable=False),
        sa.Column("source_thread_id", sa.String(36), nullable=True),
        sa.Column("source_message_id", sa.String(36), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", now, nullable=False),
        sa.Column("updated_at", now, nullable=False),
        sa.Column("expires_at", now, nullable=True),
        sa.Column("tombstone", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint(
            "scope_type IN ('user', 'project')",
            name="ck_memory_records_scope_type",
        ),
        sa.CheckConstraint(
            "(scope_type = 'user' AND project_id IS NULL) OR "
            "(scope_type = 'project' AND project_id IS NOT NULL)",
            name="ck_memory_records_scope_project",
        ),
        sa.CheckConstraint(
            "kind IN ('user_preference', 'project_fact', 'project_decision', 'workflow_note')",
            name="ck_memory_records_kind",
        ),
        sa.CheckConstraint(
            "(kind = 'user_preference' AND scope_type = 'user') OR "
            "(kind IN ('project_fact', 'project_decision') AND scope_type = 'project') OR "
            "kind = 'workflow_note'",
            name="ck_memory_records_kind_scope",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'superseded', 'expired', 'forgotten', 'quarantined')",
            name="ck_memory_records_status",
        ),
        sa.CheckConstraint(
            "origin IN ('explicit_user', 'approved_import')",
            name="ck_memory_records_origin",
        ),
        sa.CheckConstraint("revision > 0", name="ck_memory_records_revision_positive"),
        sa.CheckConstraint(
            "(status = 'forgotten' AND tombstone = TRUE) OR "
            "(status <> 'forgotten' AND tombstone = FALSE)",
            name="ck_memory_records_tombstone_status",
        ),
        sa.CheckConstraint(
            "(status IN ('active', 'superseded', 'expired') AND content IS NOT NULL "
            "AND length(trim(content)) > 0) OR "
            "(status IN ('forgotten', 'quarantined') AND (content IS NULL OR content = ''))",
            name="ck_memory_records_body_status",
        ),
    )
    op.create_index("ix_memory_records_tenant_id", "memory_records", ["tenant_id"])
    op.create_index("ix_memory_records_project_id", "memory_records", ["project_id"])
    op.create_index(
        "uq_memory_records_active_user_key",
        "memory_records",
        ["tenant_id", "owner_user_id", "kind", "semantic_key"],
        unique=True,
        sqlite_where=sa.text("scope_type = 'user' AND status = 'active'"),
        postgresql_where=sa.text("scope_type = 'user' AND status = 'active'"),
    )
    op.create_index(
        "uq_memory_records_active_project_key",
        "memory_records",
        ["tenant_id", "project_id", "kind", "semantic_key"],
        unique=True,
        sqlite_where=sa.text("scope_type = 'project' AND status = 'active'"),
        postgresql_where=sa.text("scope_type = 'project' AND status = 'active'"),
    )

    op.create_table(
        "memory_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("event_type", sa.String(16), nullable=False),
        sa.Column(
            "memory_id",
            sa.String(36),
            sa.ForeignKey("memory_records.memory_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("actor_hash", sa.String(128), nullable=False),
        sa.Column("before_content_hash", sa.String(128), nullable=True),
        sa.Column("after_content_hash", sa.String(128), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", now, nullable=False),
        sa.Column("source_thread_id", sa.String(36), nullable=True),
        sa.Column("source_message_id", sa.String(36), nullable=True),
        sa.Column("reason", sa.String(256), nullable=False, server_default=""),
        sa.CheckConstraint(
            "event_type IN ('created', 'reaffirmed', 'corrected', 'superseded', "
            "'expired', 'forgotten', 'quarantined', 'recalled')",
            name="ck_memory_events_event_type",
        ),
        sa.CheckConstraint("revision > 0", name="ck_memory_events_revision_positive"),
    )
    op.create_index("ix_memory_events_memory_id", "memory_events", ["memory_id"])

    op.create_table(
        "memory_usage",
        sa.Column("usage_id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column(
            "memory_id",
            sa.String(36),
            sa.ForeignKey("memory_records.memory_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_thread_id", sa.String(36), nullable=True),
        sa.Column("run_id", sa.String(36), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("created_at", now, nullable=False),
    )
    op.create_index("ix_memory_usage_memory_id", "memory_usage", ["memory_id"])
    op.create_index(
        "ix_memory_usage_tenant_user",
        "memory_usage",
        ["tenant_id", "user_id"],
    )


def downgrade() -> None:
    raise RuntimeError(
        "Step 39 migrations are forward-only; restore a database backup to roll back"
    )
