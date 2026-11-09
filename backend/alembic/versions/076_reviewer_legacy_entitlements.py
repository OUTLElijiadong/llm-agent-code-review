"""Preserve reviewer access that existed before paid reviewer entitlements.

Revision ID: 076_reviewer_legacy_entitlements
Revises: 075_payment_checkout_state
"""

from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.sql import quoted_name

from alembic import op

revision = "076_reviewer_legacy_entitlements"
down_revision = "075_payment_checkout_state"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    users = sa.table(
        quoted_name("user", True),
        sa.column("id", sa.BigInteger()),
        sa.column("role", sa.String(20)),
        sa.column("status", sa.SmallInteger()),
    )
    user_roles = sa.table("user_role", sa.column("user_id", sa.BigInteger()), sa.column("role_id", sa.BigInteger()))
    roles = sa.table("role", sa.column("id", sa.BigInteger()), sa.column("code", sa.String(64)),
                     sa.column("status", sa.String(16)))
    permanent = sa.table(
        "billing_permanent_reviewer",
        sa.column("user_id", sa.BigInteger()), sa.column("status", sa.String(16)),
        sa.column("granted_by", sa.BigInteger()), sa.column("granted_at", sa.DateTime()),
    )
    events = sa.table(
        "billing_permanent_reviewer_event",
        sa.column("user_id", sa.BigInteger()), sa.column("actor_id", sa.BigInteger()),
        sa.column("action", sa.String(16)), sa.column("reason", sa.String(500)),
        sa.column("created_at", sa.DateTime()),
    )

    conflicting_active_role = sa.exists(
        sa.select(sa.literal(1))
        .select_from(user_roles.join(roles, roles.c.id == user_roles.c.role_id))
        .where(
            user_roles.c.user_id == users.c.id,
            roles.c.status == "active",
            roles.c.code != "reviewer",
        )
    )
    legacy_ids = connection.execute(
        sa.select(users.c.id)
        .where(users.c.role == "reviewer", users.c.status.in_([0, 1]), sa.not_(conflicting_active_role))
        .order_by(users.c.id)
    ).scalars().all()
    if not legacy_ids:
        return

    admin_id = connection.execute(
        sa.text("""
            SELECT u.id
            FROM `user` AS u
            JOIN user_role AS ur ON ur.user_id = u.id
            JOIN `role` AS r ON r.id = ur.role_id
            WHERE u.username = :username AND u.role = :role AND u.status = 1
              AND r.code = :role AND r.status = 'active'
            ORDER BY u.id
            LIMIT 1
        """),
        {"username": "admin", "role": "super_admin"},
    ).scalar_one_or_none()
    if admin_id is None:
        raise RuntimeError("审查员权益回填需要唯一 super_admin 的一致 RBAC 绑定；迁移已拒绝伪造授权人")
    actor_id = int(admin_id)
    granted_at = datetime.now(timezone.utc).replace(tzinfo=None)
    for user_id in legacy_ids:
        exists = connection.execute(
            sa.select(permanent.c.user_id).where(permanent.c.user_id == user_id)
        ).first()
        if exists:
            continue
        connection.execute(permanent.insert().values(
            user_id=user_id, status="active", granted_by=actor_id, granted_at=granted_at,
        ))
        connection.execute(events.insert().values(
            user_id=user_id, actor_id=actor_id, action="grandfather",
            reason="076 迁移：保留升级前有效的审查员永久权益",
            created_at=granted_at,
        ))


def downgrade():
    # Entitlement and audit history must survive application rollback.
    raise RuntimeError("审查员历史权益回填是前向迁移；请通过备份恢复，禁止将 Alembic 版本号单独回退")
