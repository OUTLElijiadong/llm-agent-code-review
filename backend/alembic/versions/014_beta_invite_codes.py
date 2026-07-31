"""add beta invite codes

Revision ID: 014
Revises: 013
Create Date: 2026-07-31
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "014"
down_revision: Union[str, None] = "013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "beta_invite_code",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("code_hash", sa.String(64), nullable=False, comment="HMAC-SHA256 摘要"),
        sa.Column("display_prefix", sa.String(32), nullable=False, comment="脱敏前缀"),
        sa.Column("label", sa.String(100), nullable=True, comment="管理员备注"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("used_by", sa.BigInteger(), nullable=True),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("update_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ux_beta_invite_code_hash", "beta_invite_code", ["code_hash"], unique=True)
    op.create_index(
        "ix_beta_invite_status_expires",
        "beta_invite_code",
        ["status", "expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_beta_invite_created_by",
        "beta_invite_code",
        ["created_by"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_beta_invite_created_by", table_name="beta_invite_code")
    op.drop_index("ix_beta_invite_status_expires", table_name="beta_invite_code")
    op.drop_index("ux_beta_invite_code_hash", table_name="beta_invite_code")
    op.drop_table("beta_invite_code")
