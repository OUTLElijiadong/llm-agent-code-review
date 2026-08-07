"""Add project_source_revision table for repaired source copies.

Revision ID: 029
Revises: 028
Create Date: 2026-08-07

保存语法修复 Agent 产出的"修复后源码"副本,作为项目的一部分;
下次审计可选择原始源码或修复副本。兼容 SQLite 与 MySQL,幂等。
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "029"
down_revision: Union[str, None] = "028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(conn, name: str) -> bool:
    return sa.inspect(conn).has_table(name)


def upgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "project_source_revision"):
        return
    dialect = bind.dialect.name
    if dialect == "mysql":
        blob_type = sa.LargeBinary().with_variant(sa.LargeBinary, "mysql")
        # MySQL 必须用 LONGBLOB(修复后源码 zip 可达上百 MB),不能用默认 BLOB(64KB)
        from sqlalchemy.dialects.mysql import LONGBLOB
        blob_type = LONGBLOB()
    else:
        blob_type = sa.LargeBinary
    op.create_table(
        "project_source_revision",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("parent_sha256", sa.String(length=64), nullable=True),
        sa.Column("repaired_files_json", sa.Text(), nullable=False),
        sa.Column("repair_notes", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("archive_blob", blob_type, nullable=False),
        sa.Column("create_time", sa.DateTime(), nullable=False),
        sa.Column("update_time", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "revision_no", name="uk_project_source_revision_no"),
    )
    op.create_index("ix_project_source_revision_project", "project_source_revision", ["project_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, "project_source_revision"):
        return
    op.drop_index("ix_project_source_revision_project", table_name="project_source_revision")
    op.drop_table("project_source_revision")
