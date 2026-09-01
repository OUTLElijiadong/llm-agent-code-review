"""Persist deterministic finding aggregation and human-review metadata.

Revision ID: 046_finding_aggregation
Revises: 045_skill_asset_user_grant
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "046_finding_aggregation"
down_revision: Union[str, None] = "045_skill_asset_user_grant"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("review_issue") as batch:
        batch.add_column(sa.Column("aggregation_version", sa.String(32), nullable=True))
        batch.add_column(sa.Column("evidence_quality", sa.String(20), nullable=True))
        batch.add_column(sa.Column("conflict_status", sa.String(20), nullable=True))
        batch.add_column(sa.Column("human_review_status", sa.String(24), nullable=True))
        batch.add_column(sa.Column("risk_score", sa.Float(), nullable=True))
        batch.add_column(sa.Column("aggregation_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("review_issue") as batch:
        batch.drop_column("aggregation_json")
        batch.drop_column("risk_score")
        batch.drop_column("human_review_status")
        batch.drop_column("conflict_status")
        batch.drop_column("evidence_quality")
        batch.drop_column("aggregation_version")
