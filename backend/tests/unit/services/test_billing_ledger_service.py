from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from fractions import Fraction
from threading import Barrier

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.billing_ledger import BillingCreditBatch, BillingLimit, BillingReservation
from app.services import billing_ledger_service
from app.services.billing_ledger_service import (
    BillingError,
    BillingProviderIdentity,
    IdempotencyConflict,
    InsufficientCredits,
    available_milli,
    get_or_create_wallet,
    grant,
    mark_unknown,
    quote_milli,
    settle,
)
from app.services.billing_ledger_service import (
    reserve as _reserve,
)
from app.services.billing_subscription_rules import cycle_boundary, prorate_seats, renewal_anchor, subscription_status

NOW = datetime(2026, 10, 31, 0, 0)
_TEST_PROVIDER_IDENTITY = BillingProviderIdentity(
    identity_version="deepseek-official-v1", provider_name="deepseek",
    endpoint_host="api.deepseek.com", credential_source="system",
)


def reserve(db, **kwargs):
    return _reserve(db, provider_identity=_TEST_PROVIDER_IDENTITY, **kwargs)


def _wallet(db, kind, owner, amount, key, *, expiry=None):
    wallet = get_or_create_wallet(db, kind, owner)
    grant(db, wallet_id=wallet.id, grant_key=key,
          kind="subscription" if expiry else "purchase", amount_milli=amount, expires_at=expiry)
    db.commit()
    return wallet


def test_model_rates_are_exact_and_unknown_models_fail_closed():
    assert quote_milli("deepseek-flash", 10000, 2000) == 18000
    assert quote_milli("deepseek-v4-pro", 10000, 2000) == 72000
    assert quote_milli("deepseek-v4-pro", 1_000_000, 0) == 4_500_000
    assert quote_milli("deepseek-v4-pro", 0, 1_000_000) == 13_500_000
    assert quote_milli("deepseek-v4-pro", 1, 0) == 5  # fractional milli-credit rounds up
    assert quote_milli("deepseek-flash", 1, 0) == 1
    assert quote_milli("deepseek-v4-flash-vision-exp", 10000, 2000) == 18000
    for model, prompt, output in [("other-model", 1, 1), ("deepseek-flash", -1, 0),
                                  ("deepseek-flash", True, 1)]:
        with pytest.raises(BillingError):
            quote_milli(model, prompt, output)


def test_new_reservations_require_a_verified_provider_identity(db):
    with pytest.raises(BillingError, match="身份缺失"):
        _reserve(db, request_key="missing-provider", model_name="deepseek-flash", user_id=42,
                 max_milli=10)


def test_grant_idempotency_and_mismatch(db):
    wallet = _wallet(db, "user", 42, 200000, "grant:1")
    batch = grant(db, wallet_id=wallet.id, grant_key="grant:1", kind="purchase", amount_milli=200000)
    assert batch.remaining_milli == 200000
    with pytest.raises(IdempotencyConflict):
        grant(db, wallet_id=wallet.id, grant_key="grant:1", kind="purchase", amount_milli=1)


def test_company_limits_then_personal_with_company_first_settlement(db):
    company = _wallet(db, "enterprise", 7, 60000, "company")
    personal = _wallet(db, "user", 42, 100000, "personal")
    for key, group, user, amount in [("group", 9, None, 50000), ("member", None, 42, 40000),
                                     ("group_member", 9, 42, 80000)]:
        db.add(BillingLimit(limit_key=key, enterprise_id=7, group_id=group, user_id=user,
                            period_start=NOW-timedelta(days=1), period_end=NOW+timedelta(days=30),
                            limit_milli=amount, used_milli=0, reserved_milli=0))
    db.commit()
    res = reserve(db, request_key="r1", model_name="deepseek-flash", user_id=42, max_milli=100000,
                  enterprise_wallet_id=company.id, personal_wallet_id=personal.id,
                  enterprise_id=7, group_id=9, allow_personal=True, now=NOW)
    assert res.status == "reserved"
    assert available_milli(db, company.id, NOW) == 20000
    assert available_milli(db, personal.id, NOW) == 40000
    settle(db, request_key="r1", actual_milli=70000, input_tokens=50000, output_tokens=5000, now=NOW)
    db.commit()
    assert available_milli(db, company.id, NOW) == 20000
    assert available_milli(db, personal.id, NOW) == 70000
    assert {r.limit_key: (r.used_milli, r.reserved_milli) for r in db.query(BillingLimit)} == {
        "group": (40000, 0), "member": (40000, 0), "group_member": (40000, 0)}


