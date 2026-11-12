"""Search/Fetch按成功请求计费的额度预占、状态转移和企业账本验证。"""

from datetime import datetime, timedelta, timezone
from sqlite3 import IntegrityError as SqliteIntegrityError

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from app.models.billing_ledger import (
    BillingCreditBatch,
    BillingLimit,
    BillingReservation,
    BillingReservationLimit,
    BillingReservationPart,
)
from app.services import billing_rate_catalog_service as catalog
from app.services import billing_reservation_reconciliation_service as reconciliation
from app.services.billing_ledger_service import (
    BillingError,
    BillingProviderIdentity,
    IdempotencyConflict,
    InsufficientCredits,
    available_milli,
    get_or_create_wallet,
    grant,
    mark_tool_attempt_started,
    mark_unknown,
    release_not_sent,
    release_tool_nonbillable,
    reserve_tool,
    settle_tool,
)

UTC = timezone.utc
NOW = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
PROVIDER = BillingProviderIdentity(
    identity_version="deepseek-official-v1",
    provider_name="deepseek",
    endpoint_host="api.deepseek.com",
    credential_source="system",
)
TOOL_SOURCES = {
    "tool_cny_source_url": "https://api-docs.deepseek.com/zh-cn/guides/search/",
    "tool_cny_source_checked_at": NOW - timedelta(minutes=30),
    "tool_usd_source_url": "https://api-docs.deepseek.com/quick_start/pricing/",
    "tool_usd_source_checked_at": NOW - timedelta(minutes=30),
    "tool_search_cost_cny_micros_per_success": 5_038,
    "tool_search_price_milli_per_success": 1_800,
    "tool_fetch_cost_cny_micros_per_success": 5_038,
    "tool_fetch_price_milli_per_success": 1_800,
}


def _enable_tool_prices(db):
    effective_at = NOW + timedelta(hours=2)
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
        change_note="测试 Search/Fetch 按次费率。",
        items=catalog.baseline_items(),
        now=NOW,
        **TOOL_SOURCES,
    )
    catalog.approve_draft(db, catalog_id=draft.id, actor_id=11, now=NOW)
    db.flush()
    return draft


def _wallet(db, owner_type, owner_id, amount, grant_key):
    wallet = get_or_create_wallet(db, owner_type, owner_id)
    grant(db, wallet_id=wallet.id, grant_key=grant_key, kind="purchase", amount_milli=amount)
    db.flush()
    return wallet


def _limit(db, *, enterprise_id=7, group_id=9, user_id=42, amount=5_000):
    row = BillingLimit(
        limit_key=f"tool-limit:{enterprise_id}:{group_id}:{user_id}",
        enterprise_id=enterprise_id,
        group_id=group_id,
        user_id=user_id,
        period_start=(NOW - timedelta(days=1)).replace(tzinfo=None),
        period_end=(NOW + timedelta(days=31)).replace(tzinfo=None),
        limit_milli=amount,
        used_milli=0,
        reserved_milli=0,
    )
    db.add(row)
    db.flush()
    return row


def _reserve(db, *, key="tool-1", tool="search", amount=2_000,
             enterprise_id=7, group_id=9, user_id=42):
    draft = _enable_tool_prices(db)
    company = _wallet(db, "enterprise", enterprise_id, amount, f"company:{key}")
    personal = _wallet(db, "user", user_id, 50_000, f"personal:{key}")
    limit = _limit(db, enterprise_id=enterprise_id, group_id=group_id, user_id=user_id)
    reservation = reserve_tool(
        db,
        request_key=key,
        tool_name=tool,
        user_id=user_id,
        enterprise_id=enterprise_id,
        enterprise_wallet_id=company.id,
        group_id=group_id,
        provider_identity=PROVIDER,
        now=draft.effective_at,
    )
    return draft, company, personal, limit, reservation


