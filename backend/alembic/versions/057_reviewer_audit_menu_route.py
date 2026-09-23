"""route the merged reviewer audit menu to its permission-backed page

Revision ID: 057_reviewer_audit_menu_route
Revises: 056_older_run_titles

The historic auditor menu points into the admin-only route tree. Since the
auditor permissions are merged into reviewer, move only the matching menu row
to the standalone audit page and keep its previous path for a precise rollback.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "057_reviewer_audit_menu_route"
down_revision: Union[str, None] = "056_older_run_titles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STATE_TABLE = "migration_057_audit_menu_route_state"


def upgrade() -> None:
    op.create_table(
        STATE_TABLE,
        sa.Column("menu_id", sa.BigInteger(), primary_key=True),
        sa.Column("old_path", sa.String(255), nullable=False),
    )
    op.execute(sa.text(f"""
        INSERT INTO {STATE_TABLE} (menu_id, old_path)
        SELECT id, path FROM menu
        WHERE path='/admin/audit' AND permission_code='audit:view'
    """))
    op.execute(sa.text(f"""
        UPDATE menu SET path='/audit'
        WHERE id IN (SELECT menu_id FROM {STATE_TABLE})
    """))


def downgrade() -> None:
    op.execute(sa.text(f"""
        UPDATE menu
        SET path=(SELECT old_path FROM {STATE_TABLE} WHERE menu_id=menu.id)
        WHERE id IN (SELECT menu_id FROM {STATE_TABLE})
    """))
    op.drop_table(STATE_TABLE)
