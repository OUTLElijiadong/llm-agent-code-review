"""MySQL DATETIME(0) must not postpone an already paid subscription."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import test_fresh_database_chain as chain
from sqlalchemy.orm import sessionmaker

from app.models.billing_ledger import BillingCreditBatch, BillingOrder, BillingPaymentEvent, BillingSubscriptionPeriod
from app.models.user import User
from app.services import billing_checkout_service as checkout
from app.services import billing_ledger_service as ledger

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


@pytest.mark.parametrize("product", ["reviewer_month", "enterprise_create"])
@pytest.mark.parametrize("microsecond", [499999, 500000, 900000])
def test_paid_period_is_active_at_payment_time_and_replay_is_idempotent(fresh_chain, product, microsecond):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    suffix = uuid4().hex[:12]
    paid_at = datetime(2026, 1, 31, 23, 59, 59, microsecond, tzinfo=timezone.utc)
    expected_start = paid_at.replace(tzinfo=None, microsecond=0)
    with factory.begin() as db:
        buyer = User(username=f"time-{suffix}", password="x", role="user", status=1)
        db.add(buyer)
        db.flush()
        kwargs = {"enterprise_name": "精度测试企业"} if product == "enterprise_create" else {}
        order = checkout.create_order(db, user_id=buyer.id, product=product, quantity=1, channel="alipay", **kwargs)
        verified = {"trade_id": f"precision-{suffix}"}
        checkout._apply_verified(db, order, verified, now=paid_at)
        checkout._apply_verified(db, order, verified, now=paid_at + timedelta(microseconds=1))
        period = db.query(BillingSubscriptionPeriod).filter_by(order_no=order.order_no).one()
        owner_type = "enterprise" if product == "enterprise_create" else "user"
        owner_id = order.enterprise_id if owner_type == "enterprise" else buyer.id
        wallet = ledger.get_or_create_wallet(db, owner_type, owner_id)
        assert period.granted_at is not None
        assert period.starts_at == expected_start
        assert ledger.available_milli(db, wallet.id, paid_at.replace(tzinfo=None)) == 5_000_000
        assert db.query(BillingPaymentEvent).filter_by(order_no=order.order_no).count() == 1
        assert db.query(BillingCreditBatch).filter_by(grant_key=f"period:{period.id}").count() == 1


def test_future_renewal_and_expired_restart_preserve_second_boundaries(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    suffix = uuid4().hex[:12]
    paid_at = datetime(2026, 1, 31, 23, 59, 59, 900000, tzinfo=timezone.utc)
    with factory.begin() as db:
        buyer = User(username=f"renew-{suffix}", password="x", role="user", status=1)
        db.add(buyer)
        db.flush()
        initial = checkout.create_order(db, user_id=buyer.id, product="enterprise_create", quantity=1,
                                        channel="alipay", enterprise_name="续费精度测试")
        checkout._apply_verified(db, initial, {"trade_id": f"create-{suffix}"}, now=paid_at)
        first = db.query(BillingSubscriptionPeriod).filter_by(order_no=initial.order_no).one()
        initial_end, company_id, buyer_id = first.ends_at, initial.enterprise_id, buyer.id
    with factory.begin() as db:
        renewal = checkout.create_order(db, user_id=buyer_id, product="enterprise_renew", quantity=2,
                                        channel="alipay", enterprise_id=company_id)
        checkout._apply_verified(db, renewal, {"trade_id": f"renew-{suffix}"}, now=paid_at + timedelta(days=1))
        second = db.query(BillingSubscriptionPeriod).filter_by(order_no=renewal.order_no).one()
        assert second.starts_at == initial_end
        assert second.granted_at is None
        second_id, second_end = second.id, second.ends_at
    with factory.begin() as db:
        assert not checkout.activate_due_period(db, second_id, now=initial_end - timedelta(microseconds=1))
        assert checkout.activate_due_period(db, second_id, now=initial_end)
        assert not checkout.activate_due_period(db, second_id, now=initial_end)
    restart_at = second_end + timedelta(days=7, microseconds=900000)
    with factory.begin() as db:
        restart = checkout.create_order(db, user_id=buyer_id, product="enterprise_renew", quantity=3,
                                        channel="alipay", enterprise_id=company_id)
        checkout._apply_verified(db, restart, {"trade_id": f"restart-{suffix}"}, now=restart_at)
        third = db.query(BillingSubscriptionPeriod).filter_by(order_no=restart.order_no).one()
        assert third.starts_at == restart_at.replace(microsecond=0)
        assert third.granted_at is not None
        assert db.query(BillingCreditBatch).filter_by(grant_key=f"period:{third.id}").count() == 1


def test_payment_rollback_leaves_no_payment_or_entitlement(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    suffix = uuid4().hex[:12]
    with factory.begin() as db:
        buyer = User(username=f"rollback-{suffix}", password="x", role="user", status=1)
        db.add(buyer)
        db.flush()
        order = checkout.create_order(db, user_id=buyer.id, product="enterprise_create", quantity=1,
                                      channel="alipay", enterprise_name="回滚精度测试")
        order_no = order.order_no
    with factory() as db:
        order = db.query(BillingOrder).filter_by(order_no=order_no).one()
        checkout._apply_verified(db, order, {"trade_id": f"rollback-{suffix}"},
                                 now=datetime(2026, 3, 1, 0, 0, 0, 900000, tzinfo=timezone.utc))
        db.rollback()
    with factory() as db:
        order = db.query(BillingOrder).filter_by(order_no=order_no).one()
        assert order.status == "created" and order.enterprise_id is None
        assert db.query(BillingPaymentEvent).filter_by(order_no=order_no).count() == 0
        assert db.query(BillingSubscriptionPeriod).filter_by(order_no=order_no).count() == 0
