"""Verified payment events and subscription periods.

Revision ID: 070_billing_subscription
Revises: 069_enterprise_project
"""
import sqlalchemy as sa

from alembic import op

revision = "070_billing_subscription"
down_revision = "069_enterprise_project"
branch_labels = None
depends_on = None


def _id():
    return sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True)


def upgrade():
    op.add_column("billing_order", sa.Column("enterprise_name", sa.String(120), nullable=True))
    op.add_column("billing_order", sa.Column("paid_at", sa.DateTime(), nullable=True))
    op.create_table("billing_payment_event", _id(), sa.Column("channel", sa.String(16), nullable=False),
                    sa.Column("trade_id", sa.String(128), nullable=False),
                    sa.Column("order_no", sa.String(64), nullable=False),
                    sa.Column("amount_fen", sa.BigInteger(), nullable=False),
                    sa.Column("verified_at", sa.DateTime(), nullable=False),
                    sa.UniqueConstraint("channel", "trade_id", name="uq_billing_payment_trade"),
                    sa.UniqueConstraint("order_no", name="uq_billing_payment_order"))
    op.create_table("billing_subscription", _id(), sa.Column("owner_type", sa.String(20), nullable=False),
                    sa.Column("owner_id", sa.BigInteger(), nullable=False),
                    sa.Column("product", sa.String(24), nullable=False),
                    sa.Column("anchor_at", sa.DateTime(), nullable=False),
                    sa.Column("last_end_at", sa.DateTime(), nullable=False),
                    sa.Column("next_offset", sa.Integer(), nullable=False),
                    sa.UniqueConstraint("owner_type", "owner_id", "product", name="uq_billing_subscription_owner"))
    op.create_table("billing_subscription_period", _id(),
                    sa.Column("subscription_id", sa.BigInteger(), nullable=False),
                    sa.Column("order_no", sa.String(64), nullable=False),
                    sa.Column("starts_at", sa.DateTime(), nullable=False),
                    sa.Column("ends_at", sa.DateTime(), nullable=False),
                    sa.Column("credit_milli", sa.BigInteger(), nullable=False),
                    sa.Column("seats", sa.Integer(), nullable=False),
                    sa.Column("granted_at", sa.DateTime(), nullable=True),
                    sa.UniqueConstraint("order_no", name="uq_billing_period_order"))


def downgrade():
    # Preserve payment and entitlement history during application rollback.
    raise RuntimeError("订阅与权益账本是前向迁移；请通过备份恢复，禁止将 Alembic 版本号单独回退")
