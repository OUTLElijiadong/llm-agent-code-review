"""Real MySQL lock order for monthly quota changes and seat activation."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
import test_fresh_database_chain as chain
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.models.billing_ledger import BillingOrder, BillingPaymentEvent, BillingSubscription, BillingSubscriptionPeriod
from app.models.enterprise_org import Enterprise, EnterpriseMember
from app.models.user import User
from app.services import billing_checkout_service as checkout
from app.services import billing_ledger_service as ledger
from app.services import billing_quota_service as quota

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


@pytest.mark.parametrize("winner", ["quota", "activation"])
def test_quota_and_seat_activation_share_lock_order(fresh_chain, winner):
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    token = uuid4().hex[:16]
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with sessions.begin() as db:
        owner = User(username=f"quota-seat-{token}", password="x", role="user", status=1)
        db.add(owner)
        db.flush()
        company = Enterprise(name="额度席位锁序", owner_id=owner.id, seat_limit=2, status="active",
                             paid_order_id=f"company-{token}")
        db.add(company)
        db.flush()
        db.add(EnterpriseMember(enterprise_id=company.id, user_id=owner.id, role="owner", status="active"))
        order = BillingOrder(order_no=f"order-{token}", user_id=owner.id, enterprise_id=company.id,
                             product="enterprise_renew", amount_fen=6900, quantity=1, channel="alipay",
                             status="paid", payment_status="paid", paid_at=now, created_at=now)
        db.add(order)
        db.add(BillingPaymentEvent(channel="alipay", trade_id=f"trade-{token}", order_no=order.order_no,
                                   amount_fen=6900, verified_at=now))
        sub = BillingSubscription(owner_type="enterprise", owner_id=company.id, product="enterprise",
                                  anchor_at=now-timedelta(days=1), last_end_at=now+timedelta(days=20),
                                  next_offset=2)
        db.add(sub)
        db.flush()
        period = BillingSubscriptionPeriod(subscription_id=sub.id, order_no=order.order_no,
                                           starts_at=now-timedelta(minutes=1), ends_at=now+timedelta(days=20),
                                           credit_milli=5_000_000, seats=1)
        db.add(period)
        ledger.get_or_create_wallet(db, "enterprise", company.id)
        db.flush()
        owner_id, company_id, period_id = owner.id, company.id, period.id

    first_ready, release, contender_ready = Event(), Event(), Event()
    contender_connection = []

    def do_work(db, operation):
        if operation == "quota":
            quota.set_limit(db, company_id, owner_id, period=quota.current_period(),
                            scope="enterprise", limit_credits=100)
        else:
            assert checkout.activate_due_period(db, period_id, now=now)

    def first():
        with sessions.begin() as db:
            do_work(db, winner)
            first_ready.set()
            assert release.wait(15)

    def second():
        other = "activation" if winner == "quota" else "quota"
        with sessions() as db:
            contender_connection.append(db.execute(text("SELECT CONNECTION_ID()")).scalar_one())
            # Activate an RR snapshot, as authentication/earlier reads would.
            db.query(Enterprise.id).filter_by(id=company_id).first()
            contender_ready.set()
            try:
                do_work(db, other)
                db.commit()
                return "ok"
            except ledger.BillingError as exc:
                db.rollback()
                if other != "quota" or "账本刚发生变化" not in str(exc):
                    raise
        # An old snapshot is allowed to fail closed and retry in a fresh transaction.
        with sessions.begin() as fresh:
            do_work(fresh, other)
        return "ok"

    with ThreadPoolExecutor(max_workers=2) as pool:
        initial = pool.submit(first)
        assert first_ready.wait(12)
        try:
            contender = pool.submit(second)
            assert contender_ready.wait(12)
            deadline = monotonic() + 8
            waiting = False
            while monotonic() < deadline:
                with fresh_chain.connect() as observer:
                    waiting = bool(observer.execute(text(
                        "SELECT COUNT(*) FROM performance_schema.data_lock_waits AS w "
                        "JOIN performance_schema.threads AS t ON t.thread_id=w.requesting_thread_id "
                        "WHERE t.processlist_id=:cid"
                    ), {"cid": contender_connection[0]}).scalar_one())
                if waiting:
                    break
                sleep(0.02)
            assert waiting, "额度设置与周期生效应在共同锁上串行"
        finally:
            release.set()
        initial.result(timeout=15)
        assert contender.result(timeout=15) == "ok"


@pytest.mark.parametrize("winner", ["quota", "renewal"])
def test_quota_and_renewal_payment_share_lock_order(fresh_chain, winner):
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    token = uuid4().hex[:16]
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with sessions.begin() as db:
        owner = User(username=f"quota-renew-{token}", password="x", role="user", status=1)
        db.add(owner)
        db.flush()
        company = Enterprise(name="续费额度锁序", owner_id=owner.id, seat_limit=2, status="active",
                             paid_order_id=f"company-renew-{token}")
        db.add(company)
        db.flush()
        db.add(EnterpriseMember(enterprise_id=company.id, user_id=owner.id, role="owner", status="active"))
        ledger.get_or_create_wallet(db, "enterprise", company.id)
        order = BillingOrder(order_no=f"renew-{token}", user_id=owner.id, enterprise_id=company.id,
                             product="enterprise_renew", amount_fen=6900, quantity=1, channel="alipay",
                             status="created", payment_status="ready", created_at=now)
        db.add(order)
        db.flush()
        owner_id, company_id, order_no = owner.id, company.id, order.order_no
    first_ready, release, contender_ready = Event(), Event(), Event()
    contender_connection = []

    def perform(db, operation):
        if operation == "quota":
            quota.set_limit(db, company_id, owner_id, period=quota.current_period(),
                            scope="enterprise", limit_credits=100)
        else:
            order = checkout.apply_alipay_query(db, order_no=order_no, result={
                "order_no": order_no, "state": "TRADE_SUCCESS", "amount_fen": 6900, "trade_id": f"trade-{token}",
            }, now=now)
            assert order.status == "paid"

    def first():
        with sessions.begin() as db:
            perform(db, winner)
            first_ready.set()
            assert release.wait(15)

    def second():
        other = "renewal" if winner == "quota" else "quota"
        with sessions() as db:
            contender_connection.append(db.execute(text("SELECT CONNECTION_ID()")).scalar_one())
            db.query(Enterprise.id).filter_by(id=company_id).first()
            contender_ready.set()
            try:
                perform(db, other)
                db.commit()
                return "ok"
            except ledger.BillingError as exc:
                db.rollback()
                if other != "quota" or "账本刚发生变化" not in str(exc):
                    raise
        with sessions.begin() as fresh:
            perform(fresh, other)
        return "ok"

    with ThreadPoolExecutor(max_workers=2) as pool:
        initial = pool.submit(first)
        assert first_ready.wait(12)
        try:
            contender = pool.submit(second)
            assert contender_ready.wait(12)
            deadline = monotonic() + 8
            waiting = False
            while monotonic() < deadline:
                with fresh_chain.connect() as observer:
                    waiting = bool(observer.execute(text(
                        "SELECT COUNT(*) FROM performance_schema.data_lock_waits AS w "
                        "JOIN performance_schema.threads AS t ON t.thread_id=w.requesting_thread_id "
                        "WHERE t.processlist_id=:cid"
                    ), {"cid": contender_connection[0]}).scalar_one())
                if waiting:
                    break
                sleep(0.02)
            assert waiting, "续费和额度设置应在共同锁上串行"
        finally:
            release.set()
        initial.result(timeout=15)
        assert contender.result(timeout=15) == "ok"


@pytest.mark.parametrize("winner", ["quota", "refund"])
def test_quota_and_confirmed_enterprise_refund_share_lock_order(fresh_chain, winner):
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    token = uuid4().hex[:16]
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with sessions.begin() as db:
        owner = User(username=f"quota-refund-{token}", password="x", role="user", status=1)
        db.add(owner)
        db.flush()
        company = Enterprise(name="退款额度锁序", owner_id=owner.id, seat_limit=2, status="active",
                             paid_order_id=f"company-refund-{token}")
        db.add(company)
        db.flush()
        db.add(EnterpriseMember(enterprise_id=company.id, user_id=owner.id, role="owner", status="active"))
        order = BillingOrder(order_no=f"refund-order-{token}", user_id=owner.id, enterprise_id=company.id,
                             product="enterprise_renew", amount_fen=6900, quantity=1, channel="alipay",
                             status="paid", payment_status="paid", paid_at=now, created_at=now)
        db.add(order)
        db.add(BillingPaymentEvent(channel="alipay", trade_id=f"refund-trade-{token}", order_no=order.order_no,
                                   amount_fen=6900, verified_at=now))
        sub = BillingSubscription(owner_type="enterprise", owner_id=company.id, product="enterprise",
                                  anchor_at=now-timedelta(days=1), last_end_at=now+timedelta(days=20), next_offset=2)
        db.add(sub)
        db.flush()
        period = BillingSubscriptionPeriod(subscription_id=sub.id, order_no=order.order_no,
                                           starts_at=now-timedelta(minutes=1), ends_at=now+timedelta(days=20),
                                           credit_milli=5_000_000, seats=1)
        db.add(period)
        db.flush()
        assert checkout.activate_due_period(db, period.id, now=now)
        refund = checkout.request_refund(db, order_no=order.order_no, requester_id=owner.id, reason="测试")
        checkout.review_manual_refund(db, refund_no=refund.refund_no, admin_id=owner.id + 1_000_000,
                                      decision="approve", reason="测试", now=now)
        checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=owner.id + 2_000_000)
        checkout.update_refund_result(db, refund_no=refund.refund_no, result={"state": "PROCESSING"},
                                      attempt_no=1, create_response_verified=True, now=now)
        refund.updated_at = now - timedelta(seconds=16)
        db.flush()
        claim = checkout.claim_refund_reconciliation(db, refund_no=refund.refund_no, now=now)
        assert claim
        owner_id, company_id, refund_no = owner.id, company.id, refund.refund_no
    first_ready, release, contender_ready = Event(), Event(), Event()
    contender_connection = []

    def perform(db, operation):
        if operation == "quota":
            quota.set_limit(db, company_id, owner_id, period=quota.current_period(),
                            scope="enterprise", limit_credits=100)
        else:
            result = checkout.update_refund_result(
                db, refund_no=refund_no, result={"state": "SUCCESS", "provider_refund_id": "test"},
                verified_provider_query=True, reconciliation_token=claim, now=now,
            )
            assert result.status == "succeeded"

    def first():
        with sessions.begin() as db:
            perform(db, winner)
            first_ready.set()
            assert release.wait(15)

    def second():
        other = "refund" if winner == "quota" else "quota"
        with sessions() as db:
            contender_connection.append(db.execute(text("SELECT CONNECTION_ID()")).scalar_one())
            db.query(Enterprise.id).filter_by(id=company_id).first()
            contender_ready.set()
            try:
                perform(db, other)
                db.commit()
                return "ok"
            except ledger.BillingError as exc:
                db.rollback()
                if "账本刚发生变化" not in str(exc):
                    raise
        with sessions.begin() as fresh:
            perform(fresh, other)
        return "ok"

    with ThreadPoolExecutor(max_workers=2) as pool:
        initial = pool.submit(first)
        assert first_ready.wait(12)
        try:
            contender = pool.submit(second)
            assert contender_ready.wait(12)
            deadline = monotonic() + 8
            waiting = False
            while monotonic() < deadline:
                with fresh_chain.connect() as observer:
                    waiting = bool(observer.execute(text(
                        "SELECT COUNT(*) FROM performance_schema.data_lock_waits AS w "
                        "JOIN performance_schema.threads AS t ON t.thread_id=w.requesting_thread_id "
                        "WHERE t.processlist_id=:cid"
                    ), {"cid": contender_connection[0]}).scalar_one())
                if waiting:
                    break
                sleep(0.02)
            assert waiting, "退款结果和额度设置应在共同锁上串行"
        finally:
            release.set()
        initial.result(timeout=15)
        assert contender.result(timeout=15) == "ok"
