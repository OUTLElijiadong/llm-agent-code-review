"""Track refund decisions and lock purchased credits during provider processing.

Revision ID: 077_payment_refunds
Revises: 076_reviewer_legacy_entitlements
"""

import sqlalchemy as sa

from alembic import op

revision = "077_payment_refunds"
down_revision = "076_reviewer_legacy_entitlements"
branch_labels = None
depends_on = None


def _id():
    return sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True,
                     autoincrement=True)


def upgrade():
    op.add_column(
        "billing_credit_batch",
        sa.Column("refund_reserved_milli", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_table(
        "billing_refund",
        _id(),
        sa.Column("order_no", sa.String(64), nullable=False),
        sa.Column("refund_no", sa.String(64), nullable=False),
        sa.Column("amount_fen", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("requested_by", sa.BigInteger(), nullable=False),
        sa.Column("reviewed_by", sa.BigInteger(), nullable=True),
        sa.Column("review_note", sa.String(500), nullable=True),
        sa.Column("provider_refund_id", sa.String(128), nullable=True),
        sa.Column("provider_status", sa.String(32), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("order_no", name="uq_billing_refund_order"),
        sa.UniqueConstraint("refund_no", name="uq_billing_refund_no"),
    )
    op.create_index("ix_billing_refund_status", "billing_refund", ["status", "created_at"])


def downgrade():
    # Financial actions and their frozen-credit history must survive rollback.
    raise RuntimeError("退款和资金冻结记录是前向迁移；请通过备份恢复，禁止将 Alembic 版本号单独回退")
