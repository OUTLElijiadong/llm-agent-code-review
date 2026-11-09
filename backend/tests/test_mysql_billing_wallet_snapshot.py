"""MySQL RR must never spend cached batch values after taking the wallet lock.

The temporary database, wallets, balances and catalog are synthetic. No real
provider request is made; fixture requires an explicitly supplied empty MySQL.
"""
from uuid import uuid4

import pytest
import test_fresh_database_chain as chain
from sqlalchemy.orm import sessionmaker

from app.models.billing_ledger import BillingCreditBatch, BillingReservation, BillingReservationPart
from app.services import billing_ledger_service as ledger
from app.services import billing_rate_catalog_service as catalog

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url
verified_model_catalog = chain.verified_model_catalog


@pytest.mark.parametrize("source", ["personal", "enterprise", "mixed", "tool"])
def test_stale_snapshot_cannot_overreserve_wallet(fresh_chain, verified_model_catalog, monkeypatch, source):
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    suffix = uuid4().hex
    owner_id = 10_000_000_000 + (uuid4().int % 1_000_000_000_000)
    enterprise_id = owner_id + 1
    with sessions.begin() as db:
        wallets = {}
        for scope in ({"user"} if source == "personal" else {"enterprise", "user"} if source == "mixed"
                      else {"enterprise"}):
            wallet = ledger.get_or_create_wallet(db, scope, owner_id if scope == "user" else enterprise_id)
            amount = 50_000 if source == "mixed" else 100_000
            ledger.grant(db, wallet_id=wallet.id, grant_key=f"snapshot-{scope}-{suffix}",
                         kind="purchase", amount_milli=amount)
            wallets[scope] = wallet.id
    provider = ledger.BillingProviderIdentity("deepseek-official-v1", "deepseek", "api.deepseek.com", "system")
    if source == "tool":
        monkeypatch.setattr(catalog, "tool_rate_snapshot", lambda *_a, **_kw: ("test-rate", 5038, 80_000))

    def make_reservation(db, key):
        if source == "tool":
            return ledger.reserve_tool(db, request_key=key, tool_name="search", user_id=owner_id,
                                       enterprise_id=enterprise_id, enterprise_wallet_id=wallets["enterprise"],
                                       group_id=None, provider_identity=provider)
        return ledger.reserve(db, request_key=key, model_name="deepseek-flash", user_id=owner_id,
                              max_milli=80_000, personal_wallet_id=wallets.get("user"),
                              enterprise_wallet_id=wallets.get("enterprise"),
                              enterprise_id=enterprise_id if "enterprise" in wallets else None,
                              allow_personal=source == "mixed", provider_identity=provider)

    with sessions() as stale:
        # Retain identities to reproduce an authenticated, long-lived ORM
        # session that had already inspected these balances before the winner.
        cached = stale.query(BillingCreditBatch).filter(BillingCreditBatch.wallet_id.in_(wallets.values())).all()
        assert all(batch.reserved_milli == 0 for batch in cached)
        with sessions.begin() as winner:
            make_reservation(winner, f"snapshot-win-{suffix}")
        with pytest.raises(ledger.InsufficientCredits):
            make_reservation(stale, f"snapshot-lose-{suffix}")
        stale.rollback()
    with sessions() as db:
        reservations = db.query(BillingReservation).filter(
            BillingReservation.request_key.in_([f"snapshot-win-{suffix}", f"snapshot-lose-{suffix}"])
        ).all()
        assert len(reservations) == 1
        parts = db.query(BillingReservationPart).filter_by(reservation_id=reservations[0].id).all()
        for batch in db.query(BillingCreditBatch).filter(BillingCreditBatch.wallet_id.in_(wallets.values())):
            assert batch.reserved_milli == sum(part.reserved_milli for part in parts if part.batch_id == batch.id)
            assert batch.reserved_milli <= batch.remaining_milli


@pytest.mark.parametrize("tool", [False, True], ids=["model", "tool"])
def test_stale_identity_cannot_overwrite_other_settlement(fresh_chain, verified_model_catalog, monkeypatch, tool):
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    suffix = uuid4().hex
    owner_id = 10_000_000_000 + (uuid4().int % 1_000_000_000_000)
    enterprise_id = owner_id + 1
    provider = ledger.BillingProviderIdentity("deepseek-official-v1", "deepseek", "api.deepseek.com", "system")
    monkeypatch.setattr(catalog, "tool_rate_snapshot", lambda *_a, **_kw: ("test-rate", 5038, 80_000))
    keys = [f"snapshot-settle-{suffix}-{index}" for index in range(2)]
    with sessions.begin() as db:
        wallet = ledger.get_or_create_wallet(db, "enterprise", enterprise_id)
        batch = ledger.grant(db, wallet_id=wallet.id, grant_key=f"snapshot-settle-{suffix}",
                             kind="purchase", amount_milli=200_000)
        batch_id = batch.id
        for key in keys:
            if tool:
                ledger.reserve_tool(db, request_key=key, tool_name="search", user_id=owner_id,
                                    enterprise_id=enterprise_id, enterprise_wallet_id=wallet.id,
                                    group_id=None, provider_identity=provider)
                ledger.mark_tool_attempt_started(db, key)
            else:
                ledger.reserve(db, request_key=key, model_name="deepseek-flash", user_id=owner_id,
                               enterprise_id=enterprise_id, enterprise_wallet_id=wallet.id,
                               max_milli=80_000, provider_identity=provider)

    def settle(db, key):
        if tool:
            return ledger.settle_tool(db, request_key=key, tool_successful_calls=1)
        charge = ledger.quote_reservation_milli(db, request_key=key, input_tokens=10_000, output_tokens=0)
        return ledger.settle(db, request_key=key, actual_milli=charge, input_tokens=10_000, output_tokens=0)

    with sessions() as stale:
        cached = stale.query(BillingCreditBatch).filter_by(id=batch_id).one()
        assert cached.reserved_milli == 160_000
        with sessions.begin() as winner:
            settle(winner, keys[0])
        settle(stale, keys[1])
        stale.commit()
    with sessions() as db:
        batch = db.query(BillingCreditBatch).filter_by(id=batch_id).one()
        expected_charge = 160_000 if tool else 20_000
        assert (batch.remaining_milli, batch.reserved_milli) == (200_000 - expected_charge, 0)
