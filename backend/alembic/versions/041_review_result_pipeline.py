"""Add review result provenance and versioned scoring metadata.

Revision ID: 041_review_result_pipeline
Revises: 040
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "041_review_result_pipeline"
down_revision: Union[str, None] = "040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("review_issue", sa.Column("source_details", sa.JSON(), nullable=True))
    op.add_column(
        "review_issue",
        sa.Column("confirmation_count", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column("review_issue", sa.Column("finding_fingerprint", sa.String(length=64), nullable=True))
    op.add_column("review_issue", sa.Column("cvss_version", sa.String(length=8), nullable=True))
    op.add_column("review_issue", sa.Column("cvss_source", sa.String(length=16), nullable=True))
    op.create_index(
        "ix_review_issue_task_fingerprint",
        "review_issue",
        ["task_id", "finding_fingerprint"],
        unique=False,
    )
    op.add_column("review_task", sa.Column("score_version", sa.String(length=32), nullable=True))
    op.add_column("review_task", sa.Column("score_breakdown", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("review_task", "score_breakdown")
    op.drop_column("review_task", "score_version")
    op.drop_index("ix_review_issue_task_fingerprint", table_name="review_issue")
    op.drop_column("review_issue", "cvss_source")
    op.drop_column("review_issue", "cvss_version")
    op.drop_column("review_issue", "finding_fingerprint")
    op.drop_column("review_issue", "confirmation_count")
    op.drop_column("review_issue", "source_details")
