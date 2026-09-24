"""Persist owner-scoped roundtable sessions and ordered turns.

Revision ID: 058_roundtable_sessions
Revises: 057_reviewer_audit_menu_route
"""

import sqlalchemy as sa

from alembic import op

revision = "058_roundtable_sessions"
down_revision = "057_reviewer_audit_menu_route"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "roundtable_session",
        sa.Column("session_id", sa.String(64), primary_key=True),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("task_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("project_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("file_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("review_type", sa.String(50), nullable=False, server_default="full"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("max_rounds", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("report_task_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("agents", sa.JSON(), nullable=False),
        sa.Column("progress", sa.JSON(), nullable=False),
        sa.Column("last_turn_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("origin_surface", sa.String(24), nullable=False, server_default=""),
        sa.Column("origin_session_key", sa.String(128), nullable=False, server_default=""),
        sa.Column("continued_from_session_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_roundtable_session_owner_updated", "roundtable_session", ["owner_user_id", "updated_at"])
    op.create_index("ix_roundtable_session_owner_status", "roundtable_session", ["owner_user_id", "status"])
    op.create_table(
        "roundtable_turn",
        sa.Column("session_id", sa.String(64), sa.ForeignKey("roundtable_session.session_id"), primary_key=True),
        sa.Column("seq", sa.Integer(), primary_key=True),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("turn", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_roundtable_turn_owner_session", "roundtable_turn", ["owner_user_id", "session_id"])


def downgrade() -> None:
    op.drop_index("ix_roundtable_turn_owner_session", table_name="roundtable_turn")
    op.drop_table("roundtable_turn")
    op.drop_index("ix_roundtable_session_owner_status", table_name="roundtable_session")
    op.drop_index("ix_roundtable_session_owner_updated", table_name="roundtable_session")
    op.drop_table("roundtable_session")