def test_tool_reservation_snapshots_cost_and_price_and_holds_only_company_wallet(db):
    _draft, company, personal, limit, reservation = _reserve(db)

    assert (reservation.billable_kind, reservation.tool_name, reservation.model_name) == (
        "search", "search", "deepseek-search",
    )
    assert (reservation.tool_cost_cny_micros_per_success,
            reservation.tool_price_milli_per_success, reservation.requested_milli) == (5_038, 1_800, 1_800)
    assert reservation.pricing_version
    assert reservation.personal_wallet_id is None
    assert reservation.enterprise_wallet_id == company.id
    assert reservation.provider_attempt_started_at is None
    assert available_milli(db, company.id, NOW) == 200
    assert available_milli(db, personal.id, NOW) == 50_000
    assert (limit.used_milli, limit.reserved_milli) == (0, 1_800)
    part = db.query(BillingReservationPart).filter_by(reservation_id=reservation.id).one()
    assert (part.wallet_id, part.reserved_milli, part.charged_milli) == (company.id, 1_800, None)
    limit_part = db.query(BillingReservationLimit).filter_by(reservation_id=reservation.id).one()
    assert limit_part.reserved_milli == 1_800


def test_tool_request_fails_closed_without_price_company_balance_or_monthly_limit(db):
    company = _wallet(db, "enterprise", 7, 1_000, "company-low")
    personal = _wallet(db, "user", 42, 50_000, "personal-full")
    _limit(db)
    draft = _enable_tool_prices(db)
    db.commit()

    with pytest.raises(InsufficientCredits, match="企业积分或适用月额度不足"):
        reserve_tool(db, request_key="tool-low-company", tool_name="search", user_id=42,
                     enterprise_id=7, enterprise_wallet_id=company.id, group_id=9,
                     provider_identity=PROVIDER, now=draft.effective_at)
    db.rollback()
    assert available_milli(db, company.id, NOW) == 1_000
    assert available_milli(db, personal.id, NOW) == 50_000
    assert db.query(BillingReservation).filter_by(request_key="tool-low-company").count() == 0

    # No company balance object can be substituted with a personal wallet.
    with pytest.raises(BillingError, match="企业钱包归属不匹配"):
        reserve_tool(db, request_key="tool-personal-only", tool_name="search", user_id=42,
                     enterprise_id=7, enterprise_wallet_id=personal.id, group_id=9,
                     provider_identity=PROVIDER, now=draft.effective_at)


def test_tool_request_is_never_replayed_for_an_existing_idempotency_key(db):
    draft, company, _personal, _limit_row, first = _reserve(db)
    db.commit()
    with pytest.raises(IdempotencyConflict, match="禁止重复创建 reservation"):
        reserve_tool(db, request_key=first.request_key, tool_name="search", user_id=42,
                     enterprise_id=7, enterprise_wallet_id=company.id, group_id=9,
                     provider_identity=PROVIDER, now=draft.effective_at)


def test_tool_reserve_does_not_lock_a_missing_unique_key_gap(db):
    draft = _enable_tool_prices(db)
    company = _wallet(db, "enterprise", 7, 2_000, "gap-company")
    _limit(db)
    locking_reads = []

    def record_reservation_reads(state):
        if (state.is_select and "billing_reservation.request_key" in str(state.statement)
                and ".request_key =" in str(state.statement)):
            locking_reads.append(state.statement._for_update_arg is not None)

    event.listen(db, "do_orm_execute", record_reservation_reads)
    try:
        reserve_tool(db, request_key="missing-gap-key", tool_name="search", user_id=42,
                     enterprise_id=7, enterprise_wallet_id=company.id, group_id=9,
                     provider_identity=PROVIDER, now=draft.effective_at)
    finally:
        event.remove(db, "do_orm_execute", record_reservation_reads)
    assert locking_reads
    assert not any(locking_reads), "缺失 request_key 的锁定查询会在 MySQL RR 锁索引间隙"


@pytest.mark.parametrize("message,expected", [
    ("UNIQUE constraint failed: billing_reservation.request_key", IdempotencyConflict),
    ("CHECK constraint failed: other_constraint", IntegrityError),
])
def test_tool_reserve_maps_only_request_key_unique_conflicts(db, monkeypatch, message, expected):
    draft = _enable_tool_prices(db)
    company = _wallet(db, "enterprise", 7, 2_000, "conflict-company")
    _limit(db)
    original_flush = db.flush

    def fail_reservation_insert(*args, **kwargs):
        if any(isinstance(row, BillingReservation) for row in db.new):
            raise IntegrityError("INSERT", {}, SqliteIntegrityError(message))
        return original_flush(*args, **kwargs)

    monkeypatch.setattr(db, "flush", fail_reservation_insert)
    with pytest.raises(expected):
        reserve_tool(db, request_key="conflicting-key", tool_name="search", user_id=42,
                     enterprise_id=7, enterprise_wallet_id=company.id, group_id=9,
                     provider_identity=PROVIDER, now=draft.effective_at)
    db.rollback()


