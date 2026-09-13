"""P5 Step 44 review-only feedback candidates."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0004_step44_workspace"
down_revision = "0003_step41_memory_fts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    now = sa.DateTime(timezone=True)
    op.create_table(
        "feedback_candidates",
        sa.Column("feedback_id", sa.String(36), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("runs.run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "thread_id",
            sa.String(36),
            sa.ForeignKey("web_sessions.thread_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rating", sa.String(16), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="candidate"),
        sa.Column("created_at", now, nullable=False),
        sa.CheckConstraint(
            "rating IN ('useful', 'not_useful')",
            name="ck_feedback_candidates_rating",
        ),
        sa.CheckConstraint(
            "status IN ('candidate', 'reviewed', 'rejected', 'accepted')",
            name="ck_feedback_candidates_status",
        ),
        sa.UniqueConstraint("run_id", "user_id", name="uq_feedback_candidates_run_user"),
    )
    op.create_index("ix_feedback_candidates_user_id", "feedback_candidates", ["user_id"])
    op.create_index("ix_feedback_candidates_run_id", "feedback_candidates", ["run_id"])


def downgrade() -> None:
    raise RuntimeError(
        "Step 44 migrations are forward-only; restore a database backup to roll back"
    )
