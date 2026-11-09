"""Persist retry boundaries and terminal XiaoLing output for safe recovery.

Revision ID: 080_assistant_recovery
Revises: 079_enterprise_assistant_runs
"""

import sqlalchemy as sa

from alembic import op

revision = "080_assistant_recovery"
down_revision = "079_enterprise_assistant_runs"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("enterprise_assistant_run") as batch:
        batch.add_column(sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("response_text", sa.Text(), nullable=True))


def downgrade():
    raise RuntimeError("企业小菱调用与结果恢复记录不可自动删除；请按备份恢复，禁止单独回退迁移版本")
