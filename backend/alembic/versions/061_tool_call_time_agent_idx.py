"""Index time-bounded Agent activity aggregation.

Revision ID: 061_overview_time_indexes
Revises: 060_transcript_message_sha256
"""

from alembic import op

revision = "061_overview_time_indexes"
down_revision = "060_transcript_message_sha256"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_tool_call_time_agent",
        "tool_call_log",
        ["create_time", "agent_code"],
    )
    op.create_index(
        "ix_ai_call_log_time_agent_model",
        "ai_call_log",
        ["create_time", "agent_label", "model_name"],
    )


def downgrade() -> None:
    op.drop_index("ix_tool_call_time_agent", table_name="tool_call_log")
    op.drop_index("ix_ai_call_log_time_agent_model", table_name="ai_call_log")
