"""Search/Fetch concurrency against the isolated fresh-chain MySQL fixture."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import uuid4

import httpx
import pytest
import test_fresh_database_chain as chain
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from app.models.billing_ledger import BillingCreditBatch, BillingReservation, BillingToolExecutionResult
from app.services import billing_ledger_service as ledger
from app.services import billing_rate_catalog_service as catalog
from app.services import billing_search_fetch_service as service

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
    barrier = Barrier(2)

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
