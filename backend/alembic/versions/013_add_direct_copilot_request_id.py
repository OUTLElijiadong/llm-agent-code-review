"""add direct copilot idempotency log

Revision ID: 013
Revises: 012
Create Date: 2026-07-31
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "013"
down_revision: Union[str, None] = "012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("tool_call_log") as batch_op:
        batch_op.add_column(sa.Column("copilot_request_id", sa.String(64), nullable=True))
    op.create_index("ix_tool_call_copilot_request", "tool_call_log", ["copilot_request_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_tool_call_copilot_request", table_name="tool_call_log")
    with op.batch_alter_table("tool_call_log") as batch_op:
        batch_op.drop_column("copilot_request_id")
