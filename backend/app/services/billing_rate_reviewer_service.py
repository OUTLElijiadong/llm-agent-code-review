"""Independent, revocable authority for model and tool rate review."""

from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.exceptions import ForbiddenError, ValidationError
from app.models.audit_log import AuditLog
from app.models.billing_ledger import BillingRateReviewerGrant
from app.models.user import User
from app.services.billing_c4_reviewer_service import _locked_user_and_role


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _grant(db: Session, user_id: int):
    found = db.query(BillingRateReviewerGrant.id).filter_by(user_id=user_id).one_or_none()
    if found is None:
        return None
    return (db.query(BillingRateReviewerGrant).filter_by(id=found.id).populate_existing()
            .with_for_update().one_or_none())


def _audit(db: Session, actor_id: int | None, user: User, action: str, reason: str):
    db.add(AuditLog(actor_id=actor_id, action=action, target_type="billing_rate_reviewer",
                    target_id=str(user.id), detail=reason[:500], status="success", create_time=_now()))


def require_reviewer(db: Session, user_id: int, token_version: int):
    """Lock live user, RBAC and this specific grant until the operation commits."""
    user = _locked_user_and_role(db, user_id)
    if int(user.token_version or 0) != token_version:
        raise ForbiddenError("账号会话已更新，请重新登录")
    if user.role == "super_admin" and user.username == "admin":
        return user
    if user.role != "admin":
        raise ForbiddenError("无费率独立复核权限")
    grant = _grant(db, user_id)
    if grant is None or grant.active != 1:
        raise ForbiddenError("无费率独立复核权限")
    return user


def set_reviewer(db: Session, *, actor_id: int, user_id: int, active: bool, reason: str):
    if type(user_id) is not int or user_id <= 0 or type(active) is not bool:
        raise ValidationError("委任参数无效")
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
        raise ValidationError("委任说明无效")
    if actor_id == user_id:
        raise ForbiddenError("不能委任本人")
    user = _locked_user_and_role(db, user_id)
    if user.role != "admin":
        raise ForbiddenError("只能委任有效的平台管理员")
    # The caller locks the unique super-admin before the target user. Use a
    # current read after those locks so an old RR snapshot cannot miss a grant
    # committed while this request waited for the actor or target lock.
    row = (db.query(BillingRateReviewerGrant).filter_by(user_id=user_id)
           .populate_existing().with_for_update().one_or_none())
    if row is None:
        row = BillingRateReviewerGrant(user_id=user_id, active=0)
        db.add(row)
        db.flush()
    if bool(row.active) != active:
        row.active = int(active)
        if active:
            row.granted_by, row.granted_at = actor_id, _now()
            row.revoked_by, row.revoked_at = None, None
        else:
            row.revoked_by, row.revoked_at = actor_id, _now()
        _audit(db, actor_id, user, "billing.rate_reviewer.grant" if active else "billing.rate_reviewer.revoke",
               reason.strip())
    db.flush()
    return row


def revoke_for_user(db: Session, user: User, *, actor_id: int | None, reason: str):
    """Caller already holds the user row; revoke in the identity transaction."""
    result = db.execute(update(BillingRateReviewerGrant)
                        .where(BillingRateReviewerGrant.user_id == user.id,
                               BillingRateReviewerGrant.active == 1)
                        .values(active=0, revoked_by=actor_id, revoked_at=_now()))
    if result.rowcount:
        _audit(db, actor_id, user, "billing.rate_reviewer.revoke", reason)
