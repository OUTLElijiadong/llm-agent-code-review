"""为每笔模型预占保存不可变费率快照。

Revision ID: 092_billing_rate_snapshot
Revises: 091_statement_refund_provider
"""

import sqlalchemy as sa

from alembic import op

revision = "092_billing_rate_snapshot"
down_revision = "091_statement_refund_provider"
branch_labels = None
depends_on = None


_RATE_COLUMNS = (
    sa.Column("pricing_version", sa.String(64), nullable=True),
    sa.Column("input_rate_numerator", sa.BigInteger(), nullable=True),
    sa.Column("input_rate_denominator", sa.BigInteger(), nullable=True),
    sa.Column("output_rate_numerator", sa.BigInteger(), nullable=True),
    sa.Column("output_rate_denominator", sa.BigInteger(), nullable=True),
)

def upgrade():
    for column in _RATE_COLUMNS:
        op.add_column("billing_reservation", column)

    # Do not infer the historical rate of any existing reservation. Old pending or
    # unknown rows remain all-NULL and need manual reconciliation before settlement.
    with op.batch_alter_table("billing_reservation") as batch:
        batch.create_check_constraint(
            "ck_billing_reservation_rate_snapshot_pair",
            "(pricing_version IS NULL AND input_rate_numerator IS NULL AND input_rate_denominator IS NULL "
            "AND output_rate_numerator IS NULL AND output_rate_denominator IS NULL) OR "
            "(pricing_version IS NOT NULL AND pricing_version <> '' AND input_rate_numerator IS NOT NULL "
            "AND input_rate_denominator IS NOT NULL AND output_rate_numerator IS NOT NULL "
            "AND output_rate_denominator IS NOT NULL AND input_rate_numerator > 0 "
            "AND input_rate_denominator > 0 AND output_rate_numerator > 0 AND output_rate_denominator > 0)",
        )


def downgrade():
    raise RuntimeError("模型预占费率快照是财务结算依据；请通过备份恢复，禁止自动删除")
