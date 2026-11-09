"""Real RBAC rows for remote approval tests that mock other permission checks."""

from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.user import User


def grant_remote_approval(db, *user_ids: int) -> None:
    """Create the reviewer grant checked by the transactional authorization fence."""
    role = db.query(Role).filter_by(code="reviewer").one_or_none()
    if role is None:
        role = Role(code="reviewer", name="审查员", status="active")
        db.add(role)
        db.flush()
    permission = db.query(Permission).filter_by(code="pentest:authorize").one_or_none()
    if permission is None:
        permission = Permission(code="pentest:authorize", name="批准远测", module="pentest")
        db.add(permission)
        db.flush()
    if not db.query(RolePermission.id).filter_by(role_id=role.id, permission_id=permission.id).first():
        db.add(RolePermission(role_id=role.id, permission_id=permission.id))
    for user_id in user_ids:
        user = db.get(User, user_id)
        if user is None:
            continue
        assert user.role == "reviewer"
        if not db.query(UserRole.id).filter_by(user_id=user_id, role_id=role.id).first():
            db.add(UserRole(user_id=user_id, role_id=role.id))
    db.commit()
