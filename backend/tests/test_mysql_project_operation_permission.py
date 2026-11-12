"""Live operation grants and lock conflicts for project/review writes."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event, get_ident
from time import monotonic, sleep

import pytest
import test_fresh_database_chain as chain
import test_mysql_reviewer_refund_approval as reviewer_cases
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker
from test_mysql_pentest_revoked_project_access import _seed

from app.api.v1 import issues, projects, review
from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import authenticate_access_token
from app.core.error_handlers import register_handlers
from app.core.exceptions import AuthError, ConflictError, PermissionError
from app.core.permission_codes import PermissionCode
from app.core.rbac_dependency import require_permission
from app.core.security import create_access_token
from app.models import load_all_models
from app.models.billing_ledger import BillingOrder, BillingPermanentReviewer, BillingRefund
from app.models.project import Project
from app.models.rbac import Permission, Role, RolePermission
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.user import User
from app.schemas.project import ProjectUpdateIn
from app.services import billing_checkout_service, issue_service, project_service, rbac_service, review_service

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


def _invoke(db, actor, operation, project_id, task_id, issue_id):
    if operation == "project_update":
        return project_service.update_project(db, actor, project_id, ProjectUpdateIn(description="authorized"))
    if operation == "project_delete":
        return project_service.delete_project(db, actor, project_id)
    if operation == "issue_status":
        return issue_service.update_status(db, actor, issue_id, "fixed")
    if operation == "issue_decision":
        return issue_service.review_decision(db, actor, issue_id, "accepted")
    if operation == "issue_batch":
        return issue_service.batch_update_status(db, actor, [issue_id], "fixed")
    if operation == "review_cancel":
        return review_service.cancel_task(db, actor, task_id)
    return review_service.delete_task(db, actor, task_id)


OPERATIONS = [
    ("project_update", PermissionCode.PROJECT_UPDATE),
    ("project_delete", PermissionCode.PROJECT_DELETE),
    ("issue_status", PermissionCode.ISSUE_HANDLE),
    ("issue_decision", PermissionCode.ISSUE_HANDLE),
    ("issue_batch", PermissionCode.ISSUE_BATCH),
    ("review_cancel", PermissionCode.REVIEW_CANCEL),
    ("review_delete", PermissionCode.REVIEW_CANCEL),
]


@pytest.mark.parametrize("operation,permission", OPERATIONS)
def test_operation_grant_revoked_after_jwt_dependency_fails_closed(fresh_chain, operation, permission):
    load_all_models()
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    owner_id, _, _, project_id, _, role_id = _seed(factory)
    with factory.begin() as db:
        permission_id = db.query(Permission.id).filter_by(code=permission).scalar()
        assert permission_id is not None
        if not db.query(RolePermission).filter_by(role_id=role_id, permission_id=permission_id).first():
            db.add(RolePermission(role_id=role_id, permission_id=permission_id))
        task = ReviewTask(user_id=owner_id, project_id=project_id,
                          status="completed" if operation == "project_delete" else "running")
        db.add(task)
        db.flush()
        issue = ReviewIssue(task_id=task.id, issue_type="probe", severity="低", description="audit")
        db.add(issue)
        db.flush()
        task_id, issue_id = task.id, issue.id
    token = create_access_token(owner_id, "user", token_version=0)
    with factory() as stale:
        actor = authenticate_access_token(token, stale)
        require_permission(permission)(actor, stale)
        with factory() as current:
            other_grants = [row.permission_id for row in current.query(RolePermission)
                            .filter_by(role_id=role_id) if row.permission_id != permission_id]
            rbac_service.assign_permissions_to_role(current, role_id, other_grants)
        with pytest.raises(PermissionError):
            _invoke(stale, actor, operation, project_id, task_id, issue_id)


def test_project_delete_maps_task_lock_timeout_to_conflict(fresh_chain):
    load_all_models()
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    owner_id, _, _, project_id, _, _ = _seed(factory)
    with factory.begin() as db:
        task = ReviewTask(user_id=owner_id, project_id=project_id, status="running")
        db.add(task)
        db.flush()
        task_id = task.id
    held, release = Event(), Event()

    def lock_task():
        with factory.begin() as db:
            db.query(ReviewTask).filter_by(id=task_id).with_for_update().one()
            held.set()
            assert release.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(lock_task)
        try:
            assert held.wait(5)
            with factory() as db:
                db.execute(text("SET SESSION innodb_lock_wait_timeout=1"))
                with pytest.raises(ConflictError):
                    project_service.delete_project(db, db.get(User, owner_id), project_id)
        finally:
            release.set()
        holder.result(timeout=10)


def test_batch_uses_its_own_permission_not_single_issue_permission(fresh_chain):
    load_all_models()
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    owner_id, _, _, project_id, _, role_id = _seed(factory)
    with factory.begin() as db:
        codes = [PermissionCode.ISSUE_BATCH, PermissionCode.ISSUE_HANDLE]
        ids = {row.code: row.id for row in db.query(Permission).filter(Permission.code.in_(codes))}
        db.query(RolePermission).filter_by(role_id=role_id, permission_id=ids[PermissionCode.ISSUE_HANDLE]).delete()
        batch_grant = db.query(RolePermission).filter_by(
            role_id=role_id, permission_id=ids[PermissionCode.ISSUE_BATCH],
        ).first()
        if not batch_grant:
            db.add(RolePermission(role_id=role_id, permission_id=ids[PermissionCode.ISSUE_BATCH]))
        task = ReviewTask(user_id=owner_id, project_id=project_id, status="completed")
        db.add(task)
        db.flush()
        issues = [ReviewIssue(task_id=task.id, issue_type="probe", severity="低", description="audit")
                  for _ in range(2)]
        db.add_all(issues)
        db.flush()
        issue_ids = [row.id for row in issues]
    with factory() as db:
        actor = authenticate_access_token(create_access_token(owner_id, "user", token_version=0), db)
        require_permission(PermissionCode.ISSUE_BATCH)(actor, db)
        issue_service.batch_update_status(db, actor, issue_ids, "fixed")
        assert [db.get(ReviewIssue, item).status for item in issue_ids] == ["fixed", "fixed"]


def test_batch_rechecks_grant_after_each_independent_commit(fresh_chain, monkeypatch):
    load_all_models()
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    owner_id, _, _, project_id, _, role_id = _seed(factory)
    with factory.begin() as db:
        permission_id = db.query(Permission.id).filter_by(code=PermissionCode.ISSUE_BATCH).scalar()
        if not db.query(RolePermission).filter_by(role_id=role_id, permission_id=permission_id).first():
            db.add(RolePermission(role_id=role_id, permission_id=permission_id))
        task = ReviewTask(user_id=owner_id, project_id=project_id, status="completed")
        db.add(task)
        db.flush()
        issues = [ReviewIssue(task_id=task.id, issue_type="probe", severity="低", description="audit")
                  for _ in range(2)]
        db.add_all(issues)
        db.flush()
        issue_ids = [row.id for row in issues]
    original = issue_service.update_status
    calls = 0

    def change_after_first(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            with factory() as db:
                remaining = [row.permission_id for row in db.query(RolePermission)
                             .filter_by(role_id=role_id) if row.permission_id != permission_id]
                rbac_service.assign_permissions_to_role(db, role_id, remaining)
        return result

    monkeypatch.setattr(issue_service, "update_status", change_after_first)
    with factory() as db:
        actor = authenticate_access_token(create_access_token(owner_id, "user", token_version=0), db)
        require_permission(PermissionCode.ISSUE_BATCH)(actor, db)
        with pytest.raises(PermissionError):
            issue_service.batch_update_status(db, actor, issue_ids, "fixed")
    with factory() as db:
        assert db.get(ReviewIssue, issue_ids[0]).status == "fixed"
        assert db.get(ReviewIssue, issue_ids[1]).status != "fixed"


def test_real_jwt_project_api_rechecks_grant_after_dependency(fresh_chain, monkeypatch):
    load_all_models()
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    owner_id, _, _, project_id, _, role_id = _seed(factory)
    with factory.begin() as db:
        permission_id = db.query(Permission.id).filter_by(code=PermissionCode.PROJECT_UPDATE).scalar()
        if not db.query(RolePermission).filter_by(role_id=role_id, permission_id=permission_id).first():
            db.add(RolePermission(role_id=role_id, permission_id=permission_id))
    original = project_service.update_project

    def revoke_then_update(*args, **kwargs):
        with factory() as db:
            remaining = [row.permission_id for row in db.query(RolePermission)
                         .filter_by(role_id=role_id) if row.permission_id != permission_id]
            rbac_service.assign_permissions_to_role(db, role_id, remaining)
        return original(*args, **kwargs)

    monkeypatch.setattr(project_service, "update_project", revoke_then_update)
    app = FastAPI()
    register_handlers(app)
    app.include_router(projects.router, prefix="/projects")

    def isolated_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = isolated_db
    headers = {"Authorization": f"Bearer {create_access_token(owner_id, 'user', token_version=0)}"}
    with TestClient(app) as client:
        response = client.put(f"/projects/{project_id}", headers=headers, json={"description": "denied"})
    assert response.status_code == 403, response.text
    assert response.json()["code"] == 40303


@pytest.mark.parametrize("kind,permission", [
    ("batch", PermissionCode.ISSUE_BATCH),
    ("cancel", PermissionCode.REVIEW_CANCEL),
])
def test_real_jwt_review_and_batch_apis_recheck_their_grant(fresh_chain, monkeypatch, kind, permission):
    load_all_models()
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    owner_id, _, _, project_id, _, role_id = _seed(factory)
    with factory.begin() as db:
        permission_id = db.query(Permission.id).filter_by(code=permission).scalar()
        if not db.query(RolePermission).filter_by(role_id=role_id, permission_id=permission_id).first():
            db.add(RolePermission(role_id=role_id, permission_id=permission_id))
        task = ReviewTask(user_id=owner_id, project_id=project_id, status="running")
        db.add(task)
        db.flush()
        issue = ReviewIssue(task_id=task.id, issue_type="probe", severity="低", description="audit")
        db.add(issue)
        db.flush()
        task_id, issue_id = task.id, issue.id
    module = issue_service if kind == "batch" else review_service
    method = "batch_update_status" if kind == "batch" else "cancel_task"
    original = getattr(module, method)

    def revoke_then_write(*args, **kwargs):
        with factory() as db:
            remaining = [row.permission_id for row in db.query(RolePermission)
                         .filter_by(role_id=role_id) if row.permission_id != permission_id]
            rbac_service.assign_permissions_to_role(db, role_id, remaining)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, method, revoke_then_write)
    app = FastAPI()
    register_handlers(app)
    app.include_router(issues.router, prefix="/issues")
    app.include_router(review.router, prefix="/review")

    def isolated_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = isolated_db
    headers = {"Authorization": f"Bearer {create_access_token(owner_id, 'user', token_version=0)}"}
    with TestClient(app) as client:
        if kind == "batch":
            response = client.post("/issues/batch-status", headers=headers, json={"ids": [issue_id], "status": "fixed"})
        else:
            response = client.post(f"/review/tasks/{task_id}/cancel", headers=headers)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == 40303


@pytest.mark.parametrize("operation,permission", OPERATIONS)
def test_current_authenticated_operation_grant_keeps_existing_behavior(fresh_chain, operation, permission):
    load_all_models()
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    owner_id, _, _, project_id, _, role_id = _seed(factory)
    with factory.begin() as db:
        permission_id = db.query(Permission.id).filter_by(code=permission).scalar()
        if not db.query(RolePermission).filter_by(role_id=role_id, permission_id=permission_id).first():
            db.add(RolePermission(role_id=role_id, permission_id=permission_id))
        task = ReviewTask(user_id=owner_id, project_id=project_id,
                          status="completed" if operation == "project_delete" else "running")
        db.add(task)
        db.flush()
        issue = ReviewIssue(task_id=task.id, issue_type="probe", severity="低", description="audit")
        db.add(issue)
        db.flush()
        task_id, issue_id = task.id, issue.id
    with factory() as db:
        actor = authenticate_access_token(create_access_token(owner_id, "user", token_version=0), db)
        require_permission(permission)(actor, db)
        _invoke(db, actor, operation, project_id, task_id, issue_id)


def test_operation_grant_lock_conflict_is_retryable_without_wait(fresh_chain):
    load_all_models()
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    owner_id, _, _, project_id, _, role_id = _seed(factory)
    with factory.begin() as db:
        permission_id = db.query(Permission.id).filter_by(code=PermissionCode.PROJECT_UPDATE).scalar()
        grant = db.query(RolePermission).filter_by(role_id=role_id, permission_id=permission_id).first()
        if not grant:
            grant = RolePermission(role_id=role_id, permission_id=permission_id)
            db.add(grant)
            db.flush()
    held, release = Event(), Event()

    def lock_grant():
        with factory.begin() as db:
            db.query(RolePermission).filter_by(
                role_id=role_id, permission_id=permission_id,
            ).delete(synchronize_session=False)
            held.set()
            assert release.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(lock_grant)
        try:
            assert held.wait(5)
            with factory() as db:
                actor = authenticate_access_token(create_access_token(owner_id, "user", token_version=0), db)
                require_permission(PermissionCode.PROJECT_UPDATE)(actor, db)
                started = monotonic()
                with pytest.raises(ConflictError):
                    project_service.update_project(db, actor, project_id, ProjectUpdateIn(description="denied"))
                assert monotonic() - started < 2
        finally:
            release.set()
        holder.result(timeout=10)


def test_reviewer_refund_after_request_dependency_prevents_project_write(fresh_chain, monkeypatch):
    load_all_models()
    monkeypatch.setattr(settings, "billing_entitlement_enforcement_ready", True)
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    reviewer_id, refund_no, _ = reviewer_cases._seed(factory)
    with factory.begin() as db:
        reviewer = db.get(User, reviewer_id)
        role_id = db.query(Role.id).filter_by(code="reviewer").scalar()
        assert reviewer.role == "reviewer"
        pid = db.query(Permission.id).filter_by(code=PermissionCode.PROJECT_UPDATE).scalar()
        if not db.query(RolePermission).filter_by(role_id=role_id, permission_id=pid).first():
            db.add(RolePermission(role_id=role_id, permission_id=pid))
        project = Project(user_id=reviewer_id, project_name="reviewer-fence", status="active")
        db.add(project)
        db.flush()
        project_id = project.id
    with factory() as stale:
        actor = authenticate_access_token(create_access_token(reviewer_id, "reviewer", token_version=0), stale)
        require_permission(PermissionCode.PROJECT_UPDATE)(actor, stale)
        with factory.begin() as current:
            billing_checkout_service.review_manual_refund(
                current, refund_no=refund_no, admin_id=reviewer_id + 3000, decision="approve", reason="test",
            )
        with pytest.raises((AuthError, PermissionError)):
            project_service.update_project(stale, actor, project_id, ProjectUpdateIn(description="denied"))


def test_permanent_reviewer_keeps_authorized_project_write_after_paid_refund(fresh_chain, monkeypatch):
    load_all_models()
    monkeypatch.setattr(settings, "billing_entitlement_enforcement_ready", True)
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    reviewer_id, refund_no, _ = reviewer_cases._seed(factory)
    with factory.begin() as db:
        role_id = db.query(Role.id).filter_by(code="reviewer").scalar()
        pid = db.query(Permission.id).filter_by(code=PermissionCode.PROJECT_UPDATE).scalar()
        if not db.query(RolePermission).filter_by(role_id=role_id, permission_id=pid).first():
            db.add(RolePermission(role_id=role_id, permission_id=pid))
        db.add(BillingPermanentReviewer(
            user_id=reviewer_id, status="active", granted_by=1,
            granted_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1),
        ))
        project = Project(user_id=reviewer_id, project_name="permanent-reviewer", status="active")
        db.add(project)
        db.flush()
        project_id = project.id
    with factory() as stale:
        actor = authenticate_access_token(create_access_token(reviewer_id, "reviewer", token_version=0), stale)
        require_permission(PermissionCode.PROJECT_UPDATE)(actor, stale)
        with factory.begin() as current:
            billing_checkout_service.review_manual_refund(
                current, refund_no=refund_no, admin_id=reviewer_id + 3000, decision="approve", reason="test",
            )
        changed = project_service.update_project(stale, actor, project_id, ProjectUpdateIn(description="permitted"))
        assert changed.description == "permitted"


def test_reviewer_entitlement_lock_conflict_is_retryable_without_wait(fresh_chain, monkeypatch):
    load_all_models()
    monkeypatch.setattr(settings, "billing_entitlement_enforcement_ready", True)
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    reviewer_id, refund_no, _ = reviewer_cases._seed(factory)
    with factory.begin() as db:
        role_id = db.query(Role.id).filter_by(code="reviewer").scalar()
        pid = db.query(Permission.id).filter_by(code=PermissionCode.PROJECT_UPDATE).scalar()
        if not db.query(RolePermission).filter_by(role_id=role_id, permission_id=pid).first():
            db.add(RolePermission(role_id=role_id, permission_id=pid))
        order_no = db.query(BillingRefund).filter_by(refund_no=refund_no).one().order_no
        project = Project(user_id=reviewer_id, project_name="reviewer-lock", status="active")
        db.add(project)
        db.flush()
        project_id = project.id
    held, release = Event(), Event()

    def lock_order():
        with factory.begin() as db:
            db.query(BillingOrder).filter_by(order_no=order_no).with_for_update().one()
            held.set()
            assert release.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(lock_order)
        try:
            assert held.wait(5)
            with factory() as db:
                actor = authenticate_access_token(create_access_token(reviewer_id, "reviewer", token_version=0), db)
                require_permission(PermissionCode.PROJECT_UPDATE)(actor, db)
                started = monotonic()
                with pytest.raises(ConflictError):
                    project_service.update_project(db, actor, project_id, ProjectUpdateIn(description="wait"))
                assert monotonic() - started < 2
        finally:
            release.set()
        holder.result(timeout=10)


def test_operation_grant_revocation_waits_for_inflight_project_commit(fresh_chain, monkeypatch):
    load_all_models()
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    owner_id, _, _, project_id, _, role_id = _seed(factory)
    with factory.begin() as db:
        permission_id = db.query(Permission.id).filter_by(code=PermissionCode.PROJECT_UPDATE).scalar()
        if not db.query(RolePermission).filter_by(role_id=role_id, permission_id=permission_id).first():
            db.add(RolePermission(role_id=role_id, permission_id=permission_id))
    fenced, release, delete_started = Event(), Event(), Event()
    changer_threads, changer_connections = [], []
    original = project_service.require_live_operation_permission

    def pause_fenced(*args, **kwargs):
        original(*args, **kwargs)
        fenced.set()
        assert release.wait(10)

    def observe(conn, _cursor, statement, _params, _context, _many):
        if changer_threads and get_ident() == changer_threads[0] and statement.lstrip().upper().startswith("DELETE"):
            changer_connections.append(conn.connection.driver_connection.thread_id())
            delete_started.set()

    def write():
        with factory() as db:
            actor = authenticate_access_token(create_access_token(owner_id, "user", token_version=0), db)
            return project_service.update_project(db, actor, project_id, ProjectUpdateIn(description="completed")).id

    def revoke():
        changer_threads.append(get_ident())
        with factory() as db:
            remaining = [row.permission_id for row in db.query(RolePermission)
                         .filter_by(role_id=role_id) if row.permission_id != permission_id]
            rbac_service.assign_permissions_to_role(db, role_id, remaining)

    monkeypatch.setattr(project_service, "require_live_operation_permission", pause_fenced)
    event.listen(fresh_chain, "before_cursor_execute", observe)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            writer = pool.submit(write)
            try:
                assert fenced.wait(10)
                changer = pool.submit(revoke)
                assert delete_started.wait(10)
                deadline, observed = monotonic() + 5, False
                while monotonic() < deadline:
                    with fresh_chain.connect() as conn:
                        observed = bool(conn.execute(text(
                            "SELECT 1 FROM performance_schema.data_lock_waits w "
                            "JOIN performance_schema.threads t ON t.THREAD_ID=w.REQUESTING_THREAD_ID "
                            "WHERE t.PROCESSLIST_ID=:connection_id LIMIT 1"
                        ), {"connection_id": changer_connections[0]}).first())
                    if observed:
                        break
                    sleep(0.05)
                assert observed, "grant revocation must wait for the authorized write transaction"
            finally:
                release.set()
            assert writer.result(timeout=10) == project_id
            changer.result(timeout=10)
    finally:
        release.set()
        event.remove(fresh_chain, "before_cursor_execute", observe)
