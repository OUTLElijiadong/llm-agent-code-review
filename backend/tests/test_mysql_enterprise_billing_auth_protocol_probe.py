"""Proof-of-concept only: serialize subscription-refund changes with an auth mutex.

This isolated test creates a throwaway table. No application migration or gate is enabled.
"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import httpx
import pytest
import test_fresh_database_chain as chain
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.models.billing_ledger import BillingReservation
from app.models.user import User
from app.services import billing_checkout_service as checkout
from app.services import billing_ledger_service as ledger
from app.services import enterprise_assistant_billing_group as billing_group
from app.services import enterprise_chat_service as chat

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url
verified_model_catalog = chain.verified_model_catalog


def _scenario(factory):
    suffix = uuid4().hex[:12]
    paid_at = datetime.now(timezone.utc).replace(microsecond=0)
    with factory.begin() as db:
        buyer = User(username=f"auth-buyer-{suffix}", password="x", role="user", status=1)
        reviewer = User(username=f"auth-reviewer-{suffix}", password="x", role="user", status=1)
        db.add_all([buyer, reviewer])
        db.flush()
        initial = checkout.create_order(db, user_id=buyer.id, product="enterprise_create", quantity=1,
                                        channel="alipay", enterprise_name="授权锁测试")
        checkout._apply_verified(db, initial, {"trade_id": f"auth-initial-{suffix}"}, now=paid_at)
        company, buyer_id, reviewer_id = initial.enterprise_id, buyer.id, reviewer.id
    with factory.begin() as db:
        renewal = checkout.create_order(db, user_id=buyer_id, product="enterprise_renew", quantity=1,
                                        channel="alipay", enterprise_id=company)
        checkout._apply_verified(db, renewal, {"trade_id": f"auth-renewal-{suffix}"}, now=paid_at)
        refund = checkout.request_refund(db, order_no=renewal.order_no, requester_id=buyer_id, reason="隔离测试")
        assert refund.status == "manual_review"
        wallet = ledger.get_or_create_wallet(db, "enterprise", company)
        refund_no, wallet_id = refund.refund_no, wallet.id
    with factory.begin() as db:
        room_id = chat.open_conversation(db, company, buyer_id, "company").id
    request_key = f"auth-probe-{suffix}"
    with factory.begin() as db:
        reservation = ledger.reserve(
            db, request_key=request_key, model_name="deepseek-flash", user_id=buyer_id, max_milli=100,
            enterprise_wallet_id=wallet_id, enterprise_id=company,
            provider_identity=ledger.BillingProviderIdentity(
                "deepseek-official-v1", "deepseek", "api.deepseek.com", "system",
            ),
        )
        assert reservation.status == "reserved"
    return company, buyer_id, reviewer_id, room_id, refund_no, request_key


def _create_mutex_table(engine, company):
    with engine.begin() as db:
        db.execute(text("CREATE TABLE test_billing_auth_mutex "
                        "(enterprise_id BIGINT PRIMARY KEY, epoch BIGINT NOT NULL DEFAULT 0) ENGINE=InnoDB"))
        db.execute(text("INSERT INTO test_billing_auth_mutex (enterprise_id) VALUES (:id)"), {"id": company})


@pytest.mark.asyncio
@pytest.mark.parametrize("winner", ["send", "refund"])
@pytest.mark.parametrize("mutation", ["review", "prepare", "late_success"])
async def test_proposed_auth_mutex_serializes_real_refund_approval_and_send(
    fresh_chain, verified_model_catalog, winner, mutation,
):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, buyer, reviewer, room, refund_no, request_key = _scenario(factory)
    if mutation != "review":
        observed_at = datetime.now(timezone.utc).replace(microsecond=0)
        with factory.begin() as db:
            checkout.review_manual_refund(db, refund_no=refund_no, admin_id=reviewer,
                                          decision="approve", reason="协议测试", now=observed_at)
            checkout.prepare_refund(db, refund_no=refund_no, admin_id=reviewer + 10000)
            checkout.update_refund_result(db, refund_no=refund_no, result={"state": "UNKNOWN"},
                                          now=observed_at, attempt_no=1, create_response_verified=True)
        with factory.begin() as db:
            token = checkout.claim_refund_reconciliation(db, refund_no=refund_no,
                                                         now=observed_at + timedelta(seconds=16))
            assert token
            refund = checkout.update_refund_result(
                db, refund_no=refund_no, result={"state": "FAILED"}, verified_provider_query=True,
                reconciliation_token=token, now=observed_at + timedelta(seconds=17),
            )
            assert refund.status == "approved" and refund.provider_status == "FAILED"
        with factory() as db:
            assert not checkout.enterprise_subscription_refund_pending(db, company)
    _create_mutex_table(fresh_chain, company)
    sender_ready, sender_release, writer_ready, writer_release, received, respond = (
        threading.Event() for _ in range(6)
    )
    sender_locked, sender_continue = threading.Event(), threading.Event()
    sender_attempt, writer_attempt = threading.Event(), threading.Event()
    connection_ids = {}
    calls = []

    def send_guard():
        with factory.begin() as db:
            connection_ids["sender"] = db.execute(text("SELECT CONNECTION_ID()")).scalar_one()
            sender_attempt.set()
            db.execute(text("SELECT enterprise_id FROM test_billing_auth_mutex "
                            "WHERE enterprise_id=:id FOR SHARE"), {"id": company}).one()
            sender_locked.set()
            if winner == "send":
                assert sender_continue.wait(10)
            # The refund predicate remains the only truth; never cache a duplicate boolean.
            with factory() as fresh:
                pending = checkout.enterprise_subscription_refund_pending(fresh, company)
            if pending:
                sender_ready.set()
                return "denied"
            snapshot = billing_group.capture(db, enterprise_id=company, user_id=buyer,
                                             conversation_id=room, lock_company=False)
            assert snapshot.group_id is None
            sender_ready.set()
            assert sender_release.wait(10)
            return "authorized"

    def refund_writer():
        with factory.begin() as db:
            connection_ids["writer"] = db.execute(text("SELECT CONNECTION_ID()")).scalar_one()
            checkout._lock_refund_and_order(db, refund_no)  # Existing actual order -> refund order.
            writer_attempt.set()
            db.execute(text("SELECT enterprise_id FROM test_billing_auth_mutex "
                            "WHERE enterprise_id=:id FOR UPDATE"), {"id": company}).one()
            writer_ready.set()
            if winner == "refund":
                assert writer_release.wait(10)
            if mutation == "review":
                refund = checkout.review_manual_refund(db, refund_no=refund_no, admin_id=reviewer,
                                                       decision="approve", reason="隔离测试")
                assert refund.status == "approved"
            elif mutation == "prepare":
                refund, _ = checkout.prepare_refund(db, refund_no=refund_no, admin_id=reviewer + 10000)
                assert refund.status == "processing"
            else:
                refund = checkout.update_refund_result(db, refund_no=refund_no, result={"state": "SUCCESS"},
                                                       attempt_no=1, create_response_verified=True)
                assert (refund.status, refund.provider_status) == ("manual_review", "UNKNOWN")
            db.execute(text("UPDATE test_billing_auth_mutex SET epoch=epoch+1 WHERE enterprise_id=:id"),
                       {"id": company})

    def waiting_table(connection_id):
        with fresh_chain.connect() as connection:
            return connection.execute(text(
                "SELECT l.OBJECT_NAME FROM performance_schema.data_lock_waits w "
                "JOIN performance_schema.threads t ON t.THREAD_ID=w.REQUESTING_THREAD_ID "
                "JOIN performance_schema.data_locks l ON l.ENGINE_LOCK_ID=w.REQUESTING_ENGINE_LOCK_ID "
                "WHERE t.PROCESSLIST_ID=:cid LIMIT 1"
            ), {"cid": connection_id}).scalar_one_or_none()

    async def wait_for_wait(name):
        for _ in range(200):
            table = await asyncio.to_thread(waiting_table, connection_ids[name])
            if table:
                return table
            await asyncio.sleep(0.02)
        pytest.fail("未观察到授权互斥行上的锁等待")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            calls.append(1)
            received.set()
            if respond.wait(10):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        if winner == "send":
            sender = pool.submit(send_guard)
            assert await asyncio.to_thread(sender_locked.wait, 5)
            writer = pool.submit(refund_writer)
            assert await asyncio.to_thread(writer_attempt.wait, 5)
            assert await wait_for_wait("writer") == "test_billing_auth_mutex"
            sender_continue.set()
            assert await asyncio.to_thread(sender_ready.wait, 5)

            async def trace(name, _info):
                if name == "http11.send_request_body.complete":
                    sender_release.set()

            async def send():
                async with httpx.AsyncClient(trust_env=False, http2=False, timeout=10) as client:
                    async with client.stream("POST", f"http://127.0.0.1:{server.server_port}/",
                                             json={"model": "test"}, extensions={"trace": trace}) as response:
                        return await response.aread()

            task = asyncio.create_task(send())
            assert await asyncio.to_thread(received.wait, 5)
            assert await asyncio.to_thread(sender.result, 5) == "authorized"
            await asyncio.to_thread(writer.result, 5)
            assert not task.done()
            respond.set()
            assert await task == b"ok" and calls == [1]
            # Local fixture usage, not a claim that a real supplier returned it.
            with factory.begin() as db:
                row = db.query(BillingReservation).filter_by(request_key=request_key).one()
                actual = ledger._quote_with_rates(ledger._reservation_rates(row), 10, 5)
                settled = ledger.settle(db, request_key=request_key, actual_milli=actual,
                                        input_tokens=10, output_tokens=5)
                assert settled.status == "settled"
        else:
            writer = pool.submit(refund_writer)
            assert await asyncio.to_thread(writer_ready.wait, 5)
            sender = pool.submit(send_guard)
            assert await asyncio.to_thread(sender_attempt.wait, 5)
            assert await wait_for_wait("sender") == "test_billing_auth_mutex"
            writer_release.set()
            await asyncio.to_thread(writer.result, 5)
            assert await asyncio.to_thread(sender.result, 5) == "denied"
            assert not calls
            with factory() as db:
                assert db.query(BillingReservation).filter_by(request_key=request_key).one().status == "reserved"
    finally:
        sender_release.set()
        sender_continue.set()
        writer_release.set()
        respond.set()
        pool.shutdown(wait=True, cancel_futures=True)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
        with fresh_chain.begin() as db:
            db.execute(text("DROP TABLE test_billing_auth_mutex"))


def test_unmodified_old_refund_worker_bypasses_candidate_mutex(fresh_chain, verified_model_catalog):
    """Schema alone does not protect mixed old/new worker deployments."""
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, _buyer, reviewer, _room, refund_no, _request_key = _scenario(factory)
    _create_mutex_table(fresh_chain, company)

    def old_refund_worker():
        with factory.begin() as db:
            refund = checkout.review_manual_refund(db, refund_no=refund_no, admin_id=reviewer,
                                                   decision="approve", reason="旧 worker 隔离测试")
            assert refund.status == "approved"

    try:
        with factory.begin() as guard:
            guard.execute(text("SELECT enterprise_id FROM test_billing_auth_mutex "
                               "WHERE enterprise_id=:id FOR SHARE"), {"id": company}).one()
            with factory() as fresh:
                assert not checkout.enterprise_subscription_refund_pending(fresh, company)
            pool = ThreadPoolExecutor(max_workers=1)
            try:
                pool.submit(old_refund_worker).result(timeout=5)
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
            with factory() as fresh:
                assert checkout.enterprise_subscription_refund_pending(fresh, company)
    finally:
        with fresh_chain.begin() as db:
            db.execute(text("DROP TABLE test_billing_auth_mutex"))


def test_proposed_refund_epoch_and_review_roll_back_together(fresh_chain, verified_model_catalog):
    """A failed refund transaction cannot leave a false committed authorization epoch."""
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, _buyer, reviewer, _room, refund_no, _request_key = _scenario(factory)
    _create_mutex_table(fresh_chain, company)
    try:
        with pytest.raises(RuntimeError, match="forced rollback"):
            with factory.begin() as db:
                checkout._lock_refund_and_order(db, refund_no)
                db.execute(text("SELECT epoch FROM test_billing_auth_mutex "
                                "WHERE enterprise_id=:id FOR UPDATE"), {"id": company}).one()
                checkout.review_manual_refund(db, refund_no=refund_no, admin_id=reviewer,
                                              decision="approve", reason="回滚隔离测试")
                db.execute(text("UPDATE test_billing_auth_mutex SET epoch=epoch+1 WHERE enterprise_id=:id"),
                           {"id": company})
                raise RuntimeError("forced rollback")
        with factory() as db:
            assert db.execute(text("SELECT epoch FROM test_billing_auth_mutex WHERE enterprise_id=:id"),
                              {"id": company}).scalar_one() == 0
            assert not checkout.enterprise_subscription_refund_pending(db, company)
    finally:
        with fresh_chain.begin() as db:
            db.execute(text("DROP TABLE test_billing_auth_mutex"))
