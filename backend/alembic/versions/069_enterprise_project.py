"""企业项目归属与授权结构；项目创建入口在全链路隔离完成前保持关闭。

Revision ID: 069_enterprise_project
Revises: 068_enterprise_chat
"""

import sqlalchemy as sa

from alembic import op

revision = "069_enterprise_project"
down_revision = "068_enterprise_chat"
branch_labels = None
depends_on = None


def _id():
    return sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True)


def _timestamps():
    return (
        sa.Column("create_time", sa.DateTime(), nullable=False),
        sa.Column("update_time", sa.DateTime(), nullable=False),
    )


def upgrade():
    op.create_table(
        "enterprise_project",
        _id(),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("project_id", name="uk_enterprise_project_project"),
    )
    op.create_index("ix_enterprise_project_group", "enterprise_project", ["enterprise_id", "group_id"])
    op.create_table(
        "enterprise_project_grant",
        _id(),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("enterprise_id", "project_id", "user_id", name="uk_enterprise_project_grant"),
    )
    op.create_index(
        "ix_enterprise_project_grant_user", "enterprise_project_grant", ["enterprise_id", "user_id", "status"]
    )


def downgrade():
    raise RuntimeError("企业项目授权含业务数据，不支持破坏性回退；请按备份恢复流程处理")
