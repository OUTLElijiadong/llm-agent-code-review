"""enlarge review_issue.owasp and cwe columns

Revision ID: 009
Revises: 008
Create Date: 2026-06-25

本次迁移扩大 review_issue 表的 owasp/cwe 列长度:
- owasp: VARCHAR(32) → VARCHAR(128)
  原因:OWASP Top10 完整标题如 "A07:2021-Identification and Authentication Failures"
  长度约 46 字符,超过 32 上限导致 DataError(1406, "Data too long for column 'owasp'")
- cwe: VARCHAR(32) → VARCHAR(64)
  原因:预留扩展空间,部分 CWE 标题可能含描述后缀

使用 batch_alter_table 兼容 SQLite(测试)与 MySQL(生产)。
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """升级:扩大 review_issue.owasp 与 cwe 列长度

    Steps:
        1. owasp: VARCHAR(32) → VARCHAR(128) — 容纳 OWASP Top10 完整标题
        2. cwe: VARCHAR(32) → VARCHAR(64) — 预留扩展空间
    """
    with op.batch_alter_table("review_issue") as batch_op:
        batch_op.alter_column(
            "owasp",
            existing_type=sa.String(32),
            type_=sa.String(128),
            existing_nullable=True,
            existing_comment="OWASP 编号,如 A03:2021-Injection",
        )
        batch_op.alter_column(
            "cwe",
            existing_type=sa.String(32),
            type_=sa.String(64),
            existing_nullable=True,
            existing_comment="CWE 编号,如 CWE-89",
        )


def downgrade() -> None:
    """回滚:恢复 review_issue.owasp 与 cwe 列长度为 32

    ⚠️ 回滚前需确认无超长数据,否则会触发 DataError(1406)。
    """
    with op.batch_alter_table("review_issue") as batch_op:
        batch_op.alter_column(
            "owasp",
            existing_type=sa.String(128),
            type_=sa.String(32),
            existing_nullable=True,
            existing_comment="OWASP 编号,如 A03:2021-Injection",
        )
        batch_op.alter_column(
            "cwe",
            existing_type=sa.String(64),
            type_=sa.String(32),
            existing_nullable=True,
            existing_comment="CWE 编号,如 CWE-89",
        )
