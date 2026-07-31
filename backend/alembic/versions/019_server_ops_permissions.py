"""Add dedicated server operations permissions.

Revision ID: 019
Revises: 018
"""

from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "019"
down_revision: Union[str, None] = "018"
branch_labels = None
depends_on = None

PERMISSIONS = (
    ("server_ops:view", "查看服务器运维状态", "查看宿主机、容器、systemd 和日志状态"),
    ("server_ops:execute", "执行服务器运维变更", "批准后执行结构化服务器变更"),
    ("server_ops:critical", "执行服务器高危变更", "批准后执行文件、软件包、防火墙、账户等高危变更"),
)


def upgrade() -> None:
    conn = op.get_bind()
    permission = sa.table(
        "permission",
        sa.column("id", sa.BigInteger()),
        sa.column("code", sa.String()),
        sa.column("name", sa.String()),
        sa.column("module", sa.String()),
        sa.column("description", sa.String()),
    )
    role = sa.table("role", sa.column("id", sa.BigInteger()), sa.column("code", sa.String()))
    role_permission = sa.table(
        "role_permission",
        sa.column("role_id", sa.BigInteger()),
        sa.column("permission_id", sa.BigInteger()),
    )
    codes = [item[0] for item in PERMISSIONS]
    existing_codes = set(conn.execute(sa.select(permission.c.code).where(permission.c.code.in_(codes))).scalars())
    missing = [
        {
            "code": code,
            "name": name,
            "module": "server_ops",
            "description": description,
        }
        for code, name, description in PERMISSIONS
        if code not in existing_codes
    ]
    if missing:
        conn.execute(permission.insert(), missing)

    permission_ids = dict(
        conn.execute(sa.select(permission.c.code, permission.c.id).where(permission.c.code.in_(codes))).all()
    )
    role_ids = dict(
        conn.execute(sa.select(role.c.code, role.c.id).where(role.c.code.in_(("admin", "super_admin")))).all()
    )
    existing_links = set(
        conn.execute(
            sa.select(role_permission.c.role_id, role_permission.c.permission_id).where(
                role_permission.c.role_id.in_(role_ids.values()),
                role_permission.c.permission_id.in_(permission_ids.values()),
            )
        ).all()
    )
    links = [
        {"role_id": role_id, "permission_id": permission_id}
        for role_id in role_ids.values()
        for permission_id in permission_ids.values()
        if (role_id, permission_id) not in existing_links
    ]
    if links:
        conn.execute(role_permission.insert(), links)


def downgrade() -> None:
    conn = op.get_bind()
    codes = [item[0] for item in PERMISSIONS]
    permission = sa.table("permission", sa.column("id", sa.BigInteger()), sa.column("code", sa.String()))
    role_permission = sa.table("role_permission", sa.column("permission_id", sa.BigInteger()))
    permission_ids = list(conn.execute(sa.select(permission.c.id).where(permission.c.code.in_(codes))).scalars())
    if permission_ids:
        conn.execute(role_permission.delete().where(role_permission.c.permission_id.in_(permission_ids)))
        conn.execute(permission.delete().where(permission.c.id.in_(permission_ids)))
