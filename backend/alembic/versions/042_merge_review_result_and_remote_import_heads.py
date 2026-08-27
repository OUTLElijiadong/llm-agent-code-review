"""Merge the review-result and remote-import migration branches.

Revision IDs must fit Alembic's default ``version_num VARCHAR(32)`` column.
"""

from __future__ import annotations

from typing import Sequence, Union

revision: str = "042_merge_review_import_heads"
down_revision: Union[str, Sequence[str], None] = (
    "041_review_result_pipeline",
    "041a_remote_import_tasks",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Join both 041 branches without changing application tables."""


def downgrade() -> None:
    """Re-open the two historical heads when rolling back the merge."""
