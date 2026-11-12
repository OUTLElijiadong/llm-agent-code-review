"""MySQL proof for company-row serialization of enterprise billing authorization."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import test_fresh_database_chain as chain
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.core.exceptions import ForbiddenError
from app.models.billing_ledger import BillingCreditBatch, BillingRefund, BillingRefundReviewerGrant, BillingReservation
from app.models.enterprise_org import EnterpriseMember
from app.models.user import User
from app.services import billing_checkout_service as checkout
from app.services import billing_ledger_service as ledger
from app.services import enterprise_org_service as org
from app.services import user_service

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url
verified_model_catalog = chain.verified_model_catalog


def _seed_company(factory, *, with_refund=True):
    suffix = uuid4().hex[:12]
    paid_at = datetime.now(timezone.utc).replace(microsecond=0)
    with factory.begin() as db:
        buyer = User(username=f"auth-buyer-{suffix}", password="x", role="user", status=1)
        reviewer = User(username=f"auth-reviewer-{suffix}", password="x", role="user", status=1)
        executor = User(username=f"auth-executor-{suffix}", password="x", role="user", status=1)
        db.add_all([buyer, reviewer, executor])
        db.flush()
        order = checkout.create_order(db, user_id=buyer.id, product="enterprise_create", quantity=1,
                                      channel="alipay", enterprise_name="授权锁测试")
        checkout._apply_verified(db, order, {"trade_id": f"auth-initial-{suffix}"}, now=paid_at)
        company_id, buyer_id, reviewer_id, executor_id = order.enterprise_id, buyer.id, reviewer.id, executor.id
        order_no = order.order_no
    refund_no = None
    with factory.begin() as db:
        wallet = ledger.get_or_create_wallet(db, "enterprise", company_id)
        wallet_id = wallet.id
        if with_refund:
            refund = checkout.request_refund(db, order_no=order_no, requester_id=buyer_id, reason="隔离测试")
            assert refund.status == "manual_review"
            refund_no = refund.refund_no
    return {
        "company": company_id,
        "buyer": buyer_id,
        "reviewer": reviewer_id,
        "executor": executor_id,
        "order_no": order_no,
        "refund_no": refund_no,
        "wallet": wallet_id,
        "suffix": suffix,
    }


def _grant_reviewer(factory, seeded):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with factory.begin() as db:
        user = db.query(User).filter_by(id=seeded["reviewer"]).populate_existing().with_for_update().one()
        user_service.set_role(db, user.id, "admin", admin_id=seeded["buyer"], commit=False)
        db.add(BillingRefundReviewerGrant(
            user_id=user.id, active=1, granted_by=seeded["buyer"], granted_at=now,
        ))
        seeded["reviewer_token_version"] = int(user.token_version or 0)
    return seeded["reviewer_token_version"]


def _reserve_one(factory, seeded):
    request_key = f"auth-probe-{seeded['suffix']}"
    with factory.begin() as db:
        reservation = ledger.reserve(
            db,
            request_key=request_key,
            model_name="deepseek-flash",
            user_id=seeded["buyer"],
            max_milli=100,
            enterprise_wallet_id=seeded["wallet"],
            enterprise_id=seeded["company"],
            provider_identity=ledger.BillingProviderIdentity(
                "deepseek-official-v1", "deepseek", "api.deepseek.com", "system",
            ),
        )
        assert reservation.status == "reserved"
    return request_key


@pytest.mark.parametrize("reservation_status", ["reserved", "unknown"])
def test_mysql_current_period_refund_rejects_unsettled_reservation_statuses(
    fresh_chain, verified_model_catalog, reservation_status,
):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    seeded = _seed_company(factory)
    _grant_reviewer(factory, seeded)
    request_key = _reserve_one(factory, seeded)
    if reservation_status == "unknown":
        with factory.begin() as db:
            ledger.mark_unknown(db, request_key)

    with factory.begin() as db:
        with pytest.raises(ledger.BillingError, match="未结算预占"):
            checkout.review_manual_refund(
                db,
                refund_no=seeded["refund_no"],
                admin_id=seeded["reviewer"],
                decision="approve",
                reason="验证未结算小菱调用",
                reviewer_token_version=seeded["reviewer_token_version"],
            )

    with factory() as db:
        reservation = db.query(BillingReservation).filter_by(request_key=request_key).one()
        refund = db.query(checkout.BillingRefund).filter_by(refund_no=seeded["refund_no"]).one()
        assert reservation.status == reservation_status
        assert refund.status == "manual_review"


def _waiting_table(engine, connection_id):
    with engine.connect() as connection:
        return connection.execute(text(
            "SELECT l.OBJECT_NAME FROM performance_schema.data_lock_waits w "
            "JOIN performance_schema.threads t ON t.THREAD_ID=w.REQUESTING_THREAD_ID "
            "JOIN performance_schema.data_locks l ON l.ENGINE_LOCK_ID=w.REQUESTING_ENGINE_LOCK_ID "
            "WHERE t.PROCESSLIST_ID=:cid LIMIT 1"
        ), {"cid": connection_id}).scalar_one_or_none()


def _wait_for_enterprise_lock(engine, connection_id):
    for _ in range(200):
        if _waiting_table(engine, connection_id) == "enterprise":
            return True
        time.sleep(0.02)
    return False


@pytest.mark.parametrize("operation", ["request", "review", "prepare", "update"])
def test_mysql_enterprise_refund_writers_lock_company_before_refund_state(fresh_chain, operation):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    seeded = _seed_company(factory, with_refund=operation != "request")
    _grant_reviewer(factory, seeded)
    reconciliation_at = None
    if operation in {"prepare", "update"}:
        with factory.begin() as db:
            checkout.review_manual_refund(db, refund_no=seeded["refund_no"], admin_id=seeded["reviewer"],
                                          decision="approve", reason="准备并发状态",
                                          reviewer_token_version=seeded["reviewer_token_version"])
            if operation == "update":
                checkout.prepare_refund(db, refund_no=seeded["refund_no"], admin_id=seeded["executor"])
                checkout.update_refund_result(
                    db, refund_no=seeded["refund_no"], result={"state": "UNKNOWN"},
                    attempt_no=1, create_response_verified=True,
                )
        if operation == "update":
            reconciliation_at = datetime.now(timezone.utc) + timedelta(seconds=16)
            with factory.begin() as db:
                reconciliation_token = checkout.claim_refund_reconciliation(
                    db, refund_no=seeded["refund_no"], now=reconciliation_at,
                )
                assert reconciliation_token

    started = threading.Event()
    connection_ids = {}

    def write_refund_state():
        with factory.begin() as db:
            connection_ids["writer"] = db.execute(text("SELECT CONNECTION_ID()")).scalar_one()
            started.set()
            if operation == "request":
                return checkout.request_refund(
                    db, order_no=seeded["order_no"], requester_id=seeded["buyer"], reason="并发申请",
                ).status
            if operation == "review":
                return checkout.review_manual_refund(
                    db, refund_no=seeded["refund_no"], admin_id=seeded["reviewer"],
                    decision="approve", reason="并发审核",
                    reviewer_token_version=seeded["reviewer_token_version"],
                ).status
            if operation == "prepare":
                return checkout.prepare_refund(
                    db, refund_no=seeded["refund_no"], admin_id=seeded["executor"],
                )[0].status
            return checkout.update_refund_result(
                db, refund_no=seeded["refund_no"], result={"state": "UNKNOWN"},
                verified_provider_query=True,
                reconciliation_token=reconciliation_token,
                now=reconciliation_at + timedelta(seconds=1),
            ).status

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with factory.begin() as guard:
            company = org._company(guard, seeded["company"], lock=True)
            assert company.status == "active"
            task = pool.submit(write_refund_state)
            assert started.wait(5)
            assert _wait_for_enterprise_lock(fresh_chain, connection_ids["writer"])
            assert not task.done()
        expected = {"request": "manual_review", "review": "approved", "prepare": "processing",
                    "update": "unknown"}[operation]
        assert task.result(timeout=5) == expected
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def test_mysql_enterprise_refund_rechecks_admin_after_revocation_commits(fresh_chain):
    """A stale API authorization cannot create or freeze a refund after demotion."""
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    seeded = _seed_company(factory, with_refund=False)
    requester_id = seeded["reviewer"]
    with factory.begin() as db:
        db.add(EnterpriseMember(
            enterprise_id=seeded["company"], user_id=requester_id, role="admin", status="active",
        ))

    api_authorized = threading.Event()
    connection_ids = {}

    def request_after_stale_api_check():
        with factory.begin() as db:
            connection_ids["requester"] = db.execute(text("SELECT CONNECTION_ID()")).scalar_one()
            # Mirrors the route's preflight check, which can race with demotion.
            org.require_enterprise_admin(db, seeded["company"], requester_id)
            api_authorized.set()
            try:
                checkout.request_refund(
                    db, order_no=seeded["order_no"], requester_id=requester_id, reason="撤权并发退款",
                )
            except ForbiddenError:
                return "denied"
            return "created"

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with factory.begin() as revoke:
            org._company(revoke, seeded["company"], lock=True)
            member = (revoke.query(EnterpriseMember)
                      .filter_by(enterprise_id=seeded["company"], user_id=requester_id)
                      .populate_existing().with_for_update().one())
            member.role = "member"
            future = pool.submit(request_after_stale_api_check)
            assert api_authorized.wait(5)
            assert _wait_for_enterprise_lock(fresh_chain, connection_ids["requester"])
            assert not future.done()
        assert future.result(timeout=10) == "denied"
    finally:
        pool.shutdown(wait=True, cancel_futures=True)

    with factory() as db:
        assert db.query(BillingRefund).filter_by(order_no=seeded["order_no"]).count() == 0
        refund_holds = (db.query(BillingCreditBatch.refund_reserved_milli)
                        .filter_by(wallet_id=seeded["wallet"]).all())
        assert refund_holds and all(value == 0 for (value,) in refund_holds)


@pytest.mark.parametrize("winner", ["send", "refund"])
def test_mysql_send_reservation_and_current_period_refund_have_one_winner(
    fresh_chain, verified_model_catalog, winner,
):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    seeded = _seed_company(factory)
    _grant_reviewer(factory, seeded)
    sender_started, sender_locked, sender_continue = threading.Event(), threading.Event(), threading.Event()
    writer_started = threading.Event()
    connection_ids = {}

    def authorize_and_reserve():
        with factory.begin() as db:
            connection_ids["sender"] = db.execute(text("SELECT CONNECTION_ID()")).scalar_one()
            sender_started.set()
            org._company(db, seeded["company"], lock=True)
            db.query(User).filter_by(id=seeded["reviewer"]).populate_existing().with_for_update().one()
            sender_locked.set()
            if winner == "send":
                assert sender_continue.wait(10)
            status = checkout.enterprise_subscription_status_current(db, seeded["company"])
            if status not in {"active", "grace"}:
                return "denied"
            ledger.reserve(
                db,
                request_key=f"auth-race-{seeded['suffix']}",
                model_name="deepseek-flash",
                user_id=seeded["buyer"],
                max_milli=100,
                enterprise_wallet_id=seeded["wallet"],
                enterprise_id=seeded["company"],
                provider_identity=ledger.BillingProviderIdentity(
                    "deepseek-official-v1", "deepseek", "api.deepseek.com", "system",
                ),
            )
            return "authorized"

    def approve_refund():
        with factory.begin() as db:
            connection_ids["writer"] = db.execute(text("SELECT CONNECTION_ID()")).scalar_one()
            writer_started.set()
            return checkout.review_manual_refund(
                db, refund_no=seeded["refund_no"], admin_id=seeded["reviewer"],
                decision="approve", reason="并发裁决",
                reviewer_token_version=seeded["reviewer_token_version"],
            ).status

    pool = ThreadPoolExecutor(max_workers=2)
    try:
        if winner == "send":
            sender = pool.submit(authorize_and_reserve)
            assert sender_locked.wait(5)
            writer = pool.submit(approve_refund)
            assert writer_started.wait(5)
            assert _wait_for_enterprise_lock(fresh_chain, connection_ids["writer"])
            sender_continue.set()
            assert sender.result(timeout=5) == "authorized"
            with pytest.raises(ledger.BillingError):
                writer.result(timeout=5)
            with factory() as db:
                reservation = db.query(BillingReservation).filter_by(
                    request_key=f"auth-race-{seeded['suffix']}"
                ).one()
                refund = db.query(checkout.BillingRefund).filter_by(refund_no=seeded["refund_no"]).one()
                assert reservation.status == "reserved"
                assert refund.status == "manual_review"
                assert checkout.enterprise_subscription_refund_pending(db, seeded["company"]) is False
            with factory.begin() as db:
                with pytest.raises(ledger.BillingError, match="未结算预占"):
                    checkout.review_manual_refund(
                        db, refund_no=seeded["refund_no"], admin_id=seeded["reviewer"],
                        decision="approve", reason="重试并发审核",
                        reviewer_token_version=seeded["reviewer_token_version"],
                    )
        else:
            writer = pool.submit(approve_refund)
            assert writer.result(timeout=5) == "approved"
            sender = pool.submit(authorize_and_reserve)
            assert sender_started.wait(5)
            assert sender.result(timeout=5) == "denied"
            with factory() as db:
                assert db.query(BillingReservation).filter_by(
                    request_key=f"auth-race-{seeded['suffix']}"
                ).first() is None
                assert checkout.enterprise_subscription_refund_pending(db, seeded["company"]) is True
    finally:
        sender_continue.set()
        pool.shutdown(wait=True, cancel_futures=True)


def test_mysql_current_subscription_check_uses_fresh_snapshot_after_company_lock(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    seeded = _seed_company(factory)
    _grant_reviewer(factory, seeded)
    stale = factory()
    try:
        assert checkout.enterprise_subscription_status(stale, seeded["company"]) == "active"
        with factory.begin() as db:
            checkout.review_manual_refund(db, refund_no=seeded["refund_no"], admin_id=seeded["reviewer"],
                                          decision="approve", reason="旧快照验证",
                                          reviewer_token_version=seeded["reviewer_token_version"])
        org._company(stale, seeded["company"], lock=True)
        assert checkout.enterprise_subscription_status(stale, seeded["company"]) == "active"
        assert checkout.enterprise_subscription_status_current(stale, seeded["company"]) == "none"
    finally:
        stale.rollback()
        stale.close()


def test_mysql_future_renewal_refund_keeps_current_period_authorized(
    fresh_chain, verified_model_catalog,
):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    seeded = _seed_company(factory, with_refund=False)
    paid_at = datetime.now(timezone.utc).replace(microsecond=0)
    with factory.begin() as db:
        renewal = checkout.create_order(db, user_id=seeded["buyer"], product="enterprise_renew", quantity=1,
                                        channel="alipay", enterprise_id=seeded["company"])
        checkout._apply_verified(db, renewal, {"trade_id": f"auth-renewal-{seeded['suffix']}"}, now=paid_at)
        future_refund = checkout.request_refund(
            db, order_no=renewal.order_no, requester_id=seeded["buyer"], reason="取消下周期续费",
        )
        assert future_refund.status == "manual_review"
        future_refund_no = future_refund.refund_no
    request_key = _reserve_one(factory, seeded)
    with factory.begin() as db:
        approved = checkout.review_manual_refund(
            db, refund_no=future_refund_no, admin_id=seeded["reviewer"],
            decision="approve", reason="取消下周期续费",
        )
        assert approved.status == "approved"
        assert checkout.enterprise_subscription_status_current(db, seeded["company"]) == "active"
    with factory() as db:
        assert db.query(BillingReservation).filter_by(request_key=request_key).one().status == "reserved"
