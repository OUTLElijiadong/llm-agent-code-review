"""Explicitly delegated, narrowly scoped C4 review authority."""

from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.exceptions import ForbiddenError, ValidationError
from app.models.audit_log import AuditLog
from app.models.billing_ledger import BillingReconciliationReviewerGrant
from app.models.rbac import Role, UserRole
from app.models.user import User


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _locked_user_and_role(db: Session, user_id: int):
    user = db.query(User).filter_by(id=user_id).populate_existing().with_for_update().one_or_none()
    if user is None or user.status != 1:
        raise ForbiddenError("账号已停用或不存在")
    roles = (db.query(Role.code, Role.status).select_from(UserRole).join(Role, Role.id == UserRole.role_id)
             .filter(UserRole.user_id == user_id).with_for_update().all())
    active = {code for code, status in roles if status == "active"}
    if active != {user.role}:
        raise ForbiddenError("平台账号角色不一致")
    return user


def _grant(db: Session, user_id: int, *, lock: bool):
    # Do not take a missing-key gap lock; the user row serializes same-user writers.
    found = db.query(BillingReconciliationReviewerGrant.id).filter_by(user_id=user_id).one_or_none()
    if found is None:
        return None
    q = db.query(BillingReconciliationReviewerGrant).filter_by(id=found.id).populate_existing()
    return (q.with_for_update() if lock else q).one_or_none()


def _audit(db: Session, *, actor_id: int | None, target: User, action: str, reason: str):
    db.add(AuditLog(actor_id=actor_id, action=action, target_type="billing_c4_reviewer",
                    target_id=str(target.id), detail=reason[:500], status="success", create_time=_now()))


def reviewer_eligibility(db: Session, user_ids: list[int]) -> dict[int, bool]:
    """Display-only identity eligibility; decision still rechecks under locks."""
    if not user_ids:
        return {}
    users = (db.query(User.id, User.status, User.role).filter(User.id.in_(user_ids)).all())
    bindings = (db.query(UserRole.user_id, Role.code, Role.status)
                .join(Role, Role.id == UserRole.role_id).filter(UserRole.user_id.in_(user_ids)).all())
    active_roles: dict[int, set[str]] = {}
    for user_id, code, status in bindings:
        if status == "active":
            active_roles.setdefault(int(user_id), set()).add(code)
    return {int(user_id): status == 1 and role == "admin" and active_roles.get(int(user_id)) == {"admin"}
            for user_id, status, role in users}


def require_reviewer(db: Session, user_id: int, token_version: int):
    """Lock live authority before any C4 decision; held until transaction ends."""
    user = _locked_user_and_role(db, user_id)
    if int(user.token_version or 0) != token_version:
        raise ForbiddenError("账号会话已更新，请重新登录")
    if user.role == "super_admin" and user.username == "admin":
        return user
    if user.role != "admin":
        raise ForbiddenError("无 C4 独立复核权限")
    grant = _grant(db, user_id, lock=True)
    if grant is None or grant.active != 1:
        raise ForbiddenError("无 C4 独立复核权限")
    return user


def set_reviewer(db: Session, *, actor_id: int, user_id: int, active: bool, reason: str):
    if type(user_id) is not int or user_id <= 0 or type(active) is not bool:
        raise ValidationError("委任参数无效")
    if (not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500
            or any(ord(char) < 32 or ord(char) == 127 for char in reason)):
        raise ValidationError("委任说明无效")
    if actor_id == user_id:
        raise ForbiddenError("不能委任本人")
    user = _locked_user_and_role(db, user_id)
    if user.role != "admin":
        raise ForbiddenError("只能委任有效的平台管理员")
    # The unique super-admin actor row is locked by the caller, serializing
    # grants. A current read is required after waiting on the target user;
    # the authenticated request may have opened an older RR snapshot.
    row = (db.query(BillingReconciliationReviewerGrant).filter_by(user_id=user_id)
           .populate_existing().with_for_update().one_or_none())
    if row is None:
        row = BillingReconciliationReviewerGrant(user_id=user_id, active=0)
        db.add(row)
        db.flush()
    if bool(row.active) != active:
        row.active = int(active)
        if active:
            row.granted_by, row.granted_at = actor_id, _now()
            row.revoked_by, row.revoked_at = None, None
        else:
            row.revoked_by, row.revoked_at = actor_id, _now()
        _audit(db, actor_id=actor_id, target=user,
               action="billing.c4_reviewer.grant" if active else "billing.c4_reviewer.revoke", reason=reason.strip())
    db.flush()
    return row


def revoke_for_user(db: Session, user: User, *, actor_id: int | None, reason: str):
    """Caller must hold the user row lock and commit this with the identity change."""
    # A current UPDATE cannot miss a newly granted row due to an old RR snapshot.
    result = db.execute(update(BillingReconciliationReviewerGrant)
                        .where(BillingReconciliationReviewerGrant.user_id == user.id,
                               BillingReconciliationReviewerGrant.active == 1)
                        .values(active=0, revoked_by=actor_id, revoked_at=_now()))
    if result.rowcount:
        _audit(db, actor_id=actor_id, target=user, action="billing.c4_reviewer.revoke", reason=reason)
