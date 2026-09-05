"""Persist verifiable scan input and execution coverage without inventing legacy snapshots."""

import sqlalchemy as sa

from alembic import op

revision = "047_review_input_snapshot"
down_revision = "046_finding_aggregation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("review_task_file", sa.Column("version_no", sa.Integer(), nullable=True))
    op.add_column("review_task_file", sa.Column("content_sha256", sa.String(64), nullable=True))
    op.add_column("review_task_file", sa.Column("file_snapshot", sa.JSON(), nullable=True))
    op.add_column("review_task", sa.Column("coverage", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("review_task", "coverage")
    op.drop_column("review_task_file", "file_snapshot")
    op.drop_column("review_task_file", "content_sha256")
    op.drop_column("review_task_file", "version_no")
