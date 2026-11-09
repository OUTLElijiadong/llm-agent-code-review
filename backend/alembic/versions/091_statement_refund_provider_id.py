"""补齐对账退款标识与渠道来源/摘要验证元数据。

Revision ID: 091_statement_refund_provider
Revises: 090_payment_statement_reconcile
"""

import sqlalchemy as sa

from alembic import op

revision = "091_statement_refund_provider"
down_revision = "090_payment_statement_reconcile"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("billing_statement_batch", sa.Column("provider_hash_type", sa.String(16), nullable=True))
    op.add_column("billing_statement_batch", sa.Column("provider_hash_value", sa.String(64), nullable=True))
    op.add_column(
        "billing_statement_row",
        sa.Column("local_provider_refund_id", sa.String(128), nullable=True),
    )


def downgrade():
    raise RuntimeError("渠道退款号对账快照是财务审计依据；请通过备份恢复，禁止自动删除对账历史")
