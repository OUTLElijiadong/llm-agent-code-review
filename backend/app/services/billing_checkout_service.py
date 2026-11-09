"""Server-priced orders and verified, idempotent payment application.

These services are not exposed for sales until channel configuration, model
pre-authorization and entitlement enforcement have passed end-to-end validation.
Caller owns the database transaction and must commit only on success.
"""

import base64
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.billing_ledger import (
    BillingCreditBatch,
    BillingOrder,
    BillingPaymentEvent,
    BillingRefund,
    BillingReservation,
    BillingReservationPart,
    BillingSubscription,
    BillingSubscriptionPeriod,
    BillingWallet,
)
from app.models.user import User
from app.services import billing_ledger_service as ledger
from app.services import enterprise_org_service as org
from app.services.billing_payment_verification import verify_alipay, verify_wechat
from app.services.billing_products import CREDIT_PACK_MILLI
from app.services.billing_refund_policy import (
    IN_FLIGHT_PROVIDER_STATUSES as _IN_FLIGHT_PROVIDER_STATUSES,
)
from app.services.billing_refund_policy import (
    pending_access_expression,
    refund_requires_reconciliation,
    requires_reconciliation_expression,
)
from app.services.billing_subscription_rules import cycle_boundary, subscription_status

_PENDING_REFUND_STATUSES = {"processing", "unknown"}
REFUND_RECONCILIATION_COOLDOWN = timedelta(seconds=15)
REFUND_RECONCILIATION_LEASE = timedelta(seconds=60)


def _pending_refund_orders(db: Session):
    return db.query(BillingRefund.order_no).filter(pending_access_expression())


def enterprise_subscription_refund_pending(db: Session, enterprise_id: int) -> bool:
    return db.query(BillingRefund.id).join(
        BillingOrder, BillingOrder.order_no == BillingRefund.order_no,
    ).filter(
        BillingOrder.enterprise_id == enterprise_id,
        BillingOrder.product.in_({"enterprise_create", "enterprise_renew"}),
        pending_access_expression(),
    ).first() is not None


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _active_credit_pack_price(db: Session, quantity: int) -> int:
    from app.services import billing_rate_catalog_service

    try:
        catalog = billing_rate_catalog_service.active_catalog(db, lock_for_use=True)
    except billing_rate_catalog_service.RateCatalogError as exc:
        raise ledger.BillingError("积分商品与费率目录未对齐，暂不能创建积分订单") from exc
    return catalog.credit_pack_price_fen * quantity


def _naive(value):
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _new_order_number() -> str:
    """128-bit random ID in the shared Alipay/WeChat merchant character set."""
    return "pr" + base64.b32encode(secrets.token_bytes(16)).decode("ascii").rstrip("=")


