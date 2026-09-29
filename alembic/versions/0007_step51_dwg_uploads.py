"""P5-S51 allow DWG drawings as uploads (converted by LibreDWG, then reviewed)."""

from __future__ import annotations

from alembic import op

revision = "0007_step51_dwg_uploads"
down_revision = "0006_step50_dxf_uploads"
branch_labels = None
depends_on = None

_OLD = "suffix IN ('.docx', '.pdf', '.txt', '.md', '.dxf')"
_NEW = "suffix IN ('.docx', '.pdf', '.txt', '.md', '.dxf', '.dwg')"


def upgrade() -> None:
    with op.batch_alter_table("uploads") as batch:
        batch.drop_constraint("ck_uploads_suffix", type_="check")
        batch.create_check_constraint("ck_uploads_suffix", _NEW)


def downgrade() -> None:
    # 回退前必须先删掉 DWG 行，否则旧约束加不回去——显式失败比静默删数据好。
    with op.batch_alter_table("uploads") as batch:
        batch.drop_constraint("ck_uploads_suffix", type_="check")
        batch.create_check_constraint("ck_uploads_suffix", _OLD)