def test_attempt_marker_is_one_way_and_unknown_result_keeps_the_entire_hold(db):
    draft, company, _personal, limit, reservation = _reserve(db)
    with pytest.raises(BillingError, match="未提交 provider attempt"):
        mark_unknown(db, reservation.request_key)

    release_not_sent(db, reservation.request_key)
    db.commit()
    assert available_milli(db, company.id, NOW) == 2_000
    assert db.query(BillingReservation).filter_by(id=reservation.id).one().usage_state == "not_sent"

    # A new call gets a distinct key; committing the marker is the required boundary before HTTP.
    new_reservation = reserve_tool(
        db, request_key="tool-attempted", tool_name="fetch", user_id=42, enterprise_id=7,
        enterprise_wallet_id=company.id, group_id=9, provider_identity=PROVIDER, now=draft.effective_at,
    )
    marker_time = draft.effective_at.replace(tzinfo=None) + timedelta(seconds=1)
    marked = mark_tool_attempt_started(db, new_reservation.request_key, now=marker_time)
    assert marked.provider_attempt_started_at == marker_time
    db.commit()

    with pytest.raises(IdempotencyConflict, match="已开始或已结束"):
        mark_tool_attempt_started(db, new_reservation.request_key, now=marker_time + timedelta(seconds=1))
    with pytest.raises(BillingError, match="provider attempt 已持久化"):
        release_not_sent(db, new_reservation.request_key)
    with pytest.raises(BillingError, match="不得记录供应商内置工具计数"):
        mark_unknown(db, new_reservation.request_key, server_tool_search_calls=0,
                     server_tool_fetch_calls=0)
    mark_unknown(db, new_reservation.request_key)
    db.commit()

    current = db.query(BillingReservation).filter_by(id=new_reservation.id).one()
    assert current.status == "unknown" and current.usage_state == "unknown"
    assert current.actual_milli is None and current.tool_successful_calls is None
    assert available_milli(db, company.id, NOW) == 200
    assert limit.used_milli == 0
    assert limit.reserved_milli == 1_800


def test_confirmed_nonbillable_provider_response_releases_hold_without_fabricating_success(db):
    draft, company, _personal, limit, reservation = _reserve(db)
    mark_tool_attempt_started(db, reservation.request_key, now=draft.effective_at.replace(tzinfo=None))
    db.commit()
    settled = release_tool_nonbillable(db, reservation.request_key, now=NOW.replace(tzinfo=None))
    db.commit()

    assert (settled.status, settled.usage_state, settled.tool_successful_calls,
            settled.actual_milli) == ("released", "not_billable", 0, 0)
    batch = db.query(BillingCreditBatch).filter_by(wallet_id=company.id).one()
    assert (batch.remaining_milli, batch.reserved_milli) == (2_000, 0)
    assert (limit.used_milli, limit.reserved_milli) == (0, 0)


def test_successful_tool_charges_the_original_snapshot_and_updates_company_limits(db):
    draft, company, _personal, limit, reservation = _reserve(db)
    mark_tool_attempt_started(db, reservation.request_key, now=draft.effective_at.replace(tzinfo=None))
    db.commit()

    # A future catalog mutation must not reinterpret an already-created reservation.
    catalog_row = catalog.active_catalog(db, now=draft.effective_at)
    catalog_row.tool_search_price_milli_per_success = 1_900
    db.flush()
    settled = settle_tool(db, request_key=reservation.request_key, tool_successful_calls=1,
                          now=NOW.replace(tzinfo=None) + timedelta(seconds=2))
    db.commit()

    assert (settled.status, settled.usage_state, settled.actual_milli,
            settled.tool_successful_calls) == ("settled", "reported", 1_800, 1)
    assert available_milli(db, company.id, NOW) == 200
    batch = db.query(BillingCreditBatch).filter_by(wallet_id=company.id).one()
    assert (batch.remaining_milli, batch.reserved_milli) == (200, 0)
    assert (limit.used_milli, limit.reserved_milli) == (1_800, 0)
    part = db.query(BillingReservationPart).filter_by(reservation_id=reservation.id).one()
    assert (part.reserved_milli, part.charged_milli) == (1_800, 1_800)


