"""Billing wallet, credit batches, reservation and order primitives.

Revision ID: 067_billing_ledger
Revises: 066_enterprise_org
"""
import sqlalchemy as sa

from alembic import op

revision = "067_billing_ledger"
down_revision = "066_enterprise_org"
branch_labels = None
depends_on = None


def _id():
    return sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True)


def upgrade():
    op.create_table("billing_wallet", _id(), sa.Column("owner_type", sa.String(20), nullable=False),
                    sa.Column("owner_id", sa.BigInteger(), nullable=False),
                    sa.Column("lock_version", sa.Integer(), nullable=False, server_default="0"),
                    sa.Column("created_at", sa.DateTime(), nullable=False),
                    sa.UniqueConstraint("owner_type", "owner_id", name="uq_billing_wallet_owner"))
    op.create_table("billing_credit_batch", _id(), sa.Column("wallet_id", sa.BigInteger(), nullable=False),
                    sa.Column("grant_key", sa.String(160), nullable=False),
                    sa.Column("kind", sa.String(24), nullable=False),
                    sa.Column("issued_milli", sa.BigInteger(), nullable=False),
                    sa.Column("remaining_milli", sa.BigInteger(), nullable=False),
                    sa.Column("reserved_milli", sa.BigInteger(), nullable=False, server_default="0"),
                    sa.Column("expires_at", sa.DateTime(), nullable=True),
                    sa.Column("created_at", sa.DateTime(), nullable=False),
                    sa.UniqueConstraint("grant_key", name="uq_billing_credit_grant"))
    op.create_index("ix_billing_batch_wallet_expiry", "billing_credit_batch", ["wallet_id", "expires_at"])
    op.create_table("billing_limit", _id(), sa.Column("limit_key", sa.String(160), nullable=False),
                    sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
                    sa.Column("category", sa.String(20), nullable=False, server_default="normal"),
                    sa.Column("group_id", sa.BigInteger(), nullable=True),
                    sa.Column("user_id", sa.BigInteger(), nullable=True),
                    sa.Column("period_start", sa.DateTime(), nullable=False),
                    sa.Column("period_end", sa.DateTime(), nullable=False),
                    sa.Column("limit_milli", sa.BigInteger(), nullable=False),
                    sa.Column("used_milli", sa.BigInteger(), nullable=False, server_default="0"),
                    sa.Column("reserved_milli", sa.BigInteger(), nullable=False, server_default="0"),
                    sa.UniqueConstraint("limit_key", name="uq_billing_limit_key"))
    op.create_index("ix_billing_limit_scope", "billing_limit", ["enterprise_id", "period_start", "period_end"])
    op.create_table("billing_reservation", _id(), sa.Column("request_key", sa.String(160), nullable=False),
                    sa.Column("model_name", sa.String(80), nullable=False),
                    sa.Column("enterprise_id", sa.BigInteger(), nullable=True),
                    sa.Column("group_id", sa.BigInteger(), nullable=True),
                    sa.Column("user_id", sa.BigInteger(), nullable=True),
                    sa.Column("usage_kind", sa.String(20), nullable=False, server_default="member"),
                    sa.Column("enterprise_wallet_id", sa.BigInteger(), nullable=True),
                    sa.Column("personal_wallet_id", sa.BigInteger(), nullable=True),
                    sa.Column("allow_personal", sa.Integer(), nullable=False),
                    sa.Column("requested_milli", sa.BigInteger(), nullable=False),
                    sa.Column("actual_milli", sa.BigInteger(), nullable=True),
                    sa.Column("input_tokens", sa.BigInteger(), nullable=True),
                    sa.Column("output_tokens", sa.BigInteger(), nullable=True),
                    sa.Column("usage_state", sa.String(16), nullable=False, server_default="pending"),
                    sa.Column("status", sa.String(20), nullable=False),
                    sa.Column("created_at", sa.DateTime(), nullable=False),
                    sa.Column("settled_at", sa.DateTime(), nullable=True),
                    sa.UniqueConstraint("request_key", name="uq_billing_reservation_request"))
    op.create_table("billing_reservation_part", _id(), sa.Column("reservation_id", sa.BigInteger(), nullable=False),
                    sa.Column("wallet_id", sa.BigInteger(), nullable=False),
                    sa.Column("batch_id", sa.BigInteger(), nullable=False),
                    sa.Column("reserved_milli", sa.BigInteger(), nullable=False),
                    sa.Column("charged_milli", sa.BigInteger(), nullable=True))
    op.create_index("ix_billing_part_reservation", "billing_reservation_part", ["reservation_id"])
    op.create_table("billing_reservation_limit", _id(), sa.Column("reservation_id", sa.BigInteger(), nullable=False),
                    sa.Column("limit_id", sa.BigInteger(), nullable=False),
                    sa.Column("reserved_milli", sa.BigInteger(), nullable=False),
                    sa.UniqueConstraint("reservation_id", "limit_id", name="uq_billing_reservation_limit"))
    op.create_table("billing_order", _id(), sa.Column("order_no", sa.String(64), nullable=False),
                    sa.Column("user_id", sa.BigInteger(), nullable=False),
                    sa.Column("enterprise_id", sa.BigInteger(), nullable=True),
                    sa.Column("product", sa.String(32), nullable=False),
                    sa.Column("amount_fen", sa.BigInteger(), nullable=False),
                    sa.Column("quantity", sa.Integer(), nullable=False),
                    sa.Column("channel", sa.String(16), nullable=False),
                    sa.Column("status", sa.String(20), nullable=False),
                    sa.Column("created_at", sa.DateTime(), nullable=False),
                    sa.UniqueConstraint("order_no", name="uq_billing_order_no"))


def downgrade():
    # Financial history must never be silently destroyed by an application rollback.
    raise RuntimeError("积分与支付账本是前向迁移；请通过备份恢复，禁止将 Alembic 版本号单独回退")
