"""Reviewer fences serialize revocation without blocking independent users."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic, sleep

import pytest
import test_mysql_reviewer_refund_approval as cases
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

import app.main  # noqa: F401 - register all models for the fresh-chain fixture
from app.core.config import settings
from app.core.permission_codes import PermissionCode
from app.models.rbac import Permission, Role, RolePermission
from app.services import rbac_service

fresh_chain = cases.fresh_chain
fresh_chain_url = cases.fresh_chain_url


def test_different_reviewers_can_hold_authority_concurrently(fresh_chain, monkeypatch):
    """The shared role must not serialize unrelated reviewer writes."""
    monkeypatch.setattr(settings, "billing_entitlement_enforcement_ready", True)
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    first_id, _, _ = cases._seed(sessions)
    second_id, _, _ = cases._seed(sessions)
    first_locked, release_first, second_done = Event(), Event(), Event()

    def first():
        with sessions.begin() as db:
            assert rbac_service.check_paid_permission_fenced(db, first_id, PermissionCode.PENTEST_AUTHORIZE)
            first_locked.set()
            assert release_first.wait(10)

    def second():
        assert first_locked.wait(10)
        with sessions.begin() as db:
            assert rbac_service.check_paid_permission_fenced(db, second_id, PermissionCode.PENTEST_AUTHORIZE)
            second_done.set()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(first), pool.submit(second)
            try:
                assert second_done.wait(3), "不相关审查员不应等待共享角色排他锁"
            finally:
                release_first.set()
            a.result(timeout=10)
            b.result(timeout=10)
    finally:
        release_first.set()


@pytest.mark.parametrize("revocation", ["permission", "role"])
def test_role_permission_revocation_waits_for_fenced_write(fresh_chain, monkeypatch, revocation):
    """Role and RolePermission remain locked until the protected write commits."""
    monkeypatch.setattr(settings, "billing_entitlement_enforcement_ready", True)
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    reviewer_id, _, _ = cases._seed(sessions)
    locked, release, started = Event(), Event(), Event()
    connection = []

    def reviewer():
        with sessions.begin() as db:
            assert rbac_service.check_paid_permission_fenced(db, reviewer_id, PermissionCode.PENTEST_AUTHORIZE)
            locked.set()
            assert release.wait(10)

    def revoke():
        assert locked.wait(10)
        with sessions.begin() as db:
            connection.append(db.execute(text("SELECT CONNECTION_ID()")).scalar())
            role_id = db.query(Role.id).filter_by(code="reviewer").scalar()
            permission_id = db.query(Permission.id).filter_by(code=PermissionCode.PENTEST_AUTHORIZE).scalar()
            started.set()
            if revocation == "permission":
                db.query(RolePermission).filter_by(role_id=role_id, permission_id=permission_id).delete()
            else:
                db.query(Role).filter_by(id=role_id).update({Role.status: "disabled"})

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(reviewer), pool.submit(revoke)
            assert started.wait(10)
            deadline = monotonic() + 6
            blocked = False
            while monotonic() < deadline:
                with fresh_chain.connect() as conn:
                    blocked = bool(conn.execute(text(
                        "SELECT 1 FROM performance_schema.data_lock_waits w "
                        "JOIN performance_schema.threads t ON t.THREAD_ID = w.REQUESTING_THREAD_ID "
                        "WHERE t.PROCESSLIST_ID = :connection_id LIMIT 1"
                    ), {"connection_id": connection[0]}).first())
                if blocked:
                    break
                sleep(0.05)
            assert blocked, "角色权限撤销必须等待已授权事务"
            release.set()
            a.result(timeout=10)
            b.result(timeout=10)
        with sessions() as db:
            assert not rbac_service.check_paid_permission_fenced(db, reviewer_id, PermissionCode.PENTEST_AUTHORIZE)
    finally:
        release.set()
