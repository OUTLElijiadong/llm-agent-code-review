"""Persist sanitized Search/Fetch output atomically with its ledger result.

Revision ID: 101_billing_tool_exec_result
Revises: 100_billing_tool_reservations
"""

import sqlalchemy as sa

from alembic import op

revision = "101_billing_tool_exec_result"
down_revision = "100_billing_tool_reservations"
branch_labels = None
depends_on = None


def _id_type():
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "billing_tool_execution_result",
        sa.Column("id", _id_type(), primary_key=True, autoincrement=True),
        sa.Column("request_key", sa.String(160), nullable=False),
        sa.Column("tool_name", sa.String(16), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("request_key", name="uq_billing_tool_result_request"),
        sa.CheckConstraint("length(input_sha256) = 64", name="ck_billing_tool_result_input_sha256"),
    )
    op.create_index(
        "ix_billing_tool_result_created",
        "billing_tool_execution_result",
        ["created_at"],
    )


def downgrade() -> None:
    raise RuntimeError("不得删除已持久化的企业 Search/Fetch 运行结果")
