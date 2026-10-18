"""P7-S15 record the chosen model per session (default) and per run."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0008_step15_model_choice"
down_revision = "0007_step51_dwg_uploads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 可空列：既有行 = 用全局模型，行为与迁移前逐字相同
    op.add_column("web_sessions", sa.Column("model", sa.String(length=200), nullable=True))
    op.add_column("runs", sa.Column("model", sa.String(length=200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("model")
    with op.batch_alter_table("web_sessions") as batch:
        batch.drop_column("model")
