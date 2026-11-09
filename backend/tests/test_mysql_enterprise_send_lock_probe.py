"""Isolated experiment: a membership-only send lock can still block other members.

This is not an implemented billing protocol or evidence of provider receipt.
"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import httpx
import pytest
import test_fresh_database_chain as chain
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from test_mysql_billing_group_version import _seed

from app.models.user import User
from app.services import enterprise_assistant_billing_group as billing_group
from app.services import enterprise_chat_service as chat
from app.services import enterprise_org_service as org

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


@pytest.mark.asyncio
@pytest.mark.parametrize("first_operation", ["change_default", "remove_member"])
async def test_mysql_member_send_lock_can_indirectly_block_unrelated_member(fresh_chain, first_operation):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, owner, first, group_a, group_b = _seed(factory)
    with factory.begin() as db:
        second = User(username=f"send-probe-{uuid4().hex}", password="x", role="user", status=1)
        db.add(second)
        db.flush()
        second_id = second.id
    with factory.begin() as db:
        invitation = org.create_invitation(db, company, owner)
    with factory.begin() as db:
        org.accept_invitation(db, invitation, second_id)
    with factory.begin() as db:
        for user_id in (first, second_id):
            org.set_group_member(db, company, group_a, owner, user_id, "member")
            org.set_group_member(db, company, group_b, owner, user_id, "member")
            org.set_default_group(db, company, owner, user_id, group_a)
    with factory.begin() as db:
        room = chat.open_conversation(db, company, owner, "company").id

    held, release, received, respond = (threading.Event() for _ in range(4))
    writer_started = [threading.Event(), threading.Event()]
    connection_ids = [None, None]

    def hold_member():
        with factory.begin() as db:
            snapshot = billing_group.capture(db, enterprise_id=company, user_id=first,
                                             conversation_id=room, lock_company=False)
            assert snapshot.group_id == group_a
            held.set()
            assert release.wait(12)

    def change_group(index, user_id):
        with factory.begin() as db:
            connection_ids[index] = db.execute(text("SELECT CONNECTION_ID()")).scalar_one()
            writer_started[index].set()
            if index == 0 and first_operation == "remove_member":
                org.remove_enterprise_member(db, company, owner, user_id)
            else:
                org.set_default_group(db, company, owner, user_id, group_b)

    def waiting_table(connection_id):
        with fresh_chain.connect() as connection:
            return connection.execute(text(
                "SELECT l.OBJECT_NAME FROM performance_schema.data_lock_waits w "
                "JOIN performance_schema.threads t ON t.THREAD_ID=w.REQUESTING_THREAD_ID "
                "JOIN performance_schema.data_locks l ON l.ENGINE_LOCK_ID=w.REQUESTING_ENGINE_LOCK_ID "
                "WHERE t.PROCESSLIST_ID=:cid LIMIT 1"
            ), {"cid": connection_id}).scalar_one_or_none()

    async def wait_for_wait(index):
        for _ in range(200):
            table = await asyncio.to_thread(waiting_table, connection_ids[index])
            if table:
                return table
            await asyncio.sleep(0.02)
        pytest.fail("未观察到预期的数据库行锁等待")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            received.set()
            if not respond.wait(10):
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    pool = ThreadPoolExecutor(max_workers=3)
    try:
        gate = pool.submit(hold_member)
        assert await asyncio.to_thread(held.wait, 5)
        writer_a = pool.submit(change_group, 0, first)
        assert await asyncio.to_thread(writer_started[0].wait, 5)
        assert await wait_for_wait(0) == "enterprise_member"
        writer_b = pool.submit(change_group, 1, second_id)
        assert await asyncio.to_thread(writer_started[1].wait, 5)
        assert await wait_for_wait(1) == "enterprise"

        async def trace(name, _info):
            if name == "http11.send_request_body.complete":
                release.set()

        async def send():
            async with httpx.AsyncClient(trust_env=False, http2=False, timeout=10) as client:
                async with client.stream("POST", f"http://127.0.0.1:{server.server_port}/",
                                         json={"model": "local-test"}, extensions={"trace": trace}) as response:
                    return await response.aread()

        task = asyncio.create_task(send())
        assert await asyncio.to_thread(received.wait, 5)
        await asyncio.to_thread(gate.result, 5)
        await asyncio.to_thread(writer_a.result, 5)
        await asyncio.to_thread(writer_b.result, 5)
        assert not task.done()  # Server still withholds headers: no full-stream lock.
        respond.set()
        assert await task == b"ok"
    finally:
        release.set()
        respond.set()
        pool.shutdown(wait=True, cancel_futures=True)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


def test_mysql_company_shared_lock_does_not_fence_real_subscription_refund(fresh_chain):
    """An actual refund review can revoke access despite a held company share lock.

    No reservation is created and no payment provider is contacted; this only
    tests the suggested company-row locking boundary, not full billing behavior.
    """
    from app.models.enterprise_org import Enterprise
    from app.services import billing_checkout_service as checkout

    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    suffix = uuid4().hex[:12]
    with factory.begin() as db:
        buyer = User(username=f"send-refund-buyer-{suffix}", password="x", role="user", status=1)
        reviewer = User(username=f"send-refund-reviewer-{suffix}", password="x", role="user", status=1)
        executor = User(username=f"send-refund-executor-{suffix}", password="x", role="user", status=1)
        db.add_all([buyer, reviewer, executor])
        db.flush()
        order = checkout.create_order(db, user_id=buyer.id, product="enterprise_create", quantity=1,
                                      channel="alipay", enterprise_name="退款并发测试")
        checkout._apply_verified(db, order, {"trade_id": f"send-refund-trade-{suffix}"})
        refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="并发测试")
        assert refund.status == "manual_review"
        company, refund_no, reviewer_id, executor_id = order.enterprise_id, refund.refund_no, reviewer.id, executor.id

    def approve_then_prepare():
        with factory.begin() as db:
            result = checkout.review_manual_refund(db, refund_no=refund_no, admin_id=reviewer_id,
                                                   decision="approve", reason="并发测试")
            assert result.status == "approved"
        with factory.begin() as db:
            result, _ = checkout.prepare_refund(db, refund_no=refund_no, admin_id=executor_id)
            assert result.status == "processing"

    with factory.begin() as guard:
        assert (guard.query(Enterprise).filter_by(id=company).populate_existing().with_for_update(read=True).one()
                .status == "active")
        with factory() as verify:
            assert not checkout.enterprise_subscription_refund_pending(verify, company)
        # A timeout is a failure here, but the scope eventually releases the guard.
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            task = pool.submit(approve_then_prepare)
            task.result(timeout=5)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        with factory() as verify:
            assert checkout.enterprise_subscription_refund_pending(verify, company)


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["cancel", "read_timeout"])
async def test_local_http_interruption_keeps_real_mysql_reservation_unknown(
    fresh_chain, verified_model_catalog, monkeypatch, interruption,
):
    """Real transport and ledger, with provider identity replaced only for loopback."""
    from app.models.billing_ledger import BillingReservation
    from app.services import billing_ledger_service as ledger
    from app.services.agent_responses_service import NativeResponsesTransport
    from app.services.billing_runtime_gate import billing_scope
    from app.utils.api_resolver import ApiConfig

    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        user = User(username=f"send-unknown-{uuid4().hex[:12]}", password="x", role="user", status=1)
        db.add(user)
        db.flush()
        wallet = ledger.get_or_create_wallet(db, "user", user.id)
        ledger.grant(db, wallet_id=wallet.id, grant_key=f"send-unknown-{uuid4().hex}",
                     kind="purchase", amount_milli=1_000_000)
        user_id, wallet_id = user.id, wallet.id
    identity = ledger.BillingProviderIdentity("deepseek-official-v1", "deepseek", "api.deepseek.com", "system")
    monkeypatch.setattr(ledger, "resolve_billing_provider_identity", lambda **_kwargs: identity)
    received, respond = threading.Event(), threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            requests.append(1)
            received.set()
            if respond.wait(8):
                try:
                    self.send_response(200)
                    self.end_headers()
                except BrokenPipeError:
                    pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    transport = NativeResponsesTransport(ApiConfig(
        api_key="loopback-test", base_url=f"http://127.0.0.1:{server.server_port}",
        model="deepseek-flash", timeout_seconds=1,
    ))

    async def stream():
        return [event async for event in transport._stream({"model": "deepseek-flash", "max_output_tokens": 50})]

    try:
        with billing_scope(session_factory=factory, user_id=user_id, personal_wallet_id=wallet_id,
                           max_input_tokens=100, max_output_tokens=50):
            task = asyncio.create_task(stream())
            assert await asyncio.to_thread(received.wait, 5)
            if interruption == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(ledger.BillingError, match="未知"):
                    await task
        with factory() as db:
            reservation = db.query(BillingReservation).filter_by(user_id=user_id).one()
            assert reservation.status == "unknown" and reservation.actual_milli is None
            assert ledger.available_milli(db, wallet_id) == 1_000_000 - reservation.requested_milli
        assert len(requests) == 1
    finally:
        respond.set()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


verified_model_catalog = chain.verified_model_catalog


@pytest.mark.asyncio
async def test_mysql_sync_lock_wait_blocks_same_event_loop_http_even_with_thread_owned_session(fresh_chain):
    """Real sync DB waiting stalls unrelated asyncio tasks until lock timeout."""
    import time

    from sqlalchemy.exc import OperationalError

    from app.models.enterprise_org import EnterpriseMember

    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, _owner, user, _, _ = _seed(factory)
    locked, release = threading.Event(), threading.Event()

    def hold_member():
        with factory.begin() as db:
            db.query(EnterpriseMember).filter_by(enterprise_id=company, user_id=user).with_for_update().one()
            locked.set()
            assert release.wait(8)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        owner = pool.submit(hold_member)
        assert await asyncio.to_thread(locked.wait, 5)
        start = time.monotonic()

        async def send_after_timer():
            await asyncio.sleep(0.05)
            start_delay = time.monotonic() - start
            async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
                response = await client.post(f"http://127.0.0.1:{server.server_port}/", json={"model": "test"})
            release.set()
            return start_delay, response.status_code

        async def wait_synchronously():
            await asyncio.sleep(0.01)
            with pytest.raises(OperationalError) as error:
                with factory.begin() as db:
                    db.execute(text("SET SESSION innodb_lock_wait_timeout=1"))
                    db.query(EnterpriseMember).filter_by(enterprise_id=company, user_id=user).with_for_update().one()
            assert error.value.orig.args[0] == 1205
            return time.monotonic() - start

        blocked, (start_delay, status) = await asyncio.gather(wait_synchronously(), send_after_timer())
        assert blocked >= 0.9 and start_delay >= 0.9 and status == 200
        await asyncio.to_thread(owner.result, 5)
    finally:
        release.set()
        pool.shutdown(wait=True, cancel_futures=True)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


def test_mysql_future_subscription_refund_can_start_after_real_reservation_and_shared_guard(
    fresh_chain, verified_model_catalog,
):
    """A future renewal refund bypasses a company-share-only send guard.

    The reservation is real, but no model or payment provider is contacted.
    """
    from app.models.billing_ledger import BillingReservation
    from app.models.enterprise_org import Enterprise
    from app.services import billing_checkout_service as checkout
    from app.services import billing_ledger_service as ledger

    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    suffix = uuid4().hex[:12]
    with factory.begin() as db:
        buyer = User(username=f"future-buyer-{suffix}", password="x", role="user", status=1)
        reviewer = User(username=f"future-reviewer-{suffix}", password="x", role="user", status=1)
        executor = User(username=f"future-executor-{suffix}", password="x", role="user", status=1)
        db.add_all([buyer, reviewer, executor])
        db.flush()
        initial = checkout.create_order(db, user_id=buyer.id, product="enterprise_create", quantity=1,
                                        channel="alipay", enterprise_name="未来续费退款测试")
        checkout._apply_verified(db, initial, {"trade_id": f"future-initial-{suffix}"})
        company = initial.enterprise_id
        buyer_id, reviewer_id, executor_id = buyer.id, reviewer.id, executor.id
    with factory.begin() as db:
        renewal = checkout.create_order(db, user_id=buyer_id, product="enterprise_renew", quantity=1,
                                        channel="alipay", enterprise_id=company)
        checkout._apply_verified(db, renewal, {"trade_id": f"future-renewal-{suffix}"})
        refund = checkout.request_refund(db, order_no=renewal.order_no, requester_id=buyer_id, reason="未来周期测试")
        assert refund.status == "manual_review"
        wallet = ledger.get_or_create_wallet(db, "enterprise", company)
        wallet_id, refund_no = wallet.id, refund.refund_no
    with factory.begin() as db:
        room = chat.open_conversation(db, company, buyer_id, "company").id
    request_key = f"future-refund-{suffix}"
    with factory.begin() as db:
        reservation = ledger.reserve(
            db, request_key=request_key, model_name="deepseek-flash", user_id=buyer_id, max_milli=100,
            enterprise_wallet_id=wallet_id, enterprise_id=company,
            provider_identity=ledger.BillingProviderIdentity(
                "deepseek-official-v1", "deepseek", "api.deepseek.com", "system",
            ),
        )
        assert reservation.status == "reserved"

    def approve_then_prepare():
        with factory.begin() as db:
            result = checkout.review_manual_refund(db, refund_no=refund_no, admin_id=reviewer_id,
                                                   decision="approve", reason="未来周期测试")
            assert result.status == "approved"
        with factory.begin() as db:
            result, _ = checkout.prepare_refund(db, refund_no=refund_no, admin_id=executor_id)
            assert result.status == "processing"

    with factory.begin() as guard:
        guard.query(Enterprise).filter_by(id=company).populate_existing().with_for_update(read=True).one()
        snapshot = billing_group.capture(guard, enterprise_id=company, user_id=buyer_id,
                                         conversation_id=room, lock_company=False)
        assert snapshot.group_id is None
        with factory() as verify:
            assert not checkout.enterprise_subscription_refund_pending(verify, company)
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            pool.submit(approve_then_prepare).result(timeout=5)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        with factory() as verify:
            assert checkout.enterprise_subscription_refund_pending(verify, company)
            assert verify.query(BillingReservation).filter_by(request_key=request_key).one().status == "reserved"


def test_mysql_independent_members_can_capture_same_room_and_group_concurrently(fresh_chain):
    """Group and room are read-only shared authority; different users need not serialize."""
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, owner, first, group, _ = _seed(factory)
    with factory.begin() as db:
        second = User(username=f"capture-peer-{uuid4().hex[:12]}", password="x", role="user", status=1)
        db.add(second)
        db.flush()
        second_id = second.id
    with factory.begin() as db:
        invitation = org.create_invitation(db, company, owner)
    with factory.begin() as db:
        org.accept_invitation(db, invitation, second_id)
    with factory.begin() as db:
        for user_id in (first, second_id):
            org.set_group_member(db, company, group, owner, user_id, "member")
            org.set_default_group(db, company, owner, user_id, group)
    with factory.begin() as db:
        room = chat.open_conversation(db, company, owner, "company").id
    held, release, other_done = threading.Event(), threading.Event(), threading.Event()
    connection_ids = []

    def first_capture():
        with factory.begin() as db:
            assert billing_group.capture(db, enterprise_id=company, user_id=first, conversation_id=room,
                                         lock_company=False).group_id == group
            held.set()
            assert release.wait(8)

    def second_capture():
        with factory.begin() as db:
            connection_ids.append(db.execute(text("SELECT CONNECTION_ID()")).scalar_one())
            assert billing_group.capture(db, enterprise_id=company, user_id=second_id, conversation_id=room,
                                         lock_company=False).group_id == group
            other_done.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_result = pool.submit(first_capture)
        try:
            assert held.wait(5)
            second_result = pool.submit(second_capture)
            if not other_done.wait(2):
                with fresh_chain.connect() as observer:
                    wait_table = observer.execute(text(
                        "SELECT l.OBJECT_NAME FROM performance_schema.data_lock_waits w "
                        "JOIN performance_schema.threads t ON t.THREAD_ID=w.REQUESTING_THREAD_ID "
                        "JOIN performance_schema.data_locks l ON l.ENGINE_LOCK_ID=w.REQUESTING_ENGINE_LOCK_ID "
                        "WHERE t.PROCESSLIST_ID=:cid LIMIT 1"
                    ), {"cid": connection_ids[0]}).scalar_one_or_none()
                pytest.fail(f"不同成员的只读归属校验发生锁等待: {wait_table}")
        finally:
            release.set()
        first_result.result(timeout=5)
        second_result.result(timeout=5)


def test_mysql_shared_group_read_still_blocks_concurrent_group_deactivation(fresh_chain):
    """Synthetic status update checks lock compatibility; it is not a public API."""
    from app.models.enterprise_org import EnterpriseGroup

    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, owner, user, group, _ = _seed(factory)
    with factory.begin() as db:
        org.set_group_member(db, company, group, owner, user, "member")
        org.set_default_group(db, company, owner, user, group)
    with factory.begin() as db:
        room = chat.open_conversation(db, company, owner, "company").id
    held, release, attempted, done = (threading.Event() for _ in range(4))
    connection_ids = []

    def reader():
        with factory.begin() as db:
            assert billing_group.capture(db, enterprise_id=company, user_id=user,
                                         conversation_id=room, lock_company=False).group_id == group
            held.set()
            assert release.wait(8)

    def deactivate():
        with factory.begin() as db:
            connection_ids.append(db.execute(text("SELECT CONNECTION_ID()")).scalar_one())
            attempted.set()
            group_row = db.query(EnterpriseGroup).filter_by(id=group).with_for_update().one()
            group_row.status = "inactive"
        done.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        reader_result = pool.submit(reader)
        try:
            assert held.wait(5)
            writer = pool.submit(deactivate)
            assert attempted.wait(5)
            for _ in range(200):
                with fresh_chain.connect() as observer:
                    table = observer.execute(text(
                        "SELECT l.OBJECT_NAME FROM performance_schema.data_lock_waits w "
                        "JOIN performance_schema.threads t ON t.THREAD_ID=w.REQUESTING_THREAD_ID "
                        "JOIN performance_schema.data_locks l ON l.ENGINE_LOCK_ID=w.REQUESTING_ENGINE_LOCK_ID "
                        "WHERE t.PROCESSLIST_ID=:cid LIMIT 1"
                    ), {"cid": connection_ids[0]}).scalar_one_or_none()
                if table:
                    break
                threading.Event().wait(0.02)
            assert table == "enterprise_group" and not done.is_set()
        finally:
            release.set()
        reader_result.result(timeout=5)
        writer.result(timeout=5)
    with factory() as db:
        with pytest.raises(billing_group.DefaultBillingGroupRequired):
            billing_group.capture(db, enterprise_id=company, user_id=user, conversation_id=room, lock_company=False)
