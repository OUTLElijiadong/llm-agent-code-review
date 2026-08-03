"""Unique super-administrator identity invariants."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.rbac import Role, UserRole
from app.models.user import User

SUPER_ADMIN_USERNAME = "admin"
SUPER_ADMIN_ROLE = "super_admin"
SERVER_OPS_PERMISSION_PREFIX = "server_ops:"


def is_unique_super_admin(db: Session, user: User | None) -> bool:
    """Return whether ``user`` is the active, uniquely named super administrator.

    Both the legacy role and the RBAC binding must agree. This fails closed when
    migration data is incomplete or has been tampered with.
    """

    if (
        user is None
        or user.status != 1
        or user.username != SUPER_ADMIN_USERNAME
        or user.role != SUPER_ADMIN_ROLE
    ):
        return False
    binding = (
        db.query(UserRole.id)
        .join(Role, Role.id == UserRole.role_id)
        .filter(
            UserRole.user_id == user.id,
            Role.code == SUPER_ADMIN_ROLE,
            Role.status == "active",
        )
        .first()
    )
    return binding is not None
