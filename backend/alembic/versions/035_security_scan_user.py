"""Grant ordinary members the security:scan permission.

Revision ID: 035
Revises: 034
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "035"
down_revision: Union[str, None] = "034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PERMISSION_CODES = ("security:scan",)
# 只授予普通用户。reviewer 的 security:scan 已由 007 授予，035 的 downgrade
# 不得误删该历史授权。
_TARGET_ROLE_CODES = ("user",)


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


def _required_ids(conn, role, permission) -> tuple[list[int], dict[str, int]]:
    roles = conn.execute(
        sa.select(role.c.id, role.c.code).where(role.c.code.in_(_TARGET_ROLE_CODES))
    ).mappings().all()
    role_ids = {str(row["code"]): int(row["id"]) for row in roles}
    missing_roles = sorted(set(_TARGET_ROLE_CODES) - set(role_ids))
    if missing_roles:
        raise RuntimeError(f"missing required roles: {', '.join(missing_roles)}")

    permissions = conn.execute(
        sa.select(permission.c.id, permission.c.code).where(permission.c.code.in_(_PERMISSION_CODES))
    ).mappings().all()
    permission_ids = {str(row["code"]): int(row["id"]) for row in permissions}
    missing_permissions = sorted(set(_PERMISSION_CODES) - set(permission_ids))
    if missing_permissions:
        raise RuntimeError(f"missing required permissions: {', '.join(missing_permissions)}")
    return [role_ids[code] for code in _TARGET_ROLE_CODES], permission_ids


def _render_offline_upgrade(role, permission, role_permission) -> None:
    missing_pair = ~sa.exists(
        sa.select(1).where(
            role_permission.c.role_id == role.c.id,
            role_permission.c.permission_id == permission.c.id,
        )
    )
    source = (
        sa.select(role.c.id, permission.c.id)
        .select_from(role.join(permission, sa.true()))
        .where(
            role.c.code.in_(_TARGET_ROLE_CODES),
            permission.c.code.in_(_PERMISSION_CODES),
            missing_pair,
        )
    )
    op.execute(
        role_permission.insert().from_select(
            [role_permission.c.role_id, role_permission.c.permission_id],
            source,
        )
    )


def _render_offline_downgrade(role, permission, role_permission) -> None:
    op.execute(
        role_permission.delete().where(
            role_permission.c.role_id.in_(
                sa.select(role.c.id).where(role.c.code.in_(_TARGET_ROLE_CODES))
            ),
            role_permission.c.permission_id.in_(
                sa.select(permission.c.id).where(permission.c.code.in_(_PERMISSION_CODES))
            ),
        )
    )


def upgrade() -> None:
    role, permission, role_permission = _tables()
    if op.get_context().as_sql:
        _render_offline_upgrade(role, permission, role_permission)
        return

    conn = op.get_bind()
    role_ids, permission_ids = _required_ids(conn, role, permission)
    existing = set(conn.execute(
        sa.select(role_permission.c.role_id, role_permission.c.permission_id).where(
            role_permission.c.role_id.in_(role_ids),
            role_permission.c.permission_id.in_(list(permission_ids.values())),
        )
    ).all())
    rows = [
        {"role_id": role_id, "permission_id": permission_ids[code]}
        for role_id in role_ids
        for code in _PERMISSION_CODES
        if (role_id, permission_ids[code]) not in existing
    ]
    if rows:
        conn.execute(role_permission.insert(), rows)


def downgrade() -> None:
    role, permission, role_permission = _tables()
    if op.get_context().as_sql:
        _render_offline_downgrade(role, permission, role_permission)
        return

    conn = op.get_bind()
    role_ids = conn.execute(
        sa.select(role.c.id).where(role.c.code.in_(_TARGET_ROLE_CODES))
    ).scalars().all()
    permission_ids = conn.execute(
        sa.select(permission.c.id).where(permission.c.code.in_(_PERMISSION_CODES))
    ).scalars().all()
    if role_ids and permission_ids:
        conn.execute(role_permission.delete().where(
            role_permission.c.role_id.in_(role_ids),
            role_permission.c.permission_id.in_(permission_ids),
        ))
