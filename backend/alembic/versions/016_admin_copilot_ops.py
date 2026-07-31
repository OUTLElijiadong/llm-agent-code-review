"""Add persistent admin copilot conversations and operations audit.

Revision ID: 016
Revises: 015
"""

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "016"
down_revision: Union[str, None] = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_chat_session",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("session_key", sa.String(128), nullable=False),
        sa.Column("title", sa.String(200), nullable=False, server_default="管理副驾驶"),
        sa.Column("status", sa.String(30), nullable=False, server_default="active"),
        sa.Column("last_message_at", sa.DateTime(), nullable=True),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("update_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("uq_admin_chat_session_owner_key", "admin_chat_session", ["user_id", "session_key"], unique=True)
    op.create_index("ix_admin_chat_session_last_message", "admin_chat_session", ["last_message_at"])

    op.create_table(
        "admin_chat_message",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("message_type", sa.String(30), nullable=False, server_default="text"),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("action_token_hash", sa.String(64), nullable=True),
        sa.Column("action_status", sa.String(30), nullable=True),
        sa.Column("agent_code", sa.String(80), nullable=True),
        sa.Column("trace_id", sa.String(80), nullable=True),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("update_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_admin_chat_message_session_id", "admin_chat_message", ["session_id", "id"])
    op.create_index("ix_admin_chat_message_action_hash", "admin_chat_message", ["action_token_hash"])
    op.create_index("ix_admin_chat_message_trace", "admin_chat_message", ["trace_id"])

    op.create_table(
        "ops_execution",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("session_id", sa.BigInteger(), nullable=True),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("risk_level", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="running"),
        sa.Column("params_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("update_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("uq_ops_execution_request_id", "ops_execution", ["request_id"], unique=True)
    op.create_index("ix_ops_execution_status", "ops_execution", ["status"])
    op.create_index("ix_ops_execution_action", "ops_execution", ["action"])


def downgrade() -> None:
    op.drop_table("ops_execution")
    op.drop_table("admin_chat_message")
    op.drop_table("admin_chat_session")
