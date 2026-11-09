"""Explicit permanent and paid reviewer entitlement with a migration-only legacy backfill."""

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.billing_ledger import (
    BillingOrder,
    BillingPaymentEvent,
    BillingPermanentReviewer,
    BillingPermanentReviewerEvent,
    BillingRefund,
    BillingSubscription,
    BillingSubscriptionPeriod,
)
from app.models.rbac import Role
from app.models.user import User
from app.services import billing_ledger_service as ledger
from app.services import rbac_service
from app.services.billing_refund_policy import pending_access_expression
from app.services.billing_subscription_rules import SHANGHAI


def _pending_refund_orders(db: Session):
    return db.query(BillingRefund.order_no).filter(pending_access_expression())


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive(value):
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _authorize(db: Session, actor_id: int, user_id: int, reason: str):
    if not rbac_service.is_super_admin_user(db, actor_id):
        raise ledger.BillingError("只有平台最高管理员可以管理永久审查员")
    user = db.get(User, user_id)
    if not user or user.status != 1:
        raise ledger.BillingError("目标账号不可用")
    if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 500:
        raise ledger.BillingError("必须填写不超过 500 字的操作原因")


def set_permanent(db: Session, *, actor_id: int, user_id: int, active: bool, reason: str,
                  now: datetime | None = None) -> BillingPermanentReviewer:
    _authorize(db, actor_id, user_id, reason)
    if type(active) is not bool:
        raise ledger.BillingError("权益状态无效")
    now = _naive(now) if now is not None else _now()
    # Lock subject wallet to serialize grants/revocations and concurrent monthly issuance.
    wallet = ledger.get_or_create_wallet(db, "user", user_id)
    ledger._lock_wallets(db, [wallet.id])
    grant = db.query(BillingPermanentReviewer).filter_by(user_id=user_id).with_for_update().first()
    if grant is None:
        if not active:
            raise ledger.BillingError("该用户没有永久审查员权益")
        grant = BillingPermanentReviewer(user_id=user_id, status="active", granted_by=actor_id, granted_at=now)
        db.add(grant)
    elif (grant.status == "active") == active:
        return grant
    elif active:
        grant.status, grant.granted_by, grant.granted_at = "active", actor_id, now
        grant.revoked_by, grant.revoked_at = None, None
    else:
        grant.status, grant.revoked_by, grant.revoked_at = "revoked", actor_id, now
    db.add(BillingPermanentReviewerEvent(user_id=user_id, actor_id=actor_id,
                                        action="grant" if active else "revoke", reason=reason.strip(), created_at=now))
    db.flush()
    return grant


