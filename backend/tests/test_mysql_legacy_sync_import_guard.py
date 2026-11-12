"""MySQL RR coverage for the deprecated synchronous HTTP import fence."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from io import BytesIO
from threading import Event
from time import monotonic, sleep
from uuid import uuid4
from zipfile import ZipFile

import pymysql
import pytest
import test_fresh_database_chain as chain
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.api.v1 import code_files, projects
from app.core import error_handlers
from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import authenticate_access_token
from app.core.exceptions import AuthError, PermissionError
from app.core.permission_codes import PermissionCode
from app.core.security import create_access_token
from app.models.billing_ledger import BillingOrder, BillingPaymentEvent, BillingSubscription, BillingSubscriptionPeriod
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.project_source_archive import ProjectSourceArchive
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.user import User
from app.schemas.project import RemoteProjectImportIn
from app.services import billing_entitlement_service, code_file_service, project_member_service, project_source_service

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


def _archive():
    data = BytesIO()
    with ZipFile(data, "w") as archive:
        archive.writestr("main.py", "print(1)")
    return data.getvalue()


def _identity(factory, *, reviewer=False, now=None):
    suffix = uuid4().hex[:14]
    with factory.begin() as db:
        actor = User(username=f"legacy_{suffix}", password="x", role="reviewer" if reviewer else "admin",
                     status=1, token_version=0)
        db.add(actor)
        db.flush()
        if reviewer:
            role = db.query(Role).filter_by(code="reviewer", status="active").one()
            perm = db.query(Permission).filter_by(code=PermissionCode.PROJECT_IMPORT).one()
            if not db.query(RolePermission.id).filter_by(role_id=role.id, permission_id=perm.id).first():
                db.add(RolePermission(role_id=role.id, permission_id=perm.id))
            db.add(UserRole(user_id=actor.id, role_id=role.id))
            order_no = f"legacy-{suffix}"
            db.add(BillingOrder(order_no=order_no, user_id=actor.id, product="reviewer_month", quantity=1,
                                channel="alipay", amount_fen=100, status="paid", payment_status="paid"))
            db.add(BillingPaymentEvent(channel="alipay", trade_id=f"trade-{suffix}", order_no=order_no,
                                       amount_fen=100))
            subscription = BillingSubscription(owner_type="user", owner_id=actor.id, product="reviewer",
                                               anchor_at=now - timedelta(days=30), last_end_at=now, next_offset=1)
            db.add(subscription)
            db.flush()
            db.add(BillingSubscriptionPeriod(subscription_id=subscription.id, order_no=order_no,
                                             starts_at=now - timedelta(days=30),
                                             ends_at=now - timedelta(days=7) + timedelta(seconds=1),
                                             credit_milli=5_000_000, seats=1))
        return actor.id


def _call(db, actor_id, role, name, audit_mode):
    user = authenticate_access_token(create_access_token(actor_id, role), db)
    payload = RemoteProjectImportIn(url="https://example.com/repo.zip", project_name=name, audit_mode=audit_mode)
    return projects.import_remote_project(payload, Response(), db, user)


def _counts(factory, name):
    with factory() as db:
        rows = db.query(Project).filter_by(project_name=name).all()
        ids = [row.id for row in rows]
        return (len(rows), db.query(ProjectMember).filter(ProjectMember.project_id.in_(ids)).count(),
                db.query(CodeFile).filter(CodeFile.project_id.in_(ids)).count(),
                db.query(ProjectSourceArchive).filter(ProjectSourceArchive.project_id.in_(ids)).count())


@pytest.mark.parametrize("audit_mode", [False, True])
@pytest.mark.parametrize("expired", [False, True])
def test_mysql_import_token_expiry(fresh_chain, monkeypatch, audit_mode, expired):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    actor_id = _identity(factory)
    name = f"legacy-token-{uuid4().hex[:12]}"
    monkeypatch.setattr(project_source_service, "download_remote_archive", lambda _url: (_archive(), "repo.zip"))
    with factory() as db:
        user = authenticate_access_token(create_access_token(actor_id, "admin"), db)
        expiry = user._authenticated_token_expires_at
        clock = [expiry - 1]
        monkeypatch.setattr(project_member_service, "time", lambda: clock[0])

        class Clock:
            @staticmethod
            def now(_tz=None):
                return datetime.fromtimestamp(clock[0], timezone.utc)

        monkeypatch.setattr(code_files, "datetime", Clock)

        def scan(*_args, **_kwargs):
            if expired:
                clock[0] = expiry
            if audit_mode:
                return "clean", 0, "{}"

        monkeypatch.setattr(project_source_service if audit_mode else code_file_service,
                            "_scan_quarantined_zip" if audit_mode else "_scan_extracted_file", scan)
        payload = RemoteProjectImportIn(url="https://example.com/repo.zip", project_name=name, audit_mode=audit_mode)
        if expired:
            with pytest.raises(AuthError):
                projects.import_remote_project(payload, Response(), db, user)
        else:
            projects.import_remote_project(payload, Response(), db, user)
    assert _counts(factory, name) == ((0, 0, 0, 0) if expired else (1, 1, int(not audit_mode), int(audit_mode)))


@pytest.mark.parametrize("audit_mode", [False, True])
def test_mysql_import_reviewer_expiry(fresh_chain, monkeypatch, audit_mode):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    clock = [now]
    monkeypatch.setattr(settings, "billing_entitlement_enforcement_ready", True)
    monkeypatch.setattr(billing_entitlement_service, "_now", lambda: clock[0])
    actor_id = _identity(factory, reviewer=True, now=now)
    name = f"legacy-reviewer-{uuid4().hex[:12]}"
    monkeypatch.setattr(project_source_service, "download_remote_archive", lambda _url: (_archive(), "repo.zip"))

    def scan(*_args, **_kwargs):
        clock[0] = now + timedelta(seconds=2)
        if audit_mode:
            return "clean", 0, "{}"

    monkeypatch.setattr(project_source_service if audit_mode else code_file_service,
                        "_scan_quarantined_zip" if audit_mode else "_scan_extracted_file", scan)
    with factory() as db, pytest.raises(PermissionError):
        _call(db, actor_id, "reviewer", name, audit_mode)
    assert _counts(factory, name) == (0, 0, 0, 0)


def test_mysql_permission_revoke_waits_for_import_commit(fresh_chain, monkeypatch):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    actor_id = _identity(factory, reviewer=True, now=datetime.now(timezone.utc).replace(tzinfo=None))
    name = f"legacy-lock-{uuid4().hex[:12]}"
    scanning, release, writer_id = Event(), Event(), []
    monkeypatch.setattr(settings, "billing_entitlement_enforcement_ready", False)
    monkeypatch.setattr(project_source_service, "download_remote_archive", lambda _url: (_archive(), "repo.zip"))

    def hold_scan(*_args, **_kwargs):
        scanning.set()
        assert release.wait(15)

    monkeypatch.setattr(code_file_service, "_scan_extracted_file", hold_scan)

    def import_work():
        with factory() as db:
            return _call(db, actor_id, "reviewer", name, False)

    def revoke_work():
        with factory.begin() as db:
            writer_id.append(db.execute(text("SELECT CONNECTION_ID()")).scalar_one())
            role = db.query(Role).filter_by(code="reviewer").one()
            perm = db.query(Permission).filter_by(code=PermissionCode.PROJECT_IMPORT).one()
            db.query(RolePermission).filter_by(role_id=role.id, permission_id=perm.id).delete()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            importing = pool.submit(import_work)
            assert scanning.wait(10)
            revoking = pool.submit(revoke_work)
            deadline, waiting = monotonic() + 5, False
            while monotonic() < deadline:
                if writer_id:
                    with fresh_chain.connect() as connection:
                        waiting = bool(connection.execute(text(
                            "SELECT 1 FROM performance_schema.data_lock_waits w "
                            "JOIN performance_schema.threads t ON t.THREAD_ID=w.REQUESTING_THREAD_ID "
                            "WHERE t.PROCESSLIST_ID=:cid LIMIT 1"
                        ), {"cid": writer_id[0]}).first())
                    if waiting:
                        break
                sleep(0.05)
            assert waiting, "permission revocation must wait for the import transaction"
            release.set()
            importing.result(timeout=10)
            revoking.result(timeout=10)
        assert _counts(factory, name) == (1, 1, 1, 0)
    finally:
        release.set()


def test_mysql_permission_revoke_before_import_beats_old_snapshot(fresh_chain, monkeypatch):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    actor_id = _identity(factory, reviewer=True, now=datetime.now(timezone.utc).replace(tzinfo=None))
    name = f"legacy-revoked-{uuid4().hex[:12]}"
    monkeypatch.setattr(settings, "billing_entitlement_enforcement_ready", False)
    monkeypatch.setattr(project_source_service, "download_remote_archive", lambda _url: (_archive(), "repo.zip"))
    monkeypatch.setattr(code_file_service, "_scan_extracted_file", lambda *_a, **_k: pytest.fail("must not scan"))
    with factory() as db:
        user = authenticate_access_token(create_access_token(actor_id, "reviewer"), db)
        role = db.query(Role).filter_by(code="reviewer").one()
        perm = db.query(Permission).filter_by(code=PermissionCode.PROJECT_IMPORT).one()
        assert db.query(RolePermission.id).filter_by(role_id=role.id, permission_id=perm.id).first()
        with factory.begin() as other:
            other.query(RolePermission).filter_by(role_id=role.id, permission_id=perm.id).delete()
        with pytest.raises(PermissionError):
            projects.import_remote_project(
                RemoteProjectImportIn(url="https://example.com/repo.zip", project_name=name), Response(), db, user,
            )
    assert _counts(factory, name) == (0, 0, 0, 0)


@pytest.mark.parametrize("change", ["token", "disabled"])
def test_mysql_account_revoked_before_import_beats_old_snapshot(fresh_chain, monkeypatch, change):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    actor_id = _identity(factory)
    name = f"legacy-account-{uuid4().hex[:12]}"
    monkeypatch.setattr(project_source_service, "download_remote_archive", lambda _url: (_archive(), "repo.zip"))
    monkeypatch.setattr(code_file_service, "_scan_extracted_file", lambda *_a, **_k: pytest.fail("must not scan"))
    with factory() as db:
        user = authenticate_access_token(create_access_token(actor_id, "admin"), db)
        # Force the authenticated session's REPEATABLE READ snapshot first.
        assert db.get(User, actor_id).status == 1
        with factory.begin() as other:
            current = other.get(User, actor_id)
            if change == "token":
                current.token_version += 1
            else:
                current.status = 0
        with pytest.raises(PermissionError):
            projects.import_remote_project(
                RemoteProjectImportIn(url="https://example.com/repo.zip", project_name=name), Response(), db, user,
            )
    assert _counts(factory, name) == (0, 0, 0, 0)


def test_mysql_logout_waits_for_import_without_exposing_uncommitted_project(fresh_chain, monkeypatch):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    actor_id = _identity(factory)
    name = f"legacy-logout-{uuid4().hex[:12]}"
    scanning, release, writer_id = Event(), Event(), []
    monkeypatch.setattr(project_source_service, "download_remote_archive", lambda _url: (_archive(), "repo.zip"))

    def hold_scan(*_args, **_kwargs):
        scanning.set()
        assert release.wait(15)

    monkeypatch.setattr(code_file_service, "_scan_extracted_file", hold_scan)

    def import_work():
        with factory() as db:
            return _call(db, actor_id, "admin", name, False)

    def logout_work():
        with factory.begin() as db:
            writer_id.append(db.execute(text("SELECT CONNECTION_ID()")).scalar_one())
            # The import has not yet committed its new project while scanning.
            assert db.query(Project.id).filter_by(project_name=name).first() is None
            db.query(User).filter_by(id=actor_id).update({User.token_version: User.token_version + 1})

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            importing = pool.submit(import_work)
            assert scanning.wait(10)
            revoking = pool.submit(logout_work)
            deadline, waiting = monotonic() + 5, False
            while monotonic() < deadline:
                if writer_id:
                    with fresh_chain.connect() as connection:
                        waiting = bool(connection.execute(text(
                            "SELECT 1 FROM performance_schema.data_lock_waits w "
                            "JOIN performance_schema.threads t ON t.THREAD_ID=w.REQUESTING_THREAD_ID "
                            "WHERE t.PROCESSLIST_ID=:cid LIMIT 1"
                        ), {"cid": writer_id[0]}).first())
                    if waiting:
                        break
                sleep(0.05)
            assert waiting, "logout must wait for the import's actor lock"
            release.set()
            importing.result(timeout=10)
            revoking.result(timeout=10)
        assert _counts(factory, name) == (1, 1, 1, 0)
    finally:
        release.set()


def test_mysql_http_busy_actor_lock_returns_retryable_conflict_without_writes(fresh_chain, monkeypatch):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    actor_id = _identity(factory)
    name = f"legacy-busy-{uuid4().hex[:12]}"
    monkeypatch.setattr(project_source_service, "download_remote_archive", lambda _url: (_archive(), "repo.zip"))
    locked, release = Event(), Event()
    original_authorize = code_files._authorize_folder_actor
    observed_codes = []

    def trace_authorize(*args, **kwargs):
        try:
            return original_authorize(*args, **kwargs)
        except OperationalError as exc:
            observed_codes.append(exc.orig.args[0])
            raise

    monkeypatch.setattr(code_files, "_authorize_folder_actor", trace_authorize)

    def hold_actor():
        with factory.begin() as db:
            db.query(User).filter_by(id=actor_id).with_for_update().one()
            locked.set()
            assert release.wait(15)

    app = FastAPI()
    error_handlers.register_handlers(app)
    app.include_router(projects.router, prefix="/api/projects")

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            holder = pool.submit(hold_actor)
            assert locked.wait(10)
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/projects/import-remote",
                    headers={"Authorization": f"Bearer {create_access_token(actor_id, 'admin')}"},
                    json={"url": "https://example.com/repo.zip", "project_name": name},
                )
            assert response.status_code == 409
            assert response.json()["code"] == 40901
            assert response.json()["retryable"] is True
            assert observed_codes == [3572]
            assert _counts(factory, name) == (0, 0, 0, 0)
            release.set()
            holder.result(timeout=10)
    finally:
        release.set()


@pytest.mark.parametrize("errno,expected_status", [(1205, 409), (1213, 409), (3572, 409), (2006, 500), (None, 500)])
def test_mysql_http_authorization_only_maps_known_lock_errors(fresh_chain, monkeypatch, errno, expected_status):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    actor_id = _identity(factory)
    name = f"legacy-errno-{uuid4().hex[:12]}"
    monkeypatch.setattr(project_source_service, "download_remote_archive", lambda _url: (_archive(), "repo.zip"))

    def fail_authorize(*_args, **_kwargs):
        original = pymysql.err.OperationalError() if errno is None else pymysql.err.OperationalError(errno, "test")
        raise OperationalError("synthetic authorization failure", {}, original)

    monkeypatch.setattr(code_files, "_authorize_folder_actor", fail_authorize)
    if expected_status == 500:
        monkeypatch.setattr(error_handlers.logger, "exception", lambda *_args, **_kwargs: None)
    app = FastAPI()
    error_handlers.register_handlers(app)
    app.include_router(projects.router, prefix="/api/projects")

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/projects/import-remote",
            headers={"Authorization": f"Bearer {create_access_token(actor_id, 'admin')}"},
            json={"url": "https://example.com/repo.zip", "project_name": name},
        )
    assert response.status_code == expected_status
    if expected_status == 409:
        assert response.json()["code"] == 40901
        assert response.json()["retryable"] is True
    assert _counts(factory, name) == (0, 0, 0, 0)