def test_company_chat_never_falls_back_to_personal(db):
    company = _wallet(db, "enterprise", 7, 50000, "company-chat")
    personal = _wallet(db, "user", 42, 100000, "personal-chat")
    with pytest.raises(InsufficientCredits):
        reserve(db, request_key="chat", model_name="deepseek-flash", user_id=42, max_milli=100000,
                enterprise_wallet_id=company.id, personal_wallet_id=personal.id,
                enterprise_id=7, allow_personal=False, now=NOW)
    db.rollback()
    assert available_milli(db, personal.id, NOW) == 100000


def test_reservation_and_settlement_idempotent(db):
    wallet = _wallet(db, "user", 42, 200000, "idempotence")
    kwargs = dict(request_key="idempotent", model_name="deepseek-flash", user_id=42,
                  max_milli=100000, personal_wallet_id=wallet.id, now=NOW)
    first = reserve(db, **kwargs)
    assert reserve(db, **kwargs).id == first.id
    assert available_milli(db, wallet.id, NOW) == 100000
    settle(db, request_key="idempotent", actual_milli=35000, input_tokens=30000, output_tokens=1250)
    settle(db, request_key="idempotent", actual_milli=35000, input_tokens=30000, output_tokens=1250)
    with pytest.raises(IdempotencyConflict):
        settle(db, request_key="idempotent", actual_milli=40000, input_tokens=30000, output_tokens=1250)
    assert available_milli(db, wallet.id, NOW) == 165000
    with pytest.raises(IdempotencyConflict):
        reserve(db, **{**kwargs, "max_milli": 2})


def test_reservation_settles_with_rate_snapshot_after_catalog_changes(db, monkeypatch):
    wallet = _wallet(db, "user", 42, 100000, "rate-snapshot")
    max_milli = quote_milli("deepseek-flash", 10000, 2000)
    reserve(db, request_key="rate-snapshot", model_name="deepseek-flash", user_id=42,
            max_milli=max_milli, personal_wallet_id=wallet.id, now=NOW)

    # A later price publication must not reinterpret an already authorized call.
    monkeypatch.setitem(billing_ledger_service._RATES, "deepseek-flash", (Fraction(2), Fraction(8)))
    settled = settle(db, request_key="rate-snapshot", actual_milli=18000,
                     input_tokens=10000, output_tokens=2000, now=NOW)

    assert settled.actual_milli == 18000
    assert settled.status == "settled"
    assert (settled.pricing_version, settled.input_rate_numerator, settled.input_rate_denominator,
            settled.output_rate_numerator, settled.output_rate_denominator) == (
                billing_ledger_service.RATE_CATALOG_VERSION, 1, 1, 4, 1,
            )


def test_reservation_with_missing_rate_snapshot_cannot_auto_settle(db):
    wallet = _wallet(db, "user", 42, 100000, "missing-rate-snapshot")
    reserve(db, request_key="missing-rate-snapshot", model_name="deepseek-flash", user_id=42,
            max_milli=18000, personal_wallet_id=wallet.id, now=NOW)
    reservation = db.query(BillingReservation).filter_by(request_key="missing-rate-snapshot").one()
    reservation.pricing_version = None
    reservation.input_rate_numerator = None
    reservation.input_rate_denominator = None
    reservation.output_rate_numerator = None
    reservation.output_rate_denominator = None
    db.flush()

    with pytest.raises(BillingError, match="输入和输出 Token 用量"):
        settle(db, request_key="missing-rate-snapshot", actual_milli=10000, now=NOW)

    with pytest.raises(BillingError, match="缺少费率快照"):
        settle(db, request_key="missing-rate-snapshot", actual_milli=18000,
               input_tokens=10000, output_tokens=2000, now=NOW)

    assert reservation.status == "reserved"


def test_legacy_reservation_without_provider_identity_stays_frozen(db):
    wallet = _wallet(db, "user", 42, 100000, "legacy-provider")
    reserve(db, request_key="legacy-provider", model_name="deepseek-flash", user_id=42,
            max_milli=18000, personal_wallet_id=wallet.id, now=NOW)
    reservation = db.query(BillingReservation).filter_by(request_key="legacy-provider").one()
    reservation.provider_identity_version = None
    reservation.provider_name = None
    reservation.provider_endpoint_host = None
    reservation.credential_source = None
    db.commit()

    with pytest.raises(BillingError, match="缺少供应商身份快照"):
        settle(db, request_key="legacy-provider", actual_milli=18000,
               input_tokens=10000, output_tokens=2000, now=NOW)
    db.rollback()
    reservation = db.query(BillingReservation).filter_by(request_key="legacy-provider").one()
    assert reservation.status == "reserved" and reservation.actual_milli is None
    assert available_milli(db, wallet.id, NOW) == 82000


