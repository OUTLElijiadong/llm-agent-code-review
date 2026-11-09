import uuid
from datetime import datetime, timedelta

import pytest

from app.models.billing_ledger import (
    BillingCreditBatch,
    BillingReservation,
    BillingReservationPart,
    BillingReservationReconciliationEvent,
)
from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun
from app.services import billing_ledger_service
from app.services import billing_reservation_reconciliation_service as reconciliation
from app.services.billing_ledger_service import (
    BillingProviderIdentity,
    get_or_create_wallet,
    grant,
    mark_unknown,
    reserve,
    settle,
)

NOW = datetime(2026, 11, 6)
PROVIDER = BillingProviderIdentity(
    identity_version="deepseek-official-v1", provider_name="deepseek",
    endpoint_host="api.deepseek.com", credential_source="system",
)
EVIDENCE = {
    "evidence_reference": "provider-console-case-42",
    "evidence_sha256": "a" * 64,
    "note": "核对供应商原始调用记录",
}


def _reservation(db, request_key="reconcile-1", *, unknown=True, tool_counts=None):
    wallet = get_or_create_wallet(db, "user", 101)
    grant(db, wallet_id=wallet.id, grant_key=f"grant:{request_key}", kind="purchase", amount_milli=100_000)
    reservation = reserve(
        db, request_key=request_key, model_name="deepseek-flash", user_id=101,
        max_milli=80_000, personal_wallet_id=wallet.id, provider_identity=PROVIDER, now=NOW,
    )
    if unknown:
        if tool_counts is None:
            mark_unknown(db, request_key)
        else:
            mark_unknown(
                db, request_key,
                server_tool_search_calls=tool_counts[0], server_tool_fetch_calls=tool_counts[1],
            )
    db.commit()
    return wallet, reservation


def test_settlement_requires_two_different_reviewers_and_uses_snapshot_quote(db):
    wallet, reservation = _reservation(db, tool_counts=(0, 0))
    proposal = reconciliation.propose_reconciliation(
        db, reservation_id=reservation.id, actor_id=11, resolution="settle",
        provider_request_id="resp_verified_42", input_tokens=10_000, output_tokens=5_000,
        **EVIDENCE,
    )
    assert proposal.decision == "proposed"
    assert proposal.resolution == "settle"
    assert proposal.actual_milli == billing_ledger_service.quote_reservation_milli(
        db, request_key=reservation.request_key, input_tokens=10_000, output_tokens=5_000,
    ) == 30_000
    db.commit()

    with pytest.raises(reconciliation.ReconciliationError, match="不能审批本人"):
        reconciliation.decide_reconciliation(
            db, reservation_id=reservation.id, actor_id=11, decision="approve", note="同一人不能自批",
        )
    approved, approval_event = reconciliation.decide_reconciliation(
        db, reservation_id=reservation.id, actor_id=12, decision="approve", note="已交叉核验供应商凭据",
    )
    db.commit()

    assert approved.status == "settled"
    assert approved.actual_milli == 30_000
    assert (approved.input_tokens, approved.output_tokens) == (10_000, 5_000)
    assert db.query(BillingCreditBatch).filter_by(wallet_id=wallet.id).one().remaining_milli == 70_000
    events = reconciliation.list_reconciliation_events(db, reservation_id=reservation.id)
    assert [event.decision for event in events] == ["proposed", "blocked", "approved"]
    assert events[1].actor_id == 11 and events[1].related_event_id == proposal.id
    assert events[1].note == "提案人不能审批本人提案"
    assert events[1].resolution == "settle"
    assert all(getattr(events[1], field) is None for field in (
        "provider_request_id", "input_tokens", "output_tokens", "actual_milli",
        "evidence_reference", "evidence_sha256",
    ))
    assert events[2].actor_id == 12 and events[2].related_event_id == proposal.id
    assert events[2].actual_milli == 30_000
    assert approval_event.id == events[2].id


