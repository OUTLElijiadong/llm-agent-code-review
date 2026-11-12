"""Real MySQL RR checks for refund holds versus reservation and settlement.

All accounts, orders and rates are synthetic. No payment/provider is contacted.
The fixture requires an explicitly supplied isolated, empty MySQL database.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
import test_fresh_database_chain as chain
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.models.billing_ledger import (
    BillingCreditBatch,
    BillingOrder,
    BillingPaymentEvent,
    BillingReservation,
    BillingSubscriptionPeriod,
)
from app.models.enterprise_org import Enterprise, EnterpriseMember
from app.models.user import User
from app.services import billing_checkout_service as checkout
from app.services import billing_ledger_service as ledger
from app.services import billing_rate_catalog_service as catalog

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url
verified_model_catalog = chain.verified_model_catalog
PROVIDER = ledger.BillingProviderIdentity("deepseek-official-v1", "deepseek", "api.deepseek.com", "system")


def _seed_purchase(sessions, scope):
    token = uuid4().hex[:16]
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with sessions.begin() as db:
        buyer = User(username=f"refund-race-{token}", password="x", role="user", status=1)
        db.add(buyer)
        db.flush()
        enterprise = None
        if scope == "enterprise":
            enterprise = Enterprise(name="退款竞争测试", owner_id=buyer.id, seat_limit=1, status="active",
                                    paid_order_id=f"company-{token}")
            db.add(enterprise)
            db.flush()
            db.add(EnterpriseMember(enterprise_id=enterprise.id, user_id=buyer.id, role="owner", status="active"))
        order = BillingOrder(order_no=f"order-{token}", user_id=buyer.id,
                             enterprise_id=enterprise.id if enterprise else None,
                             product="enterprise_credits" if enterprise else "personal_credits",
                             amount_fen=100, quantity=1, channel="alipay", status="paid",
                             payment_status="paid", paid_at=now, created_at=now)
        db.add(order)
        db.add(BillingPaymentEvent(channel="alipay", trade_id=f"trade-{token}", order_no=order.order_no,
                                   amount_fen=order.amount_fen, verified_at=now))
        wallet = ledger.get_or_create_wallet(db, scope if enterprise else "user",
                                             enterprise.id if enterprise else buyer.id)
        batch = ledger.grant(db, wallet_id=wallet.id, grant_key=f"order:{order.order_no}",
                             kind="purchase", amount_milli=1_000_000)
        return {"buyer": buyer.id, "enterprise": enterprise.id if enterprise else None,
                "wallet": wallet.id, "batch": batch.id, "order": order.order_no}


def _reserve(db, seed, key):
    enterprise = seed["enterprise"]
    return ledger.reserve(db, request_key=key, model_name="deepseek-flash", user_id=seed["buyer"],
                          max_milli=80_000, personal_wallet_id=seed["wallet"] if enterprise is None else None,
                          enterprise_wallet_id=seed["wallet"] if enterprise is not None else None,
                          enterprise_id=enterprise, provider_identity=PROVIDER)


def _request_refund(db, seed):
    return checkout.request_refund(db, order_no=seed["order"], requester_id=seed["buyer"],
                                   reason="隔离并发测试")


@pytest.mark.parametrize("scope", ["user", "enterprise"])
def test_cached_purchase_batch_cannot_be_refunded_after_reservation(fresh_chain, verified_model_catalog, scope):
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    seed = _seed_purchase(sessions, scope)
    with sessions() as stale:
        batch = stale.query(BillingCreditBatch).filter_by(id=seed["batch"]).one()
        assert batch.reserved_milli == 0
        with sessions.begin() as winner:
            _reserve(winner, seed, f"reserve-{uuid4().hex}")
        with pytest.raises(ledger.BillingError, match="账本刚发生变化"):
            _request_refund(stale, seed)
        stale.rollback()
    with sessions.begin() as fresh:
        assert _request_refund(fresh, seed).status == "manual_review"
    with sessions() as db:
        batch = db.query(BillingCreditBatch).filter_by(id=seed["batch"]).one()
        assert (batch.reserved_milli, batch.refund_reserved_milli, batch.remaining_milli) == (80_000, 0, 1_000_000)


@pytest.mark.parametrize("scope", ["user", "enterprise"])
@pytest.mark.parametrize("winner", ["reserve", "refund"])
def test_purchase_refund_and_hold_serialize(fresh_chain, verified_model_catalog, scope, winner):
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    seed = _seed_purchase(sessions, scope)
    first_ready, release, contender_ready = Event(), Event(), Event()
    connection_id = []

    def first():
        with sessions.begin() as db:
            if winner == "reserve":
                _reserve(db, seed, f"first-{uuid4().hex}")
            else:
                assert _request_refund(db, seed).status == "requested"
            first_ready.set()
            assert release.wait(12)

    def second():
        with sessions() as db:
            connection_id.append(db.execute(text("SELECT CONNECTION_ID()")).scalar_one())
            contender_ready.set()
            if winner == "reserve":
                with pytest.raises(ledger.BillingError, match="账本刚发生变化"):
                    _request_refund(db, seed)
                db.rollback()
                return "retry"
            with pytest.raises(ledger.InsufficientCredits):
                _reserve(db, seed, f"second-{uuid4().hex}")
            db.rollback()
            return "insufficient"

    with ThreadPoolExecutor(max_workers=2) as pool:
        initial = pool.submit(first)
        assert first_ready.wait(10)
        try:
            contender = pool.submit(second)
            assert contender_ready.wait(10)
            deadline = monotonic() + 8
            waiting = False
            while monotonic() < deadline:
                with fresh_chain.connect() as observer:
                    waiting = bool(observer.execute(text(
                        "SELECT COUNT(*) FROM performance_schema.data_lock_waits AS w "
                        "JOIN performance_schema.threads AS t ON t.thread_id=w.requesting_thread_id "
                        "WHERE t.processlist_id=:cid"
                    ), {"cid": connection_id[0]}).scalar_one())
                if waiting:
                    break
                sleep(0.02)
            assert waiting, "退款与预占应由共同的钱包/批次锁串行"
        finally:
            release.set()
        initial.result(timeout=15)
        assert contender.result(timeout=15) == ("retry" if winner == "reserve" else "insufficient")
    if winner == "reserve":
        with sessions.begin() as db:
            assert _request_refund(db, seed).status == "manual_review"
    with sessions() as db:
        batch = db.query(BillingCreditBatch).filter_by(id=seed["batch"]).one()
        assert batch.reserved_milli + batch.refund_reserved_milli <= batch.remaining_milli


def test_cached_subscription_batch_cannot_approve_refund_after_reservation(fresh_chain, verified_model_catalog):
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    token = uuid4().hex[:16]
    with sessions.begin() as db:
        buyer = User(username=f"refund-sub-{token}", password="x", role="user", status=1)
        db.add(buyer)
        db.flush()
        order = checkout.create_order(db, user_id=buyer.id, product="reviewer_month", quantity=1, channel="alipay")
        checkout._apply_verified(db, order, {"trade_id": f"subscription-{token}"})
        refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="隔离并发测试")
        assert refund.status == "manual_review"
        wallet = ledger.get_or_create_wallet(db, "user", buyer.id)
        period = db.query(BillingSubscriptionPeriod).filter_by(order_no=order.order_no).one()
        seed = {"buyer": buyer.id, "enterprise": None, "wallet": wallet.id, "period": period.id,
                "refund": refund.refund_no}
    with sessions.begin() as db:
        checkout.activate_due_period(db, seed["period"], now=datetime.now(timezone.utc) + timedelta(seconds=1))
        seed["batch"] = db.query(BillingCreditBatch).filter_by(wallet_id=seed["wallet"]).one().id
    with sessions() as stale:
        cached = stale.query(BillingCreditBatch).filter_by(id=seed["batch"]).one()
        assert cached.reserved_milli == 0
        with sessions.begin() as winner:
            _reserve(winner, seed, f"subscription-hold-{token}")
        with pytest.raises(ledger.BillingError):
            checkout.review_manual_refund(stale, refund_no=seed["refund"], admin_id=987654,
                                          decision="approve", reason="隔离审核")
        stale.rollback()
    with sessions() as db:
        batch = db.query(BillingCreditBatch).filter_by(id=seed["batch"]).one()
        assert batch.reserved_milli == 80_000
        assert batch.refund_reserved_milli == 0


def test_cached_reservation_cannot_revert_settled_to_unknown(fresh_chain, verified_model_catalog):
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    seed = _seed_purchase(sessions, "user")
    key = f"unknown-stale-{uuid4().hex}"
    with sessions.begin() as db:
        _reserve(db, seed, key)
    with sessions() as stale:
        cached = stale.query(BillingReservation).filter_by(request_key=key).one()
        assert cached.status == "reserved"
        with sessions.begin() as winner:
            ledger.settle(winner, request_key=key, actual_milli=30_000, input_tokens=30_000, output_tokens=0)
        with pytest.raises(ledger.BillingError):
            ledger.mark_unknown(stale, key)
        stale.rollback()
    with sessions() as db:
        current = db.query(BillingReservation).filter_by(request_key=key).one()
        assert (current.status, current.actual_milli) == ("settled", 30_000)


def test_cached_tool_reservation_cannot_revert_settled_to_unknown(fresh_chain, verified_model_catalog, monkeypatch):
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    seed = _seed_purchase(sessions, "enterprise")
    key = f"unknown-tool-stale-{uuid4().hex}"
    monkeypatch.setattr(catalog, "tool_rate_snapshot", lambda *_args, **_kwargs: ("synthetic", 5_038, 10_000))
    with sessions.begin() as db:
        ledger.reserve_tool(db, request_key=key, tool_name="search", user_id=seed["buyer"],
                            enterprise_id=seed["enterprise"], enterprise_wallet_id=seed["wallet"],
                            group_id=None, provider_identity=PROVIDER)
        ledger.mark_tool_attempt_started(db, key)
    with sessions() as stale:
        cached = stale.query(BillingReservation).filter_by(request_key=key).one()
        assert cached.status == "reserved"
        with sessions.begin() as winner:
            ledger.settle_tool(winner, request_key=key, tool_successful_calls=1)
        with pytest.raises(ledger.BillingError):
            ledger.mark_unknown(stale, key)
        stale.rollback()
    with sessions() as db:
        current = db.query(BillingReservation).filter_by(request_key=key).one()
        assert (current.status, current.actual_milli) == ("settled", 10_000)
