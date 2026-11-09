"""渠道账单导入、逐笔差异与只追加人工审阅记录。

Revision ID: 090_payment_statement_reconcile
Revises: 089_refund_reconcile_lease
"""

import sqlalchemy as sa

from alembic import op

revision = "090_payment_statement_reconcile"
down_revision = "089_refund_reconcile_lease"
branch_labels = None
depends_on = None


def _id():
    return sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True)


def upgrade():
    op.create_table(
        "billing_statement_batch",
        _id(),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("source_origin", sa.String(24), nullable=False, server_default="manual_upload"),
        sa.Column("bill_date", sa.Date(), nullable=False),
        sa.Column("bill_type", sa.String(16), nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("file_name", sa.String(200), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False),
        sa.Column("parser_version", sa.String(24), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("payment_total_fen", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("refund_total_fen", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("received_total_fen", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("received_missing_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fee_total_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("fee_missing_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("imported_by", sa.BigInteger(), nullable=False),
        sa.Column("imported_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "channel", "bill_date", "bill_type", "source_origin", "file_sha256",
            name="uq_billing_statement_source",
        ),
    )
    op.create_index("ix_billing_statement_date", "billing_statement_batch", ["channel", "bill_date", "id"])

    op.create_table(
        "billing_statement_row",
        _id(),
        sa.Column("batch_id", sa.BigInteger(), nullable=False),
        sa.Column("source_line_number", sa.Integer(), nullable=False),
        sa.Column("event_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("record_type", sa.String(16), nullable=False),
        sa.Column("order_no", sa.String(64), nullable=True),
        sa.Column("provider_trade_id", sa.String(128), nullable=True),
        sa.Column("refund_no", sa.String(64), nullable=True),
        sa.Column("provider_refund_id", sa.String(128), nullable=True),
        sa.Column("provider_status", sa.String(32), nullable=True),
        sa.Column("currency", sa.String(8), nullable=False, server_default="CNY"),
        sa.Column("amount_fen", sa.BigInteger(), nullable=False),
        sa.Column("received_fen", sa.BigInteger(), nullable=True),
        sa.Column("fee_micros", sa.BigInteger(), nullable=True),
        sa.Column("local_order_status", sa.String(20), nullable=True),
        sa.Column("local_amount_fen", sa.BigInteger(), nullable=True),
        sa.Column("local_trade_id", sa.String(128), nullable=True),
        sa.Column("local_refund_no", sa.String(64), nullable=True),
        sa.Column("local_refund_status", sa.String(24), nullable=True),
        sa.Column("local_refund_amount_fen", sa.BigInteger(), nullable=True),
        sa.Column("discrepancy_code", sa.String(64), nullable=False),
        sa.Column("review_status", sa.String(24), nullable=False, server_default="unreviewed"),
        sa.Column("last_reviewed_by", sa.BigInteger(), nullable=True),
        sa.Column("last_reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("last_review_note", sa.String(500), nullable=True),
        sa.UniqueConstraint(
            "batch_id", "source_line_number", "event_index", name="uq_billing_statement_line_event",
        ),
    )
    op.create_index(
        "ix_billing_statement_row_batch_issue", "billing_statement_row", ["batch_id", "discrepancy_code", "id"],
    )
    op.create_index("ix_billing_statement_row_order", "billing_statement_row", ["order_no", "record_type"])

    op.create_table(
        "billing_statement_review_event",
        _id(),
        sa.Column("statement_row_id", sa.BigInteger(), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=False),
        sa.Column("decision", sa.String(24), nullable=False),
        sa.Column("note", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_billing_statement_review_row", "billing_statement_review_event", ["statement_row_id", "id"],
    )


def downgrade():
    raise RuntimeError("支付渠道账单是财务审计依据；请通过备份恢复，禁止自动删除对账历史")
