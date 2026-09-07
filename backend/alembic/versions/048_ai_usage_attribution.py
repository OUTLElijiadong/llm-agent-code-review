"""Add exact usage lineage without guessing or rewriting historical records."""

import sqlalchemy as sa

from alembic import op

revision = "048_ai_usage_attribution"
down_revision = "047_review_input_snapshot"
branch_labels = None
depends_on = None

_FIELDS = {
    "root_agent_run_id": "agent_response_run",
    "agent_run_id": "agent_response_run",
    "tool_execution_id": "agent_tool_execution",
    "agent_team_id": "agent_team",
    "agent_team_task_id": "agent_team_task",
    "agent_execution_event_id": "agent_team_event",
}
_TABLES = ("agent_response_run", "agent_team", "review_task", "ai_call_log", "agent_mesh_message", "pentest_engagement", "sandbox_environment")


def _fields(table):
    return _FIELDS


def upgrade():
    for table in _TABLES:
        with op.batch_alter_table(table) as batch:
            for column, target in _fields(table).items():
                batch.add_column(sa.Column(column, sa.BigInteger(), nullable=True))
                batch.create_foreign_key(f"fk_{table}_{column}", target, [column], ["id"], ondelete="SET NULL")
                batch.create_index(f"ix_{table}_{column}", [column])


def downgrade():
    for table in reversed(_TABLES):
        with op.batch_alter_table(table) as batch:
            for column in reversed(_fields(table)):
                batch.drop_constraint(f"fk_{table}_{column}", type_="foreignkey")
                batch.drop_index(f"ix_{table}_{column}")
                batch.drop_column(column)
