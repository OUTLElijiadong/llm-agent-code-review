"""Restrict Agent Studio asset permissions to reviewer and above.

Revision ID: 049_agent_studio_reviewer_only
Revises: 048_ai_usage_attribution

Agent 工坊(AgentStudio)定位为审查者(评审员)及以上角色的创作工具。
034/045 曾把 agent_asset 与 skill_asset 草稿权限授予普通用户;本迁移
收回普通用户(user 角色)的这些授权,reviewer/admin 由 015 迁移授予的
权限保持不变。downgrade 按 034/045 的口径重新授予普通用户。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "049_agent_studio_reviewer_only"
down_revision: Union[str, None] = "048_ai_usage_attribution"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PERMISSION_CODES = (
    "agent_asset:create",
    "agent_asset:update_own",
    "agent_asset:test",
    "agent_asset:submit",
    "skill_asset:create",
    "skill_asset:update_own",
)
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


def _required_ids(conn, role, permission) -> dict[str, int]:
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
        # 权限码由 007/015 迁移种入;缺码说明库状态异常,显式失败而不是静默跳过。
        raise RuntimeError(f"missing required permissions: {', '.join(missing_permissions)}")
    return {**role_ids, **permission_ids}


def _render_offline_upgrade(role, permission, role_permission) -> None:
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


def _render_offline_downgrade(role, permission, role_permission) -> None:
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


def upgrade() -> None:
    role, permission, role_permission = _tables()
    if op.get_context().as_sql:
        _render_offline_upgrade(role, permission, role_permission)
        return
    bind = op.get_bind()
    _required_ids(bind, role, permission)
    _render_offline_upgrade(role, permission, role_permission)


def downgrade() -> None:
    role, permission, role_permission = _tables()
    if op.get_context().as_sql:
        _render_offline_downgrade(role, permission, role_permission)
        return
    bind = op.get_bind()
    _required_ids(bind, role, permission)
    _render_offline_downgrade(role, permission, role_permission)