def test_tool_settlement_requires_committed_attempt_and_rejects_model_token_fields(db):
    _draft, _company, _personal, _limit_row, reservation = _reserve(db)
    with pytest.raises(BillingError, match="尚无已提交的 provider attempt"):
        settle_tool(db, request_key=reservation.request_key, tool_successful_calls=1)
    assert reservation.status == "reserved"

    reservation.input_tokens = 1
    db.flush()
    with pytest.raises(BillingError, match="混入模型 Token"):
        mark_tool_attempt_started(db, reservation.request_key)


@pytest.mark.parametrize("successful_calls, expected_charge", [(0, 0), (1, 1_800)])
def test_tool_unknown_reconciliation_uses_snapshot_and_requires_a_second_reviewer(
    db, successful_calls, expected_charge,
):
    draft, company, _personal, limit, reservation = _reserve(db, key=f"tool-c4-{successful_calls}")
    mark_tool_attempt_started(db, reservation.request_key, now=draft.effective_at.replace(tzinfo=None))
    db.commit()
    mark_unknown(db, reservation.request_key)
    db.commit()

    evidence = {
        "evidence_reference": f"provider-console-tool-{successful_calls}",
        "evidence_sha256": "c" * 64,
        "note": "按供应商原始 Search/Fetch 成功记录核对",
    }
    proposal = reconciliation.propose_reconciliation(
        db, reservation_id=reservation.id, actor_id=20, resolution="settle",
        provider_request_id=f"tool-request-{successful_calls}", input_tokens=None,
        output_tokens=None, tool_successful_calls=successful_calls, **evidence,
    )
    assert proposal.tool_successful_calls == successful_calls
    assert proposal.actual_milli == expected_charge
    db.commit()

    with pytest.raises(reconciliation.ReconciliationError, match="不能审批本人"):
        reconciliation.decide_reconciliation(
            db, reservation_id=reservation.id, actor_id=20, decision="approve", note="自批不得通过",
        )
    approved, event = reconciliation.decide_reconciliation(
        db, reservation_id=reservation.id, actor_id=21, decision="approve",
        note="已独立核对供应商记录",
    )
    db.commit()

    assert approved.status == "settled"
    assert approved.usage_state == "reported"
    assert approved.tool_successful_calls == successful_calls
    assert approved.actual_milli == expected_charge
    assert approved.input_tokens is None and approved.output_tokens is None
    batch = db.query(BillingCreditBatch).filter_by(wallet_id=company.id).one()
    assert (batch.remaining_milli, batch.reserved_milli) == (2_000 - expected_charge, 0)
    assert (limit.used_milli, limit.reserved_milli) == (expected_charge, 0)
    assert event.tool_successful_calls == successful_calls
    assert event.related_event_id == proposal.id


def test_sent_tool_cannot_be_proposed_as_never_sent(db):
    draft, _company, _personal, _limit_row, reservation = _reserve(db, key="tool-no-release")
    mark_tool_attempt_started(db, reservation.request_key, now=draft.effective_at.replace(tzinfo=None))
    db.commit()
    mark_unknown(db, reservation.request_key)
    db.commit()

    with pytest.raises(reconciliation.ReconciliationBlocked, match="provider attempt 已持久化"):
        reconciliation.propose_reconciliation(
            db, reservation_id=reservation.id, actor_id=30,
            resolution="release_not_sent", provider_request_id=None,
            input_tokens=None, output_tokens=None, evidence_reference="provider-absence-proof",
            evidence_sha256="d" * 64, note="尝试错误释放已发送请求",
        )
    events = reconciliation.list_reconciliation_events(db, reservation_id=reservation.id)
    assert [event.decision for event in events] == ["blocked"]
    assert events[0].evidence_reference is None
