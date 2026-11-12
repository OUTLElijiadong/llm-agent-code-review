"""MySQL RR quota history and concurrent billing serialization."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
import test_fresh_database_chain as chain
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.models.billing_ledger import BillingLimit, BillingReservationLimit
from app.models.enterprise_org import Enterprise, EnterpriseMember
from app.models.user import User
from app.services import billing_quota_service as quota
from app.services import billing_reservation_reconciliation_service as reconciliation
from app.services.billing_ledger_service import (
    BillingError,
    BillingProviderIdentity,
    get_or_create_wallet,
    grant,
    mark_unknown,
    release_not_sent,
    reserve,
    settle,
)

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url
verified_model_catalog = chain.verified_model_catalog
PROVIDER = BillingProviderIdentity("deepseek-official-v1", "deepseek", "api.deepseek.com", "system")


def _seed(sessions):
    with sessions.begin() as db:
        owner = User(username=f"quotaowner{uuid4().hex[:16]}", password="x", role="user", status=1)
        employee = User(username=f"quotaemp{uuid4().hex[:16]}", password="x", role="user", status=1)
        db.add_all([owner, employee])
        db.flush()
        company = Enterprise(name="隔离额度测试", owner_id=owner.id, seat_limit=3, status="active",
                             paid_order_id=f"quota-{uuid4().hex}")
        db.add(company)
        db.flush()
        db.add_all([EnterpriseMember(enterprise_id=company.id, user_id=owner.id, role="owner", status="active"),
                    EnterpriseMember(enterprise_id=company.id, user_id=employee.id, role="member", status="active")])
        wallet = get_or_create_wallet(db, "enterprise", company.id)
        grant(db, wallet_id=wallet.id, grant_key=f"quota-grant-{uuid4().hex}", kind="purchase", amount_milli=100_000)
        return owner.id, employee.id, company.id, wallet.id


@pytest.mark.parametrize("operation", ["settle", "release"])
def test_quota_link_added_after_old_snapshot_is_applied_by_terminal_operation(
    fresh_chain, verified_model_catalog, operation,
):
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    owner, employee, company, wallet = _seed(sessions)
    key = f"quota-prior-{uuid4().hex}"
    with sessions.begin() as db:
        record = reserve(db, request_key=key, model_name="deepseek-flash", user_id=employee, max_milli=40_000,
                         enterprise_wallet_id=wallet, enterprise_id=company, provider_identity=PROVIDER)
        record_id = record.id

    with sessions() as old:
        # Establish an RR snapshot before the quota and its link are created.
        assert old.query(BillingReservationLimit).filter_by(reservation_id=record_id).count() == 0
        with sessions.begin() as current:
            result = quota.set_limit(current, company, owner, period=quota.current_period(),
                                     scope="enterprise", limit_credits=50)
            assert (result["used_milli"], result["reserved_milli"]) == (0, 40_000)
        if operation == "settle":
            settle(old, request_key=key, actual_milli=30_000, input_tokens=30_000, output_tokens=0)
        else:
            release_not_sent(old, key)
        old.commit()

    with sessions() as db:
        limit = db.query(BillingLimit).filter_by(enterprise_id=company).one()
        assert (limit.used_milli, limit.reserved_milli) == ((30_000, 0) if operation == "settle" else (0, 0))
        assert db.query(BillingReservationLimit).filter_by(reservation_id=record_id, limit_id=limit.id).count() == 1


def test_other_enterprise_reservation_can_proceed_while_empty_quota_is_set(fresh_chain, verified_model_catalog):
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    owner_a, _, company_a, _ = _seed(sessions)
    _, employee_b, company_b, wallet_b = _seed(sessions)
    started = Event()
    release = Event()

    def change_a():
        with sessions.begin() as db:
            quota.set_limit(db, company_a, owner_a, period=quota.current_period(), scope="enterprise",
                            limit_credits=50)
            started.set()
            assert release.wait(10)

    def reserve_b():
        with sessions.begin() as db:
            record = reserve(db, request_key=f"quota-other-{uuid4().hex}", model_name="deepseek-flash",
                             user_id=employee_b, max_milli=10_000, enterprise_wallet_id=wallet_b,
                             enterprise_id=company_b, provider_identity=PROVIDER)
            return record.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(change_a)
        try:
            assert started.wait(10)
            second = pool.submit(reserve_b)
            assert second.result(timeout=3) == "reserved", "其他企业不应等待此企业的空额度历史查询"
        finally:
            release.set()
        first.result(timeout=10)


def test_limit_set_rejects_old_snapshot_after_new_reservation(fresh_chain, verified_model_catalog):
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    owner, employee, company, wallet = _seed(sessions)
    with sessions() as old:
        # Freeze the old transaction before any usage exists.
        assert old.query(BillingLimit).filter_by(enterprise_id=company).count() == 0
        with sessions.begin() as current:
            reserve(current, request_key=f"quota-new-{uuid4().hex}", model_name="deepseek-flash",
                    user_id=employee, max_milli=40_000, enterprise_wallet_id=wallet,
                    enterprise_id=company, provider_identity=PROVIDER)
        with pytest.raises(BillingError, match="账本刚发生变化"):
            quota.set_limit(old, company, owner, period=quota.current_period(), scope="enterprise", limit_credits=50)
        old.rollback()
    with sessions.begin() as fresh:
        result = quota.set_limit(fresh, company, owner, period=quota.current_period(),
                                 scope="enterprise", limit_credits=50)
        assert (result["used_milli"], result["reserved_milli"]) == (0, 40_000)


def test_c4_settlement_sees_quota_link_created_after_old_snapshot(fresh_chain, verified_model_catalog):
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    owner, employee, company, wallet = _seed(sessions)
    with sessions.begin() as db:
        record = reserve(db, request_key=f"quota-c4-{uuid4().hex}", model_name="deepseek-flash", user_id=employee,
                         max_milli=40_000, enterprise_wallet_id=wallet, enterprise_id=company,
                         provider_identity=PROVIDER)
        mark_unknown(db, record.request_key, server_tool_search_calls=0, server_tool_fetch_calls=0)
        reconciliation.propose_reconciliation(
            db, reservation_id=record.id, actor_id=1, resolution="settle", provider_request_id="test-provider",
            input_tokens=10_000, output_tokens=5_000, evidence_reference="test-evidence",
            evidence_sha256="a" * 64, note="隔离测试",
        )
        record_id = record.id
    with sessions() as old:
        assert old.query(BillingReservationLimit).filter_by(reservation_id=record_id).count() == 0
        with sessions.begin() as current:
            quota.set_limit(current, company, owner, period=quota.current_period(), scope="enterprise",
                            limit_credits=50)
        result, _ = reconciliation.decide_reconciliation(
            old, reservation_id=record_id, actor_id=2, decision="approve", note="隔离复核",
        )
        assert result.status == "settled"
        old.commit()
    with sessions() as db:
        limit = db.query(BillingLimit).filter_by(enterprise_id=company).one()
        assert (limit.used_milli, limit.reserved_milli) == (30_000, 0)


@pytest.mark.parametrize("winner", ["limit", "reservation"])
def test_limit_and_new_reservation_serialize_with_wallet_lock(fresh_chain, verified_model_catalog, winner):
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    owner, employee, company, wallet = _seed(sessions)
    first_locked, release, second_started = Event(), Event(), Event()
    contender_id: list[int] = []

    def first():
        with sessions.begin() as db:
            if winner == "limit":
                quota.set_limit(db, company, owner, period=quota.current_period(), scope="enterprise",
                                limit_credits=50)
            else:
                reserve(db, request_key=f"quota-first-{uuid4().hex}", model_name="deepseek-flash",
                        user_id=employee, max_milli=40_000, enterprise_wallet_id=wallet,
                        enterprise_id=company, provider_identity=PROVIDER)
            first_locked.set()
            assert release.wait(10)

    def second():
        with sessions() as db:
            contender_id.append(db.execute(text("SELECT CONNECTION_ID()")).scalar_one())
            # Freeze an RR snapshot before the other transaction commits.
            db.query(BillingLimit).filter_by(enterprise_id=company).count()
            second_started.set()
            if winner == "limit":
                reserve(db, request_key=f"quota-second-{uuid4().hex}", model_name="deepseek-flash",
                        user_id=employee, max_milli=40_000, enterprise_wallet_id=wallet,
                        enterprise_id=company, provider_identity=PROVIDER)
                db.commit()
                return "reserved"
            with pytest.raises(BillingError, match="账本刚发生变化"):
                quota.set_limit(db, company, owner, period=quota.current_period(), scope="enterprise",
                                limit_credits=50)
            db.rollback()
            return "retry"

    with ThreadPoolExecutor(max_workers=2) as pool:
        initial = pool.submit(first)
        assert first_locked.wait(10)
        try:
            contender = pool.submit(second)
            assert second_started.wait(10)
            deadline = monotonic() + 5
            waiting = False
            while monotonic() < deadline:
                with fresh_chain.connect() as connection:
                    waiting = bool(connection.execute(text(
                        "SELECT COUNT(*) FROM performance_schema.data_lock_waits AS w "
                        "JOIN performance_schema.threads AS t ON t.thread_id=w.requesting_thread_id "
                        "WHERE t.processlist_id=:connection_id"
                    ), {"connection_id": contender_id[0]}).scalar_one())
                if waiting:
                    break
                sleep(0.02)
            assert waiting, "应观察到钱包锁竞争"
        finally:
            release.set()
        initial.result(timeout=15)
        assert contender.result(timeout=15) == ("reserved" if winner == "limit" else "retry")
    if winner == "reservation":
        with sessions.begin() as db:
            result = quota.set_limit(db, company, owner, period=quota.current_period(),
                                     scope="enterprise", limit_credits=50)
            assert result["reserved_milli"] == 40_000
