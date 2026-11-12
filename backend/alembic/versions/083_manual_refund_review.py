"""Add a separate billing suspension flag for refunded enterprise subscriptions.

Revision ID: 083_manual_refund_review
Revises: 082_enterprise_assistant_runtime
"""

import sqlalchemy as sa

from alembic import op

revision = "083_manual_refund_review"
down_revision = "082_enterprise_assistant_runtime"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "enterprise",
        sa.Column("billing_suspended", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade():
    raise RuntimeError("账单暂停状态用于保留退款后企业访问边界；请通过备份恢复，禁止单独回退迁移")
