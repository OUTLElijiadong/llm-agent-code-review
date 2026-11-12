from datetime import datetime, timedelta, timezone
from fractions import Fraction

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.billing_ledger import BillingReservation
from app.services import billing_ledger_service, billing_rate_catalog_service
from app.services.billing_ledger_service import (
    BillingError,
    available_milli,
    get_or_create_wallet,
    grant,
    resolve_billing_provider_identity,
)
from app.services.billing_metered_call import metered_call as _metered_call

_TEST_PROVIDER_IDENTITY = resolve_billing_provider_identity(
    provider_name="deepseek", base_url="https://api.deepseek.com/v1",
    credential_source="system", model_name="deepseek-flash",
)


def metered_call(**kwargs):
    kwargs.setdefault("provider_identity", _TEST_PROVIDER_IDENTITY)
    return _metered_call(**kwargs)


@pytest.fixture
def wallet_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'meter.sqlite'}")
    tables = ("billing_wallet", "billing_credit_batch", "billing_limit", "billing_reservation",
              "billing_reservation_part", "billing_reservation_limit", "billing_rate_catalog",
              "billing_rate_catalog_item", "billing_rate_catalog_event")
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[name] for name in tables])
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session.begin() as db:
        from app.services.billing_rate_catalog_service import seed_test_baseline

        seed_test_baseline(db)
        wallet = get_or_create_wallet(db, "user", 10)
        grant(db, wallet_id=wallet.id, grant_key="initial", kind="purchase", amount_milli=1000)
        wallet_id = wallet.id
    yield Session, wallet_id
    engine.dispose()


def test_prehold_precedes_request_and_actual_usage_settles(wallet_factory):
    Session, wallet_id = wallet_factory
    def send():
        with Session() as db:
            assert available_milli(db, wallet_id) == 700  # 100 input + 50*4 output
        return "ok", {"prompt_tokens": 80, "completion_tokens": 30}
    assert metered_call(session_factory=Session, request_key="r1", model_name="deepseek-flash", user_id=10,
                        max_input_tokens=100, max_output_tokens=50, personal_wallet_id=wallet_id, send=send) == "ok"
    with Session() as db:
        assert available_milli(db, wallet_id) == 800
        record = db.query(BillingReservation).filter_by(request_key="r1").one()
        assert (record.status, record.usage_state, record.input_tokens, record.output_tokens) == (
            "settled", "reported", 80, 30)
    with pytest.raises(BillingError, match="禁止重复"):
        metered_call(session_factory=Session, request_key="r1", model_name="deepseek-flash", user_id=10,
                     max_input_tokens=100, max_output_tokens=50, personal_wallet_id=wallet_id, send=send)


def test_metered_call_uses_reservation_rate_snapshot_if_price_changes_during_send(wallet_factory, monkeypatch):
    Session, wallet_id = wallet_factory

    def send():
        monkeypatch.setitem(billing_ledger_service._RATES, "deepseek-flash", (Fraction(2), Fraction(8)))
        return "ok", {"prompt_tokens": 80, "completion_tokens": 30}

    assert metered_call(session_factory=Session, request_key="price-change", model_name="deepseek-flash",
                        user_id=10, max_input_tokens=100, max_output_tokens=50,
                        personal_wallet_id=wallet_id, send=send) == "ok"
    with Session() as db:
        reservation = db.query(BillingReservation).filter_by(request_key="price-change").one()
        assert (reservation.status, reservation.actual_milli) == ("settled", 200)


def test_metered_call_preholds_with_the_scheduled_catalog_active_in_its_transaction(wallet_factory, monkeypatch):
    Session, wallet_id = wallet_factory
    checked = datetime.now(timezone.utc)
    effective = checked + timedelta(hours=2)
    with Session.begin() as db:
        items = billing_rate_catalog_service.baseline_items()
        for item in items:
            item["input_rate_numerator"] = 2
            item["output_rate_numerator"] = 8
        draft = billing_rate_catalog_service.create_draft(
            db, actor_id=10, effective_at=effective,
            source_url="https://api-docs.deepseek.com/quick_start/pricing/",
            source_checked_at=checked,
            model_cny_source_url="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
            model_cny_source_checked_at=checked,
            fx_source_url="https://www.boc.cn/sourcedb/whpj/index.html",
            fx_source_checked_at=checked, fx_cny_per_usd_scaled=67_168,
            change_note="并发验证计划费率切换与预占使用同一版本。", items=items, now=checked,
        )
        billing_rate_catalog_service.approve_draft(
            db, catalog_id=draft.id, actor_id=11, now=checked,
        )
    monkeypatch.setattr(billing_ledger_service, "_now",
                        lambda: effective.astimezone(timezone.utc).replace(tzinfo=None))
    catalog_now = billing_rate_catalog_service._now
    monkeypatch.setattr(billing_rate_catalog_service, "_now",
                        lambda now=None: catalog_now(effective if now is None else now))

    def send():
        with Session() as db:
            reservation = db.query(BillingReservation).filter_by(request_key="scheduled-catalog").one()
            assert reservation.requested_milli == 600
        return "ok", {"prompt_tokens": 80, "completion_tokens": 30}

    assert metered_call(session_factory=Session, request_key="scheduled-catalog", model_name="deepseek-flash",
                        user_id=10, max_input_tokens=100, max_output_tokens=50,
                        personal_wallet_id=wallet_id, send=send) == "ok"
    with Session() as db:
        reservation = db.query(BillingReservation).filter_by(request_key="scheduled-catalog").one()
        assert reservation.status == "settled"
        assert reservation.actual_milli == 400
        assert (reservation.input_rate_numerator, reservation.output_rate_numerator) == (2, 8)


def test_insufficient_funds_never_sends_and_missing_usage_freezes(wallet_factory):
    Session, wallet_id = wallet_factory
    sends = []
    def send_unknown():
        sends.append(True)
        return "ok", {}
    with pytest.raises(BillingError, match="不足"):
        metered_call(session_factory=Session, request_key="expensive", model_name="deepseek-flash", user_id=10,
                     max_input_tokens=1000, max_output_tokens=1000, personal_wallet_id=wallet_id, send=send_unknown)
    assert not sends
    with pytest.raises(BillingError, match="缺失"):
        metered_call(session_factory=Session, request_key="unknown", model_name="deepseek-flash", user_id=10,
                     max_input_tokens=100, max_output_tokens=50, personal_wallet_id=wallet_id, send=send_unknown)
    with Session() as db:
        assert available_milli(db, wallet_id) == 700
        record = db.query(BillingReservation).filter_by(request_key="unknown").one()
        assert (record.status, record.usage_state, record.input_tokens, record.output_tokens) == (
            "unknown", "unknown", None, None)


def test_metered_call_without_provider_identity_never_sends(wallet_factory):
    Session, wallet_id = wallet_factory
    sent = []
    with pytest.raises(BillingError, match="身份缺失"):
        _metered_call(
            session_factory=Session, request_key="missing-provider", model_name="deepseek-flash",
            user_id=10, max_input_tokens=100, max_output_tokens=50,
            personal_wallet_id=wallet_id, send=lambda: sent.append(True),
        )
    assert sent == []
    with Session() as db:
        assert db.query(BillingReservation).count() == 0
