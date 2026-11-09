"""Bind enterprise remote-test approvals and executions to an enterprise project.

Revision ID: 087_enterprise_remote_project
Revises: 086_server_tool_usage
"""

import sqlalchemy as sa

from alembic import op

revision = "087_enterprise_remote_project"
down_revision = "086_server_tool_usage"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("enterprise_remote_approval") as batch:
        batch.add_column(sa.Column("project_id", sa.BigInteger(), nullable=True))
        batch.create_index(
            "ix_enterprise_remote_approval_project",
            ["enterprise_id", "project_id", "status", "id"],
        )
    with op.batch_alter_table("remote_pentest_engagement") as batch:
        batch.add_column(sa.Column("project_id", sa.BigInteger(), nullable=True))
        batch.create_index("ix_remote_pentest_project", ["project_id", "id"])


def downgrade():
    raise RuntimeError(
        "企业远测项目授权与执行记录属于安全审计数据，不支持破坏性回退；请按备份恢复流程处理"
    )