def issue_permanent_month(db: Session, user_id: int, *, now: datetime | None = None) -> bool:
    """Grant once per Shanghai calendar month; prior expired/revoked grants are not recreated."""
    now = _naive(now) if now is not None else _now()
    wallet = ledger.get_or_create_wallet(db, "user", user_id)
    ledger._lock_wallets(db, [wallet.id])
    grant = db.query(BillingPermanentReviewer).filter_by(user_id=user_id, status="active").with_for_update().first()
    user = db.get(User, user_id)
    if not grant or not user or user.status != 1 or grant.granted_at > now:
        return False
    local = now.replace(tzinfo=timezone.utc).astimezone(SHANGHAI)
    if local.month == 12:
        next_month = local.replace(year=local.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        next_month = local.replace(month=local.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
    key = f"permanent:{user_id}:{local.year:04d}-{local.month:02d}"
    ledger.grant(db, wallet_id=wallet.id, grant_key=key, kind="permanent", amount_milli=5_000_000,
                 expires_at=next_month)
    return True


def has_reviewer_entitlement(db: Session, user_id: int, *, now: datetime | None = None) -> bool:
    now = _naive(now) if now is not None else _now()
    user = db.get(User, user_id)
    if not user or user.status != 1:
        return False
    permanent = db.query(BillingPermanentReviewer).filter_by(user_id=user_id, status="active").first()
    if permanent and permanent.granted_at <= now:
        return True
    return db.query(BillingSubscriptionPeriod.id).join(
        BillingSubscription, BillingSubscriptionPeriod.subscription_id == BillingSubscription.id,
    ).join(BillingOrder, BillingOrder.order_no == BillingSubscriptionPeriod.order_no).join(
        BillingPaymentEvent, BillingPaymentEvent.order_no == BillingOrder.order_no,
    ).filter(BillingSubscription.owner_type == "user", BillingSubscription.owner_id == user_id,
             BillingSubscription.product == "reviewer", BillingOrder.status == "paid",
             BillingSubscriptionPeriod.starts_at <= now,
             BillingSubscriptionPeriod.ends_at > now - timedelta(days=7),
             ~BillingOrder.order_no.in_(_pending_refund_orders(db))).first() is not None


def lock_reviewer_entitlement_evidence(db: Session, user_id: int, *, nowait: bool = False):
    """Lock current entitlement evidence before locking the reviewer account.

    Refund writers lock order then refund, then wallet/period and finally the
    user. Permanent-grant writers lock the grant before the user. Use shared
    current reads in that order; the caller keeps this same transaction open
    until its protected write commits. The initial consistent read only finds
    candidate orders: missing a newly purchased period fails closed and a
    subsequent request can retry with a new snapshot.
    """
    order_ids = [row.id for row in (
        db.query(BillingOrder.id)
        .join(BillingSubscriptionPeriod, BillingSubscriptionPeriod.order_no == BillingOrder.order_no)
        .join(BillingSubscription, BillingSubscription.id == BillingSubscriptionPeriod.subscription_id)
        .filter(BillingSubscription.owner_type == "user", BillingSubscription.owner_id == user_id,
                BillingSubscription.product == "reviewer")
        .order_by(BillingOrder.id)
        .all()
    )]
    orders = []
    for order_id in order_ids:
        row = (db.query(BillingOrder).filter_by(id=order_id).populate_existing()
               .with_for_update(read=True, nowait=nowait).first())
        if row is not None:
            orders.append(row)
    order_nos = [row.order_no for row in orders]
    blocked_orders = set()
    if order_nos:
        blocked_orders = {row.order_no for row in (
            db.query(BillingRefund.order_no, pending_access_expression().label("pending"))
            .filter(BillingRefund.order_no.in_(order_nos)).order_by(BillingRefund.order_no)
            .with_for_update(read=True, nowait=nowait).all()
        ) if row.pending}
    subscription = (db.query(BillingSubscription)
                    .filter_by(owner_type="user", owner_id=user_id, product="reviewer")
                    .populate_existing().with_for_update(read=True, nowait=nowait).first())
    periods = []
    paid_orders = set()
    if subscription is not None and order_nos:
        periods = (db.query(BillingSubscriptionPeriod)
                   .filter(BillingSubscriptionPeriod.subscription_id == subscription.id,
                           BillingSubscriptionPeriod.order_no.in_(order_nos))
                   .order_by(BillingSubscriptionPeriod.id).populate_existing()
                   .with_for_update(read=True, nowait=nowait).all())
        paid_orders = {row.order_no for row in (
            db.query(BillingPaymentEvent.order_no).filter(BillingPaymentEvent.order_no.in_(order_nos))
            .order_by(BillingPaymentEvent.order_no).with_for_update(read=True, nowait=nowait).all()
        )}
    permanent = (db.query(BillingPermanentReviewer).filter_by(user_id=user_id)
                 .populate_existing().with_for_update(read=True, nowait=nowait).first())
    return orders, blocked_orders, periods, paid_orders, permanent


def reviewer_entitlement_evidence_is_valid(evidence, *, now: datetime | None = None) -> bool:
    """Evaluate already-locked evidence at the time of the protected write."""
    now = _naive(now) if now is not None else _now()
    orders, blocked_orders, periods, paid_orders, permanent = evidence
    if permanent and permanent.status == "active" and permanent.granted_at <= now:
        return True
    paid = {row.order_no for row in orders if row.status == "paid"}
    return any(period.order_no in paid and period.order_no in paid_orders
               and period.order_no not in blocked_orders
               and period.starts_at <= now and period.ends_at > now - timedelta(days=7)
               for period in periods)


def sync_reviewer_role(db: Session, user_id: int, *, now: datetime | None = None) -> bool:
    """Synchronize a paid reviewer subscription to the single global RBAC role."""
    user = db.get(User, user_id)
    if not user or user.status != 1 or user.role in {"admin", "super_admin"}:
        return False
    target_code = "reviewer" if has_reviewer_entitlement(db, user_id, now=now) else "user"
    if user.role == target_code:
        return False
    role = db.query(Role).filter_by(code=target_code, status="active").first()
    if role is None:
        raise ledger.BillingError(f"基础角色 {target_code} 未初始化，无法同步审查员权益")
    rbac_service.assign_roles_to_user(db, user_id, [role.id], commit=False)
    return True
