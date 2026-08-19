"""Persist immutable sandbox input and recovery lease.

Revision ID: 038
Revises: 037
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "038"
down_revision: Union[str, None] = "037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sandbox_environment",
        sa.Column("source_archive_blob", mysql.LONGBLOB().with_variant(sa.LargeBinary(), "sqlite"), nullable=True),
    )
    op.add_column(
        "sandbox_environment",
        sa.Column("execution_token", sa.String(length=64), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("sandbox_environment", "execution_token")
    op.drop_column("sandbox_environment", "source_archive_blob")
