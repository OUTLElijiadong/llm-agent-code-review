"""Persist the verified provider identity used by every billable reservation.

Revision ID: 093_billing_provider_identity
Revises: 092_billing_rate_snapshot
"""

import sqlalchemy as sa

from alembic import op

revision = "093_billing_provider_identity"
down_revision = "092_billing_rate_snapshot"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("billing_reservation", sa.Column("provider_identity_version", sa.String(32), nullable=True))
    op.add_column("billing_reservation", sa.Column("provider_name", sa.String(32), nullable=True))
    op.add_column("billing_reservation", sa.Column("provider_endpoint_host", sa.String(253), nullable=True))
    op.add_column("billing_reservation", sa.Column("credential_source", sa.String(16), nullable=True))

    # Existing reservations predate provider identity capture. Keep all four
    # fields NULL: an endpoint or credential owner cannot be inferred later.
    with op.batch_alter_table("billing_reservation") as batch:
        batch.create_check_constraint(
            "ck_billing_reservation_provider_identity_pair",
            "(provider_identity_version IS NULL AND provider_name IS NULL "
            "AND provider_endpoint_host IS NULL AND credential_source IS NULL) OR "
            "(provider_identity_version IS NOT NULL AND provider_name IS NOT NULL "
            "AND provider_endpoint_host IS NOT NULL AND credential_source IS NOT NULL "
            "AND provider_identity_version = 'deepseek-official-v1' AND provider_name = 'deepseek' "
            "AND provider_endpoint_host = 'api.deepseek.com' AND credential_source IN ('system', 'global'))",
        )


def downgrade():
    raise RuntimeError("计费供应商身份是财务审计依据；请通过备份恢复，禁止自动删除")