def test_unknown_usage_cannot_release_hold_without_reconciled_tokens(db):
    wallet = _wallet(db, "user", 42, 100000, "unknown")
    reserve(db, request_key="u", model_name="deepseek-flash", user_id=42, max_milli=80000,
            personal_wallet_id=wallet.id, now=NOW)
    mark_unknown(db, "u")
    with pytest.raises(BillingError, match="超出预占"):
        settle(db, request_key="u", actual_milli=80001, input_tokens=80001, output_tokens=0)
    with pytest.raises(BillingError, match="输入和输出 Token 用量"):
        settle(db, request_key="u", actual_milli=75000)
    assert available_milli(db, wallet.id, NOW) == 20000
    reservation = db.query(BillingReservation).filter_by(request_key="u").one()
    assert reservation.status == "unknown" and reservation.actual_milli is None


def test_unknown_positive_server_tool_usage_cannot_be_settled_or_release_hold(db):
    wallet = _wallet(db, "user", 42, 100000, "unpriced-server-tool")
    reserve(db, request_key="server-tool", model_name="deepseek-flash", user_id=42, max_milli=80000,
            personal_wallet_id=wallet.id, now=NOW)
    mark_unknown(db, "server-tool", server_tool_search_calls=1, server_tool_fetch_calls=0)
    with pytest.raises(BillingError, match="未计价 Search/Fetch"):
        settle(db, request_key="server-tool", actual_milli=10000)
    assert available_milli(db, wallet.id, NOW) == 20000
    record = db.query(BillingReservation).filter_by(request_key="server-tool").one()
    assert (record.status, record.server_tool_search_calls, record.server_tool_fetch_calls) == ("unknown", 1, 0)


def test_zero_server_tool_counts_survive_manual_unknown_settlement(db):
    wallet = _wallet(db, "user", 42, 100000, "zero-server-tool")
    reserve(db, request_key="server-tool-zero", model_name="deepseek-flash", user_id=42, max_milli=80000,
            personal_wallet_id=wallet.id, now=NOW)
    mark_unknown(db, "server-tool-zero", server_tool_search_calls=0, server_tool_fetch_calls=0)
    settle(db, request_key="server-tool-zero", actual_milli=10000, input_tokens=10000, output_tokens=0)
    record = db.query(BillingReservation).filter_by(request_key="server-tool-zero").one()
    assert (record.status, record.server_tool_search_calls, record.server_tool_fetch_calls) == (
        "settled", 0, 0,
    )


def test_expiring_reserved_batch_settles_without_reviving_expired_balance(db):
    expiry = NOW + timedelta(hours=1)
    wallet = _wallet(db, "user", 42, 100000, "monthly", expiry=expiry)
    grant(db, wallet_id=wallet.id, grant_key="bought", kind="purchase", amount_milli=50000)
    reserve(db, request_key="expiry", model_name="deepseek-flash", user_id=42, max_milli=80000,
            personal_wallet_id=wallet.id, now=NOW)
    settle(db, request_key="expiry", actual_milli=60000, input_tokens=40000, output_tokens=5000,
           now=NOW+timedelta(hours=2))
    assert available_milli(db, wallet.id, NOW+timedelta(hours=2)) == 50000
    batch = db.query(BillingCreditBatch).filter_by(grant_key="monthly").one()
    assert (batch.remaining_milli, batch.reserved_milli) == (40000, 0)


def test_wallet_identity_cannot_use_somebody_elses_money(db):
    wallet = _wallet(db, "user", 44, 100000, "someone-else")
    with pytest.raises(BillingError, match="归属"):
        reserve(db, request_key="wrong-owner", model_name="deepseek-flash", user_id=42,
                max_milli=1000, personal_wallet_id=wallet.id, now=NOW)


def test_calendar_anchor_does_not_drift_after_short_month():
    # 31 Jan, 10:30 Shanghai; February uses month end, March returns to 31st.
    anchor = datetime(2027, 1, 31, 2, 30)
    assert cycle_boundary(anchor, 1).isoformat() == "2027-02-28T02:30:00+00:00"
    assert cycle_boundary(anchor, 2).isoformat() == "2027-03-31T02:30:00+00:00"


