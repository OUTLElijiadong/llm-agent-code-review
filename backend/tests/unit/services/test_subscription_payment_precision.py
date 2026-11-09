"""Cycle boundaries use portable second precision without rewriting payment evidence."""

from datetime import datetime, timedelta, timezone

import pytest

from app.models.billing_ledger import BillingPaymentEvent, BillingSubscriptionPeriod
from app.models.rbac import Role
from app.models.user import User
from app.services import billing_checkout_service as checkout
from app.services import billing_entitlement_service as entitlement
from app.services import billing_ledger_service as ledger
from app.services.billing_subscription_rules import cycle_boundary


@pytest.mark.parametrize("product", ["reviewer_month", "enterprise_create"])
@pytest.mark.parametrize("zone", [timezone.utc, timezone(timedelta(hours=8))])
def test_fractional_payment_grants_immediately_and_keeps_exact_payment_evidence(db, product, zone):
    buyer = User(username=f"payment-precision-{product}", password="x", role="user", status=1)
    db.add_all([buyer, Role(name="普通用户", code="user", status="active", sort=10, is_builtin=1),
                Role(name="审查员", code="reviewer", status="active", sort=20, is_builtin=1)])
    db.flush()
    paid_at = datetime(2026, 1, 31, 23, 59, 59, 900000, tzinfo=zone)
    utc_paid_at = paid_at.astimezone(timezone.utc).replace(tzinfo=None)
    kwargs = {"enterprise_name": "时间精度企业"} if product == "enterprise_create" else {}
    order = checkout.create_order(db, user_id=buyer.id, product=product, quantity=1, channel="alipay", **kwargs)
    checkout._apply_verified(db, order, {"trade_id": f"time-{product}"}, now=paid_at)
    period = db.query(BillingSubscriptionPeriod).filter_by(order_no=order.order_no).one()
    event = db.query(BillingPaymentEvent).filter_by(order_no=order.order_no).one()
    assert period.starts_at == utc_paid_at.replace(microsecond=0)
    assert period.ends_at == cycle_boundary(period.starts_at, 1).replace(tzinfo=None)
    assert period.granted_at == utc_paid_at
    assert order.paid_at == event.verified_at == utc_paid_at
    owner_type = "enterprise" if product == "enterprise_create" else "user"
    owner_id = order.enterprise_id if owner_type == "enterprise" else buyer.id
    wallet = ledger.get_or_create_wallet(db, owner_type, owner_id)
    assert ledger.available_milli(db, wallet.id, utc_paid_at) == 5_000_000
    if product == "reviewer_month":
        assert buyer.role == "reviewer"
        assert entitlement.has_reviewer_entitlement(db, buyer.id, now=paid_at)
