"""Search/Fetch concurrency against the isolated fresh-chain MySQL fixture."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import test_fresh_database_chain as chain
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker

from app.core.exceptions import ForbiddenError
from app.models.billing_ledger import BillingCreditBatch, BillingLimit, BillingReservation, BillingToolExecutionResult
from app.models.enterprise_org import EnterpriseMember
from app.models.user import User
from app.services import billing_ledger_service as ledger
from app.services import billing_rate_catalog_service as catalog
from app.services import billing_search_fetch_service as service
from app.services import enterprise_assistant_billing_group as billing_group
from app.services import enterprise_chat_service
from app.services import enterprise_org_service as org

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


def _setup(fresh_chain, monkeypatch, enterprise_ids):
    monkeypatch.setattr(catalog, "tool_rate_snapshot", lambda *_a, **_kw: ("test-rate", 5038, 1800))
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    wallets = {}
    with sessions.begin() as db:
        for enterprise_id in enterprise_ids:
            wallet = ledger.get_or_create_wallet(db, "enterprise", enterprise_id)
            ledger.grant(db, wallet_id=wallet.id, grant_key=f"sf-test:{uuid4().hex}",
                         kind="purchase", amount_milli=10_000)
            wallets[enterprise_id] = wallet.id
    return sessions, wallets


def _invoke(sessions, wallet_id, enterprise_id, key, authorize=lambda _db: None):
    return asyncio.run(service.execute_tool(
        session_factory=sessions, request_key=key, tool_name="search", arguments={"query": "test"},
        user_id=enterprise_id, enterprise_id=enterprise_id, enterprise_wallet_id=wallet_id,
        group_id=None, provider_name="deepseek", base_url="https://api.deepseek.com",
        credential_source="system", api_key="sk-test", authorize=authorize,
    ))


def test_mysql_different_enterprises_complete_search_without_gap_deadlock(fresh_chain, monkeypatch):
    ids = [610_001, 610_002]
    sessions, wallets = _setup(fresh_chain, monkeypatch, ids)
    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, json={"results": [{"title": "ok"}]}))
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kw: original_client(transport=transport, **kw))
    batch_lock_waits = []

    def observe_batch_lock_waits():
        with fresh_chain.connect() as observer:
            count = observer.execute(text(
                "SELECT COUNT(*) FROM performance_schema.data_lock_waits w "
                "JOIN performance_schema.data_locks l "
                "ON l.ENGINE_LOCK_ID = w.REQUESTING_ENGINE_LOCK_ID "
                "WHERE l.OBJECT_SCHEMA = DATABASE() AND l.OBJECT_NAME = 'billing_credit_batch'"
            )).scalar_one()
        batch_lock_waits.append(int(count))

    barrier = Barrier(2, action=observe_batch_lock_waits)

    def synchronize_insert(_conn, _cursor, statement, _params, _ctx, _many):
        if statement.lstrip().upper().startswith("INSERT INTO BILLING_RESERVATION "):
            barrier.wait(timeout=10)

    event.listen(fresh_chain, "before_cursor_execute", synchronize_insert)
    prefix = f"sf-gap:{uuid4().hex}:"
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(_invoke, sessions, wallets[org], org, prefix + str(org)) for org in ids]
            results = [future.result(timeout=20) for future in futures]
    finally:
        event.remove(fresh_chain, "before_cursor_execute", synchronize_insert)
    assert [result.status for result in results] == ["success", "success"]
    # Sample while both transactions hold their pre-insert locks at the barrier.
    assert batch_lock_waits == [0]
    with sessions() as db:
        assert db.query(BillingReservation).filter(BillingReservation.request_key.startswith(prefix)).count() == 2
        assert db.query(BillingToolExecutionResult).filter(
            BillingToolExecutionResult.request_key.startswith(prefix),
        ).count() == 2


def test_mysql_old_rr_snapshot_retries_terminal_result_without_another_call(fresh_chain, monkeypatch):
    org = 610_003
    sessions, wallets = _setup(fresh_chain, monkeypatch, [org])
    key = f"sf-old-snapshot:{uuid4().hex}"
    provider = ledger.resolve_billing_tool_provider_identity(
        provider_name="deepseek", base_url="https://api.deepseek.com", credential_source="system",
    )
    with sessions.begin() as db:
        ledger.reserve_tool(db, request_key=key, tool_name="search", user_id=org, enterprise_id=org,
                            enterprise_wallet_id=wallets[org], group_id=None, provider_identity=provider)
    with sessions.begin() as db:
        ledger.mark_tool_attempt_started(db, key)
    checks = []
    digest = service._input_sha256("search", service.validate_arguments("search", {"query": "test"}))

    def authorize(db):
        checks.append(1)
        if len(checks) == 1:
            assert db.query(BillingReservation).filter_by(request_key=key).one().status == "reserved"
            service._finalize_result(
                sessions, key, service.SearchFetchResult(
                    tool_name="search", status="success", billable=True, payload={"results": [{"title": "saved"}]},
                ), digest, lambda _db: None,
            )

    def unexpected_http(**_kwargs):
        raise AssertionError("replayed result must not make an HTTP request")

    monkeypatch.setattr(service.httpx, "AsyncClient", unexpected_http)
    result = _invoke(sessions, wallets[org], org, key, authorize)
    assert result.status == "success" and result.payload["results"][0]["title"] == "saved"
    assert len(checks) == 2


@pytest.mark.parametrize(("tool_name", "arguments"), [
    ("search", {"query": "默认小组授权变更"}),
    ("fetch", {"url": "https://example.com/page"}),
])
def test_mysql_default_group_aba_releases_search_fetch_before_provider_send(
    fresh_chain, monkeypatch, tool_name, arguments,
):
    """A real MySQL RR default-group ABA cannot pass the final tool send fence."""
    suffix = uuid4().hex[:12]
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with sessions.begin() as db:
        owner = User(username=f"sf-aba-owner-{suffix}", password="x", role="user", status=1)
        member = User(username=f"sf-aba-member-{suffix}", password="x", role="user", status=1)
        db.add_all([owner, member])
        db.flush()
        company = org.create_enterprise_from_paid_order(
            db, owner_id=int(owner.id), name=f"Search Fetch ABA {suffix}", seat_limit=3,
            paid_order_id=f"sf-aba-{suffix}",
        )
        enterprise_id, owner_id, member_id = int(company.id), int(owner.id), int(member.id)
        db.add(EnterpriseMember(
            enterprise_id=enterprise_id, user_id=member_id, role="member", status="active",
        ))
        db.flush()
        first = org.create_group(db, enterprise_id, owner_id, f"甲组-{suffix}")
        second = org.create_group(db, enterprise_id, owner_id, f"乙组-{suffix}")
        first_id, second_id = int(first.id), int(second.id)
        org.set_group_member(db, enterprise_id, first_id, owner_id, member_id, "member")
        org.set_group_member(db, enterprise_id, second_id, owner_id, member_id, "member")
        org.set_default_group(db, enterprise_id, owner_id, member_id, first_id)

    with sessions() as db:
        room = enterprise_chat_service.open_conversation(db, enterprise_id, member_id, "company")
        room_id = int(room.id)
    with sessions.begin() as db:
        snapshot = billing_group.capture(
            db, enterprise_id=enterprise_id, user_id=member_id, conversation_id=room_id,
        )
        assert snapshot.group_id == first_id
        run = SimpleNamespace(
            enterprise_id=enterprise_id,
            initiator_user_id=member_id,
            conversation_id=room_id,
            group_id=None,
            billing_attribution_version=1,
            billing_group_id=first_id,
            billing_membership_version=snapshot.membership_version,
            billing_user_token_version=snapshot.user_token_version,
        )

    sessions, wallets = _setup(fresh_chain, monkeypatch, [enterprise_id])
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with sessions.begin() as db:
        db.add(BillingLimit(
            limit_key=f"sf-aba-limit:{suffix}", enterprise_id=enterprise_id, category="normal",
            group_id=first_id, user_id=member_id, period_start=now - timedelta(days=1),
            period_end=now + timedelta(days=31), limit_milli=10_000, used_milli=0, reserved_milli=0,
        ))

    monkeypatch.setattr(
        "app.services.enterprise_assistant_runtime_service._CROSS_SCOPE_ATTRIBUTION_VERIFIED", True,
    )
    request_key = f"sf-aba:{suffix}:{tool_name}"
    outbound = []
    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: (outbound.append(request),
                                                       httpx.Response(200, json={"results": []}))[1])
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kw: original_client(transport=transport, **kw))
    checks = 0

    def authorize(db):
        nonlocal checks
        checks += 1
        if checks == 2:
            # The reservation is committed. Change the real member through both
            # intermediate states before the final authorization/read-current fence.
            with sessions.begin() as mutation:
                org.set_default_group(mutation, enterprise_id, owner_id, member_id, second_id)
            with sessions.begin() as mutation:
                org.set_default_group(mutation, enterprise_id, owner_id, member_id, first_id)
        return billing_group.validate(db, run)

    with pytest.raises(ForbiddenError, match="计费归属已变化"):
        asyncio.run(service.execute_tool(
            session_factory=sessions, request_key=request_key, tool_name=tool_name, arguments=arguments,
            user_id=member_id, enterprise_id=enterprise_id, enterprise_wallet_id=wallets[enterprise_id],
            group_id=first_id, provider_name="deepseek", base_url="https://api.deepseek.com",
            credential_source="system", api_key="sk-test", authorize=authorize,
        ))

    assert checks == 2
    assert outbound == []
    with sessions() as db:
        reservation = db.query(BillingReservation).filter_by(request_key=request_key).one()
        batch = db.query(BillingCreditBatch).filter_by(wallet_id=wallets[enterprise_id]).one()
        member_row = db.query(EnterpriseMember).filter_by(
            enterprise_id=enterprise_id, user_id=member_id,
        ).one()
        assert (reservation.enterprise_id, reservation.group_id, reservation.user_id) == (
            enterprise_id, first_id, member_id,
        )
        assert (reservation.status, reservation.usage_state, reservation.provider_attempt_started_at) == (
            "released", "not_sent", None,
        )
        assert (batch.remaining_milli, batch.reserved_milli) == (10_000, 0)
        assert (member_row.default_group_id, member_row.billing_group_version) == (
            first_id, snapshot.membership_version + 2,
        )


def test_mysql_same_key_concurrent_workers_freeze_unresolved_attempt_once(fresh_chain, monkeypatch):
    """Use an authorization stub to stress the service, not real run lease acquisition."""
    org = 610_004
    sessions, wallets = _setup(fresh_chain, monkeypatch, [org])
    key = f"sf-same-key:{uuid4().hex}"
    provider_started, allow_response = Event(), Event()
    outbound = []
    original_client = httpx.AsyncClient

    def handler(_request):
        outbound.append(1)
        provider_started.set()
        assert allow_response.wait(10), "second worker did not finish"
        return httpx.Response(200, json={"results": [{"title": "late"}]})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kw: original_client(transport=transport, **kw))
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(_invoke, sessions, wallets[org], org, key)
            try:
                assert provider_started.wait(10), "first worker did not mark and invoke provider"
                second = pool.submit(_invoke, sessions, wallets[org], org, key)
                with pytest.raises(service.SearchFetchOutcomeUnknown):
                    second.result(timeout=10)
            finally:
                allow_response.set()
            with pytest.raises(service.SearchFetchOutcomeUnknown):
                first.result(timeout=10)
    finally:
        allow_response.set()
    assert len(outbound) == 1
    with sessions() as db:
        row = db.query(BillingReservation).filter_by(request_key=key).one()
        batch = db.query(BillingCreditBatch).filter_by(wallet_id=wallets[org]).one()
        assert (row.status, row.actual_milli) == ("unknown", None)
        assert (batch.remaining_milli, batch.reserved_milli) == (10_000, 1_800)
        assert db.query(BillingToolExecutionResult).filter_by(request_key=key).count() == 0


def test_mysql_same_key_concurrent_replay_returns_one_saved_result(fresh_chain, monkeypatch):
    """A completed provider request can be replayed concurrently without a new call."""
    org = 610_005
    sessions, wallets = _setup(fresh_chain, monkeypatch, [org])
    key = f"sf-same-replay:{uuid4().hex}"
    outbound = []
    original_client = httpx.AsyncClient

    def handler(_request):
        outbound.append(1)
        return httpx.Response(200, json={"results": [{"title": "saved"}]})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kw: original_client(transport=transport, **kw))
    initial = _invoke(sessions, wallets[org], org, key)
    barrier = Barrier(2)

    def replay():
        barrier.wait(timeout=10)
        return _invoke(sessions, wallets[org], org, key)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(replay) for _ in range(2)]
        results = [future.result(timeout=15) for future in futures]
    assert results == [initial, initial] and len(outbound) == 1
    with sessions() as db:
        row = db.query(BillingReservation).filter_by(request_key=key).one()
        batch = db.query(BillingCreditBatch).filter_by(wallet_id=wallets[org]).one()
        assert (row.status, row.actual_milli) == ("settled", 1_800)
        assert (batch.remaining_milli, batch.reserved_milli) == (8_200, 0)
        assert db.query(BillingToolExecutionResult).filter_by(request_key=key).count() == 1
