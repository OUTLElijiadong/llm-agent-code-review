"""add unique admin copilot request id

Revision ID: 012
Revises: 011
Create Date: 2026-07-31
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "012"
down_revision: Union[str, None] = "011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(conn, table: str, column: str) -> bool:
    return column in {item["name"] for item in sa.inspect(conn).get_columns(table)}


def _has_index(conn, table: str, index: str) -> bool:
    return index in {item["name"] for item in sa.inspect(conn).get_indexes(table)}


def upgrade() -> None:
    """新增副驾驶请求唯一标识，保证多 worker 确认幂等。"""
    conn = op.get_bind()
    if not _has_column(conn, "approval_item", "copilot_request_id"):
        with op.batch_alter_table("approval_item") as batch_op:
            batch_op.add_column(sa.Column("copilot_request_id", sa.String(64), nullable=True))
    if not _has_index(conn, "approval_item", "ix_approval_item_copilot_request"):
        op.create_index(
            "ix_approval_item_copilot_request",
            "approval_item",
            ["copilot_request_id"],
            unique=True,
        )


def downgrade() -> None:
    """移除副驾驶请求唯一标识。"""
    conn = op.get_bind()
    if _has_index(conn, "approval_item", "ix_approval_item_copilot_request"):
        op.drop_index("ix_approval_item_copilot_request", table_name="approval_item")
    if _has_column(conn, "approval_item", "copilot_request_id"):
        with op.batch_alter_table("approval_item") as batch_op:
            batch_op.drop_column("copilot_request_id")
