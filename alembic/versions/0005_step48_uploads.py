"""P5-S48 uploaded documents and their retrievable text chunks."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0005_step48_uploads"
down_revision = "0004_step44_workspace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    now = sa.DateTime(timezone=True)
    op.create_table(
        "uploads",
        sa.Column("upload_id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column(
            "owner_user_id",
            sa.String(36),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.project_id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("safe_filename", sa.String(255), nullable=False),
        sa.Column("suffix", sa.String(16), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("extracted_chars", sa.Integer(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("extract_engine", sa.String(32), nullable=False),
        sa.Column(
            "suspicious", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("redacted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", now, nullable=False),
        sa.CheckConstraint(
            "suffix IN ('.docx', '.pdf', '.txt', '.md')", name="ck_uploads_suffix"
        ),
        sa.CheckConstraint("size_bytes > 0", name="ck_uploads_size_positive"),
        sa.CheckConstraint("extracted_chars >= 0", name="ck_uploads_extracted_chars"),
        sa.CheckConstraint(
            "page_count IS NULL OR page_count >= 0", name="ck_uploads_page_count"
        ),
    )
    op.create_index("ix_uploads_tenant_id", "uploads", ["tenant_id"])
    op.create_index("ix_uploads_owner_user_id", "uploads", ["owner_user_id"])
    op.create_index("ix_uploads_project_id", "uploads", ["project_id"])

    op.create_table(
        "upload_chunks",
        sa.Column("chunk_id", sa.String(36), primary_key=True),
        sa.Column(
            "upload_id",
            sa.String(36),
            sa.ForeignKey("uploads.upload_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.CheckConstraint("ordinal >= 0", name="ck_upload_chunks_ordinal"),
        sa.CheckConstraint("page IS NULL OR page >= 1", name="ck_upload_chunks_page"),
        sa.UniqueConstraint(
            "upload_id", "ordinal", name="uq_upload_chunks_upload_ordinal"
        ),
    )
    op.create_index("ix_upload_chunks_upload_id", "upload_chunks", ["upload_id"])

    # PostgreSQL full-text index for upload recall, same shape as the Step 41
    # memory index (0003_step41_memory_fts).  SQLite is the deterministic
    # unit-test substitute and has no ``tsvector`` equivalent, so this part is
    # intentionally a no-op there -- upload recall falls back to the literal
    # keyword score on that dialect (documents/repository.py).
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_upload_chunks_content_fts "
            "ON upload_chunks USING gin "
            "(to_tsvector('simple', coalesce(content, '')))"
        )
    )


def downgrade() -> None:
    raise RuntimeError(
        "Step 48 migrations are forward-only; restore a database backup to roll back"
    )
