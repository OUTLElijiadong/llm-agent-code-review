"""narrow user.password column to bcrypt's actual length

Revision ID: 052_user_password_len
Revises: 051_agent_multimodal_assets
Create Date: 2026-09-16

user.password 存 bcrypt 哈希,passlib bcrypt 输出固定 60 字符
($2b$12$ 前缀 7 字符 + 53 字符盐与摘要)。历史上 String(255) 是
SQLAlchemy 未指定长度时的宽松默认值,远超实际需要;按实际收窄到 60。
现有数据最长 60 字符,缩列无截断风险。SQLite 忽略 VARCHAR 长度,仅 MySQL 生效。
幂等,兼容 SQLite/MySQL。
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "052_user_password_len"
down_revision: Union[str, None] = "051_agent_multimodal_assets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_BCRYPT_HASH_LEN = 60


def _current_varchar_len(conn, table: str, column: str) -> int:
    rows = conn.execute(
        sa.text(
            "SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND COLUMN_NAME = :c"
        ),
        {"t": table, "c": column},
    ).scalar()
    return int(rows or 0)


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "sqlite":
        return
    if _current_varchar_len(conn, "user", "password") == _BCRYPT_HASH_LEN:
        return
    op.alter_column(
        "user",
        "password",
        existing_type=sa.String(255),
        type_=sa.String(_BCRYPT_HASH_LEN),
        existing_nullable=False,
    )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "sqlite":
        return
    if _current_varchar_len(conn, "user", "password") != _BCRYPT_HASH_LEN:
        return
    op.alter_column(
        "user",
        "password",
        existing_type=sa.String(_BCRYPT_HASH_LEN),
        type_=sa.String(255),
        existing_nullable=False,
    )
