"""Install realistic Studio grants for tests that build published fixtures."""

from app.core.permission_codes import PermissionCode
from app.models.rbac import Permission, Role, RolePermission, UserRole

STUDIO_PERMISSIONS = (
    PermissionCode.AGENT_ASSET_CREATE,
    PermissionCode.AGENT_ASSET_UPDATE_OWN,
    PermissionCode.AGENT_ASSET_TEST,
    PermissionCode.AGENT_ASSET_SUBMIT,
    PermissionCode.SKILL_ASSET_CREATE,
    PermissionCode.SKILL_ASSET_UPDATE_OWN,
)


def grant_studio_actor(db, user):
    """Mirror the relevant seeded RBAC rows rather than bypassing the fence."""
    role = db.query(Role).filter_by(code=user.role).first()
    if role is None:
        role = Role(name=user.role, code=user.role, status="active", is_builtin=1)
        db.add(role)
        db.flush()
    if not db.query(UserRole.id).filter_by(user_id=user.id, role_id=role.id).first():
        db.add(UserRole(user_id=user.id, role_id=role.id))
    for code in STUDIO_PERMISSIONS:
        permission = db.query(Permission).filter_by(code=code).first()
        if permission is None:
            permission = Permission(code=code, name=code, module="agent", type="api")
            db.add(permission)
            db.flush()
        if not db.query(RolePermission.id).filter_by(role_id=role.id, permission_id=permission.id).first():
            db.add(RolePermission(role_id=role.id, permission_id=permission.id))
            db.flush()
    db.flush()
