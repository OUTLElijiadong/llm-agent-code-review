"""The deprecated synchronous HTTP import must not write after auth expires."""

from datetime import datetime, timedelta, timezone
from io import BytesIO
from unittest.mock import MagicMock
from zipfile import ZipFile

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.v1 import code_files, projects
from app.core import error_handlers
from app.core.config import settings
from app.core.database import Base, get_db
from app.core.dependencies import authenticate_access_token
from app.core.exceptions import AuthError, ConflictError, PermissionError
from app.core.permission_codes import PermissionCode
from app.core.security import create_access_token
from app.models import load_all_models
from app.models.billing_ledger import BillingOrder, BillingPaymentEvent, BillingSubscription, BillingSubscriptionPeriod
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.project_source_archive import ProjectSourceArchive
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.user import User
from app.schemas.project import RemoteProjectImportIn
from app.services import billing_entitlement_service, code_file_service, project_member_service, project_source_service


@pytest.mark.parametrize("audit_mode", [False, True])
@pytest.mark.parametrize("expired", [False, True])
def test_legacy_sync_import_scan_rechecks_token_and_rolls_back(tmp_path, monkeypatch, audit_mode, expired):
    load_all_models()
    engine = create_engine(f"sqlite:///{tmp_path / 'import.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with factory.begin() as db:
        db.add(User(id=1, username="owner", password="x", role="admin", status=1, token_version=0))
    raw = BytesIO()
    with ZipFile(raw, "w") as archive:
        archive.writestr("main.py", "print(1)")
    monkeypatch.setattr(project_source_service, "download_remote_archive", lambda _url: (raw.getvalue(), "repo.zip"))

    with factory() as db:
        user = authenticate_access_token(create_access_token(1, "admin"), db)
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
            def scan_quarantine(*args, **kwargs):
                scan(*args, **kwargs)
                return "clean", 0, "{}"
            monkeypatch.setattr(project_source_service, "_scan_quarantined_zip", scan_quarantine)
        else:
            monkeypatch.setattr(code_file_service, "_scan_extracted_file", scan)
        request = RemoteProjectImportIn(
            url="https://example.com/repo.zip", project_name="import", audit_mode=audit_mode,
        )
        if expired:
            with pytest.raises(AuthError):
                projects.import_remote_project(request, Response(), db, user)
        else:
            projects.import_remote_project(request, Response(), db, user)

    with factory() as db:
        assert db.query(Project).count() == (0 if expired else 1)
        assert db.query(ProjectMember).count() == (0 if expired else 1)
        assert db.query(CodeFile).count() == (0 if expired or audit_mode else 1)
        assert db.query(ProjectSourceArchive).count() == (1 if not expired and audit_mode else 0)
    engine.dispose()


@pytest.mark.parametrize("audit_mode", [False, True])
def test_legacy_sync_import_reviewer_expiry_rolls_back_all(tmp_path, monkeypatch, audit_mode):
    load_all_models()
    engine = create_engine(f"sqlite:///{tmp_path / 'reviewer.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    clock = [now]
    monkeypatch.setattr(settings, "billing_entitlement_enforcement_ready", True)
    monkeypatch.setattr(billing_entitlement_service, "_now", lambda: clock[0])
    with factory.begin() as db:
        db.add(User(id=1, username="reviewer", password="x", role="reviewer", status=1, token_version=0))
        role = Role(name="审查员", code="reviewer", status="active")
        permission = Permission(name="导入", code=PermissionCode.PROJECT_IMPORT, module="project")
        db.add_all([role, permission])
        db.flush()
        db.add_all([UserRole(user_id=1, role_id=role.id), RolePermission(role_id=role.id, permission_id=permission.id)])
        db.add(BillingOrder(order_no="order", user_id=1, product="reviewer_month", quantity=1,
                            channel="alipay", amount_fen=100, status="paid", payment_status="paid"))
        db.add(BillingPaymentEvent(channel="alipay", trade_id="trade", order_no="order", amount_fen=100))
        sub = BillingSubscription(owner_type="user", owner_id=1, product="reviewer", anchor_at=now,
                                  last_end_at=now, next_offset=1)
        db.add(sub)
        db.flush()
        db.add(BillingSubscriptionPeriod(subscription_id=sub.id, order_no="order", starts_at=now - timedelta(days=30),
                                         ends_at=now - timedelta(days=7) + timedelta(seconds=1),
                                         credit_milli=5_000_000, seats=1))
    raw = BytesIO()
    with ZipFile(raw, "w") as archive:
        archive.writestr("main.py", "print(1)")
    monkeypatch.setattr(project_source_service, "download_remote_archive", lambda _url: (raw.getvalue(), "repo.zip"))

    def expire(*_args, **_kwargs):
        clock[0] = now + timedelta(seconds=2)
        if audit_mode:
            return "clean", 0, "{}"

    if audit_mode:
        monkeypatch.setattr(project_source_service, "_scan_quarantined_zip", expire)
    else:
        monkeypatch.setattr(code_file_service, "_scan_extracted_file", expire)
    with factory() as db:
        user = authenticate_access_token(create_access_token(1, "reviewer"), db)
        with pytest.raises(PermissionError):
            projects.import_remote_project(
                RemoteProjectImportIn(url="https://example.com/repo.zip", project_name="import", audit_mode=audit_mode),
                Response(), db, user,
            )
    with factory() as db:
        assert db.query(Project).count() == 0
        assert db.query(ProjectMember).count() == 0
        assert db.query(CodeFile).count() == 0
        assert db.query(ProjectSourceArchive).count() == 0
    engine.dispose()


def test_legacy_sync_import_rolls_back_new_project_on_conflict(tmp_path, monkeypatch):
    load_all_models()
    engine = create_engine(f"sqlite:///{tmp_path / 'conflict.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with factory.begin() as db:
        db.add(User(id=1, username="owner", password="x", role="admin", status=1, token_version=0))
    monkeypatch.setattr(project_source_service, "download_remote_archive", lambda _url: (b"archive", "repo.zip"))

    def fail(*_args, **_kwargs):
        raise ConflictError("项目状态正在变化，请重试", code=40901)

    monkeypatch.setattr(code_file_service, "_upload_archive", fail)
    with factory() as db:
        user = authenticate_access_token(create_access_token(1, "admin"), db)
        with pytest.raises(ConflictError):
            projects.import_remote_project(
                RemoteProjectImportIn(url="https://example.com/repo.zip", project_name="import"), Response(), db, user,
            )
        # A reused session cannot accidentally commit the failed import either.
        db.commit()
    with factory() as db:
        assert db.query(Project).count() == 0
        assert db.query(ProjectMember).count() == 0
    engine.dispose()


@pytest.mark.parametrize("revocation", ["token", "permission", "disabled"])
def test_legacy_http_import_rechecks_after_download(tmp_path, monkeypatch, revocation):
    load_all_models()
    engine = create_engine(f"sqlite:///{tmp_path / 'revoke.db'}", connect_args={"check_same_thread": False})
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with factory.begin() as db:
        db.add(User(id=1, username="owner", password="x", role="user", status=1, token_version=0))
        role = Role(name="用户", code="user", status="active")
        permission = Permission(name="导入", code=PermissionCode.PROJECT_IMPORT, module="project")
        db.add_all([role, permission])
        db.flush()
        db.add_all([UserRole(user_id=1, role_id=role.id), RolePermission(role_id=role.id, permission_id=permission.id)])

    def download(_url):
        with factory.begin() as db:
            if revocation == "permission":
                db.query(RolePermission).delete()
            else:
                actor = db.get(User, 1)
                if revocation == "token":
                    actor.token_version += 1
                else:
                    actor.status = 0
        return b"not reached", "repo.zip"

    monkeypatch.setattr(project_source_service, "download_remote_archive", download)
    app = FastAPI()
    error_handlers.register_handlers(app)
    app.include_router(projects.router, prefix="/api/projects")

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        response = client.post(
            "/api/projects/import-remote", headers={"Authorization": f"Bearer {create_access_token(1, 'user')}"},
            json={"url": "https://example.com/repo.zip", "project_name": "import"},
        )
    assert response.status_code == 403
    with factory() as db:
        assert db.query(Project).count() == 0
        assert db.query(ProjectMember).count() == 0
        assert db.query(CodeFile).count() == 0
    engine.dispose()


@pytest.mark.parametrize("audit_mode", [False, True])
def test_legacy_http_import_keeps_project_import_only_permission(tmp_path, monkeypatch, audit_mode):
    load_all_models()
    engine = create_engine(f"sqlite:///{tmp_path / 'allowed.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with factory.begin() as db:
        db.add(User(id=1, username="owner", password="x", role="user", status=1, token_version=0))
        role = Role(name="用户", code="user", status="active")
        permission = Permission(name="导入", code=PermissionCode.PROJECT_IMPORT, module="project")
        db.add_all([role, permission])
        db.flush()
        db.add_all([UserRole(user_id=1, role_id=role.id), RolePermission(role_id=role.id, permission_id=permission.id)])
    raw = BytesIO()
    with ZipFile(raw, "w") as archive:
        archive.writestr("main.py", "print(1)")
    monkeypatch.setattr(project_source_service, "download_remote_archive", lambda _url: (raw.getvalue(), "repo.zip"))
    monkeypatch.setattr(code_file_service, "_scan_extracted_file", lambda *_a, **_k: None)
    monkeypatch.setattr(project_source_service, "_scan_quarantined_zip", lambda *_a, **_k: ("clean", 0, "{}"))
    app = FastAPI()
    error_handlers.register_handlers(app)
    app.include_router(projects.router, prefix="/api/projects")

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        response = client.post(
            "/api/projects/import-remote", headers={"Authorization": f"Bearer {create_access_token(1, 'user')}"},
            json={"url": "https://example.com/repo.zip", "project_name": "import", "audit_mode": audit_mode},
        )
    assert response.status_code == 200
    assert response.headers["Deprecation"] == "true"
    with factory() as db:
        assert db.query(Project).count() == 1
        assert db.query(ProjectMember).count() == 1
        assert db.query(CodeFile).count() == int(not audit_mode)
        assert db.query(ProjectSourceArchive).count() == int(audit_mode)
    engine.dispose()


def test_legacy_import_rejects_entitlement_mode_change_during_scan(tmp_path, monkeypatch):
    load_all_models()
    engine = create_engine(f"sqlite:///{tmp_path / 'mode.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with factory.begin() as db:
        db.add(User(id=1, username="owner", password="x", role="admin", status=1, token_version=0))
    raw = BytesIO()
    with ZipFile(raw, "w") as archive:
        archive.writestr("main.py", "print(1)")
    monkeypatch.setattr(settings, "billing_entitlement_enforcement_ready", False)
    monkeypatch.setattr(project_source_service, "download_remote_archive", lambda _url: (raw.getvalue(), "repo.zip"))

    def change_mode(*_args, **_kwargs):
        monkeypatch.setattr(settings, "billing_entitlement_enforcement_ready", True)

    monkeypatch.setattr(code_file_service, "_scan_extracted_file", change_mode)
    with factory() as db:
        user = authenticate_access_token(create_access_token(1, "admin"), db)
        with pytest.raises(PermissionError):
            projects.import_remote_project(
                RemoteProjectImportIn(url="https://example.com/repo.zip", project_name="import"), Response(), db, user,
            )
    with factory() as db:
        assert db.query(Project).count() == 0
        assert db.query(ProjectMember).count() == 0
        assert db.query(CodeFile).count() == 0
    engine.dispose()


def test_internal_import_create_conflict_does_not_rollback_callers_transaction(monkeypatch):
    db = MagicMock()
    monkeypatch.setattr(project_source_service, "download_remote_archive", lambda _url: (b"archive", "repo.zip"))

    def fail(*_args, **_kwargs):
        raise ConflictError("项目名重复", code=40901)

    monkeypatch.setattr(project_source_service.project_service, "create_project", fail)
    with pytest.raises(ConflictError):
        project_source_service.import_remote_project(
            db, User(id=1, username="owner", role="user", status=1),
            url="https://example.com/repo.zip", project_name="import",
        )
    db.rollback.assert_not_called()
    db.commit.assert_not_called()


def test_invalid_import_authorization_does_not_fall_back_to_legacy_write(monkeypatch):
    db = MagicMock()
    monkeypatch.setattr(project_source_service, "download_remote_archive", lambda _url: (b"archive", "repo.zip"))
    create = MagicMock()
    monkeypatch.setattr(project_source_service.project_service, "create_project", create)
    with pytest.raises(RuntimeError, match="同步导入授权校验不可用"):
        project_source_service.import_remote_project(
            db, User(id=1, username="owner", role="user", status=1),
            url="https://example.com/repo.zip", project_name="import",
            authorize_before_write=lambda: None,
        )
    create.assert_not_called()
    db.rollback.assert_called_once()
