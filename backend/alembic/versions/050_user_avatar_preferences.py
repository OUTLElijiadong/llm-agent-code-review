"""add user avatar, avatar blob table and preference prompt state

Revision ID: 050_user_avatar_preferences
Revises: 049_agent_studio_reviewer_only
Create Date: 2026-09-11

1) user.avatar: 头像标识('' 或 'builtin:<key>' 内置头像 / 'upload' 自定义上传)。
2) user_avatar 表: 自定义头像二进制(MEDIUMBLOB,≤512KB,上传侧校验)。
3) user_profile.preference_prompted / preference_prompted_at:
   小菱偏好询问状态(0=未问 1=已答 2=跳过),避免反复打扰。
幂等,兼容 SQLite/MySQL。
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "050_user_avatar_preferences"
down_revision: Union[str, None] = "049_agent_studio_reviewer_only"
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


def _has_table(conn, table: str) -> bool:
    if conn.dialect.name == "sqlite":
        rows = conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()
        return any(r[0] == table for r in rows)
    rows = conn.execute(
        sa.text("SELECT COUNT(*) FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"),
        {"t": table},
    ).scalar()
    return bool(rows)


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn, "user", "avatar"):
        with op.batch_alter_table("user") as batch_op:
            batch_op.add_column(sa.Column("avatar", sa.String(64), nullable=True, comment="头像标识"))
    if not _has_column(conn, "user_profile", "preference_prompted"):
        with op.batch_alter_table("user_profile") as batch_op:
            batch_op.add_column(
                sa.Column("preference_prompted", sa.SmallInteger(), nullable=False,
                          server_default=sa.text("0"), comment="偏好询问状态 0未问/1已答/2跳过")
            )
    if not _has_column(conn, "user_profile", "preference_prompted_at"):
        with op.batch_alter_table("user_profile") as batch_op:
            batch_op.add_column(sa.Column("preference_prompted_at", sa.DateTime(), nullable=True,
                                          comment="偏好询问结算时间(UTC)"))
    if not _has_table(conn, "user_avatar"):
        op.create_table(
            "user_avatar",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.BigInteger(), nullable=False, unique=True),
            sa.Column("mime", sa.String(32), nullable=False),
            sa.Column("data", sa.LargeBinary().with_variant(sa.dialects.mysql.MEDIUMBLOB(), "mysql"),
                      nullable=False),
            sa.Column("create_time", sa.DateTime(), nullable=True),
            sa.Column("update_time", sa.DateTime(), nullable=True),
            mysql_charset="utf8mb4",
            mysql_collation="utf8mb4_unicode_ci",
        )


def downgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, "user_avatar"):
        op.drop_table("user_avatar")
    if _has_column(conn, "user_profile", "preference_prompted_at"):
        with op.batch_alter_table("user_profile") as batch_op:
            batch_op.drop_column("preference_prompted_at")
    if _has_column(conn, "user_profile", "preference_prompted"):
        with op.batch_alter_table("user_profile") as batch_op:
            batch_op.drop_column("preference_prompted")
    if _has_column(conn, "user", "avatar"):
        with op.batch_alter_table("user") as batch_op:
            batch_op.drop_column("avatar")
