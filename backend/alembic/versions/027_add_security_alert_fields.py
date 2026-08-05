"""Extend agent_alert with security monitor fields.

Revision ID: 027
Revises: 026
Create Date: 2026-08-05

为 agent_alert 增加安全监控弹窗所需字段：
category / source / user_id / read_at / fingerprint，
并新增 (user_id, read_at) 与 fingerprint 两个索引。
兼容 SQLite(测试)与 MySQL(生产)，幂等(已存在则跳过)。
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "027"
down_revision: Union[str, None] = "026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(conn, table: str, column: str) -> bool:
    return column in {item["name"] for item in sa.inspect(conn).get_columns(table)}


def _has_index(conn, table: str, index: str) -> bool:
    return index in {item["name"] for item in sa.inspect(conn).get_indexes(table)}


def upgrade() -> None:
    """升级:agent_alert 新增安全监控字段与索引(幂等)。"""
    conn = op.get_bind()
    with op.batch_alter_table("agent_alert") as batch_op:
        if not _has_column(conn, "agent_alert", "category"):
            batch_op.add_column(
                sa.Column("category", sa.String(40), nullable=True, comment="告警类别")
            )
        if not _has_column(conn, "agent_alert", "source"):
            batch_op.add_column(
                sa.Column("source", sa.String(40), nullable=True, comment="告警来源")
            )
        if not _has_column(conn, "agent_alert", "user_id"):
            batch_op.add_column(
                sa.Column(
                    "user_id",
                    sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                    nullable=True,
                    comment="弹窗目标管理员(唯一超级管理员)",
                )
            )
        if not _has_column(conn, "agent_alert", "read_at"):
            batch_op.add_column(
                sa.Column("read_at", sa.DateTime(), nullable=True, comment="弹窗已读时间")
            )
        if not _has_column(conn, "agent_alert", "fingerprint"):
            batch_op.add_column(
                sa.Column("fingerprint", sa.String(120), nullable=True, comment="去重指纹")
            )
    if not _has_index(conn, "agent_alert", "ix_agent_alert_user_read"):
        op.create_index(
            "ix_agent_alert_user_read",
            "agent_alert",
            ["user_id", "read_at"],
        )
    if not _has_index(conn, "agent_alert", "ix_agent_alert_fingerprint"):
        op.create_index(
            "ix_agent_alert_fingerprint",
            "agent_alert",
            ["fingerprint"],
        )


def downgrade() -> None:
    """回滚:移除 agent_alert 安全监控字段与索引(幂等)。"""
    conn = op.get_bind()
    if _has_index(conn, "agent_alert", "ix_agent_alert_fingerprint"):
        op.drop_index("ix_agent_alert_fingerprint", table_name="agent_alert")
    if _has_index(conn, "agent_alert", "ix_agent_alert_user_read"):
        op.drop_index("ix_agent_alert_user_read", table_name="agent_alert")
    with op.batch_alter_table("agent_alert") as batch_op:
        for column in ("fingerprint", "read_at", "user_id", "source", "category"):
            if _has_column(conn, "agent_alert", column):
                batch_op.drop_column(column)
