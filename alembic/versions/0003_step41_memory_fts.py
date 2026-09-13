"""P5 Step 41 PostgreSQL full-text index for active memory recall."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0003_step41_memory_fts"
down_revision = "0002_step39_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the production PostgreSQL FTS index.

    SQLite remains the deterministic unit-test substitute and has no
    PostgreSQL ``tsvector`` equivalent, so this migration intentionally does
    nothing on that dialect.
    """

    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_memory_records_content_fts "
            "ON memory_records USING gin "
            "(to_tsvector('simple', coalesce(content, '')))"
        )
    )


def downgrade() -> None:
    raise RuntimeError(
        "Step 41 migrations are forward-only; restore a database backup to roll back"
    )
