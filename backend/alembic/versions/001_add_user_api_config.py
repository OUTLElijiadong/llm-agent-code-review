"""add user_api_config table

Revision ID: 001
Revises: None
Create Date: 2026-06-08

用户自定义 API 配置表：允许用户配置个人 API Key/端点/模型，
未配置时自动回退到系统默认 DeepSeek 配置。
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_api_config",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), autoincrement=True, nullable=False),
        sa.Column(
            "user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
            comment="用户ID（一对一关系）",
        ),
        sa.Column("provider", sa.String(32), nullable=False, server_default="deepseek", comment="提供商"),
        sa.Column("api_key_enc", sa.String(512), nullable=False, comment="Fernet 加密存储的 API Key"),
        sa.Column("base_url", sa.String(512), nullable=False, comment="API 端点地址"),
        sa.Column("model", sa.String(128), nullable=False, comment="模型名称"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1"), comment="是否启用"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now(), comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now(), comment="更新时间"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("user_api_config")
