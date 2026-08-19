"""Persist the stable worker request envelope used for recovery.

Revision ID: 040
Revises: 039
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "040"
down_revision: Union[str, None] = "039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sandbox_environment",
        sa.Column("worker_request_json", mysql.LONGTEXT().with_variant(sa.Text(), "sqlite"), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sandbox_environment", "worker_request_json")
