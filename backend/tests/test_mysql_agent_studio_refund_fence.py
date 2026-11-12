"""Reviewer Studio writes serialize with paid-entitlement refund changes on MySQL RR."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event, get_ident
from time import monotonic, sleep

import pytest
import test_mysql_reviewer_refund_approval as reviewer_cases
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker

from app.api.v1.agent_studio import require_studio_role
from app.core.config import settings
from app.core.exceptions import ForbiddenError
from app.core.permission_codes import PermissionCode
from app.core.rbac_dependency import require_permission
from app.models.user import User
from app.services import agent_studio_service, billing_checkout_service, billing_entitlement_service, rbac_service

fresh_chain = reviewer_cases.fresh_chain
fresh_chain_url = reviewer_cases.fresh_chain_url


def _create(db, actor):
    return agent_studio_service.create_agent(
        db, actor, code=f"studio_refund_{actor.id}", name="test", description="test",
        prompt="test", review_focus="test", model_config={"temperature": 0.2, "max_tokens": 1024},
    )


def test_refund_before_studio_write_rejects_old_rr_snapshot(fresh_chain, monkeypatch):
    monkeypatch.setattr(settings, "billing_entitlement_enforcement_ready", True)
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    reviewer_id, refund_no, _ = reviewer_cases._seed(sessions)
    with sessions() as stale:
        actor = stale.get(User, reviewer_id)
        require_studio_role(db=stale, user=actor)
        require_permission(PermissionCode.AGENT_ASSET_CREATE)(user=actor, db=stale)
        with sessions.begin() as refund_db:
            billing_checkout_service.review_manual_refund(
                refund_db, refund_no=refund_no, admin_id=reviewer_id + 3000,
                decision="approve", reason="test",
            )
        with pytest.raises(ForbiddenError):
            _create(stale, actor)


def test_permanent_reviewer_keeps_studio_writes_after_refund(fresh_chain, monkeypatch):
    monkeypatch.setattr(settings, "billing_entitlement_enforcement_ready", True)
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    reviewer_id, refund_no, _ = reviewer_cases._seed(sessions)
    with sessions.begin() as db:
        admin = db.query(User).filter_by(username="admin", status=1).one()
        billing_entitlement_service.set_permanent(
            db, actor_id=admin.id, user_id=reviewer_id, active=True, reason="test",
            now=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
    with sessions.begin() as db:
        billing_checkout_service.review_manual_refund(
            db, refund_no=refund_no, admin_id=reviewer_id + 3000, decision="approve", reason="test",
        )
    with sessions() as db:
        actor = db.get(User, reviewer_id)
        assert _create(db, actor)[0].id


def test_studio_write_holds_authority_before_refund_commits(fresh_chain, monkeypatch):
    monkeypatch.setattr(settings, "billing_entitlement_enforcement_ready", True)
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    reviewer_id, refund_no, _ = reviewer_cases._seed(sessions)
    locked, release, started = Event(), Event(), Event()
    refund_thread, refund_connection = [], []
    original = rbac_service.check_permission_grant

    def wait_after_lock(db, user_id, permission):
        allowed = original(db, user_id, permission)
        locked.set()
        assert release.wait(10)
        return allowed

    monkeypatch.setattr(rbac_service, "check_permission_grant", wait_after_lock)

    def observe(conn, _cursor, statement, _parameters, _context, _many):
        if refund_thread and get_ident() == refund_thread[0] and "FOR UPDATE" in statement.upper():
            if not refund_connection:
                refund_connection.append(conn.connection.driver_connection.thread_id())
                started.set()

    def studio_write():
        with sessions() as db:
            actor = db.get(User, reviewer_id)
            return _create(db, actor)[0].id

    def refund():
        refund_thread.append(get_ident())
        with sessions.begin() as db:
            billing_checkout_service.review_manual_refund(
                db, refund_no=refund_no, admin_id=reviewer_id + 3000, decision="approve", reason="test",
            )

    event.listen(fresh_chain, "before_cursor_execute", observe)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(studio_write)
            assert locked.wait(10)
            second = pool.submit(refund)
            assert started.wait(10)
            deadline = monotonic() + 8
            observed = False
            while monotonic() < deadline:
                with fresh_chain.connect() as conn:
                    observed = bool(conn.execute(text(
                        "SELECT 1 FROM performance_schema.data_lock_waits w "
                        "JOIN performance_schema.threads t ON t.THREAD_ID = w.REQUESTING_THREAD_ID "
                        "WHERE t.PROCESSLIST_ID = :connection_id LIMIT 1"
                    ), {"connection_id": refund_connection[0]}).first())
                if observed:
                    break
                sleep(0.05)
            assert observed, "退款必须等待工坊写入持有的账号权限锁"
            release.set()
            assert first.result(timeout=10)
            second.result(timeout=10)
    finally:
        release.set()
        event.remove(fresh_chain, "before_cursor_execute", observe)
