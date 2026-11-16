"""Fresh MySQL migration 121 keeps zero-evidence constraints."""
import pytest
import test_fresh_database_chain as chain
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.models.billing_ledger import BillingReservation
from app.services import billing_ledger_service as ledger
from app.services import billing_reservation_reconciliation_service as reconciliation

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url
verified_model_catalog = chain.verified_model_catalog


def test_migration_121_mysql_rejects_partial_or_nonzero_evidence(fresh_chain):
    with fresh_chain.connect() as db:
        transaction = db.begin()
        try:
            version = db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert version == "121_billing_model_evidence"
            sql = text("INSERT INTO billing_reservation_reconciliation_event "
                       "(reservation_id,actor_id,decision,resolution,evidence_reference,evidence_sha256,note,created_at,"
                       "model_search_calls_evidence,model_fetch_calls_evidence) "
                       "VALUES (1,1,'proposed','settle','case',:hash,'review',NOW(),:search,:fetch)")
            db.execute(sql, {"hash": "a" * 64, "search": 0, "fetch": 0})
            for search, fetch in [(0, None), (1, 0), (0, 1)]:
                with pytest.raises(OperationalError) as failure, db.begin_nested():
                    db.execute(sql, {"hash": "b" * 64, "search": search, "fetch": fetch})
                assert failure.value.orig.args[0] == 3819
        finally:
            transaction.rollback()


def test_mysql_external_evidence_settles_without_fabricating_provider_usage(fresh_chain, verified_model_catalog):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    provider = ledger.BillingProviderIdentity(identity_version="deepseek-official-v1", provider_name="deepseek",
                                              endpoint_host="api.deepseek.com", credential_source="system")
    with factory.begin() as db:
        wallet = ledger.get_or_create_wallet(db, "user", 1_228_701)
        ledger.grant(db, wallet_id=wallet.id, grant_key="mysql-external-evidence", kind="purchase",
                     amount_milli=100_000)
        reservation = ledger.reserve(db, request_key="mysql-external-model-evidence", model_name="deepseek-flash",
            user_id=1_228_701, max_milli=80_000, personal_wallet_id=wallet.id, provider_identity=provider)
        ledger.mark_unknown(db, reservation.request_key)
        reservation_id = reservation.id
    with factory.begin() as db:
        proposal = reconciliation.propose_reconciliation(
            db, reservation_id=reservation_id, actor_id=1, resolution="settle", provider_request_id="provider-case",
            input_tokens=10, output_tokens=10, model_search_calls_evidence=0, model_fetch_calls_evidence=0,
            evidence_reference="provider-record", evidence_sha256="a" * 64, note="核验供应商账单",
        )
        proposal_id = proposal.id
    with factory.begin() as db:
        settled, event = reconciliation.decide_reconciliation(
            db, reservation_id=reservation_id, actor_id=2, decision="approve", note="第二位管理员复核",
        )
        assert settled.status == "settled" and event.related_event_id == proposal_id
        assert (event.model_search_calls_evidence, event.model_fetch_calls_evidence) == (0, 0)
    with factory() as db:
        reservation = db.get(BillingReservation, reservation_id)
        assert (reservation.server_tool_search_calls, reservation.server_tool_fetch_calls) == (None, None)


def test_mysql_approval_uses_current_provider_counts_in_old_snapshot(fresh_chain, verified_model_catalog):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    provider = ledger.BillingProviderIdentity(identity_version="deepseek-official-v1", provider_name="deepseek",
                                              endpoint_host="api.deepseek.com", credential_source="system")
    key = "mysql-external-model-evidence-race"
    with factory.begin() as db:
        wallet = ledger.get_or_create_wallet(db, "user", 1_228_702)
        ledger.grant(db, wallet_id=wallet.id, grant_key="mysql-external-evidence-race", kind="purchase",
                     amount_milli=100_000)
        reservation = ledger.reserve(db, request_key=key, model_name="deepseek-flash", user_id=1_228_702,
            max_milli=80_000, personal_wallet_id=wallet.id, provider_identity=provider)
        ledger.mark_unknown(db, key)
        reservation_id = reservation.id
    with factory.begin() as db:
        reconciliation.propose_reconciliation(
            db, reservation_id=reservation_id, actor_id=1, resolution="settle", provider_request_id="provider-race",
            input_tokens=10, output_tokens=10, model_search_calls_evidence=0, model_fetch_calls_evidence=0,
            evidence_reference="provider-record", evidence_sha256="a" * 64, note="核验证据",
        )
    with factory() as approver:
        assert approver.get(BillingReservation, reservation_id).server_tool_search_calls is None
        with factory.begin() as provider_db:
            ledger.mark_unknown(provider_db, key, server_tool_search_calls=0, server_tool_fetch_calls=0)
        with pytest.raises(reconciliation.ReconciliationBlocked, match="冲突"):
            reconciliation.decide_reconciliation(
                approver, reservation_id=reservation_id, actor_id=2, decision="approve", note="须读取最新记录",
            )
        approver.commit()
    with factory() as db:
        reservation = db.get(BillingReservation, reservation_id)
        assert reservation.status == "unknown"
        assert (reservation.server_tool_search_calls, reservation.server_tool_fetch_calls) == (0, 0)
