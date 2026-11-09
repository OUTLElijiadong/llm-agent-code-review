"""退款渠道查单认领租约与旧结果 fencing。

Revision ID: 089_refund_reconcile_lease
Revises: 088_enterprise_kb_attach
"""

import sqlalchemy as sa

from alembic import op

revision = "089_refund_reconcile_lease"
down_revision = "088_enterprise_kb_attach"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "billing_refund",
        sa.Column("reconciliation_claim_token", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "billing_refund",
        sa.Column("reconciliation_claimed_until", sa.DateTime(), nullable=True),
    )


def downgrade():
    raise RuntimeError("退款渠道查单认领记录用于防止过期结果覆盖；请通过备份恢复，禁止自动回退")
