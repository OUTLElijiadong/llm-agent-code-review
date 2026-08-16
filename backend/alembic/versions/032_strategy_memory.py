"""Add scoped execution strategy memory.

Revision ID: 032
Revises: 031
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "032"
down_revision: Union[str, None] = "031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json_type():
    return mysql.LONGTEXT() if op.get_bind().dialect.name == "mysql" else sa.Text()


def upgrade() -> None:
    op.add_column("agent_memory", sa.Column("owner_user_id", sa.BigInteger(), nullable=True))
    op.add_column("agent_memory", sa.Column("project_id", sa.BigInteger(), nullable=True))
    op.add_column("agent_memory", sa.Column("share_scope", sa.String(20), nullable=True))
    op.add_column("agent_memory", sa.Column("fingerprint", sa.String(64), nullable=True))
    op.add_column("agent_memory", sa.Column("strategy_key", sa.String(64), nullable=True))
    op.add_column("agent_memory", sa.Column("outcome", sa.String(20), nullable=True))
    op.add_column("agent_memory", sa.Column("failure_kind", sa.String(80), nullable=True))
    op.add_column(
        "agent_memory",
        sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "agent_memory",
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "agent_memory",
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column("agent_memory", sa.Column("evidence_json", _json_type(), nullable=True))
    op.add_column("agent_memory", sa.Column("last_seen_at", sa.DateTime(), nullable=True))
    op.create_index(
        "ix_agent_memory_owner_scope",
        "agent_memory",
        ["owner_user_id", "share_scope", "status"],
    )
    op.create_index("ix_agent_memory_project", "agent_memory", ["project_id", "status"])
    op.create_index("ix_agent_memory_fingerprint", "agent_memory", ["fingerprint"])
    op.create_index("uq_agent_memory_strategy_key", "agent_memory", ["strategy_key"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_agent_memory_strategy_key", table_name="agent_memory")
    op.drop_index("ix_agent_memory_fingerprint", table_name="agent_memory")
    op.drop_index("ix_agent_memory_project", table_name="agent_memory")
    op.drop_index("ix_agent_memory_owner_scope", table_name="agent_memory")
    for column in (
        "last_seen_at",
        "evidence_json",
        "confidence",
        "failure_count",
        "success_count",
        "failure_kind",
        "outcome",
        "strategy_key",
        "fingerprint",
        "share_scope",
        "project_id",
        "owner_user_id",
    ):
        op.drop_column("agent_memory", column)
