"""Expand source archive and binary member storage capacity.

Revision ID: 026
Revises: 025
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.mysql import BIGINT, BLOB, LONGBLOB

from alembic import op

revision: str = "026"
down_revision: Union[str, None] = "025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_BLOB_MAX_BYTES = 65_535


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.alter_column(
        "code_file",
        "original_blob",
        existing_type=BLOB(),
        type_=LONGBLOB(),
        existing_nullable=True,
    )
    op.alter_column(
        "code_file",
        "size_bytes",
        existing_type=sa.Integer(),
        type_=BIGINT(),
        existing_nullable=False,
        existing_server_default=sa.text("0"),
    )
    op.alter_column(
        "code_file",
        "raw_size",
        existing_type=sa.Integer(),
        type_=BIGINT(),
        existing_nullable=False,
        existing_server_default=sa.text("0"),
    )
    op.alter_column(
        "project_source_archive",
        "compressed_size",
        existing_type=sa.Integer(),
        type_=BIGINT(),
        existing_nullable=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    oversized = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM code_file "
            "WHERE original_blob IS NOT NULL AND OCTET_LENGTH(original_blob) > :limit"
        ),
        {"limit": _BLOB_MAX_BYTES},
    ).scalar_one()
    if int(oversized or 0) > 0:
        raise RuntimeError("存在超过旧 BLOB 容量的二进制源码，拒绝降级截断数据")
    op.alter_column(
        "project_source_archive",
        "compressed_size",
        existing_type=BIGINT(),
        type_=sa.Integer(),
        existing_nullable=False,
    )
    op.alter_column(
        "code_file",
        "raw_size",
        existing_type=BIGINT(),
        type_=sa.Integer(),
        existing_nullable=False,
        existing_server_default=sa.text("0"),
    )
    op.alter_column(
        "code_file",
        "size_bytes",
        existing_type=BIGINT(),
        type_=sa.Integer(),
        existing_nullable=False,
        existing_server_default=sa.text("0"),
    )
    op.alter_column(
        "code_file",
        "original_blob",
        existing_type=LONGBLOB(),
        type_=BLOB(),
        existing_nullable=True,
    )
