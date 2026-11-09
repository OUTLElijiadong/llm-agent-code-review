"""Reviewer authorization must not deadlock its own bounded connection pool."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event, get_ident
from time import monotonic, sleep

import pytest
import test_mysql_reviewer_refund_approval as cases
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

import app.main  # noqa: F401 - register all ORM tables used by the full-chain fixture
from app.core.config import settings
from app.core.permission_codes import PermissionCode
from app.models.billing_ledger import BillingPermanentReviewer, BillingSubscriptionPeriod
from app.models.user import User
from app.services import billing_checkout_service, billing_entitlement_service, rbac_service

fresh_chain = cases.fresh_chain
fresh_chain_url = cases.fresh_chain_url


def test_fenced_reviewer_write_makes_progress_with_full_pool(fresh_chain, monkeypatch):
    """Two callers may occupy all connections; neither may need a third one."""
    monkeypatch.setattr(settings, "billing_entitlement_enforcement_ready", True)
    setup_sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    reviewer_id, _, _ = cases._seed(setup_sessions)
    small_engine = create_engine(fresh_chain.url, pool_size=2, max_overflow=0, pool_timeout=1, pool_pre_ping=True)
    sessions = sessionmaker(bind=small_engine, autoflush=False, expire_on_commit=False)
    authority_locked, other_connected = Event(), Event()
    original = rbac_service.check_permission_grant

    def wait_after_authority_lock(db, user_id, permission):
        allowed = original(db, user_id, permission)
        if not authority_locked.is_set():
            authority_locked.set()
            assert other_connected.wait(6)
        return allowed

    monkeypatch.setattr(rbac_service, "check_permission_grant", wait_after_authority_lock)

    def first():
        with sessions() as db:
            return rbac_service.check_paid_permission_fenced(db, reviewer_id, PermissionCode.PENTEST_AUTHORIZE)

    def second():
        assert authority_locked.wait(6)
        with sessions() as db:
            db.execute(text("SELECT 1"))  # occupy the remaining pool connection
            other_connected.set()
            return rbac_service.check_paid_permission_fenced(db, reviewer_id, PermissionCode.PENTEST_AUTHORIZE)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(first), pool.submit(second)
            assert a.result(timeout=8) is True
            assert b.result(timeout=8) is True
    finally:
        small_engine.dispose()


def test_fence_rechecks_permanent_revoke_and_expiry_on_old_snapshot(fresh_chain, monkeypatch):
    """A stale permanent grant must not outlive a newly expired paid period."""
    monkeypatch.setattr(settings, "billing_entitlement_enforcement_ready", True)
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    reviewer_id, _, _ = cases._seed(sessions)
    observed_at = billing_entitlement_service._now()
    with sessions.begin() as db:
        admin_id = db.query(User.id).filter_by(username="admin", status=1, role="super_admin").scalar()
        assert admin_id is not None
        period = db.query(BillingSubscriptionPeriod).filter_by(order_no=db.query(
            cases.BillingOrder.order_no,
        ).filter_by(user_id=reviewer_id, product="reviewer_month").scalar()).one()
        period.ends_at = observed_at + timedelta(hours=1)
        db.add(BillingPermanentReviewer(user_id=reviewer_id, status="active", granted_by=admin_id,
                                        granted_at=observed_at - timedelta(seconds=1)))
    with sessions() as stale:
        assert billing_entitlement_service.has_reviewer_entitlement(stale, reviewer_id, now=observed_at)
        with sessions.begin() as writer:
            billing_entitlement_service.set_permanent(writer, actor_id=admin_id, user_id=reviewer_id,
                                                      active=False, reason="test", now=observed_at)
            assert billing_entitlement_service.sync_reviewer_role(writer, reviewer_id, now=observed_at) is False
        # At revocation time the paid period remained active, so no role change
        # serialized against the old read. Advance beyond its grace period.
        monkeypatch.setattr(billing_entitlement_service, "_now", lambda: observed_at + timedelta(days=8))
        assert not rbac_service.check_paid_permission_fenced(stale, reviewer_id, PermissionCode.PENTEST_AUTHORIZE)


@pytest.mark.parametrize("operation", ["permanent", "period"])
def test_entitlement_update_waits_for_fenced_write_without_deadlock(fresh_chain, monkeypatch, operation):
    """Grant and period writers serialize with the protected transaction."""
    monkeypatch.setattr(settings, "billing_entitlement_enforcement_ready", True)
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    reviewer_id, _, _ = cases._seed(sessions)
    observed_at = billing_entitlement_service._now()
    with sessions.begin() as db:
        admin_id = db.query(User.id).filter_by(username="admin", status=1, role="super_admin").scalar()
        if operation == "permanent":
            db.add(BillingPermanentReviewer(user_id=reviewer_id, status="active", granted_by=admin_id,
                                            granted_at=observed_at - timedelta(seconds=1)))
        period_id = db.query(BillingSubscriptionPeriod.id).join(
            cases.BillingOrder, cases.BillingOrder.order_no == BillingSubscriptionPeriod.order_no,
        ).filter(cases.BillingOrder.user_id == reviewer_id).scalar()
    locked, release, started = Event(), Event(), Event()
    revoke_thread, revoke_connection = [], []
    original = rbac_service.check_permission_grant

    def pause_after_authority_lock(db, user_id, permission):
        allowed = original(db, user_id, permission)
        locked.set()
        assert release.wait(10)
        return allowed

    def observe(conn, _cursor, statement, _parameters, _context, _many):
        if revoke_thread and get_ident() == revoke_thread[0] and "FOR UPDATE" in statement.upper():
            if not revoke_connection:
                revoke_connection.append(conn.connection.driver_connection.thread_id())
                started.set()

    def writer():
        with sessions.begin() as db:
            return rbac_service.check_paid_permission_fenced(db, reviewer_id, PermissionCode.PENTEST_AUTHORIZE)

    def revoke():
        revoke_thread.append(get_ident())
        with sessions.begin() as db:
            if operation == "permanent":
                billing_entitlement_service.set_permanent(db, actor_id=admin_id, user_id=reviewer_id,
                                                          active=False, reason="test")
                billing_entitlement_service.sync_reviewer_role(db, reviewer_id)
            else:
                billing_checkout_service.activate_due_period(db, period_id)

    monkeypatch.setattr(rbac_service, "check_permission_grant", pause_after_authority_lock)
    event.listen(fresh_chain, "before_cursor_execute", observe)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(writer)
            assert locked.wait(10)
            second = pool.submit(revoke)
            assert started.wait(10)
            deadline = monotonic() + 8
            waiting = False
            while monotonic() < deadline:
                with fresh_chain.connect() as conn:
                    waiting = bool(conn.execute(text(
                        "SELECT 1 FROM performance_schema.data_lock_waits w "
                        "JOIN performance_schema.threads t ON t.THREAD_ID = w.REQUESTING_THREAD_ID "
                        "WHERE t.PROCESSLIST_ID = :connection_id LIMIT 1"
                    ), {"connection_id": revoke_connection[0]}).first())
                if waiting:
                    break
                sleep(0.05)
            assert waiting, "权益更新应等待当前授权写入提交"
            release.set()
            assert first.result(timeout=10) is True
            second.result(timeout=10)
    finally:
        release.set()
        event.remove(fresh_chain, "before_cursor_execute", observe)
