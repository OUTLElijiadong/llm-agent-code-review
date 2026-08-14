"""Create persistent dynamic Agent team/work graph tables.

Revision ID: 033
Revises: 032
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "033"
down_revision: Union[str, None] = "032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json_type():
    return mysql.LONGTEXT() if op.get_bind().dialect.name == "mysql" else sa.Text()


def upgrade() -> None:
    json_type = _json_type()
    op.create_table(
        "agent_team",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("surface", sa.String(24), nullable=False),
        sa.Column("session_key", sa.String(128), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="draft"),
        sa.Column("max_active_children", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trace_id", sa.String(80), nullable=False),
        sa.Column("deadline_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("summary_json", json_type, nullable=False),
        sa.Column("error_json", json_type, nullable=False),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("update_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_agent_team_owner_status", "agent_team", ["user_id", "status", "id"])
    op.create_index("ix_agent_team_trace", "agent_team", ["user_id", "trace_id"])

    op.create_table(
        "agent_team_member",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("team_id", sa.BigInteger(), nullable=False),
        sa.Column("member_key", sa.String(80), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("address", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("role", sa.String(24), nullable=False, server_default="worker"),
        sa.Column("template_id", sa.BigInteger(), nullable=True),
        sa.Column("template_version_id", sa.BigInteger(), nullable=True),
        sa.Column("capabilities_json", json_type, nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="created"),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("update_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("team_id", "member_key", name="uk_agent_team_member_key"),
    )
    op.create_index("ix_agent_team_member_team_status", "agent_team_member", ["team_id", "status"])

    op.create_table(
        "agent_team_task",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("team_id", sa.BigInteger(), nullable=False),
        sa.Column("member_id", sa.BigInteger(), nullable=False),
        sa.Column("task_key", sa.String(80), nullable=False),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("dependency_keys_json", json_type, nullable=False),
        sa.Column("input_json", json_type, nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="waiting_dependency"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("lease_token", sa.String(80), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("result_json", json_type, nullable=False),
        sa.Column("artifacts_json", json_type, nullable=False),
        sa.Column("errors_json", json_type, nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("update_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("team_id", "task_key", name="uk_agent_team_task_key"),
    )
    op.create_index("ix_agent_team_task_queue", "agent_team_task", ["team_id", "status", "priority", "id"])
    op.create_index("ix_agent_team_task_lease", "agent_team_task", ["status", "lease_expires_at"])

    op.create_table(
        "agent_team_event",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("team_id", sa.BigInteger(), nullable=False),
        sa.Column("task_id", sa.BigInteger(), nullable=True),
        sa.Column("member_id", sa.BigInteger(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.String(80), nullable=True),
        sa.Column("correlation_id", sa.String(80), nullable=False, server_default=""),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("from_status", sa.String(32), nullable=True),
        sa.Column("to_status", sa.String(32), nullable=True),
        sa.Column("actor_address", sa.String(200), nullable=False),
        sa.Column("trace_id", sa.String(80), nullable=False),
        sa.Column("detail_json", json_type, nullable=False),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("update_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_agent_team_event_team", "agent_team_event", ["team_id", "id"])
    op.create_index("ix_agent_team_event_trace", "agent_team_event", ["user_id", "trace_id", "id"])
    op.create_index("ix_agent_team_event_task", "agent_team_event", ["task_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_agent_team_event_task", table_name="agent_team_event")
    op.drop_index("ix_agent_team_event_trace", table_name="agent_team_event")
    op.drop_index("ix_agent_team_event_team", table_name="agent_team_event")
    op.drop_table("agent_team_event")
    op.drop_index("ix_agent_team_task_lease", table_name="agent_team_task")
    op.drop_index("ix_agent_team_task_queue", table_name="agent_team_task")
    op.drop_table("agent_team_task")
    op.drop_index("ix_agent_team_member_team_status", table_name="agent_team_member")
    op.drop_table("agent_team_member")
    op.drop_index("ix_agent_team_trace", table_name="agent_team")
    op.drop_index("ix_agent_team_owner_status", table_name="agent_team")
    op.drop_table("agent_team")
