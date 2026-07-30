"""add user.last_login_ip column

Revision ID: 011
Revises: 010
Create Date: 2026-07-30

为用户表新增 last_login_ip 列,记录最后登录来源 IP,供管理员用户管理页展示。
兼容 SQLite(测试)与 MySQL(生产),幂等(已存在则跳过)。
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "011"
down_revision: Union[str, None] = "010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(conn, table: str, column: str) -> bool:
    if conn.dialect.name == "sqlite":
        rows = conn.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
        return any(r[1] == column for r in rows)
    rows = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND COLUMN_NAME = :c"
        ),
        {"t": table, "c": column},
    ).scalar()
    return bool(rows)


def upgrade() -> None:
    """升级:user 表新增 last_login_ip 列(幂等)。"""
    conn = op.get_bind()
    if not _has_column(conn, "user", "last_login_ip"):
        with op.batch_alter_table("user") as batch_op:
            batch_op.add_column(
                sa.Column("last_login_ip", sa.String(64), nullable=True, comment="最后登录来源 IP")
            )


def downgrade() -> None:
    """回滚:移除 user.last_login_ip 列(幂等)。"""
    conn = op.get_bind()
    if _has_column(conn, "user", "last_login_ip"):
        with op.batch_alter_table("user") as batch_op:
            batch_op.drop_column("last_login_ip")
