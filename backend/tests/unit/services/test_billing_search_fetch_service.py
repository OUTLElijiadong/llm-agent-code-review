"""DeepSeek Search/Fetch transport and billable outcome contract tests."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from app.models.billing_ledger import BillingLimit, BillingReservation, BillingToolExecutionResult
from app.services import billing_ledger_service as ledger
from app.services import billing_rate_catalog_service as catalog
from app.services import billing_search_fetch_service as service
from app.services.billing_ledger_service import get_or_create_wallet, grant

NOW = datetime(2026, 11, 7, 6, 0, tzinfo=timezone.utc)
PROVIDER = {
    "provider_name": "deepseek",
    "base_url": "https://api.deepseek.com",
    "credential_source": "system",
    "api_key": "sk-test-tool-key",
}


def _session_factory(db):
    return sessionmaker(bind=db.get_bind(), autocommit=False, autoflush=False, expire_on_commit=False)


def _prepare(db, monkeypatch, *, company_balance=10_000, limit_balance=10_000):
    monkeypatch.setattr(ledger, "_now", lambda: NOW.replace(tzinfo=None) + timedelta(hours=3))
    effective_at = NOW + timedelta(hours=1)
    draft = catalog.create_draft(
        db,
        actor_id=10,
        effective_at=effective_at,
        source_url="https://api-docs.deepseek.com/quick_start/pricing/",
        source_checked_at=NOW - timedelta(minutes=30),
        model_cny_source_url="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
        model_cny_source_checked_at=NOW - timedelta(minutes=30),
        fx_source_url="https://www.boc.cn/sourcedb/whpj/index.html",
        fx_source_checked_at=NOW - timedelta(minutes=30),
        fx_cny_per_usd_scaled=67_168,
        tool_cny_source_url="https://api-docs.deepseek.com/zh-cn/guides/search/",
        tool_cny_source_checked_at=NOW - timedelta(minutes=30),
        tool_usd_source_url="https://api-docs.deepseek.com/quick_start/pricing/",
        tool_usd_source_checked_at=NOW - timedelta(minutes=30),
        tool_search_cost_cny_micros_per_success=5_038,
        tool_search_price_milli_per_success=1_800,
        tool_fetch_cost_cny_micros_per_success=5_038,
        tool_fetch_price_milli_per_success=1_800,
        change_note="Search/Fetch 传输服务测试。",
        items=catalog.baseline_items(),
        now=NOW,
    )
    catalog.approve_draft(db, catalog_id=draft.id, actor_id=11, now=NOW)
    wallet = get_or_create_wallet(db, "enterprise", 7)
    grant(db, wallet_id=wallet.id, grant_key="search-fetch-test-credit", kind="purchase",
          amount_milli=company_balance)
    db.add(BillingLimit(
        limit_key="search-fetch-test-limit",
        enterprise_id=7,
        category="normal",
        group_id=9,
        user_id=42,
        period_start=(NOW - timedelta(days=1)).replace(tzinfo=None),
        period_end=(NOW + timedelta(days=31)).replace(tzinfo=None),
        limit_milli=limit_balance,
        used_milli=0,
        reserved_milli=0,
    ))
    db.commit()
    return _session_factory(db), int(wallet.id)


def _authorize(_db):
    return None


def _patch_transport(monkeypatch, response_or_error, captured=None):
    original_client = httpx.AsyncClient
    calls = []

    async def handler(request):
        calls.append(request)
        if captured is not None:
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            captured["body"] = json.loads(request.content)
        if isinstance(response_or_error, Exception):
            raise response_or_error
        if callable(response_or_error):
            value = response_or_error(request)
            return value
        return response_or_error

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs):
        if captured is not None:
            captured["client_options"] = dict(kwargs)
        return original_client(transport=transport, **kwargs)

    monkeypatch.setattr(service.httpx, "AsyncClient", client_factory)
    return calls


def _execute(factory, wallet_id, *, request_key, tool="search", arguments=None, authorize=_authorize):
    return asyncio.run(service.execute_tool(
        session_factory=factory,
        request_key=request_key,
        tool_name=tool,
        arguments=arguments or {"query": "DeepSeek 发布"},
        user_id=42,
        enterprise_id=7,
        enterprise_wallet_id=wallet_id,
        group_id=9,
        authorize=authorize,
        **PROVIDER,
    ))


def _reservation(factory, key):
    with factory() as db:
        return db.query(BillingReservation).filter_by(request_key=key).one()


def test_search_success_uses_fixed_endpoint_caps_results_and_charges_once(db, monkeypatch):
    factory, wallet_id = _prepare(db, monkeypatch)
    captured = {}
    _patch_transport(monkeypatch, httpx.Response(200, json={"results": [
        {"title": "结果", "url": "https://example.com/a", "snippet": "摘要", "summary": "正文"},
    ]}), captured)

    result = _execute(factory, wallet_id, request_key="search-success")

    assert result.status == "success" and result.billable is True
    assert result.payload["results"][0]["url"] == "https://example.com/a"
    assert captured["url"] == "https://api.deepseek.com/v1/tools/search"
    assert captured["body"] == {"query": "DeepSeek 发布", "count": 5, "need_content": False}
    assert captured["headers"]["authorization"] == "Bearer sk-test-tool-key"
    assert captured["client_options"]["trust_env"] is False
    assert captured["client_options"]["follow_redirects"] is False
    row = _reservation(factory, "search-success")
    assert (row.status, row.tool_successful_calls, row.actual_milli) == ("settled", 1, 1_800)
    saved = factory().query(BillingToolExecutionResult).filter_by(request_key="search-success").one()
    assert json.loads(saved.result_json)["results"][0]["title"] == "结果"
    assert len(saved.input_sha256) == 64

    # A checkpoint replay returns the durable sanitized result without a second provider request.
    calls = _patch_transport(monkeypatch, httpx.ReadTimeout("must not be called"))
    replay = _execute(factory, wallet_id, request_key="search-success")
    assert replay == result
    assert calls == []

    with pytest.raises(service.SearchFetchReplayBlocked, match="参数与已保存结果不一致"):
        _execute(factory, wallet_id, request_key="search-success", arguments={"query": "another query"})
    assert calls == []
    assert _reservation(factory, "search-success").status == "settled"


@pytest.mark.parametrize(
    "response, expected_status",
    [
        (httpx.Response(200, json={"results": []}), "empty"),
        (httpx.Response(429, json={"error": {"message": "private provider detail"}}), "provider_error"),
    ],
)
def test_empty_or_http_error_search_releases_without_billing(db, monkeypatch, response, expected_status):
    factory, wallet_id = _prepare(db, monkeypatch)
    _patch_transport(monkeypatch, response)

    result = _execute(factory, wallet_id, request_key=f"search-{expected_status}")

    assert result.status == expected_status and result.billable is False
    assert "private provider detail" not in str(result.payload)
    row = _reservation(factory, f"search-{expected_status}")
    assert (row.status, row.usage_state, row.tool_successful_calls, row.actual_milli) == (
        "released", "not_billable", 0, 0,
    )


def test_timeout_and_malformed_http_200_stay_unknown_and_are_never_retried(db, monkeypatch):
    factory, wallet_id = _prepare(db, monkeypatch)
    calls = _patch_transport(monkeypatch, httpx.ReadTimeout("private timeout detail"))

    with pytest.raises(service.SearchFetchOutcomeUnknown, match="结果未知") as error:
        _execute(factory, wallet_id, request_key="search-timeout")

    assert "private timeout detail" not in str(error.value)
    assert len(calls) == 1
    row = _reservation(factory, "search-timeout")
    assert (row.status, row.usage_state, row.actual_milli) == ("unknown", "unknown", None)

    _patch_transport(monkeypatch, httpx.Response(200, content=b"not-json"))
    with pytest.raises(service.SearchFetchOutcomeUnknown, match="冻结"):
        _execute(factory, wallet_id, request_key="search-malformed")
    malformed = _reservation(factory, "search-malformed")
    assert malformed.status == "unknown" and malformed.provider_attempt_started_at is not None


def test_total_response_deadline_stops_slow_chunked_body_and_keeps_hold_unknown(db, monkeypatch):
    factory, wallet_id = _prepare(db, monkeypatch)
    monkeypatch.setattr(service, "_REQUEST_TOTAL_TIMEOUT_SECONDS", 0.09)

    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in (b'{"results":[', b'{"title":"a"},', b'{"title":"b"},', b'{"title":"c"}]}'):
                yield chunk
                await asyncio.sleep(0.04)

        async def aclose(self):
            return None

    calls = _patch_transport(monkeypatch, lambda _request: httpx.Response(200, stream=SlowBody()))
    with pytest.raises(service.SearchFetchOutcomeUnknown, match="总时限"):
        _execute(factory, wallet_id, request_key="search-slow-stream")

    assert len(calls) == 1
    row = _reservation(factory, "search-slow-stream")
    assert row.status == "unknown" and row.actual_milli is None
    with factory() as session:
        assert session.query(BillingToolExecutionResult).filter_by(request_key="search-slow-stream").count() == 0


def test_fetch_validates_url_before_reservation_and_bounds_body_after_billable_success(db, monkeypatch):
    factory, wallet_id = _prepare(db, monkeypatch)
    with pytest.raises(service.SearchFetchValidationError, match="非公网 IP"):
        _execute(factory, wallet_id, request_key="fetch-private", tool="fetch",
                 arguments={"url": "http://127.0.0.1/admin"})
    with factory() as session:
        assert session.query(BillingReservation).filter_by(request_key="fetch-private").count() == 0

    _patch_transport(monkeypatch, httpx.Response(200, json={"title": "示例", "content": "文" * 12_001}))
    result = _execute(factory, wallet_id, request_key="fetch-success", tool="fetch",
                      arguments={"url": "https://example.com/article"})
    assert result.status == "success" and result.billable is True
    assert len(result.payload["content"]) == 12_000 and result.payload["truncated"] is True
    assert _reservation(factory, "fetch-success").actual_milli == 1_800


def test_final_authorization_failure_releases_as_not_sent_without_http(db, monkeypatch):
    factory, wallet_id = _prepare(db, monkeypatch)
    calls = _patch_transport(monkeypatch, httpx.Response(200, json={"results": []}))
    checks = 0

    def revoke_before_send(_db):
        nonlocal checks
        checks += 1
        if checks == 2:
            raise PermissionError("权限在预占后撤销")

    with pytest.raises(PermissionError, match="撤销"):
        _execute(factory, wallet_id, request_key="search-revoked", authorize=revoke_before_send)
    assert len(calls) == 0
    row = _reservation(factory, "search-revoked")
    assert (row.status, row.usage_state, row.provider_attempt_started_at) == ("released", "not_sent", None)


def test_authorization_is_rechecked_after_provider_response_before_settlement(db, monkeypatch):
    factory, wallet_id = _prepare(db, monkeypatch)
    calls = _patch_transport(monkeypatch, httpx.Response(200, json={"results": [{"title": "paid"}]}))
    checks = 0

    def revoke_after_response(_db):
        nonlocal checks
        checks += 1
        if checks == 3:
            raise PermissionError("运行租约已被恢复操作 fencing")

    with pytest.raises(service.SearchFetchOutcomeUnknown, match="待人工对账"):
        _execute(factory, wallet_id, request_key="search-late-response", authorize=revoke_after_response)

    assert checks == 3 and len(calls) == 1
    row = _reservation(factory, "search-late-response")
    assert row.status == "unknown" and row.actual_milli is None
    with factory() as session:
        assert session.query(BillingToolExecutionResult).filter_by(request_key="search-late-response").count() == 0


def test_late_response_cannot_settle_after_replay_marks_attempt_unknown(db, monkeypatch):
    factory, wallet_id = _prepare(db, monkeypatch)

    def replay_while_provider_request_is_in_flight(_request):
        with factory.begin() as session:
            ledger.mark_unknown(session, "search-replay-race")
        return httpx.Response(200, json={"results": [{"title": "late"}]})

    calls = _patch_transport(monkeypatch, replay_while_provider_request_is_in_flight)
    with pytest.raises(service.SearchFetchOutcomeUnknown):
        _execute(factory, wallet_id, request_key="search-replay-race")
    assert len(calls) == 1
    row = _reservation(factory, "search-replay-race")
    assert (row.status, row.actual_milli) == ("unknown", None)
    with factory() as session:
        assert session.query(BillingToolExecutionResult).filter_by(request_key="search-replay-race").count() == 0


def test_saved_result_does_not_lock_the_missing_index_gap(db, monkeypatch):
    factory, wallet_id = _prepare(db, monkeypatch)
    _patch_transport(monkeypatch, httpx.Response(200, json={"results": [{"title": "saved"}]}))
    _execute(factory, wallet_id, request_key="search-current-read")
    locking_flags = []

    def observe(orm_state):
        statement = orm_state.statement
        if orm_state.is_select and "billing_tool_execution_result" in str(statement):
            locking_flags.append(statement._for_update_arg is not None)

    with factory() as session:
        event.listen(session, "do_orm_execute", observe)
        result = service.load_saved_result(session, "search-current-read", "search", {"query": "DeepSeek 发布"})
    assert result is not None
    assert locking_flags == [False]


def test_replay_requires_matching_payer_before_returning_cached_result(db, monkeypatch):
    factory, wallet_id = _prepare(db, monkeypatch)
    calls = _patch_transport(monkeypatch, httpx.Response(200, json={"results": [{"title": "private"}]}))
    _execute(factory, wallet_id, request_key="search-other-tenant")
    with pytest.raises(service.SearchFetchOutcomeUnknown, match="账户"):
        asyncio.run(service.execute_tool(
            session_factory=factory, request_key="search-other-tenant", tool_name="search",
            arguments={"query": "DeepSeek 发布"}, user_id=43, enterprise_id=7,
            enterprise_wallet_id=wallet_id, group_id=9, authorize=_authorize, **PROVIDER,
        ))
    assert len(calls) == 1


def test_terminal_result_missing_from_old_snapshot_is_reloaded_in_fresh_session(db, monkeypatch):
    factory, wallet_id = _prepare(db, monkeypatch)
    calls = _patch_transport(monkeypatch, httpx.Response(200, json={"results": [{"title": "saved"}]}))
    original_result = _execute(factory, wallet_id, request_key="search-old-snapshot")
    original_lookup = service._existing_result
    sessions = []

    def old_snapshot_once(session, *args):
        sessions.append(session)
        if len(sessions) == 1:
            return None
        return original_lookup(session, *args)

    monkeypatch.setattr(service, "_existing_result", old_snapshot_once)
    assert _execute(factory, wallet_id, request_key="search-old-snapshot") == original_result
    assert len(sessions) == 2 and sessions[0] is not sessions[1] and len(calls) == 1


def test_terminal_reservation_without_result_stays_nonretryable(db, monkeypatch):
    factory, wallet_id = _prepare(db, monkeypatch)
    calls = _patch_transport(monkeypatch, httpx.Response(200, json={"results": [{"title": "saved"}]}))
    _execute(factory, wallet_id, request_key="search-missing-result")
    with factory.begin() as session:
        session.query(BillingToolExecutionResult).filter_by(request_key="search-missing-result").delete()
    with pytest.raises(service.SearchFetchReplayBlocked, match="未保存可恢复结果"):
        _execute(factory, wallet_id, request_key="search-missing-result")
    assert _reservation(factory, "search-missing-result").status == "settled" and len(calls) == 1


def test_idempotency_conflict_retries_once_with_a_fresh_session(db, monkeypatch):
    factory, wallet_id = _prepare(db, monkeypatch)
    calls = _patch_transport(monkeypatch, httpx.Response(200, json={"results": [{"title": "ok"}]}))
    original = ledger.reserve_tool
    attempts = []

    def transient_conflict(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise ledger.IdempotencyConflict("simulate a row committed after the old snapshot")
        return original(*args, **kwargs)

    monkeypatch.setattr(ledger, "reserve_tool", transient_conflict)
    result = _execute(factory, wallet_id, request_key="search-fresh-snapshot")
    assert result.status == "success" and len(attempts) == 2 and len(calls) == 1


def test_finalization_authorizes_before_locking_wallet_and_saving_result(db, monkeypatch):
    factory, wallet_id = _prepare(db, monkeypatch)
    _patch_transport(monkeypatch, httpx.Response(200, json={"results": [{"title": "paid"}]}))
    sequence = []
    original_lock = service._lock_tool_reservation

    def authorize(_db):
        sequence.append("authorize")

    def lock(session, request_key):
        sequence.append("reservation")
        return original_lock(session, request_key)

    monkeypatch.setattr(service, "_lock_tool_reservation", lock)
    result = _execute(factory, wallet_id, request_key="search-final-lock-order", authorize=authorize)
    assert result.billable is True
    assert sequence == ["authorize", "authorize", "authorize", "reservation"]
    assert _reservation(factory, "search-final-lock-order").status == "settled"


def test_replayed_started_reservation_is_frozen_unknown_without_second_http_call(db, monkeypatch):
    factory, wallet_id = _prepare(db, monkeypatch)
    with factory.begin() as session:
        ledger.reserve_tool(
            session,
            request_key="search-abandoned-attempt",
            tool_name="search",
            user_id=42,
            enterprise_id=7,
            enterprise_wallet_id=wallet_id,
            group_id=9,
            provider_identity=ledger.resolve_billing_tool_provider_identity(
                provider_name="deepseek", base_url="https://api.deepseek.com", credential_source="system",
            ),
        )
    with factory.begin() as session:
        ledger.mark_tool_attempt_started(session, "search-abandoned-attempt", now=NOW)
    calls = _patch_transport(monkeypatch, httpx.Response(200, json={"results": []}))

    with pytest.raises(service.SearchFetchOutcomeUnknown, match="待人工对账"):
        _execute(factory, wallet_id, request_key="search-abandoned-attempt")

    assert calls == []
    row = _reservation(factory, "search-abandoned-attempt")
    assert row.status == "unknown" and row.provider_attempt_started_at is not None


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": " "},
        {"query": "a" * 129},
        {"query": "valid", "count": 50},
    ],
)
def test_search_rejects_invalid_or_client_controlled_cost_fields_before_reserving(db, monkeypatch, arguments):
    factory, wallet_id = _prepare(db, monkeypatch)
    with pytest.raises(service.SearchFetchValidationError):
        _execute(factory, wallet_id, request_key="search-invalid", arguments=arguments)
    with factory() as session:
        assert session.query(BillingReservation).filter_by(request_key="search-invalid").count() == 0


def test_provider_identity_rejects_custom_host_and_user_credentials(db, monkeypatch):
    factory, wallet_id = _prepare(db, monkeypatch)
    _patch_transport(monkeypatch, httpx.Response(200, json={"results": []}))

    with pytest.raises(ledger.BillingError, match="官方 HTTPS"):
        asyncio.run(service.execute_tool(
            session_factory=factory,
            request_key="search-custom-host",
            tool_name="search",
            arguments={"query": "test"},
            user_id=42,
            enterprise_id=7,
            enterprise_wallet_id=wallet_id,
            group_id=9,
            provider_name="deepseek",
            base_url="https://attacker.example",
            credential_source="system",
            api_key="sk-test-tool-key",
            authorize=_authorize,
        ))
    with pytest.raises(ledger.BillingError, match="系统或全局托管凭据"):
        asyncio.run(service.execute_tool(
            session_factory=factory,
            request_key="search-user-key",
            tool_name="search",
            arguments={"query": "test"},
            user_id=42,
            enterprise_id=7,
            enterprise_wallet_id=wallet_id,
            group_id=9,
            provider_name="deepseek",
            base_url="https://api.deepseek.com",
            credential_source="user",
            api_key="sk-test-tool-key",
            authorize=_authorize,
        ))
