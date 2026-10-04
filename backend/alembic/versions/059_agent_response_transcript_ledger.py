"""Store Responses transcripts once per session and reference them by cursor.

Revision ID: 059_response_transcript_ledger
Revises: 058_roundtable_sessions
"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision = "059_response_transcript_ledger"
down_revision = "058_roundtable_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_response_transcript_message",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("surface", sa.String(length=24), nullable=False),
        sa.Column("session_key", sa.String(length=128), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(length=80), nullable=True),
        sa.Column("message_json", mysql.LONGTEXT(), nullable=False),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("update_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_agent_response_transcript_position",
        "agent_response_transcript_message",
        ["user_id", "surface", "session_key", "position"],
        unique=True,
    )
    op.create_index(
        "ix_agent_response_transcript_run",
        "agent_response_transcript_message",
        ["run_id"],
    )


def downgrade() -> None:
    raise RuntimeError(
        "059 is forward-only: removing transcript ledger rows would invalidate compacted checkpoints"
    )