def test_verified_not_sent_release_requires_second_reviewer_and_releases_holds(db):
    wallet, reservation = _reservation(db)
    proposal = reconciliation.propose_reconciliation(
        db, reservation_id=reservation.id, actor_id=21, resolution="release_not_sent",
        provider_request_id=None, input_tokens=None, output_tokens=None,
        **{**EVIDENCE, "evidence_reference": "provider-no-request-case-8"},
    )
    db.commit()
    settled, approval_event = reconciliation.decide_reconciliation(
        db, reservation_id=reservation.id, actor_id=22, decision="approve", note="确认请求未发送",
    )
    db.commit()

    assert settled.status == "released"
    assert settled.usage_state == "not_sent"
    batch = db.query(BillingCreditBatch).filter_by(wallet_id=wallet.id).one()
    assert (batch.remaining_milli, batch.reserved_milli) == (100_000, 0)
    events = reconciliation.list_reconciliation_events(db, reservation_id=reservation.id)
    assert events[-1].related_event_id == proposal.id
    assert events[-1].decision == "approved"
    assert approval_event.id == events[-1].id


def test_release_proposal_is_blocked_if_provider_usage_arrives_before_approval(db):
    wallet, reservation = _reservation(db)
    reconciliation.propose_reconciliation(
        db, reservation_id=reservation.id, actor_id=23, resolution="release_not_sent",
        provider_request_id=None, input_tokens=None, output_tokens=None,
        **{**EVIDENCE, "evidence_reference": "provider-no-request-race-9"},
    )
    db.commit()

    mark_unknown(db, reservation.request_key, server_tool_search_calls=0, server_tool_fetch_calls=1)
    db.commit()
    with pytest.raises(reconciliation.ReconciliationError, match="用量"):
        reconciliation.decide_reconciliation(
            db, reservation_id=reservation.id, actor_id=24, decision="approve", note="供应商补录了工具用量",
        )
    db.commit()

    current = db.query(BillingReservation).filter_by(id=reservation.id).one()
    batch = db.query(BillingCreditBatch).filter_by(wallet_id=wallet.id).one()
    events = reconciliation.list_reconciliation_events(db, reservation_id=reservation.id)
    assert current.status == "unknown"
    assert (batch.remaining_milli, batch.reserved_milli) == (100_000, 80_000)
    assert [event.decision for event in events] == ["proposed", "blocked"]


def test_missing_snapshot_cannot_be_settled_even_after_proposal_but_can_stay_frozen(db):
    _, reservation = _reservation(db, tool_counts=(0, 0))
    reservation.pricing_version = None
    reservation.input_rate_numerator = None
    reservation.input_rate_denominator = None
    reservation.output_rate_numerator = None
    reservation.output_rate_denominator = None
    reservation.provider_identity_version = None
    reservation.provider_name = None
    reservation.provider_endpoint_host = None
    reservation.credential_source = None
    db.commit()

    with pytest.raises(reconciliation.ReconciliationError, match="快照"):
        reconciliation.propose_reconciliation(
            db, reservation_id=reservation.id, actor_id=31, resolution="settle",
            provider_request_id="resp-legacy", input_tokens=10_000, output_tokens=5_000,
            **EVIDENCE,
        )
    events = reconciliation.list_reconciliation_events(db, reservation_id=reservation.id)
    assert [event.decision for event in events] == ["blocked"]
    assert db.query(BillingReservation).filter_by(id=reservation.id).one().status == "unknown"


def test_positive_unpriced_search_fetch_usage_cannot_be_manually_settled(db):
    _, reservation = _reservation(db)
    mark_unknown(db, reservation.request_key, server_tool_search_calls=1, server_tool_fetch_calls=0)
    db.commit()

    with pytest.raises(reconciliation.ReconciliationError, match="Search/Fetch"):
        reconciliation.propose_reconciliation(
            db, reservation_id=reservation.id, actor_id=41, resolution="settle",
            provider_request_id="resp-search-42", input_tokens=10_000, output_tokens=5_000,
            **EVIDENCE,
        )
    events = reconciliation.list_reconciliation_events(db, reservation_id=reservation.id)
    assert [event.decision for event in events] == ["blocked"]


