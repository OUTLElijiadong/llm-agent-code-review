"""Add cancellation and heartbeat fields to remote imports.

Revision ID: 043_remote_import_lifecycle
Revises: 042_merge_review_import_heads
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "043_remote_import_lifecycle"
down_revision: Union[str, None] = "042_merge_review_import_heads"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("project_import_task", sa.Column("cancel_reason", sa.Text(), nullable=True))
    op.add_column(
        "project_import_task",
        sa.Column("cancel_requested_at", sa.DateTime(), nullable=True),
    )
    op.add_column("project_import_task", sa.Column("heartbeat_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("project_import_task", "heartbeat_at")
    op.drop_column("project_import_task", "cancel_requested_at")
    op.drop_column("project_import_task", "cancel_reason")
