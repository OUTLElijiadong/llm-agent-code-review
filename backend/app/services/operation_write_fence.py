"""Fence authenticated project-related writes against live RBAC changes."""

from functools import wraps
from time import time

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.exceptions import AuthError, ConflictError, ForbiddenError, PermissionError
from app.models.user import User


def require_live_operation_permission(db: Session, user: User, permission: str) -> None:
    """Revalidate an HTTP actor's exact grant and retain locks until commit.

    Direct trusted service callers have no authenticated JWT snapshot and
    retain their existing contract. Reviewer entitlement is locked before
    user/RBAC rows, matching the refund service's order.
    """
    token_version = getattr(user, "_authenticated_token_version", None)
    if token_version is None:
        return
    expires_at = getattr(user, "_authenticated_token_expires_at", None)
    if expires_at is None or expires_at <= time():
        raise AuthError("token非法或已过期", code=40101)
    from app.services.rbac_service import check_paid_permission_fenced

    if not check_paid_permission_fenced(
        db, int(user.id), permission, expected_token_version=token_version, nowait=True,
    ):
        # Distinguish a revoked login from a revoked operation grant.  The
        # early paid check can fail on an old snapshot, so read the account
        # currently rather than relying on the identity map.
        current = (db.query(User).filter_by(id=user.id).populate_existing()
                   .with_for_update(nowait=True).one_or_none())
        if current is None or current.status != 1:
            raise ForbiddenError("账号不存在或已禁用", code=40301)
        if int(current.token_version or 0) != token_version:
            raise AuthError("账号已在另一台设备登录，当前设备已下线", code=40102)
        if expires_at <= time():
            raise AuthError("token非法或已过期", code=40101)
        raise PermissionError(f"无操作权限: 需要 {permission}", detail={"required_permission": permission})
    if expires_at <= time():
        raise AuthError("token非法或已过期", code=40101)


def retryable_project_write(function):
    """Translate only known MySQL lock conflicts and release the transaction."""
    @wraps(function)
    def wrapped(db: Session, *args, **kwargs):
        try:
            return function(db, *args, **kwargs)
        except OperationalError as exc:
            code = getattr(exc.orig, "args", (None,))[0]
            if db.get_bind().dialect.name == "mysql" and code in (1205, 1213, 3572):
                db.rollback()
                raise ConflictError("项目权限或状态正在变更，请稍后重试", code=40901) from exc
            raise
    return wrapped
