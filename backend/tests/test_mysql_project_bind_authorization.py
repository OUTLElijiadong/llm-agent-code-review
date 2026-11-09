"""A project cannot enter an enterprise while a personal authorization is live."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, get_ident
from time import monotonic, sleep
from uuid import uuid4

import pytest
import test_fresh_database_chain as chain
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker
from test_mysql_reviewer_refund_approval import _seed

from app.core.config import settings
from app.core.exceptions import ConflictError
from app.core.permission_codes import PermissionCode
from app.core.rbac_dependency import require_permission
from app.models.enterprise_project import EnterpriseProject
from app.models.pentest import PentestEngagement
from app.models.project import Project
from app.models.user import User
from app.services import enterprise_org_service as org
from app.services import enterprise_project_service as projects
from app.services import pentest_service

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


def _scenario(sessions):
    reviewer_id, _, public_id = _seed(sessions)
    with sessions.begin() as db:
        owner = User(username=f"bind-owner-{uuid4().hex[:8]}", password="x", role="user", status=1)
        db.add(owner)
        db.flush()
        company = org.create_enterprise_from_paid_order(
            db, owner_id=owner.id, name="企业", seat_limit=2, paid_order_id=f"bind-{uuid4().hex[:8]}",
        )
        group = org.create_group(db, company.id, owner.id, "组")
        org.set_group_member(db, company.id, group.id, owner.id, owner.id, "member")
        project = Project(user_id=owner.id, project_name="待归属", status="active")
        db.add(project)
        db.flush()
        task = db.query(PentestEngagement).filter_by(public_id=public_id).one()
        task.user_id, task.project_id = owner.id, project.id
        return reviewer_id, public_id, owner.id, company.id, group.id, project.id


def test_binding_commits_before_old_snapshot_approval(fresh_chain, monkeypatch):
    monkeypatch.setattr(settings, "billing_entitlement_enforcement_ready", True)
    monkeypatch.setattr(settings, "pentest_enabled", True)
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    reviewer, public_id, owner, enterprise, group, project = _scenario(sessions)
    with sessions() as stale:
        actor = stale.get(User, reviewer)
        require_permission(PermissionCode.PENTEST_AUTHORIZE)(user=actor, db=stale)
        assert stale.query(EnterpriseProject).filter_by(project_id=project).first() is None
        with sessions() as binding:
            projects.bind_owned_project(binding, enterprise, project, group_id=group, actor_id=owner)
        with pytest.raises(pentest_service.PentestNotFoundError):
            pentest_service.authorize_engagement(stale, actor, public_id)
    with sessions() as db:
        assert db.query(PentestEngagement).filter_by(public_id=public_id).one().status == "pending_authorization"


def test_approval_first_fences_binding_and_binding_rejects_live_authorization(fresh_chain, monkeypatch):
    monkeypatch.setattr(settings, "billing_entitlement_enforcement_ready", True)
    monkeypatch.setattr(settings, "pentest_enabled", True)
    monkeypatch.setattr(pentest_service, "engagement_to_dict", lambda _db, row, **_: {"status": row.status})
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    reviewer, public_id, owner, enterprise, group, project = _scenario(sessions)
    locked, release, binding_started = Event(), Event(), Event()
    approval_thread, binding_thread, binding_connection = [], [], []

    def observe(conn, _cursor, statement, _parameters, _context, _many):
        upper = statement.upper()
        if approval_thread and get_ident() == approval_thread[0] and "FROM PROJECT" in upper and "FOR UPDATE" in upper:
            # Pause immediately before the following statement; the lock is acquired
            # by a matching after_cursor listener below.
            return
        if binding_thread and get_ident() == binding_thread[0] and "FROM PROJECT" in upper and "FOR UPDATE" in upper:
            binding_connection.append(conn.connection.driver_connection.thread_id())
            binding_started.set()

    def after(conn, _cursor, statement, _parameters, _context, _many):
        upper = statement.upper()
        if approval_thread and get_ident() == approval_thread[0] and "FROM PROJECT" in upper and "FOR UPDATE" in upper:
            locked.set()
            assert release.wait(15)

    def approve():
        approval_thread.append(get_ident())
        with sessions() as db:
            return pentest_service.authorize_engagement(db, db.get(User, reviewer), public_id)["status"]

    def bind():
        binding_thread.append(get_ident())
        with sessions() as db:
            with pytest.raises(ConflictError, match="渗透|授权"):
                projects.bind_owned_project(db, enterprise, project, group_id=group, actor_id=owner)
            db.rollback()

    event.listen(fresh_chain, "before_cursor_execute", observe)
    event.listen(fresh_chain, "after_cursor_execute", after)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(approve)
            assert locked.wait(15)
            second = pool.submit(bind)
            assert binding_started.wait(15)
            deadline = monotonic() + 8
            observed = False
            while monotonic() < deadline:
                with fresh_chain.connect() as conn:
                    observed = bool(conn.execute(text(
                        "SELECT 1 FROM performance_schema.data_lock_waits w "
                        "JOIN performance_schema.threads t ON t.THREAD_ID = w.REQUESTING_THREAD_ID "
                        "WHERE t.PROCESSLIST_ID = :connection_id LIMIT 1"
                    ), {"connection_id": binding_connection[0]}).first())
                if observed:
                    break
                sleep(0.05)
            assert observed, "企业项目绑定必须等待正在批准的项目锁"
            release.set()
            assert first.result(timeout=15) == "authorized"
            second.result(timeout=15)
    finally:
        release.set()
        event.remove(fresh_chain, "before_cursor_execute", observe)
        event.remove(fresh_chain, "after_cursor_execute", after)
    with sessions() as db:
        assert db.query(EnterpriseProject).filter_by(project_id=project).first() is None
        assert db.query(PentestEngagement).filter_by(public_id=public_id).one().status == "authorized"
