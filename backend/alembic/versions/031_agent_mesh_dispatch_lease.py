"""Add crash-safe dispatch leases to Agent Mesh messages.

Revision ID: 031
Revises: 030
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "031"
down_revision: Union[str, None] = "030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agent_mesh_message", sa.Column("lease_token", sa.String(80), nullable=True))
    op.add_column("agent_mesh_message", sa.Column("lease_expires_at", sa.DateTime(), nullable=True))
    op.add_column("agent_mesh_message", sa.Column("next_attempt_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_mesh_message", "next_attempt_at")
    op.drop_column("agent_mesh_message", "lease_expires_at")
    op.drop_column("agent_mesh_message", "lease_token")
