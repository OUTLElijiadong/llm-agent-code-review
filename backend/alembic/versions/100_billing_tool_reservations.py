"""Add fixed-price Search/Fetch reservation snapshots to the shared ledger.

Revision ID: 100_billing_tool_reservations
Revises: 099_billing_tool_rate_catalog
"""

import sqlalchemy as sa

from alembic import op

revision = "100_billing_tool_reservations"
down_revision = "099_billing_tool_rate_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("billing_reservation", sa.Column(
        "billable_kind", sa.String(16), nullable=False, server_default="model",
    ))
    op.add_column("billing_reservation", sa.Column("tool_name", sa.String(16), nullable=True))
    op.add_column("billing_reservation", sa.Column(
        "tool_cost_cny_micros_per_success", sa.BigInteger(), nullable=True,
    ))
    op.add_column("billing_reservation", sa.Column(
        "tool_price_milli_per_success", sa.BigInteger(), nullable=True,
    ))
    op.add_column("billing_reservation", sa.Column("tool_successful_calls", sa.BigInteger(), nullable=True))
    op.add_column("billing_reservation", sa.Column("provider_attempt_started_at", sa.DateTime(), nullable=True))
    # SQLite needs Alembic's batch table recreation for CHECK replacement. Keep the
    # same forward migration contract on both local SQLite tests and production MySQL.
    with op.batch_alter_table("billing_reservation") as batch:
        batch.drop_constraint("ck_billing_reservation_rate_snapshot_pair", type_="check")
        batch.create_check_constraint(
            "ck_billing_reservation_rate_snapshot_pair",
            "(billable_kind = 'model' AND ((pricing_version IS NULL AND input_rate_numerator IS NULL "
            "AND input_rate_denominator IS NULL AND output_rate_numerator IS NULL "
            "AND output_rate_denominator IS NULL) OR (pricing_version IS NOT NULL AND pricing_version <> '' "
            "AND input_rate_numerator IS NOT NULL AND input_rate_denominator IS NOT NULL "
            "AND output_rate_numerator IS NOT NULL AND output_rate_denominator IS NOT NULL "
            "AND input_rate_numerator > 0 AND input_rate_denominator > 0 "
            "AND output_rate_numerator > 0 AND output_rate_denominator > 0))) OR "
            "(billable_kind IN ('search', 'fetch') AND pricing_version IS NOT NULL AND pricing_version <> '' "
            "AND input_rate_numerator IS NULL AND input_rate_denominator IS NULL "
            "AND output_rate_numerator IS NULL AND output_rate_denominator IS NULL)",
        )
        batch.create_check_constraint(
            "ck_billing_reservation_billable_kind_snapshot",
            "billable_kind IN ('model', 'search', 'fetch') AND ("
            "(billable_kind = 'model' AND tool_name IS NULL "
            "AND tool_cost_cny_micros_per_success IS NULL AND tool_price_milli_per_success IS NULL "
            "AND tool_successful_calls IS NULL) OR "
            "(billable_kind IN ('search', 'fetch') AND tool_name IS NOT NULL "
            "AND tool_name = billable_kind AND tool_cost_cny_micros_per_success IS NOT NULL "
            "AND tool_price_milli_per_success IS NOT NULL "
            "AND tool_cost_cny_micros_per_success > 0 AND tool_price_milli_per_success > 0 "
            "AND pricing_version IS NOT NULL AND provider_identity_version IS NOT NULL "
            "AND input_rate_numerator IS NULL AND input_rate_denominator IS NULL "
            "AND output_rate_numerator IS NULL AND output_rate_denominator IS NULL))",
        )
        batch.create_check_constraint(
            "ck_billing_reservation_tool_success_count",
            "tool_successful_calls IS NULL OR tool_successful_calls IN (0, 1)",
        )
    op.create_index(
        "ix_billing_reservation_kind_created", "billing_reservation", ["billable_kind", "created_at"],
    )
    op.add_column("billing_reservation_reconciliation_event", sa.Column(
        "tool_successful_calls", sa.BigInteger(), nullable=True,
    ))
    with op.batch_alter_table("billing_reservation_reconciliation_event") as batch:
        batch.create_check_constraint(
            "ck_billing_res_recon_tool_success_count",
            "tool_successful_calls IS NULL OR tool_successful_calls IN (0, 1)",
        )


def downgrade() -> None:
    raise RuntimeError("不得删除已使用的 Search/Fetch 计费 reservation 与双人对账快照")
