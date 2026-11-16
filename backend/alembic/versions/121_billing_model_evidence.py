"""Record independently reviewed model tool-count evidence without rewriting provider usage.

Revision ID: 121_billing_model_evidence
Revises: 120_discussion_turn_watermark
"""
import sqlalchemy as sa

from alembic import op

revision = "121_billing_model_evidence"
down_revision = "120_discussion_turn_watermark"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("billing_reservation_reconciliation_event") as batch:
        batch.add_column(sa.Column("model_search_calls_evidence", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("model_fetch_calls_evidence", sa.BigInteger(), nullable=True))
        batch.create_check_constraint(
            "ck_billing_res_recon_model_evidence",
            "(model_search_calls_evidence IS NULL AND model_fetch_calls_evidence IS NULL) OR "
            "(model_search_calls_evidence IS NOT NULL AND model_fetch_calls_evidence IS NOT NULL "
            "AND model_search_calls_evidence = 0 AND model_fetch_calls_evidence = 0 "
            "AND decision IN ('proposed', 'approved', 'rejected') AND resolution = 'settle')",
        )


def downgrade():
    raise RuntimeError("已保存的计费用量对账证据不能自动降级，请按备份恢复流程处理")