def test_grace_and_proration_are_integer_and_boundary_safe():
    end = datetime(2026, 11, 30)
    assert subscription_status(end, end-timedelta(microseconds=1)) == "active"
    assert subscription_status(end, end) == "grace"
    assert subscription_status(end, end+timedelta(days=7)) == "expired"
    assert renewal_anchor(end, end+timedelta(days=3)).replace(tzinfo=None) == end
    assert renewal_anchor(end, end+timedelta(days=8)).replace(tzinfo=None) == end+timedelta(days=8)
    assert prorate_seats(cycle_start=end-timedelta(days=30), cycle_end=end,
                         purchased_at=end-timedelta(days=10), seats=1) == {
                             "amount_fen": 2300, "credit_milli": 1666666}


def test_competing_reservations_do_not_spend_same_credit(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'billing.sqlite'}", connect_args={"timeout": 5})
    names = ("billing_wallet", "billing_credit_batch", "billing_limit", "billing_reservation",
             "billing_reservation_part", "billing_reservation_limit", "billing_rate_catalog",
             "billing_rate_catalog_item", "billing_rate_catalog_event")
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[n] for n in names])
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session.begin() as db:
        from app.services.billing_rate_catalog_service import seed_test_baseline

        seed_test_baseline(db)
        wallet = get_or_create_wallet(db, "user", 42)
        grant(db, wallet_id=wallet.id, grant_key="concurrent-grant", kind="purchase", amount_milli=1000)
        wallet_id = wallet.id
    barrier = Barrier(2)

    def compete(index):
        with Session() as db:
            barrier.wait(timeout=5)
            try:
                reserve(db, request_key=f"compete-{index}", model_name="deepseek-flash", user_id=42,
                        max_milli=700, personal_wallet_id=wallet_id, now=NOW)
                db.commit()
                return "reserved"
            except InsufficientCredits:
                db.rollback()
                return "insufficient"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(compete, range(2))) == ["insufficient", "reserved"]
    with Session() as db:
        assert available_milli(db, wallet_id, NOW) == 300
    engine.dispose()


def test_enterprise_auto_uses_only_configured_enterprise_auto_limit(db):
    company = _wallet(db, "enterprise", 7, 100000, "auto-company")
    kwargs = dict(request_key="auto", model_name="deepseek-flash", user_id=None,
                  max_milli=20000, enterprise_wallet_id=company.id, enterprise_id=7,
                  usage_kind="enterprise_auto", now=NOW)
    with pytest.raises(BillingError, match="预算"):
        reserve(db, **kwargs)
    db.rollback()
    db.add_all([
        BillingLimit(limit_key="member-cap", enterprise_id=7, category="normal", user_id=42,
                     period_start=NOW-timedelta(days=1), period_end=NOW+timedelta(days=30),
                     limit_milli=1000, used_milli=0, reserved_milli=0),
        BillingLimit(limit_key="auto-cap", enterprise_id=7, category="auto", user_id=None,
                     period_start=NOW-timedelta(days=1), period_end=NOW+timedelta(days=30),
                     limit_milli=20000, used_milli=0, reserved_milli=0),
    ])
    db.commit()
    reserve(db, **kwargs)
    settle(db, request_key="auto", actual_milli=15000, input_tokens=15000, output_tokens=0)
    db.commit()
    assert db.query(BillingLimit).filter_by(limit_key="auto-cap").one().used_milli == 15000
    assert db.query(BillingLimit).filter_by(limit_key="member-cap").one().used_milli == 0
    with pytest.raises(InsufficientCredits):
        reserve(db, **{**kwargs, "request_key": "next", "max_milli": 6000})


def test_enterprise_auto_records_group_without_charging_member(db):
    company = _wallet(db, "enterprise", 7, 100000, "auto-group-company")
    db.add(BillingLimit(limit_key="auto-group-cap", enterprise_id=7, category="auto", user_id=None,
                        period_start=NOW-timedelta(days=1), period_end=NOW+timedelta(days=30),
                        limit_milli=20000, used_milli=0, reserved_milli=0))
    db.commit()
    row = reserve(db, request_key="auto-group", model_name="deepseek-flash", user_id=None,
                  max_milli=10000, enterprise_wallet_id=company.id, enterprise_id=7,
                  group_id=12, usage_kind="enterprise_auto", now=NOW)
    assert row.user_id is None and row.group_id == 12 and row.usage_kind == "enterprise_auto"
