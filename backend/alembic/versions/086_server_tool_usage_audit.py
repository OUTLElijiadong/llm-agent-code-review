"""Retain unpriced server-side Search/Fetch usage for manual reconciliation.

Revision ID: 086_server_tool_usage
Revises: 085_ent_asset_invocations
"""

import sqlalchemy as sa

from alembic import op

revision = "086_server_tool_usage"
down_revision = "085_ent_asset_invocations"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("billing_reservation", sa.Column("server_tool_search_calls", sa.BigInteger(), nullable=True))
    op.add_column("billing_reservation", sa.Column("server_tool_fetch_calls", sa.BigInteger(), nullable=True))
    with op.batch_alter_table("billing_reservation") as batch_op:
        batch_op.create_check_constraint(
            "ck_billing_reservation_server_tool_usage_pair",
            "(server_tool_search_calls IS NULL AND server_tool_fetch_calls IS NULL) OR "
            "(server_tool_search_calls IS NOT NULL AND server_tool_fetch_calls IS NOT NULL)",
        )


def downgrade():
    raise RuntimeError("供应商工具用量属于财务对账证据，不可静默删除；请通过备份恢复")
