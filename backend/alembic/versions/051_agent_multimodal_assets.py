"""add agent multimodal asset table

Revision ID: 051_agent_multimodal_assets
Revises: 050_user_avatar_preferences
Create Date: 2026-09-11

小菱多模态(图片消息)输入/输出留档表。图片二进制存本表,运行检查点只存
prism-asset://<sha256> 占位符。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "051_agent_multimodal_assets"
down_revision: Union[str, None] = "050_user_avatar_preferences"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_multimodal_asset",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(80), nullable=False, comment="所属运行 run_id"),
        sa.Column("user_id", sa.BigInteger(), nullable=False, comment="归属用户"),
        sa.Column("surface", sa.String(20), nullable=False, server_default="user", comment="user/admin"),
        sa.Column("role", sa.String(10), nullable=False, server_default="input", comment="input/output"),
        sa.Column("mime", sa.String(32), nullable=False, comment="图片 MIME(服务端嗅探)"),
        sa.Column("sha256", sa.String(64), nullable=False, comment="内容摘要(占位符关联键)"),
        sa.Column("data", sa.LargeBinary().with_variant(mysql.MEDIUMBLOB(), "mysql"),
                  nullable=False, comment="图片二进制(≤1.5MB)"),
        sa.Column("create_time", sa.DateTime(), nullable=True),
        sa.Column("update_time", sa.DateTime(), nullable=True),
        mysql_charset="utf8mb4",
        mysql_collation="utf8mb4_unicode_ci",
    )
    op.create_index("ix_agent_multimodal_asset_run", "agent_multimodal_asset", ["run_id"])
    op.create_index("ix_agent_multimodal_asset_user", "agent_multimodal_asset", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_multimodal_asset_user", table_name="agent_multimodal_asset")
    op.drop_index("ix_agent_multimodal_asset_run", table_name="agent_multimodal_asset")
    op.drop_table("agent_multimodal_asset")
