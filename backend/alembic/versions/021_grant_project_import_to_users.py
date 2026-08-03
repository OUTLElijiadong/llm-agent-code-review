"""Grant remote project import to ordinary project operators.

Revision ID: 021
Revises: 020
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "021"
down_revision: Union[str, None] = "020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PERMISSION_CODE = "project:import"
_TARGET_ROLE_CODES = ("user", "reviewer")


def _tables() -> tuple[sa.TableClause, sa.TableClause, sa.TableClause]:
    role = sa.table(
        "role",
        sa.column("id", sa.BigInteger()),
        sa.column("code", sa.String()),
    )
    permission = sa.table(
        "permission",
        sa.column("id", sa.BigInteger()),
        sa.column("code", sa.String()),
    )
    role_permission = sa.table(
        "role_permission",
        sa.column("id", sa.BigInteger()),
        sa.column("role_id", sa.BigInteger()),
        sa.column("permission_id", sa.BigInteger()),
    )
    return role, permission, role_permission


def _required_ids(conn, role, permission) -> tuple[list[int], int]:
    permission_id = conn.execute(
        sa.select(permission.c.id).where(permission.c.code == _PERMISSION_CODE)
    ).scalar_one_or_none()
    if permission_id is None:
        raise RuntimeError(f"missing required permission: {_PERMISSION_CODE}")

    rows = conn.execute(
        sa.select(role.c.id, role.c.code).where(role.c.code.in_(_TARGET_ROLE_CODES))
    ).mappings().all()
    role_ids_by_code = {str(row["code"]): int(row["id"]) for row in rows}
    missing_roles = sorted(set(_TARGET_ROLE_CODES) - set(role_ids_by_code))
    if missing_roles:
        raise RuntimeError(f"missing required roles: {', '.join(missing_roles)}")
    return [role_ids_by_code[code] for code in _TARGET_ROLE_CODES], int(permission_id)


def upgrade() -> None:
    conn = op.get_bind()
    role, permission, role_permission = _tables()
    role_ids, permission_id = _required_ids(conn, role, permission)

    for role_id in role_ids:
        existing = conn.execute(
            sa.select(role_permission.c.id).where(
                role_permission.c.role_id == role_id,
                role_permission.c.permission_id == permission_id,
            )
        ).first()
        if existing is None:
            conn.execute(
                role_permission.insert().values(
                    role_id=role_id,
                    permission_id=permission_id,
                )
            )


def downgrade() -> None:
    conn = op.get_bind()
    role, permission, role_permission = _tables()
    permission_id = conn.execute(
        sa.select(permission.c.id).where(permission.c.code == _PERMISSION_CODE)
    ).scalar_one_or_none()
    if permission_id is None:
        return
    role_ids = conn.execute(
        sa.select(role.c.id).where(role.c.code.in_(_TARGET_ROLE_CODES))
    ).scalars().all()
    if role_ids:
        conn.execute(
            role_permission.delete().where(
                role_permission.c.role_id.in_(role_ids),
                role_permission.c.permission_id == permission_id,
            )
        )
