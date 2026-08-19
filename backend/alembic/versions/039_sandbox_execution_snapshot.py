"""Persist the exact sandbox worker request snapshot.

Revision ID: 039
Revises: 038
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "039"
down_revision: Union[str, None] = "038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sandbox_environment",
        sa.Column("execution_archive_blob", mysql.LONGBLOB().with_variant(sa.LargeBinary(), "sqlite"), nullable=True),
    )
    op.add_column(
        "sandbox_environment",
        sa.Column("execution_source_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "sandbox_environment",
        sa.Column("execution_round", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("sandbox_environment", "execution_round")
    op.drop_column("sandbox_environment", "execution_source_sha256")
    op.drop_column("sandbox_environment", "execution_archive_blob")
