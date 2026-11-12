"""Persist idempotent payment checkout state and provider QR payloads.

Revision ID: 075_payment_checkout_state
Revises: 074_enterprise_remote_approval
"""

import sqlalchemy as sa

from alembic import op

revision = "075_payment_checkout_state"
down_revision = "074_enterprise_remote_approval"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("billing_order", sa.Column("client_request_id", sa.String(80), nullable=True))
    op.add_column("billing_order", sa.Column("request_fingerprint", sa.String(64), nullable=True))
    op.add_column(
        "billing_order",
        sa.Column("payment_status", sa.String(20), nullable=False, server_default="not_started"),
    )
    op.add_column("billing_order", sa.Column("payment_code_url", sa.String(2048), nullable=True))
    op.add_column("billing_order", sa.Column("provider_payment_id", sa.String(128), nullable=True))
    op.add_column("billing_order", sa.Column("payment_expires_at", sa.DateTime(), nullable=True))
    op.create_unique_constraint(
        "uq_billing_order_user_request", "billing_order", ["user_id", "client_request_id"]
    )


def downgrade():
    # Preserve financial checkout history during application rollback.
    raise RuntimeError("支付订单状态是前向迁移；请通过备份恢复，禁止将 Alembic 版本号单独回退")
