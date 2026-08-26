"""Create recoverable remote project import tasks.

Revision ID: 041a_remote_import_tasks
Revises: 040
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "041a_remote_import_tasks"
down_revision: Union[str, None] = "040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json_type():
    return mysql.LONGTEXT() if op.get_bind().dialect.name == "mysql" else sa.Text()


def upgrade() -> None:
    op.create_table(
        "project_import_task",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.String(32), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("request_json", _json_type(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="queued"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("lease_token", sa.String(80), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("project_id", sa.BigInteger(), nullable=True),
        sa.Column("result_json", _json_type(), nullable=False),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("update_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("public_id", name="uk_project_import_task_public"),
        sa.UniqueConstraint(
            "user_id",
            "idempotency_key_hash",
            name="uk_project_import_task_idempotency",
        ),
    )
    op.create_index(
        "ix_project_import_task_queue",
        "project_import_task",
        ["status", "next_attempt_at", "lease_expires_at", "id"],
    )
    op.create_index(
        "ix_project_import_task_owner_status",
        "project_import_task",
        ["user_id", "status", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_project_import_task_owner_status", table_name="project_import_task")
    op.drop_index("ix_project_import_task_queue", table_name="project_import_task")
    op.drop_table("project_import_task")
