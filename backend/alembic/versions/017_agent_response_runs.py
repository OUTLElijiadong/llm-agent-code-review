"""Add persistent Responses Agent run checkpoints.

Revision ID: 017
Revises: 016
"""

from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "017"
down_revision: Union[str, None] = "016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_response_run",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(80), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("surface", sa.String(24), nullable=False),
        sa.Column("session_key", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="running"),
        sa.Column("checkpoint_json", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("update_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("uq_agent_response_run_id", "agent_response_run", ["run_id"], unique=True)
    op.create_index(
        "ix_agent_response_run_owner_session",
        "agent_response_run",
        ["user_id", "surface", "session_key"],
    )
    op.create_index("ix_agent_response_run_status", "agent_response_run", ["status"])

    op.create_table(
        "agent_tool_execution",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(80), nullable=False),
        sa.Column("call_id", sa.String(160), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("tool_name", sa.String(120), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="executing"),
        sa.Column("arguments_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("update_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "uq_agent_tool_execution_request",
        "agent_tool_execution",
        ["request_id"],
        unique=True,
    )
    op.create_index("ix_agent_tool_execution_run", "agent_tool_execution", ["run_id"])
    op.create_index("ix_agent_tool_execution_status", "agent_tool_execution", ["status"])


def downgrade() -> None:
    op.drop_table("agent_tool_execution")
    op.drop_table("agent_response_run")
