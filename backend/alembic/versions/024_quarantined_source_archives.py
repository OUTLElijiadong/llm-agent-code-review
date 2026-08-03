"""Store quarantined whole-source archives outside editable code rows.

Revision ID: 024
Revises: 023
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.mysql import LONGBLOB, LONGTEXT

from alembic import op

revision: str = "024"
down_revision: Union[str, None] = "023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LONG_TEXT = LONGTEXT().with_variant(sa.Text(), "sqlite")
LONG_BLOB = LONGBLOB().with_variant(sa.LargeBinary(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "project_source_archive",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("media_type", sa.String(120), nullable=False, server_default="application/zip"),
        sa.Column("archive_sha256", sa.String(64), nullable=False),
        sa.Column("compressed_size", sa.Integer(), nullable=False),
        sa.Column("expanded_size", sa.BigInteger(), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("max_member_size", sa.BigInteger(), nullable=False),
        sa.Column("max_compression_ratio", sa.Float(), nullable=False, server_default="1"),
        sa.Column("storage_status", sa.String(30), nullable=False, server_default="active"),
        sa.Column("malware_status", sa.String(30), nullable=False),
        sa.Column("audit_status", sa.String(30), nullable=False, server_default="not_started"),
        sa.Column("audit_run_id", sa.String(64), nullable=True),
        sa.Column("audit_started_at", sa.DateTime(), nullable=True),
        sa.Column("audit_heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("audit_completed_at", sa.DateTime(), nullable=True),
        sa.Column("audit_result_json", LONG_TEXT, nullable=True),
        sa.Column("threat_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scan_summary_json", LONG_TEXT, nullable=False),
        sa.Column("archive_blob", LONG_BLOB, nullable=False),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("update_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "uk_project_source_archive_project",
        "project_source_archive",
        ["project_id"],
        unique=True,
    )
    op.create_index("ix_project_source_archive_owner", "project_source_archive", ["owner_id"])
    op.create_index("ix_project_source_archive_malware", "project_source_archive", ["malware_status"])


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("project_source_archive"):
        count = bind.execute(sa.text("SELECT COUNT(*) FROM project_source_archive")).scalar_one()
        if int(count or 0) > 0:
            raise RuntimeError("隔离源码归档表非空，拒绝降级丢失原始证据")
    op.drop_table("project_source_archive")
