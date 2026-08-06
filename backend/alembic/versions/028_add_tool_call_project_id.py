"""Add project_id to tool_call_log for per-project agent run stats.

Revision ID: 028
Revises: 027
Create Date: 2026-08-06

为 tool_call_log 增加 project_id，支持"整个项目的 Agent 运转次数"统计。
兼容 SQLite(测试)与 MySQL(生产)，幂等(已存在则跳过)。
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "028"
down_revision: Union[str, None] = "027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(conn, table: str, column: str) -> bool:
    return column in {item["name"] for item in sa.inspect(conn).get_columns(table)}


def _has_index(conn, table: str, index: str) -> bool:
    return index in {item["name"] for item in sa.inspect(conn).get_indexes(table)}


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn, "tool_call_log", "project_id"):
        op.add_column(
            "tool_call_log",
            sa.Column("project_id", sa.BigInteger(), nullable=True, comment="关联项目 ID(用于项目级 Agent 运转统计)"),
        )
    if not _has_index(conn, "tool_call_log", "ix_tool_call_project"):
        op.create_index("ix_tool_call_project", "tool_call_log", ["project_id"])


def downgrade() -> None:
    conn = op.get_bind()
    if _has_index(conn, "tool_call_log", "ix_tool_call_project"):
        op.drop_index("ix_tool_call_project", table_name="tool_call_log")
    if _has_column(conn, "tool_call_log", "project_id"):
        op.drop_column("tool_call_log", "project_id")