def test_missing_search_fetch_counts_are_not_assumed_to_be_zero(db):
    _, reservation = _reservation(db)
    with pytest.raises(reconciliation.ReconciliationError, match="不能推断为零"):
        reconciliation.propose_reconciliation(
            db, reservation_id=reservation.id, actor_id=45, resolution="settle",
            provider_request_id="resp-missing-tools", input_tokens=10_000, output_tokens=5_000,
            **EVIDENCE,
        )
    events = reconciliation.list_reconciliation_events(db, reservation_id=reservation.id)
    assert [event.decision for event in events] == ["blocked"]
    assert db.query(BillingReservation).filter_by(id=reservation.id).one().status == "unknown"


def test_stale_proposal_cannot_overwrite_automatic_settlement(db):
    _, reservation = _reservation(db, tool_counts=(0, 0))
    reconciliation.propose_reconciliation(
        db, reservation_id=reservation.id, actor_id=51, resolution="settle",
        provider_request_id="resp-race-42", input_tokens=10_000, output_tokens=5_000,
        **EVIDENCE,
    )
    db.commit()
    settle(db, request_key=reservation.request_key, actual_milli=30_000,
           input_tokens=10_000, output_tokens=5_000)
    db.commit()

    with pytest.raises(reconciliation.ReconciliationError, match="状态已变化"):
        reconciliation.decide_reconciliation(
            db, reservation_id=reservation.id, actor_id=52, decision="approve", note="复核",
        )
    rejected, rejection_event = reconciliation.decide_reconciliation(
        db, reservation_id=reservation.id, actor_id=52, decision="reject", note="预占已自动结算，关闭过期提案",
    )
    db.commit()
    assert rejected.status == "settled"
    assert rejection_event.decision == "rejected"
    events = reconciliation.list_reconciliation_events(db, reservation_id=reservation.id)
    assert [event.decision for event in events] == ["proposed", "blocked", "rejected"]
    assert db.query(BillingReservationReconciliationEvent).count() == 3
    assert db.query(BillingReservation).filter_by(id=reservation.id).one().status == "settled"


def test_duplicate_pending_proposal_and_bad_evidence_are_rejected(db):
    _, reservation = _reservation(db, tool_counts=(0, 0))
    with pytest.raises(reconciliation.ReconciliationError, match="SHA-256"):
        reconciliation.propose_reconciliation(
            db, reservation_id=reservation.id, actor_id=61, resolution="settle",
            provider_request_id="resp-bad-evidence", input_tokens=10, output_tokens=1,
            evidence_reference="provider-console-case-42", evidence_sha256="not-a-hash", note="依据",
        )
    reconciliation.propose_reconciliation(
        db, reservation_id=reservation.id, actor_id=61, resolution="settle",
        provider_request_id="resp-pending", input_tokens=10, output_tokens=1, **EVIDENCE,
    )
    db.commit()
    with pytest.raises(reconciliation.ReconciliationError, match="待复核"):
        reconciliation.propose_reconciliation(
            db, reservation_id=reservation.id, actor_id=62, resolution="release_not_sent",
            provider_request_id=None, input_tokens=None, output_tokens=None, **EVIDENCE,
        )


def test_reserved_call_is_visible_but_never_manually_reconcilable(db):
    _, reservation = _reservation(db, unknown=False)
    with pytest.raises(reconciliation.ReconciliationError, match="reserved 可能仍在上游处理中"):
        reconciliation.propose_reconciliation(
            db, reservation_id=reservation.id, actor_id=71, resolution="release_not_sent",
            provider_request_id=None, input_tokens=None, output_tokens=None, **EVIDENCE,
        )
    events = reconciliation.list_reconciliation_events(db, reservation_id=reservation.id)
    assert events[0].decision == "blocked" and events[0].evidence_reference is None


