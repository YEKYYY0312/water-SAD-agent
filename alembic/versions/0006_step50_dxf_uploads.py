"""P5-S50 allow DXF drawings as uploads (reviewed deterministically on ingest)."""

from __future__ import annotations

from alembic import op

revision = "0006_step50_dxf_uploads"
down_revision = "0005_step48_uploads"
branch_labels = None
depends_on = None

_OLD = "suffix IN ('.docx', '.pdf', '.txt', '.md')"
_NEW = "suffix IN ('.docx', '.pdf', '.txt', '.md', '.dxf')"


def upgrade() -> None:
    # batch_alter_table：SQLite 不支持 ALTER CONSTRAINT，batch 模式重建表；PG 上是
    # 普通的 DROP/ADD CONSTRAINT。两处写法相同，迁移在两个方言上都可执行。
    with op.batch_alter_table("uploads") as batch:
        batch.drop_constraint("ck_uploads_suffix", type_="check")
        batch.create_check_constraint("ck_uploads_suffix", _NEW)


def downgrade() -> None:
    # 回退前必须先删掉 DXF 行，否则旧约束加不回去——显式失败比静默删数据好。
    with op.batch_alter_table("uploads") as batch:
        batch.drop_constraint("ck_uploads_suffix", type_="check")
        batch.create_check_constraint("ck_uploads_suffix", _OLD)
