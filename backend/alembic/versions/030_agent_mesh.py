"""Add durable Agent Mesh conversations, messages and message events.

Revision ID: 030
Revises: 029
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "030"
down_revision: Union[str, None] = "029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json_type():
    return mysql.LONGTEXT() if op.get_bind().dialect.name == "mysql" else sa.Text()


def upgrade() -> None:
    op.add_column("agent_response_run", sa.Column("mesh_message_id", sa.String(80), nullable=True))
    op.create_index(
        "ix_agent_response_run_mesh_message",
        "agent_response_run",
        ["mesh_message_id"],
    )
    json_type = _json_type()
    op.create_table(
        "agent_mesh_conversation",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("surface", sa.String(24), nullable=False),
        sa.Column("session_key", sa.String(128), nullable=False),
        sa.Column("title", sa.String(200), nullable=False, server_default="新对话"),
        sa.Column("status", sa.String(24), nullable=False, server_default="active"),
        sa.Column("active_run_id", sa.String(80), nullable=True),
        sa.Column("active_run_status", sa.String(32), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_message_at", sa.DateTime(), nullable=True),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("update_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "surface", "session_key", name="uk_agent_mesh_conversation_owner"),
    )
    op.create_index(
        "ix_agent_mesh_conversation_owner_status",
        "agent_mesh_conversation",
        ["user_id", "status", "last_seen_at"],
    )
    op.create_table(
        "agent_mesh_message",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("message_id", sa.String(80), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("schema_version", sa.String(16), nullable=False, server_default="1.0"),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("trace_id", sa.String(80), nullable=False),
        sa.Column("correlation_id", sa.String(80), nullable=False, server_default=""),
        sa.Column("causation_id", sa.String(80), nullable=False, server_default=""),
        sa.Column("sent_from", sa.String(200), nullable=False),
        sa.Column("send_to", sa.String(200), nullable=False),
        sa.Column("message_type", sa.String(40), nullable=False),
        sa.Column("priority", sa.String(16), nullable=False, server_default="normal"),
        sa.Column("subject", sa.String(240), nullable=False),
        sa.Column("payload_json", json_type, nullable=False),
        sa.Column("context_json", json_type, nullable=False),
        sa.Column("artifacts_json", json_type, nullable=False),
        sa.Column("errors_json", json_type, nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="queued"),
        sa.Column("requires_ack", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
        sa.Column("processing_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("update_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("message_id", name="uk_agent_mesh_message_id"),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uk_agent_mesh_message_idempotency"),
    )
    op.create_index("ix_agent_mesh_message_inbox", "agent_mesh_message", ["user_id", "send_to", "status", "id"])
    op.create_index("ix_agent_mesh_message_trace", "agent_mesh_message", ["user_id", "trace_id", "id"])
    op.create_index("ix_agent_mesh_message_correlation", "agent_mesh_message", ["correlation_id"])
    op.create_table(
        "agent_mesh_message_event",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("message_id", sa.String(80), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("trace_id", sa.String(80), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("actor_address", sa.String(200), nullable=False),
        sa.Column("detail_json", json_type, nullable=False),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("update_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_agent_mesh_message_event_message", "agent_mesh_message_event", ["message_id", "id"])
    op.create_index("ix_agent_mesh_message_event_trace", "agent_mesh_message_event", ["user_id", "trace_id", "id"])


def downgrade() -> None:
    op.drop_table("agent_mesh_message_event")
    op.drop_table("agent_mesh_message")
    op.drop_table("agent_mesh_conversation")
    op.drop_index("ix_agent_response_run_mesh_message", table_name="agent_response_run")
    op.drop_column("agent_response_run", "mesh_message_id")