def _stale_search_reservation(db, *, attempt_started_at, lease_expires_at):
    run_id = f"stale-run-{uuid.uuid4().hex}"
    source_message_id = int(uuid.uuid4().hex[:7], 16)
    request_key = f"enterprise-assistant:{run_id}:tool:search:{'a' * 32}"
    run = EnterpriseAssistantRun(
        public_id=run_id, request_key=f"assistant:{run_id}", enterprise_id=7, conversation_id=20,
        group_id=9, initiator_user_id=42, source_message_id=source_message_id, status="running",
        attempt_count=1, model_name="deepseek-flash", prompt_sha256="b" * 64,
    )
    db.add(run)
    db.flush()
    state = EnterpriseAssistantRuntimeState(
        assistant_run_id=run.id, enterprise_id=7, conversation_id=20, group_id=9,
        initiator_user_id=42, source_message_id=source_message_id, status="failed", checkpoint_json="{}",
        permission_snapshot_json="{}", permission_snapshot_sha256="c" * 64,
        lease_generation=1, lease_expires_at=lease_expires_at, billing_invocation_count=1, version=1,
    )
    wallet = get_or_create_wallet(db, "enterprise", 7)
    batch = grant(db, wallet_id=wallet.id, grant_key=f"stale-grant:{run_id}", kind="purchase", amount_milli=10_000)
    batch.reserved_milli = 1_800
    reservation = BillingReservation(
        request_key=request_key, model_name="deepseek-flash", billable_kind="search", tool_name="search",
        tool_cost_cny_micros_per_success=5_038, tool_price_milli_per_success=1_800,
        tool_successful_calls=None, provider_attempt_started_at=attempt_started_at,
        provider_identity_version="deepseek-official-v1", provider_name="deepseek",
        provider_endpoint_host="api.deepseek.com", credential_source="system",
        pricing_version="catalog-v1", enterprise_id=7, group_id=9, user_id=42, usage_kind="member",
        enterprise_wallet_id=wallet.id, requested_milli=1_800, actual_milli=None, usage_state="pending",
        status="reserved", created_at=attempt_started_at - timedelta(seconds=1),
    )
    db.add_all([state, reservation])
    db.flush()
    db.add(BillingReservationPart(
        reservation_id=reservation.id, wallet_id=wallet.id, batch_id=batch.id,
        reserved_milli=1_800, charged_milli=None,
    ))
    db.commit()
    return reservation


def test_stale_search_attempt_recovery_only_moves_to_unknown_and_audits(db):
    attempt = NOW - timedelta(minutes=31)
    reservation = _stale_search_reservation(
        db, attempt_started_at=attempt, lease_expires_at=NOW - timedelta(minutes=20),
    )
    assert reconciliation.can_recover_stale_tool_attempt(db, reservation, now=NOW) is True

    recovered = reconciliation.recover_stale_tool_attempt(
        db, reservation_id=reservation.id, actor_id=91, actor_name="platform-admin",
        note="确认外呼标记与运行租约均已过期", now=NOW,
    )
    db.commit()

    assert recovered.status == "unknown"
    assert recovered.usage_state == "unknown"
    batch = db.query(BillingCreditBatch).filter_by(wallet_id=reservation.enterprise_wallet_id).one()
    assert (batch.remaining_milli, batch.reserved_milli) == (10_000, 1_800)
    event = reconciliation.list_reconciliation_events(db, reservation_id=reservation.id)[0]
    assert event.decision == "recovered" and event.resolution is None
    assert event.actor_id == 91
    assert "不释放或扣除余额" in event.note
    assert all(getattr(event, field) is None for field in (
        "provider_request_id", "input_tokens", "output_tokens", "actual_milli",
        "evidence_reference", "evidence_sha256",
    ))
    run = db.query(EnterpriseAssistantRun).filter_by(public_id=reservation.request_key.split(":")[1]).one()
    state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).one()
    assert (state.lease_generation, state.status, state.lease_expires_at) == (2, "awaiting_reconciliation", None)
    assert run.status == "awaiting_reconciliation"