def create_order(db: Session, *, user_id: int, product: str, quantity: int, channel: str,
                 client_request_id: str | None = None, enterprise_id: int | None = None,
                 enterprise_name: str | None = None) -> BillingOrder:
    """Prices are selected only on the server; external sale endpoint remains disabled."""
    user = db.get(User, user_id)
    if not user or user.status != 1:
        raise ledger.BillingError("购买账号不可用")
    if channel not in {"alipay", "wechat"} or type(quantity) is not int or not 1 <= quantity <= 10000:
        raise ledger.BillingError("支付渠道或购买数量无效")
    if product == "reviewer_month":
        if quantity != 1 or enterprise_id is not None or enterprise_name is not None:
            raise ledger.BillingError("个人审查员订单参数无效")
        price = 4900
    elif product == "personal_credits":
        if enterprise_id is not None or enterprise_name is not None:
            raise ledger.BillingError("个人积分订单参数无效")
        price = _active_credit_pack_price(db, quantity)
    elif product == "enterprise_credits":
        if not enterprise_id or enterprise_name is not None:
            raise ledger.BillingError("企业积分订单参数无效")
        org.require_enterprise_admin(db, enterprise_id, user_id)
        price = _active_credit_pack_price(db, quantity)
    elif product == "enterprise_create":
        if enterprise_id is not None or not isinstance(enterprise_name, str) or not enterprise_name.strip():
            raise ledger.BillingError("企业创建订单参数无效")
        enterprise_name = enterprise_name.strip()
        if len(enterprise_name) > 120:
            raise ledger.BillingError("企业名称过长")
        price = 6900 * quantity
    elif product == "enterprise_renew":
        if not enterprise_id or enterprise_name is not None:
            raise ledger.BillingError("企业续费订单参数无效")
        org.require_enterprise_admin(db, enterprise_id, user_id)
        org._company(db, enterprise_id)
        subscription = db.query(BillingSubscription.id).filter_by(
            owner_type="enterprise", owner_id=enterprise_id, product="enterprise",
        ).first()
        if not subscription:
            raise ledger.BillingError("企业缺少订阅记录；不能创建续费订单")
        price = 6900 * quantity
    else:
        raise ledger.BillingError("商品尚未开通")
    request_fingerprint = None
    if client_request_id is not None:
        if not isinstance(client_request_id, str) or not client_request_id.strip() or len(client_request_id) > 80:
            raise ledger.BillingError("支付请求编号无效")
        request_payload = {
            "product": product,
            "quantity": quantity,
            "channel": channel,
            "enterprise_id": enterprise_id,
            "enterprise_name": enterprise_name,
        }
        request_fingerprint = hashlib.sha256(
            json.dumps(request_payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        prior = (db.query(BillingOrder).filter_by(user_id=user_id, client_request_id=client_request_id)
                 .populate_existing().with_for_update().first())
        if prior is not None:
            if prior.request_fingerprint != request_fingerprint:
                raise ledger.IdempotencyConflict("支付请求编号已用于其他商品或金额")
            return prior
    order = BillingOrder(order_no=_new_order_number(), user_id=user_id, enterprise_id=enterprise_id,
                         enterprise_name=enterprise_name, product=product, amount_fen=price,
                         quantity=quantity, channel=channel, status="created",
                         client_request_id=client_request_id, request_fingerprint=request_fingerprint)
    try:
        with db.begin_nested():
            db.add(order)
            db.flush()
    except Exception as exc:
        if client_request_id is None:
            raise
        prior = (db.query(BillingOrder).filter_by(user_id=user_id, client_request_id=client_request_id)
                 .populate_existing().with_for_update().first())
        if prior is None:
            raise
        if prior.request_fingerprint != request_fingerprint:
            raise ledger.IdempotencyConflict("支付请求编号已用于其他商品或金额") from exc
        return prior
    return order


def _order(db: Session, order_no: str, channel: str) -> BillingOrder:
    row = db.query(BillingOrder).filter_by(order_no=order_no).with_for_update().first()
    if row is None or row.channel != channel:
        raise ledger.BillingError("支付订单不存在或渠道不匹配")
    return row


def _new_period(db, *, owner_type, owner_id, product, order_no, seats, paid_at):
    # Subscription columns use second-precision DATETIME on MySQL.  Its
    # default rounding could move a fractional payment's start into the next
    # second, so the immediately following activation would miss it.  Keep
    # the original paid_at for audit and grace comparisons; normalize only
    # newly anchored cycle boundaries, equally on every database backend.
    period_anchor = paid_at.replace(microsecond=0)
    # Lock the owner's wallet before reading/subsequently creating the subscription;
    # different paid orders cannot both create overlapping periods for one owner.
    wallet = ledger.get_or_create_wallet(db, owner_type, owner_id)
    ledger._lock_wallets(db, [wallet.id])
    sub = (db.query(BillingSubscription)
           .filter_by(owner_type=owner_type, owner_id=owner_id, product=product).with_for_update().first())
    if sub is None:
        starts = period_anchor
        anchor = period_anchor
        end = _naive(cycle_boundary(anchor, 1))
        sub = BillingSubscription(owner_type=owner_type, owner_id=owner_id, product=product,
                                  anchor_at=anchor, last_end_at=end, next_offset=2)
        db.add(sub)
        db.flush()
    elif paid_at < sub.last_end_at + timedelta(days=7):
        starts = sub.last_end_at
        end = _naive(cycle_boundary(sub.anchor_at, sub.next_offset))
        sub.last_end_at = end
        sub.next_offset += 1
    else:
        starts = period_anchor
        sub.anchor_at = period_anchor
        end = _naive(cycle_boundary(period_anchor, 1))
        sub.last_end_at = end
        sub.next_offset = 2
    period = BillingSubscriptionPeriod(subscription_id=sub.id, order_no=order_no, starts_at=starts,
                                       ends_at=end, credit_milli=5_000_000 * seats, seats=seats)
    db.add(period)
    db.flush()
    return period


def activate_due_period(db: Session, period_id: int, *, now: datetime | None = None) -> bool:
    """Activate one period per transaction; never grant without a verified payment event."""
    now = _naive(now) if now is not None else _now()
    period = db.get(BillingSubscriptionPeriod, period_id)
    if period is None:
        raise ledger.BillingError("订阅周期不存在")
    sub = db.get(BillingSubscription, period.subscription_id)
    if sub is None:
        raise ledger.BillingError("订阅不存在")
    # Enterprise operations that also need a wallet always serialize on the
    # company first (quota/usage use the same order).
    if sub.owner_type == "enterprise" and sub.product == "enterprise":
        org._company(db, int(sub.owner_id), lock=True)
    wallet = ledger.get_or_create_wallet(db, sub.owner_type, sub.owner_id)
    ledger._lock_wallets(db, [wallet.id])
    period = (db.query(BillingSubscriptionPeriod).filter_by(id=period_id)
              .populate_existing().with_for_update().one())
    if period.granted_at is not None or not period.starts_at <= now < period.ends_at:
        return False
    if db.query(BillingRefund.id).filter(
        BillingRefund.order_no == period.order_no,
        BillingRefund.order_no.in_(_pending_refund_orders(db)),
    ).first():
        return False
    order = db.query(BillingOrder).filter_by(order_no=period.order_no, status="paid").first()
    event = db.query(BillingPaymentEvent).filter_by(order_no=period.order_no).first()
    if not order or not event:
        raise ledger.BillingError("订阅缺少已核验支付记录")
    ledger.grant(db, wallet_id=wallet.id, grant_key=f"period:{period.id}", kind="subscription",
                 amount_milli=period.credit_milli, expires_at=period.ends_at)
    if sub.owner_type == "enterprise" and sub.product == "enterprise":
        order = db.query(BillingOrder).filter_by(order_no=period.order_no, status="paid").one()
        company = org.apply_subscription_seats(db, enterprise_id=sub.owner_id, seat_limit=period.seats,
                                               actor_id=order.user_id)
        if company.billing_suspended:
            company.billing_suspended = False
            org._event(db, company.id, order.user_id, "billing_suspension_lifted", detail={
                "subscription_period_id": period.id, "order_no": order.order_no,
            })
    period.granted_at = now
    db.flush()
    if sub.owner_type == "user" and sub.product == "reviewer":
        from app.services.billing_entitlement_service import sync_reviewer_role

        sync_reviewer_role(db, sub.owner_id, now=now)
        db.flush()
    return True


def activate_due_periods(db: Session, *, now: datetime | None = None, limit: int = 100) -> list[int]:
    """Activate one bounded batch; caller controls commit and scheduling.

    Rows lacking verified paid events are ignored. Each period is rechecked under
    its wallet and period locks, so concurrent runners cannot duplicate grants.
    """
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ledger.BillingError("周期批处理数量无效")
    observed_at = _naive(now) if now is not None else _now()
    rows = (db.query(BillingSubscriptionPeriod.id)
            .join(BillingOrder, BillingOrder.order_no == BillingSubscriptionPeriod.order_no)
            .join(BillingPaymentEvent, BillingPaymentEvent.order_no == BillingOrder.order_no)
            .filter(BillingOrder.status == "paid", BillingSubscriptionPeriod.granted_at.is_(None),
                    BillingSubscriptionPeriod.starts_at <= observed_at,
                    BillingSubscriptionPeriod.ends_at > observed_at,
                    ~BillingOrder.order_no.in_(_pending_refund_orders(db)))
            .order_by(BillingSubscriptionPeriod.id).limit(limit).all())
    return [row.id for row in rows if activate_due_period(db, row.id, now=observed_at)]


def _apply_verified(db: Session, order: BillingOrder, verified: dict, *, now: datetime | None = None):
    paid_at = _naive(now) if now is not None else _now()
    trade_id = verified["trade_id"]
    existing = db.query(BillingPaymentEvent).filter_by(order_no=order.order_no).first()
    if existing:
        if (existing.channel, existing.trade_id, existing.amount_fen, order.status) != (
                order.channel, trade_id, order.amount_fen, order.status) or order.status not in {"paid", "refunded"}:
            raise ledger.IdempotencyConflict("支付通知与原支付记录不一致")
        return order
    if order.status not in {"created", "cancelled"}:
        raise ledger.BillingError("订单当前不能支付")
    if db.query(BillingPaymentEvent).filter_by(channel=order.channel, trade_id=trade_id).first():
        raise ledger.IdempotencyConflict("支付流水已用于其他订单")
    db.add(BillingPaymentEvent(channel=order.channel, trade_id=trade_id, order_no=order.order_no,
                               amount_fen=order.amount_fen, verified_at=paid_at))
    order.status = "paid"
    order.payment_status = "paid"
    order.paid_at = paid_at
    period = None
    if order.product in {"personal_credits", "enterprise_credits"}:
        owner_type = "user" if order.product == "personal_credits" else "enterprise"
        owner_id = order.user_id if owner_type == "user" else order.enterprise_id
        wallet = ledger.get_or_create_wallet(db, owner_type, owner_id)
        ledger.grant(db, wallet_id=wallet.id, grant_key=f"order:{order.order_no}", kind="purchase",
                     amount_milli=order.quantity * CREDIT_PACK_MILLI)
    elif order.product == "reviewer_month":
        period = _new_period(db, owner_type="user", owner_id=order.user_id, product="reviewer",
                             order_no=order.order_no, seats=1, paid_at=paid_at)
    elif order.product == "enterprise_create":
        company = org.create_enterprise_from_paid_order(db, owner_id=order.user_id,
                                                        name=order.enterprise_name, seat_limit=order.quantity,
                                                        paid_order_id=order.order_no)
        order.enterprise_id = company.id
        period = _new_period(db, owner_type="enterprise", owner_id=company.id, product="enterprise",
                             order_no=order.order_no, seats=order.quantity, paid_at=paid_at)
    elif order.product == "enterprise_renew":
        # _order already holds the order. Company precedes wallet in both the
        # scheduler and quota paths; retain that order through activation.
        company = org._company(db, order.enterprise_id, lock=True)
        period = _new_period(db, owner_type="enterprise", owner_id=company.id, product="enterprise",
                             order_no=order.order_no, seats=order.quantity, paid_at=paid_at)
    else:
        raise ledger.BillingError("订单商品尚未支持")
    db.flush()
    if period is not None:
        activate_due_period(db, period.id, now=paid_at)
    if order.product == "reviewer_month":
        from app.services.billing_entitlement_service import sync_reviewer_role

        sync_reviewer_role(db, order.user_id, now=paid_at)
    return order


def apply_alipay_query(db: Session, *, order_no: str, result: dict, now: datetime | None = None):
    """Apply only a provider-signed Alipay query result; unknown states stay non-final."""
    order = _order(db, order_no, "alipay")
    state = result.get("state")
    if result.get("order_no") != order.order_no:
        raise ledger.BillingError("支付宝查单结果订单号不匹配")
    if state in {"TRADE_SUCCESS", "TRADE_FINISHED"}:
        if result.get("amount_fen") != order.amount_fen or not result.get("trade_id"):
            raise ledger.BillingError("支付宝查单结果金额或交易号不匹配")
        return _apply_verified(db, order, {"trade_id": result["trade_id"]}, now=now)
    if order.status == "paid" or order.status == "refunded":
        return order
    if state == "NOT_FOUND":
        order.payment_status = "not_started"
        order.payment_code_url = None
        order.provider_payment_id = None
    elif state in {"WAIT_BUYER_PAY"}:
        order.payment_status = "ready"
    elif state == "TRADE_CLOSED":
        order.status = "cancelled"
        order.payment_status = "closed"
    else:
        order.payment_status = "unknown"
    db.flush()
    return order


def apply_wechat_query(db: Session, *, order_no: str, result: dict, now: datetime | None = None):
    """Apply only a provider-signed WeChat query result; unknown states stay non-final."""
    order = _order(db, order_no, "wechat")
    state = result.get("state")
    if result.get("order_no") != order.order_no:
        raise ledger.BillingError("微信查单结果订单号不匹配")
    if state == "SUCCESS":
        if (result.get("amount_fen") != order.amount_fen or result.get("appid") is None
                or result.get("mchid") is None or not result.get("trade_id")):
            raise ledger.BillingError("微信查单结果金额或身份不匹配")
        return _apply_verified(db, order, {"trade_id": result["trade_id"]}, now=now)
    if order.status == "paid" or order.status == "refunded":
        return order
    if state == "NOT_FOUND":
        order.payment_status = "not_started"
        order.payment_code_url = None
        order.provider_payment_id = None
    elif state == "CLOSED":
        order.status = "cancelled"
        order.payment_status = "closed"
    elif state == "NOTPAY":
        order.payment_status = "ready"
    else:
        order.payment_status = "unknown"
    db.flush()
    return order


def mark_order_closed(db: Session, *, order_no: str, now: datetime | None = None):
    order = db.query(BillingOrder).filter_by(order_no=order_no).with_for_update().first()
    if order is None:
        raise ledger.BillingError("支付订单不存在")
    if order.status in {"paid", "refunded"}:
        return order
    if order.status not in {"created", "cancelled"}:
        raise ledger.BillingError("订单当前不能关闭")
    order.status = "cancelled"
    order.payment_status = "closed"
    order.payment_code_url = None
    db.flush()
    return order


def _credit_refund_batch(db: Session, order: BillingOrder, *, lock: bool = False):
    if order.product not in {"personal_credits", "enterprise_credits"}:
        return None
    wallet = None
    if lock:
        owner_type = "user" if order.product == "personal_credits" else "enterprise"
        owner_id = order.user_id if owner_type == "user" else order.enterprise_id
        if owner_id is None:
            raise ledger.BillingError("退款订单缺少钱包所有者")
        wallet = _refund_wallet(db, owner_type, int(owner_id), lock=True)
    query = db.query(BillingCreditBatch).filter_by(grant_key=f"order:{order.order_no}")
    batch = query.populate_existing().with_for_update().first() if lock else query.first()
    if batch is not None and wallet is not None and batch.wallet_id != wallet.id:
        raise ledger.BillingError("退款积分批次与订单钱包不匹配")
    return batch


def _refund_wallet(db: Session, owner_type: str, owner_id: int, *, lock: bool):
    query = db.query(BillingWallet).filter_by(owner_type=owner_type, owner_id=owner_id)
    wallet = query.populate_existing().with_for_update().first() if lock else query.first()
    if wallet is None:
        raise ledger.BillingError("退款订单对应的钱包不存在，禁止处理")
    if lock:
        # All reservation/settlement paths serialize on this wallet first.
        # If this transaction's RR snapshot predates a committed wallet change,
        # its ordinary reservation-part reads cannot safely justify a refund.
        snapshot = ledger._wallet_snapshot_version(db, int(wallet.id))
        current = ledger._lock_wallets(db, [int(wallet.id)])[int(wallet.id)]
        ledger._require_wallet_snapshot(snapshot, current)
        wallet = current
    return wallet


def _refund_eligible(db: Session, order: BillingOrder, refund: BillingRefund | None = None):
    if order.status != "paid" or order.payment_status != "paid":
        return None, "原支付订单尚未处于可退款状态"
    batch = _credit_refund_batch(db, order, lock=True)
    if batch is None:
        return None, "订阅或企业席位退款需要人工复核"
    locked = refund is not None and refund.status in {"requested", "processing", "unknown"}
    expected_lock = batch.issued_milli if locked else 0
    if (batch.kind != "purchase" or batch.issued_milli != order.quantity * CREDIT_PACK_MILLI
            or batch.remaining_milli != batch.issued_milli or batch.reserved_milli != 0
            or batch.refund_reserved_milli != expected_lock):
        return None, "积分已消费、预占或正在退款，不能自动退款"
    _assert_refund_batch_consistent(db, batch, expected_refund_lock=expected_lock)
    return batch, None


def _assert_refund_batch_consistent(db: Session, batch: BillingCreditBatch, *, expected_refund_lock: int) -> None:
    numeric = (batch.issued_milli, batch.remaining_milli, batch.reserved_milli, batch.refund_reserved_milli)
    if any(type(value) is not int or value < 0 for value in numeric):
        raise ledger.BillingError("积分退款账本字段无效，禁止处理")
    if (batch.remaining_milli > batch.issued_milli or batch.reserved_milli > batch.remaining_milli
            or batch.refund_reserved_milli > batch.remaining_milli - batch.reserved_milli):
        raise ledger.BillingError("积分退款账本余额不一致，禁止处理")
    parts = (db.query(BillingReservationPart, BillingReservation)
             .join(BillingReservation, BillingReservation.id == BillingReservationPart.reservation_id)
             .filter(BillingReservationPart.batch_id == batch.id).all())
    reservation_ids = {reservation.id for _, reservation in parts}
    all_parts = (db.query(BillingReservationPart, BillingReservation)
                 .join(BillingReservation, BillingReservation.id == BillingReservationPart.reservation_id)
                 .filter(BillingReservation.id.in_(reservation_ids)).all()) if reservation_ids else []
    reserved_by_request: dict[int, int] = {}
    charged_by_request: dict[int, int] = {}
    active_reserved = 0
    settled_charged = 0
    for part, reservation in all_parts:
        if type(part.reserved_milli) is not int or part.reserved_milli <= 0:
            raise ledger.BillingError("预占明细金额无效，禁止退款")
        if reservation.status not in {"reserved", "unknown", "settled"}:
            raise ledger.BillingError("预占状态无法核验，禁止退款")
        if reservation.status in {"reserved", "unknown"}:
            if part.charged_milli is not None or reservation.actual_milli is not None:
                raise ledger.BillingError("未结算预占含有结算金额，禁止退款")
            reserved_by_request[reservation.id] = reserved_by_request.get(reservation.id, 0) + part.reserved_milli
            if part.batch_id == batch.id:
                active_reserved += part.reserved_milli
        if reservation.status == "settled":
            if (type(reservation.actual_milli) is not int or reservation.actual_milli < 0
                    or part.charged_milli is None or type(part.charged_milli) is not int
                    or part.charged_milli < 0 or part.charged_milli > part.reserved_milli):
                raise ledger.BillingError("结算明细不完整，禁止退款")
            charged_by_request[reservation.id] = charged_by_request.get(reservation.id, 0) + part.charged_milli
            if part.batch_id == batch.id:
                settled_charged += part.charged_milli
    for _, reservation in all_parts:
        if (reservation.status == "reserved"
                and reserved_by_request.get(reservation.id, 0) != reservation.requested_milli):
            raise ledger.BillingError("预占明细与请求额度不一致，禁止退款")
        if (reservation.status == "unknown"
                and reserved_by_request.get(reservation.id, 0) != reservation.requested_milli):
            raise ledger.BillingError("未知用量预占与请求额度不一致，禁止退款")
        if (reservation.status == "settled"
                and charged_by_request.get(reservation.id, 0) != reservation.actual_milli):
            raise ledger.BillingError("结算明细与实际扣费不一致，禁止退款")
    if batch.reserved_milli != active_reserved or batch.issued_milli != batch.remaining_milli + settled_charged:
        raise ledger.BillingError("积分退款账本不一致：预占与结算明细不匹配，禁止处理")
    if active_reserved or batch.reserved_milli:
        raise ledger.BillingError("存在未结算预占，暂不能审批退款")
    if batch.refund_reserved_milli != expected_refund_lock:
        raise ledger.BillingError("积分退款冻结额与剩余额度不一致，禁止处理")


def _manual_refund_context(db: Session, order: BillingOrder, *, expected_refund_lock: int | None,
                           lock: bool = True) -> tuple[BillingCreditBatch | None, BillingSubscriptionPeriod | None]:
    """Lock and validate only the exact grant created by the refunded order."""
    if order.product in {"personal_credits", "enterprise_credits"}:
        owner_type = "user" if order.product == "personal_credits" else "enterprise"
        owner_id = int(order.user_id if owner_type == "user" else order.enterprise_id or 0)
        if owner_id <= 0:
            raise ledger.BillingError("退款订单缺少钱包所有者，禁止处理")
        wallet = _refund_wallet(db, owner_type, owner_id, lock=lock)
        batch_query = db.query(BillingCreditBatch).filter_by(grant_key=f"order:{order.order_no}")
        batch = batch_query.populate_existing().with_for_update().first() if lock else batch_query.first()
        if batch is None or batch.wallet_id != wallet.id:
            raise ledger.BillingError("退款订单的积分入账批次缺失或钱包不匹配，禁止处理")
        expected_issued = int(order.quantity) * CREDIT_PACK_MILLI
        if batch.kind != "purchase" or batch.issued_milli != expected_issued:
            raise ledger.BillingError("退款订单积分批次与原订单不一致，禁止处理")
        refund_lock = batch.refund_reserved_milli if expected_refund_lock is None else expected_refund_lock
        _assert_refund_batch_consistent(db, batch, expected_refund_lock=refund_lock)
        return batch, None

    if order.product not in {"reviewer_month", "enterprise_create", "enterprise_renew"}:
        raise ledger.BillingError("该订单商品不支持退款审核")
    period_query = db.query(BillingSubscriptionPeriod).filter_by(order_no=order.order_no)
    period = period_query.first()
    if period is None:
        raise ledger.BillingError("退款订阅周期记录缺失，禁止处理")
    sub = db.get(BillingSubscription, period.subscription_id)
    expected_owner_type = "user" if order.product == "reviewer_month" else "enterprise"
    expected_owner_id = int(order.user_id if expected_owner_type == "user" else order.enterprise_id or 0)
    expected_product = "reviewer" if expected_owner_type == "user" else "enterprise"
    if (not sub or sub.owner_type != expected_owner_type or sub.owner_id != expected_owner_id
            or sub.product != expected_product or period.credit_milli != 5_000_000 * period.seats
            or period.seats != order.quantity
            or period.ends_at <= period.starts_at):
        raise ledger.BillingError("退款订阅周期与商品归属不一致，禁止处理")
    wallet = _refund_wallet(db, expected_owner_type, expected_owner_id, lock=lock)
    if lock:
        period = (db.query(BillingSubscriptionPeriod).filter_by(id=period.id)
                  .populate_existing().with_for_update().one())
    batch_query = db.query(BillingCreditBatch).filter_by(grant_key=f"period:{period.id}")
    batch = batch_query.populate_existing().with_for_update().first() if lock else batch_query.first()
    if period.granted_at is None:
        if batch is not None:
            raise ledger.BillingError("未发放的订阅周期却存在积分批次，禁止处理")
        return None, period
    if batch is None or batch.wallet_id != wallet.id or batch.kind != "subscription" \
            or batch.issued_milli != period.credit_milli or _naive(batch.expires_at) != _naive(period.ends_at):
        raise ledger.BillingError("订阅积分批次与周期记录不一致，禁止处理")
    refund_lock = batch.refund_reserved_milli if expected_refund_lock is None else expected_refund_lock
    _assert_refund_batch_consistent(db, batch, expected_refund_lock=refund_lock)
    return batch, period


def _lock_refund_and_order(db: Session, refund_no: str) -> tuple[BillingRefund, BillingOrder]:
    """Lock payment state in the same order used by refund requests: order, then refund.

    The first read only discovers the immutable order number. Both records are then
    current locking reads, so REPEATABLE READ snapshots cannot return stale status
    and concurrent refund operations cannot invert the order/refund lock sequence.
    """
    candidate_order_no = db.query(BillingRefund.order_no).filter_by(refund_no=refund_no).scalar()
    if not candidate_order_no:
        raise ledger.BillingError("退款单不存在")
    order = db.query(BillingOrder).filter_by(order_no=candidate_order_no).populate_existing().with_for_update().first()
    if order is None:
        raise ledger.BillingError("退款单对应的支付订单不存在")
    refund = db.query(BillingRefund).filter_by(refund_no=refund_no).populate_existing().with_for_update().first()
    if refund is None or refund.order_no != order.order_no:
        raise ledger.BillingError("退款单与支付订单关系已变化")
    return refund, order


def claim_refund_reconciliation(db: Session, *, refund_no: str, now: datetime | None = None) -> str | None:
    """Atomically claim one provider query until its lease expires; return its fencing token."""
    claim_time, cooldown_cutoff, lease_until = _refund_reconciliation_claim_times(db, now)
    claim_token = secrets.token_hex(16)
    eligible = (
        BillingRefund.status.in_(_PENDING_REFUND_STATUSES)
        | requires_reconciliation_expression()
        | ((BillingRefund.status == "manual_review")
           & BillingRefund.provider_status.in_(_IN_FLIGHT_PROVIDER_STATUSES))
    )
    claimed = (db.query(BillingRefund).filter(
        BillingRefund.refund_no == refund_no,
        eligible,
        BillingRefund.updated_at <= cooldown_cutoff,
        (BillingRefund.reconciliation_claim_token.is_(None)
         | (BillingRefund.reconciliation_claimed_until <= claim_time)),
    ).update(
        {
            BillingRefund.updated_at: claim_time,
            BillingRefund.reconciliation_claim_token: claim_token,
            BillingRefund.reconciliation_claimed_until: lease_until,
        },
        synchronize_session=False,
    ))
    return claim_token if claimed == 1 else None


def release_refund_reconciliation(db: Session, *, refund_no: str, claim_token: str,
                                  now: datetime | None = None) -> bool:
    """Release a claim only while its fencing token still owns the refund row."""
    observed_at = _refund_reconciliation_clock(db, now)
    released = (db.query(BillingRefund).filter(
        BillingRefund.refund_no == refund_no,
        BillingRefund.reconciliation_claim_token == claim_token,
    ).update(
        {
            BillingRefund.updated_at: observed_at,
            BillingRefund.reconciliation_claim_token: None,
            BillingRefund.reconciliation_claimed_until: None,
        },
        synchronize_session=False,
    ))
    return released == 1


def _refund_reconciliation_clock(db: Session, now: datetime | None) -> datetime:
    if now is not None:
        return _naive(now)
    if db.get_bind().dialect.name == "mysql":
        database_now = db.query(func.utc_timestamp()).scalar()
        if isinstance(database_now, datetime):
            return _naive(database_now)
    return _now()


def _refund_reconciliation_claim_times(db: Session, now: datetime | None):
    if now is None and db.get_bind().dialect.name == "mysql":
        database_now = func.utc_timestamp()
        return (
            database_now,
            func.timestampadd(text("SECOND"), -15, database_now),
            func.timestampadd(text("SECOND"), 60, database_now),
        )
    observed_at = _refund_reconciliation_clock(db, now)
    return (
        observed_at,
        observed_at - REFUND_RECONCILIATION_COOLDOWN,
        observed_at + REFUND_RECONCILIATION_LEASE,
    )


def review_manual_refund(db: Session, *, refund_no: str, admin_id: int, decision: str, reason: str,
                         now: datetime | None = None) -> BillingRefund:
    """Platform-admin review for refunds which cannot use the automatic full-unused-credit path."""
    if decision not in {"approve", "reject"}:
        raise ledger.BillingError("退款审核决定仅支持 approve 或 reject")
    reason = reason.strip() if isinstance(reason, str) else ""
    if not reason or len(reason) > 500:
        raise ledger.BillingError("退款审核原因必须为 1 至 500 个字符")
    refund, order = _lock_refund_and_order(db, refund_no)
    if refund_requires_reconciliation(refund):
        raise ledger.BillingError("退款状态证据不完整，必须先查单核对")
    legacy_approved = (
        refund.status == "approved" and refund.reviewed_by is not None
        and refund.provider_status is None and refund.attempt_count == 0
        and db.query(User.id).filter_by(id=refund.reviewed_by, username="admin", role="super_admin").first() is not None
    )
    if refund.status != "manual_review" and not legacy_approved:
        raise ledger.BillingError("只能审核待人工复核的退款单")
    if refund.provider_status in _IN_FLIGHT_PROVIDER_STATUSES:
        raise ledger.BillingError("渠道退款结果尚未确定，必须先查单")
    if refund.provider_status == "SUCCESS":
        raise ledger.BillingError("渠道已确认退款成功，必须重新查单并完成本地账本核验，不能再次审批")
    if (order is None or order.status != "paid" or order.payment_status != "paid"
            or refund.amount_fen != order.amount_fen
            or db.query(BillingPaymentEvent).filter_by(order_no=refund.order_no).first() is None):
        raise ledger.BillingError("退款订单未通过到账/全额核验，禁止审核")
    observed_at = _refund_reconciliation_clock(db, now)
    if type(admin_id) is not int or admin_id <= 0:
        raise ledger.BillingError("退款审核人无效")
    if refund.requested_by == admin_id:
        raise ledger.BillingError("退款申请人不能审核本人申请")
    if legacy_approved:
        db.add(AuditLog(
            actor_id=refund.reviewed_by, action="billing.refund.legacy_review", target_type="billing_refund",
            target_id=refund.refund_no,
            detail=json.dumps({"decision": "approve", "note": refund.review_note,
                               "recorded_at": refund.updated_at.isoformat() if refund.updated_at else None},
                              ensure_ascii=False),
            status="success", create_time=observed_at,
        ))
    refund.reviewed_by = admin_id
    refund.updated_at = observed_at
    db.add(AuditLog(actor_id=admin_id, action="billing.refund.review", target_type="billing_refund",
                    target_id=refund.refund_no, detail=json.dumps({"decision": decision, "note": reason},
                                                                  ensure_ascii=False),
                    status="success", create_time=observed_at))
    if decision == "reject":
        batch, _period = _manual_refund_context(db, order, expected_refund_lock=None if legacy_approved else 0)
        if legacy_approved and batch is not None and batch.refund_reserved_milli != batch.remaining_milli:
            raise ledger.BillingError("人工退款冻结额与未用积分不一致，禁止复核")
        if batch is not None and batch.refund_reserved_milli:
            batch.refund_reserved_milli = 0
        refund.status = "rejected"
        refund.review_note = reason
        db.flush()
        _sync_refunded_reviewer_role(db, order, now=observed_at)
        db.flush()
        return refund

    batch, _period = _manual_refund_context(db, order, expected_refund_lock=None if legacy_approved else 0)
    if batch is not None:
        if batch.remaining_milli != batch.issued_milli:
            raise ledger.BillingError("周期积分已有已结算用量，不能批准全额退款")
        if legacy_approved and batch.refund_reserved_milli != batch.remaining_milli:
            raise ledger.BillingError("人工退款冻结额与未用积分不一致，禁止复核")
        batch.refund_reserved_milli = batch.remaining_milli
    refund.status = "approved"
    refund.review_note = reason
    db.flush()
    _sync_refunded_reviewer_role(db, order, now=observed_at)
    db.flush()
    return refund


def request_refund(db: Session, *, order_no: str, requester_id: int, reason: str,
                   enterprise_admin_authorized: bool = False) -> BillingRefund:
    reason = reason.strip() if isinstance(reason, str) else ""
    if not reason or len(reason) > 500:
        raise ledger.BillingError("退款原因必须为 1 至 500 个字符")
    order = db.query(BillingOrder).filter_by(order_no=order_no).populate_existing().with_for_update().first()
    if (order is None or (order.user_id != requester_id and
                          not (enterprise_admin_authorized and order.enterprise_id is not None))):
        raise ledger.BillingError("支付订单不存在")
    if (order.status != "paid" or order.payment_status != "paid"
            or db.query(BillingPaymentEvent).filter_by(order_no=order_no).first() is None):
        raise ledger.BillingError("只有已核验到账的订单可以申请退款")
    existing = db.query(BillingRefund).filter_by(order_no=order_no).populate_existing().with_for_update().first()
    if existing is not None:
        if existing.reason != reason:
            raise ledger.IdempotencyConflict("该订单已有退款申请")
        return existing
    observed_at = _refund_reconciliation_clock(db, None)
    batch, note = _refund_eligible(db, order)
    if batch is not None:
        batch.refund_reserved_milli = batch.remaining_milli
        status = "requested"
    else:
        status = "manual_review"
    refund = BillingRefund(
        order_no=order.order_no, refund_no=f"rf{secrets.token_hex(16)}", amount_fen=order.amount_fen,
        reason=reason, status=status, requested_by=requester_id, review_note=note,
        created_at=observed_at, updated_at=observed_at,
    )
    db.add(refund)
    db.flush()
    return refund


def prepare_refund(db: Session, *, refund_no: str, admin_id: int) -> tuple[BillingRefund, BillingOrder]:
    refund, order = _lock_refund_and_order(db, refund_no)
    observed_at = _refund_reconciliation_clock(db, None)
    if refund.status not in {"requested", "failed", "approved"}:
        raise ledger.BillingError("退款单当前不能发起渠道退款；未知状态必须先查单")
    if refund_requires_reconciliation(refund):
        raise ledger.BillingError("退款查单结果未知，必须先核对渠道状态")
    _validate_refund_order(db, refund, order)
    if refund.status == "approved":
        if refund.reviewed_by is None:
            raise ledger.BillingError("人工退款缺少审核记录，禁止发起")
        if refund.reviewed_by == admin_id:
            raise ledger.BillingError("退款审核人不能执行本人审核的退款")
        batch, _period = _manual_refund_context(db, order, expected_refund_lock=None)
        if batch is not None and batch.refund_reserved_milli != batch.remaining_milli:
            raise ledger.BillingError("人工退款冻结额与未用积分不一致，禁止发起")
        db.add(AuditLog(actor_id=admin_id, action="billing.refund.execute", target_type="billing_refund",
                        target_id=refund.refund_no,
                        detail=json.dumps({"reviewed_by": refund.reviewed_by, "amount_fen": refund.amount_fen}),
                        status="success", create_time=observed_at))
    else:
        if refund.reviewed_by is not None:
            raise ledger.BillingError("普通退款单存在人工审核记录，禁止按自动流程处理")
        batch, note = _refund_eligible(db, order, refund)
        if batch is None:
            raise ledger.BillingError(note or "该退款单不能自动处理")
        batch.refund_reserved_milli = batch.remaining_milli
        refund.review_note = "仅退回全部未消费的积分购买单"
    refund.status = "processing"
    refund.attempt_count += 1
    refund.reconciliation_claim_token = None
    refund.reconciliation_claimed_until = None
    refund.reconciliation_query_attempt = 0
    refund.refund_creation_attempt = 0
    refund.updated_at = observed_at
    db.flush()
    _sync_refunded_reviewer_role(db, order, now=observed_at)
    db.flush()
    return refund, order


def update_refund_result(db: Session, *, refund_no: str, result: dict, now: datetime | None = None,
                         verified_provider_query: bool = False,
                         reconciliation_token: str | None = None, attempt_no: int | None = None,
                         create_response_verified: bool = False):
    refund, order = _lock_refund_and_order(db, refund_no)
    if order.product in {"enterprise_create", "enterprise_renew"} and order.enterprise_id is not None:
        # A confirmed enterprise refund can change the company's seats/status.
        # Lock it before the wallet, matching activation and quota changes.
        org._company(db, int(order.enterprise_id), lock=True)
    observed_at = _refund_reconciliation_clock(db, now)
    if create_response_verified and (verified_provider_query or attempt_no is None):
        raise ledger.BillingError("退款创建响应缺少有效代次")
    if (attempt_no is None and not verified_provider_query
            and (refund.attempt_count > 1 or refund.reconciliation_query_attempt != 0)):
        raise ledger.BillingError("退款请求代次缺失，拒绝应用渠道结果")
    if attempt_no is not None:
        if type(attempt_no) is not int or attempt_no < 1 or attempt_no > refund.attempt_count:
            raise ledger.BillingError("退款请求代次无效")
        if attempt_no < refund.attempt_count and refund.status != "succeeded":
            if isinstance(result, dict) and result.get("state") == "SUCCESS":
                refund.status = "manual_review"
                refund.provider_status = "SUCCESS"
                refund.review_note = "旧退款请求返回成功，须重新查单核验"
                refund.reconciliation_claim_token = None
                refund.reconciliation_claimed_until = None
                refund.updated_at = observed_at
                db.flush()
                _sync_refunded_reviewer_role(db, order, now=observed_at)
            return refund
        if refund.reconciliation_query_attempt in (None, attempt_no):
            # A signed response proves this particular request returned. Keep
            # the newer query result, but allow a subsequent signed query to
            # decide whether a failed refund's hold can be released.
            if create_response_verified:
                refund.refund_creation_attempt = attempt_no
            state = result.get("state") if isinstance(result, dict) else None
            db.add(AuditLog(
                actor_id=None, action="billing.refund.late_create_conflict", target_type="billing_refund",
                target_id=refund.refund_no,
                detail=json.dumps({"attempt": attempt_no, "query_status": refund.provider_status,
                                   "create_status": state}, ensure_ascii=False),
                status="success", create_time=observed_at,
            ))
            if state == "SUCCESS" and (refund.provider_status == "FAILED"
                                       or refund.status in {"approved", "failed", "rejected"}):
                refund.status = "manual_review"
                refund.provider_status = "UNKNOWN"
                refund.review_note = "迟到退款响应与已记录结果冲突，须重新查单核验"
                refund.updated_at = observed_at
                _sync_refunded_reviewer_role(db, order, now=observed_at)
            db.flush()
            return refund
    if verified_provider_query and not reconciliation_token:
        raise ledger.BillingError("退款查单结果缺少有效认领令牌")
    if reconciliation_token is not None:
        if refund.reconciliation_claim_token != reconciliation_token:
            raise ledger.BillingError("退款查单认领已过期，拒绝应用旧渠道结果")
        if (refund.reconciliation_claimed_until is None
                or _naive(refund.reconciliation_claimed_until) <= observed_at):
            raise ledger.BillingError("退款查单认领已过期，拒绝应用渠道结果")
        refund.reconciliation_claim_token = None
        refund.reconciliation_claimed_until = None
    elif refund.reconciliation_claim_token is not None:
        # A late refund-create response fences any in-flight status query.
        refund.reconciliation_claim_token = None
        refund.reconciliation_claimed_until = None
    if refund.status == "succeeded":
        return refund
    if refund.status == "rejected":
        raise ledger.BillingError("已拒绝的退款单不能再变更")
    recovery_after_channel_success = refund.status == "manual_review" and refund.provider_status == "SUCCESS"
    recovery_after_in_flight_result = (
        refund.status == "manual_review" and refund.provider_status in {"PROCESSING", "UNKNOWN", "NOT_FOUND"}
        and (verified_provider_query or (refund.provider_status == "NOT_FOUND" and attempt_no == refund.attempt_count))
    )
    recovery_after_not_found = refund_requires_reconciliation(refund)
    if (refund.status not in {"processing", "unknown", "failed"}
            and not recovery_after_channel_success and not recovery_after_in_flight_result
            and not recovery_after_not_found):
        raise ledger.BillingError("退款单尚未进入渠道处理状态")
    state = result.get("state") if isinstance(result, dict) else None
    if recovery_after_channel_success and state != "SUCCESS":
        raise ledger.BillingError("渠道已确认退款成功，恢复时必须再次核验为 SUCCESS")
    if recovery_after_channel_success and not verified_provider_query:
        raise ledger.BillingError("渠道退款成功待复核，必须通过已验签查单确认")
    _validate_refund_order(db, refund, order)
    if state not in {"SUCCESS", "FAILED", "NOT_FOUND", "PROCESSING", "UNKNOWN"}:
        state = "UNKNOWN"
    if verified_provider_query:
        refund.reconciliation_query_attempt = refund.attempt_count
    elif create_response_verified:
        refund.refund_creation_attempt = refund.attempt_count
    if not verified_provider_query:
        # A create response is not the independently verified final refund result.
        if state == "SUCCESS":
            state = "PROCESSING"
        elif state in {"FAILED", "NOT_FOUND"}:
            state = "UNKNOWN"
    refund.provider_refund_id = result.get("provider_refund_id") or refund.provider_refund_id
    refund.provider_status = state
    refund.updated_at = observed_at
    if state == "SUCCESS":
        try:
            expected_lock = 0 if refund.status == "failed" else None
            batch, period = _manual_refund_context(db, order, expected_refund_lock=expected_lock)
            if refund.status == "failed" and batch is not None:
                if batch.remaining_milli != batch.issued_milli or batch.refund_reserved_milli != 0:
                    raise ledger.BillingError("失败后积分已变化，不能自动确认退款权益")
                batch.refund_reserved_milli = batch.remaining_milli
            elif batch is not None and batch.refund_reserved_milli != batch.remaining_milli:
                raise ledger.BillingError("渠道退款期间冻结额发生变化，不能自动确认退款权益")
        except ledger.RetryableWalletSnapshot:
            raise
        except ledger.BillingError as exc:
            refund.status = "manual_review"
            refund.review_note = f"渠道已确认退款成功，本地权益处理未完成：{exc}"[:500]
            db.flush()
            return refund
        if batch is not None:
            batch.remaining_milli = 0
            batch.refund_reserved_milli = 0
        refund.status = "succeeded"
        refund.completed_at = observed_at
        order.status = "refunded"
        order.payment_status = "refunded"
        if order.product in {"enterprise_create", "enterprise_renew"}:
            _reconcile_enterprise_after_refund(db, order, period, now=observed_at)
    elif state == "FAILED" and refund.refund_creation_attempt != refund.attempt_count:
        # The original create request may still be in flight. A query can race
        # it, so a failure cannot release balances until that request returns.
        refund.status = "unknown"
    elif state == "FAILED":
        if refund.reviewed_by is not None:
            try:
                batch, _period = _manual_refund_context(db, order, expected_refund_lock=None)
                if batch is not None and batch.refund_reserved_milli != batch.remaining_milli:
                    raise ledger.BillingError("人工退款冻结额发生变化")
                refund.status = "approved"
            except ledger.RetryableWalletSnapshot:
                raise
            except ledger.BillingError:
                refund.status = "manual_review"
        else:
            try:
                batch, _period = _manual_refund_context(db, order, expected_refund_lock=None)
                if batch is not None:
                    if batch.refund_reserved_milli != batch.remaining_milli:
                        raise ledger.BillingError("退款冻结额发生变化")
                    batch.refund_reserved_milli = 0
                refund.status = "failed"
            except ledger.RetryableWalletSnapshot:
                raise
            except ledger.BillingError:
                refund.status = "manual_review"
    else:
        try:
            batch, _period = _manual_refund_context(db, order, expected_refund_lock=None)
            if batch is not None and batch.refund_reserved_milli != batch.remaining_milli:
                raise ledger.BillingError("未知退款结果的冻结额发生变化")
            refund.status = "processing" if state == "PROCESSING" else "unknown"
        except ledger.RetryableWalletSnapshot:
            raise
        except ledger.BillingError:
            refund.status = "manual_review"
    db.flush()
    _sync_refunded_reviewer_role(db, order, now=observed_at)
    db.flush()
    return refund


def _sync_refunded_reviewer_role(db: Session, order: BillingOrder, *, now: datetime | None = None) -> None:
    if order.product == "reviewer_month":
        from app.services.billing_entitlement_service import sync_reviewer_role

        sync_reviewer_role(db, order.user_id, now=now)


def _validate_refund_order(db: Session, refund: BillingRefund, order: BillingOrder) -> None:
    event = db.query(BillingPaymentEvent).filter_by(order_no=order.order_no).first()
    if (order.status != "paid" or order.payment_status != "paid" or event is None
            or event.channel != order.channel or event.amount_fen != order.amount_fen
            or type(order.amount_fen) is not int or order.amount_fen <= 0
            or refund.amount_fen != order.amount_fen):
        raise ledger.BillingError("退款订单到账记录或全额金额核验失败，禁止处理")


def _reconcile_enterprise_after_refund(db: Session, order: BillingOrder,
                                       period: BillingSubscriptionPeriod | None, *, now: datetime) -> None:
    if order.enterprise_id is None or period is None:
        raise ledger.BillingError("企业退款缺少企业或订阅周期，权益未能撤销")
    company = org._company(db, int(order.enterprise_id), lock=True)
    previous = enterprise_subscription_period(db, int(company.id), now=now)
    previous_status = subscription_status(previous.ends_at, now) if previous else "none"
    if previous is not None and previous_status in {"active", "grace"}:
        org.apply_subscription_seats(db, enterprise_id=int(company.id), seat_limit=previous.seats,
                                     actor_id=order.user_id)
        if company.billing_suspended:
            company.billing_suspended = False
            org._event(db, company.id, order.user_id, "billing_suspension_lifted", detail={
                "refunded_order_no": order.order_no,
                "restored_period_id": previous.id,
            })
        return
    if not company.billing_suspended:
        company.billing_suspended = True
        org._event(db, company.id, order.user_id, "billing_suspended_after_refund", detail={
            "refunded_order_no": order.order_no,
            "refunded_period_id": period.id,
            "previous_period_id": previous.id if previous else None,
        })
    db.flush()


def enterprise_subscription_period(db: Session, enterprise_id: int, *, now: datetime | None = None):
    """Latest started and verified enterprise period, excluding future or unpaid orders."""
    observed_at = _naive(now) if now is not None else _now()
    return (db.query(BillingSubscriptionPeriod)
              .join(BillingSubscription, BillingSubscription.id == BillingSubscriptionPeriod.subscription_id)
              .join(BillingOrder, BillingOrder.order_no == BillingSubscriptionPeriod.order_no)
              .join(BillingPaymentEvent, BillingPaymentEvent.order_no == BillingOrder.order_no)
              .filter(BillingSubscription.owner_type == "enterprise", BillingSubscription.owner_id == enterprise_id,
                      BillingSubscription.product == "enterprise", BillingOrder.status == "paid",
                      BillingSubscriptionPeriod.starts_at <= observed_at,
                      ~BillingOrder.order_no.in_(_pending_refund_orders(db)))
              .order_by(BillingSubscriptionPeriod.ends_at.desc(), BillingSubscriptionPeriod.id.desc()).first())


def enterprise_subscription_status(db: Session, enterprise_id: int, *, now: datetime | None = None) -> str:
    """Status of the latest started, verified period; never trust a future or unpaid period."""
    observed_at = _naive(now) if now is not None else _now()
    period = enterprise_subscription_period(db, enterprise_id, now=observed_at)
    return subscription_status(period.ends_at, observed_at) if period else "none"


def enterprise_subscription_info(db: Session, enterprise_id: int, *, now: datetime | None = None) -> dict:
    observed_at = _naive(now) if now is not None else _now()
    period = enterprise_subscription_period(db, enterprise_id, now=observed_at)
    return {
        "status": subscription_status(period.ends_at, observed_at) if period else "none",
        "ends_at": period.ends_at if period else None,
        "seats": period.seats if period else None,
    }


def apply_alipay(db: Session, *, order_no: str, params, public_key_pem: bytes,
                 expected_appid: str, expected_seller: str, now: datetime | None = None):
    order = _order(db, order_no, "alipay")
    verified = verify_alipay(params=params, public_key_pem=public_key_pem, expected_appid=expected_appid,
                             expected_seller=expected_seller, expected_order_no=order.order_no,
                             expected_amount_fen=order.amount_fen)
    return _apply_verified(db, order, verified, now=now)


def apply_wechat(db: Session, *, order_no: str, headers, body: bytes, public_key_pem: bytes, api_v3_key: bytes,
                 expected_serial: str, expected_appid: str, expected_mchid: str, now: datetime | None = None):
    order = _order(db, order_no, "wechat")
    verified = verify_wechat(headers=headers, body=body, public_key_pem=public_key_pem, api_v3_key=api_v3_key,
                            expected_serial=expected_serial, expected_appid=expected_appid,
                            expected_mchid=expected_mchid, expected_order_no=order.order_no,
                            expected_amount_fen=order.amount_fen)
    return _apply_verified(db, order, verified, now=now)
