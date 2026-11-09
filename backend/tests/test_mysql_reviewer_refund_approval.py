"""Paid reviewer permission is fenced with a concurrent refund on MySQL RR."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event, get_ident
from time import monotonic, sleep
from uuid import uuid4

import pytest
import test_fresh_database_chain as chain
from sqlalchemy import event, func, text
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.exceptions import ForbiddenError
from app.core.permission_codes import PermissionCode
from app.core.rbac_dependency import require_permission
from app.models.billing_ledger import (
    BillingOrder,
    BillingPaymentEvent,
    BillingPermanentReviewer,
    BillingRefund,
    BillingSubscription,
    BillingSubscriptionPeriod,
)
from app.models.pentest import PentestEngagement
from app.models.project import Project
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.remote_pentest import RemotePentestEngagement
from app.models.user import User
from app.services import billing_checkout_service, billing_entitlement_service, pentest_service, remote_pentest_service
from app.services.billing_ledger_service import get_or_create_wallet

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


def _seed(sessions):
    suffix = uuid4().hex[:12]
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with sessions.begin() as db:
        role = db.query(Role).filter_by(code="reviewer", status="active").one()
        permission = db.query(Permission).filter_by(code=PermissionCode.PENTEST_AUTHORIZE).one()
        if not db.query(RolePermission.id).filter_by(role_id=role.id, permission_id=permission.id).first():
            db.add(RolePermission(role_id=role.id, permission_id=permission.id))
        reviewer = User(username=f"refund-reviewer-{suffix}", password="x", role="reviewer", status=1)
        db.add(reviewer)
        db.flush()
        db.add(UserRole(user_id=reviewer.id, role_id=role.id))
        get_or_create_wallet(db, "user", reviewer.id)
        order_no, refund_no, public_id = f"ord-{suffix}", f"ref-{suffix}", f"pen-{suffix}"
        db.add(BillingOrder(order_no=order_no, user_id=reviewer.id, product="reviewer_month", quantity=1,
                            channel="alipay", amount_fen=100, status="paid", payment_status="paid"))
        db.add(BillingPaymentEvent(channel="alipay", trade_id=f"trade-{suffix}", order_no=order_no,
                                   amount_fen=100))
        sub = BillingSubscription(owner_type="user", owner_id=reviewer.id, product="reviewer",
                                  anchor_at=now, last_end_at=now + timedelta(days=30), next_offset=1)
        db.add(sub)
        db.flush()
        db.add(BillingSubscriptionPeriod(subscription_id=sub.id, order_no=order_no,
                                         starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=30),
                                         credit_milli=5_000_000, seats=1))
        db.add(BillingRefund(order_no=order_no, refund_no=refund_no, amount_fen=100, reason="test",
                             requested_by=reviewer.id, status="manual_review"))
        project = Project(user_id=reviewer.id + 1000, project_name=f"refund-project-{suffix}", status="active")
        db.add(project)
        db.flush()
        db.add(PentestEngagement(public_id=public_id, user_id=reviewer.id + 1000, project_id=project.id,
                                 target_type="web", scope_json='{"requested_window_hours":1}',
                                 status="pending_authorization", rules_ack=1))
        reviewer_id = reviewer.id
    return reviewer_id, refund_no, public_id


def _seed_remote(sessions, reviewer_id):
    public_id = f"remote-{uuid4().hex[:12]}"
    with sessions.begin() as db:
        row = RemotePentestEngagement(
            public_id=public_id, owner_id=reviewer_id + 1000, task_name="test",
            target_url="https://example.invalid", target_host="example.invalid",
            authorization_basis="test", scope_notes="test", scope_digest="", rules_version="test",
            rules_ack=1, requested_window_hours=1, status="pending_authorization",
        )
        row.scope_digest = remote_pentest_service._scope_digest(row)
        db.add(row)
    return public_id


def test_refund_commit_invalidates_old_reviewer_approval_transaction(fresh_chain, monkeypatch):
    monkeypatch.setattr(settings, "billing_entitlement_enforcement_ready", True)
    monkeypatch.setattr(settings, "pentest_enabled", True)
    # Presentation is irrelevant to authorization and the persisted status.
    monkeypatch.setattr(pentest_service, "engagement_to_dict", lambda db, row, **_: {"status": row.status})
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    reviewer_id, refund_no, public_id = _seed(sessions)
    with sessions() as stale:
        actor = stale.get(User, reviewer_id)
        require_permission(PermissionCode.PENTEST_AUTHORIZE)(user=actor, db=stale)
        with sessions.begin() as refund_db:
            billing_checkout_service.review_manual_refund(
                refund_db, refund_no=refund_no, admin_id=reviewer_id + 3000, decision="approve", reason="test",
            )
        with pytest.raises(pentest_service.PentestAccessError):
            pentest_service.authorize_engagement(stale, actor, public_id)
    with sessions() as fresh:
        assert fresh.query(PentestEngagement).filter_by(public_id=public_id).one().status == "pending_authorization"


def test_refund_commit_invalidates_old_remote_approval_transaction(fresh_chain, monkeypatch):
    monkeypatch.setattr(settings, "billing_entitlement_enforcement_ready", True)
    monkeypatch.setattr(remote_pentest_service, "_to_dict", lambda db, row, **_: {"status": row.status})
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    reviewer_id, refund_no, _ = _seed(sessions)
    public_id = _seed_remote(sessions, reviewer_id)
    with sessions() as stale:
        actor = stale.get(User, reviewer_id)
        require_permission(PermissionCode.PENTEST_AUTHORIZE)(user=actor, db=stale)
        with sessions.begin() as refund_db:
            billing_checkout_service.review_manual_refund(
                refund_db, refund_no=refund_no, admin_id=reviewer_id + 3000, decision="approve", reason="test",
            )
        with pytest.raises(ForbiddenError, match="批准|权限"):
            remote_pentest_service.authorize_task(stale, actor, public_id)
    with sessions() as fresh:
        row = fresh.query(RemotePentestEngagement).filter_by(public_id=public_id).one()
        assert row.status == "pending_authorization"


@pytest.mark.parametrize("kind", ["personal", "remote"])
def test_permanent_reviewer_can_approve_after_paid_refund(fresh_chain, monkeypatch, kind):
    monkeypatch.setattr(settings, "billing_entitlement_enforcement_ready", True)
    monkeypatch.setattr(settings, "pentest_enabled", True)
    monkeypatch.setattr(pentest_service, "engagement_to_dict", lambda db, row, **_: {"status": row.status})
    monkeypatch.setattr(remote_pentest_service, "_to_dict", lambda db, row, **_: {"status": row.status})
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    reviewer_id, refund_no, public_id = _seed(sessions)
    if kind == "remote":
        public_id = _seed_remote(sessions, reviewer_id)
    with sessions.begin() as db:
        db.add(BillingPermanentReviewer(user_id=reviewer_id, status="active", granted_by=1,
                                        granted_at=datetime.now(timezone.utc).replace(tzinfo=None)
                                        - timedelta(seconds=1)))
    with sessions.begin() as db:
        billing_checkout_service.review_manual_refund(
            db, refund_no=refund_no, admin_id=reviewer_id + 3000, decision="approve", reason="test",
        )
    with sessions() as db:
        actor = db.get(User, reviewer_id)
        if kind == "remote":
            result = remote_pentest_service.authorize_task(db, actor, public_id)
        else:
            result = pentest_service.authorize_engagement(db, actor, public_id)
        assert result["status"] == "authorized"


def test_same_second_permanent_grant_survives_manual_refund(fresh_chain, monkeypatch):
    monkeypatch.setattr(settings, "billing_entitlement_enforcement_ready", True)
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    reviewer_id, refund_no, _ = _seed(sessions)
    with sessions() as db:
        for _ in range(100):
            db_now = db.query(func.utc_timestamp(6)).scalar()
            if 10_000 < db_now.microsecond < 400_000:
                break
            sleep(0.01)
        else:
            pytest.fail("无法取得足够长的同秒测试窗口")
        second = db_now.replace(microsecond=0)
        admin = db.query(User).filter_by(username="admin", status=1).one()
        # The grant is already in the past, but remains inside this DB second.
        billing_entitlement_service.set_permanent(
            db, actor_id=admin.id, user_id=reviewer_id, active=True, reason="test",
            now=second + timedelta(microseconds=1),
        )
        db.commit()
    with sessions.begin() as db:
        billing_checkout_service.review_manual_refund(
            db, refund_no=refund_no, admin_id=reviewer_id + 3000, decision="approve", reason="test",
        )
        finished = db.query(func.utc_timestamp(6)).scalar()
        if finished.replace(microsecond=0) != second:
            pytest.skip("测试过程中跨秒，不能判断同秒精度")
        assert db.get(User, reviewer_id).role == "reviewer"
        assert billing_entitlement_service.has_reviewer_entitlement(db, reviewer_id)


@pytest.mark.parametrize("kind", ["personal", "remote"])
def test_approval_holds_user_authority_until_commit_before_refund(fresh_chain, monkeypatch, kind):
    monkeypatch.setattr(settings, "billing_entitlement_enforcement_ready", True)
    monkeypatch.setattr(settings, "pentest_enabled", True)
    monkeypatch.setattr(pentest_service, "engagement_to_dict", lambda db, row, **_: {"status": row.status})
    monkeypatch.setattr(remote_pentest_service, "_to_dict", lambda db, row, **_: {"status": row.status})
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    reviewer_id, refund_no, public_id = _seed(sessions)
    if kind == "remote":
        public_id = _seed_remote(sessions, reviewer_id)
    locked, release, refund_started = Event(), Event(), Event()
    refund_thread, refund_connection = [], []
    original = pentest_service.rbac_service.check_permission_grant

    def wait_after_lock(db, user_id, permission):
        allowed = original(db, user_id, permission)
        locked.set()
        assert release.wait(10)
        return allowed

    monkeypatch.setattr(pentest_service.rbac_service, "check_permission_grant", wait_after_lock)

    def observe(conn, _cursor, statement, _parameters, _context, _many):
        if refund_thread and get_ident() == refund_thread[0] and "FOR UPDATE" in statement.upper():
            if not refund_connection:
                refund_connection.append(conn.connection.driver_connection.thread_id())
                refund_started.set()

    def approve():
        with sessions() as db:
            actor = db.get(User, reviewer_id)
            if kind == "remote":
                return remote_pentest_service.authorize_task(db, actor, public_id)["status"]
            return pentest_service.authorize_engagement(db, actor, public_id)["status"]

    def refund():
        refund_thread.append(get_ident())
        with sessions.begin() as db:
            billing_checkout_service.review_manual_refund(
                db, refund_no=refund_no, admin_id=reviewer_id + 3000, decision="approve", reason="test",
            )
        return "refunded"

    event.listen(fresh_chain, "before_cursor_execute", observe)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(approve)
            assert locked.wait(10)
            second = pool.submit(refund)
            assert refund_started.wait(10)
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
            assert observed, "退款必须等待审批持有的账号权限锁"
            release.set()
            assert first.result(timeout=10) == "authorized"
            assert second.result(timeout=10) == "refunded"
    finally:
        release.set()
        event.remove(fresh_chain, "before_cursor_execute", observe)