def test_stale_recovery_locks_runtime_before_wallet_and_rechecks_reservation_identity(db, monkeypatch):
    from sqlalchemy import event

    reservation = _stale_search_reservation(
        db, attempt_started_at=NOW - timedelta(minutes=31), lease_expires_at=NOW - timedelta(minutes=20),
    )
    statements = []
    engine = db.get_bind()

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", capture)
    original_lock = reconciliation._reservation_for_update

    def change_identity(session, reservation_id, *, wallet_first):
        row, parts = original_lock(session, reservation_id, wallet_first=wallet_first)
        row.user_id += 1
        return row, parts

    monkeypatch.setattr(reconciliation, "_reservation_for_update", change_identity)
    try:
        with pytest.raises(reconciliation.ReconciliationBlocked, match="预占身份已变化"):
            reconciliation.recover_stale_tool_attempt(
                db, reservation_id=reservation.id, actor_id=91, actor_name="admin", note="复核", now=NOW,
            )
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    runtime_read = next(i for i, sql in enumerate(statements) if "enterprise_assistant_runtime_state" in sql)
    wallet_lock = next(i for i, sql in enumerate(statements) if sql.lstrip().startswith("update billing_wallet"))
    assert runtime_read < wallet_lock
    db.rollback()


def test_stale_search_recovery_rejects_recent_marker_or_live_lease(db):
    recent = _stale_search_reservation(
        db, attempt_started_at=NOW - timedelta(minutes=29), lease_expires_at=NOW - timedelta(minutes=1),
    )
    with pytest.raises(reconciliation.ReconciliationBlocked, match="不足 30 分钟"):
        reconciliation.recover_stale_tool_attempt(
            db, reservation_id=recent.id, actor_id=92, actor_name="admin", note="確認", now=NOW,
        )
    db.commit()
    assert db.query(BillingReservation).filter_by(id=recent.id).one().status == "reserved"
    assert reconciliation.can_recover_stale_tool_attempt(db, recent, now=NOW) is False

    live = _stale_search_reservation(
        db, attempt_started_at=NOW - timedelta(minutes=40), lease_expires_at=NOW + timedelta(minutes=1),
    )
    with pytest.raises(reconciliation.ReconciliationBlocked, match="租约仍有效"):
        reconciliation.recover_stale_tool_attempt(
            db, reservation_id=live.id, actor_id=93, actor_name="admin", note="確認", now=NOW,
        )
    db.commit()
    assert db.query(BillingReservation).filter_by(id=live.id).one().status == "reserved"
    assert reconciliation.can_recover_stale_tool_attempt(db, live, now=NOW) is False


def test_decision_without_any_proposal_appends_minimal_block_event(db):
    wallet, reservation = _reservation(db, tool_counts=(0, 0))

    with pytest.raises(reconciliation.ReconciliationBlocked, match="没有待复核的对账提案"):
        reconciliation.decide_reconciliation(
            db, reservation_id=reservation.id, actor_id=81, decision="approve", note="没有提案不得审批",
        )
    db.commit()

    event = reconciliation.list_reconciliation_events(db, reservation_id=reservation.id)[0]
    current = db.query(BillingReservation).filter_by(id=reservation.id).one()
    batch = db.query(BillingCreditBatch).filter_by(wallet_id=wallet.id).one()
    assert event.decision == "blocked"
    assert event.resolution is None and event.related_event_id is None
    assert event.note == "没有待复核的对账提案，账本未变更"
    assert all(getattr(event, field) is None for field in (
        "provider_request_id", "input_tokens", "output_tokens", "actual_milli",
        "evidence_reference", "evidence_sha256",
    ))
    assert current.status == "unknown"
    assert (batch.remaining_milli, batch.reserved_milli) == (100_000, 80_000)
