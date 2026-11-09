"""C4 reviewer authority is rechecked after a MySQL RR lock wait."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, get_ident
from time import monotonic, sleep
from uuid import uuid4

import pytest
import test_fresh_database_chain as chain
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker

from app.core.exceptions import ForbiddenError
from app.models.billing_ledger import BillingReconciliationReviewerGrant
from app.models.rbac import Role, UserRole
from app.models.user import User
from app.services import billing_c4_reviewer_service as c4
from app.services import user_service
from app.services.billing_ledger_service import (
    BillingProviderIdentity,
    get_or_create_wallet,
    grant,
    mark_unknown,
    reserve,
)
from app.services.billing_reservation_reconciliation_service import decide_reconciliation, propose_reconciliation

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url
verified_model_catalog = chain.verified_model_catalog


@pytest.mark.parametrize("winner", ["review", "revoke"])
@pytest.mark.parametrize("revocation", ["role", "grant"])
def test_mysql_reviewer_revocation_serializes_with_authority(fresh_chain, verified_model_catalog, winner, revocation):
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with sessions.begin() as db:
        role = db.query(Role).filter_by(code="admin", status="active").one()
        reviewer = User(username=f"c4review{uuid4().hex[:16]}", password="x", role="admin", status=1, token_version=0)
        db.add(reviewer)
        db.flush()
        db.add(UserRole(user_id=reviewer.id, role_id=role.id))
        db.add(BillingReconciliationReviewerGrant(user_id=reviewer.id, active=1, granted_by=1))
        payer = User(username=f"c4payer{uuid4().hex[:16]}", password="x", role="user", status=1)
        db.add(payer)
        db.flush()
        wallet = get_or_create_wallet(db, "user", payer.id)
        grant(db, wallet_id=wallet.id, grant_key=f"c4-grant-{uuid4().hex}", kind="purchase", amount_milli=100_000)
        provider = BillingProviderIdentity("deepseek-official-v1", "deepseek", "api.deepseek.com", "system")
        reservation = reserve(db, request_key=f"c4-request-{uuid4().hex}", model_name="deepseek-flash",
                              user_id=payer.id, max_milli=80_000, personal_wallet_id=wallet.id,
                              provider_identity=provider)
        mark_unknown(db, reservation.request_key, server_tool_search_calls=0, server_tool_fetch_calls=0)
        propose_reconciliation(db, reservation_id=reservation.id, actor_id=1, resolution="settle",
                               provider_request_id="probe", input_tokens=100, output_tokens=100,
                               evidence_reference="probe", evidence_sha256="b" * 64, note="test")
        reviewer_id, reservation_id = reviewer.id, reservation.id
    locked, release, started, sql_started = Event(), Event(), Event(), Event()
    contender_thread, contender_connection = [], []

    def observe_lock(conn, _cursor, statement, _parameters, _context, _many):
        if contender_thread and get_ident() == contender_thread[0] and "FOR UPDATE" in statement.upper():
            if not contender_connection:
                contender_connection.append(conn.connection.driver_connection.thread_id())
                sql_started.set()

    def first():
        with sessions() as db:
            if winner == "review":
                c4.require_reviewer(db, reviewer_id, 0)
            elif revocation == "grant":
                c4.set_reviewer(db, actor_id=1, user_id=reviewer_id, active=False, reason="撤销")
            else:
                user_service.set_role(db, reviewer_id, "user", admin_id=1, commit=False)
            locked.set()
            assert release.wait(10)
            if winner == "review":
                result, _ = decide_reconciliation(db, reservation_id=reservation_id, actor_id=reviewer_id,
                                                  decision="approve", note="review")
                assert result.status == "settled"
            db.commit()
            return "first-committed"

    def second():
        contender_thread.append(get_ident())
        with sessions() as db:
            # Force an old RR snapshot before the first transaction commits.
            db.query(BillingReconciliationReviewerGrant).filter_by(user_id=reviewer_id).count()
            started.set()
            if winner == "review":
                if revocation == "grant":
                    c4.set_reviewer(db, actor_id=1, user_id=reviewer_id, active=False, reason="撤销")
                else:
                    user_service.set_role(db, reviewer_id, "user", admin_id=1, commit=False)
                db.commit()
                return "revoked"
            try:
                c4.require_reviewer(db, reviewer_id, 0)
            except ForbiddenError:
                db.rollback()
                return "denied"
            pytest.fail("旧快照不得越过已提交的降权")

    event.listen(fresh_chain, "before_cursor_execute", observe_lock)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(first)
            assert locked.wait(10)
            try:
                second_future = pool.submit(second)
                assert started.wait(10) and sql_started.wait(10)
                deadline, waiting = monotonic() + 5, False
                while monotonic() < deadline:
                    with fresh_chain.connect() as observer:
                        waiting = bool(observer.execute(text(
                            "SELECT COUNT(*) FROM performance_schema.data_lock_waits AS w "
                            "JOIN performance_schema.threads AS t ON t.thread_id=w.requesting_thread_id "
                            "WHERE t.processlist_id=:id"
                        ), {"id": contender_connection[0]}).scalar_one())
                    if waiting:
                        break
                    sleep(0.02)
                assert waiting, "未观察到竞争事务在 MySQL 等锁"
            finally:
                release.set()
            assert first_future.result(timeout=20) == "first-committed"
            assert second_future.result(timeout=20) == ("revoked" if winner == "review" else "denied")
    finally:
        event.remove(fresh_chain, "before_cursor_execute", observe_lock)
    with sessions() as db:
        assert db.query(BillingReconciliationReviewerGrant).filter_by(user_id=reviewer_id).one().active == 0
