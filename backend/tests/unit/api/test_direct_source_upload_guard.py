"""Authenticated archive uploads must recheck authorization before commit."""

from datetime import datetime, timedelta, timezone
from io import BytesIO
from tempfile import SpooledTemporaryFile
from zipfile import ZipFile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.datastructures import UploadFile

from app.api.v1 import code_files, projects
from app.core.config import settings
from app.core.database import Base
from app.core.dependencies import authenticate_access_token
from app.core.exceptions import AuthError, PermissionError
from app.core.permission_codes import PermissionCode
from app.core.security import create_access_token
from app.models import load_all_models
from app.models.billing_ledger import BillingOrder, BillingPaymentEvent, BillingSubscription, BillingSubscriptionPeriod
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.project_source_archive import ProjectSourceArchive
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.user import User
from app.services import billing_entitlement_service, code_file_service, project_member_service, project_source_service


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_direct_quarantine_rechecks_token_expiry_before_commit(tmp_path, monkeypatch, offset):
    load_all_models()
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with factory.begin() as db:
        db.add(User(id=1, username="owner", password="x", role="admin", status=1, token_version=0))
        db.add(Project(id=7, user_id=1, project_name="source", status="active"))
    raw = BytesIO()
    with ZipFile(raw, "w") as archive:
        archive.writestr("a.py", "print(1)")
    with factory() as db:
        user = authenticate_access_token(create_access_token(1, "admin"), db)
        expires = user._authenticated_token_expires_at
        clock = [expires - 1]
        monkeypatch.setattr(project_member_service, "time", lambda: clock[0])

        class Clock:
            @staticmethod
            def now(_tz=None):
                return datetime.fromtimestamp(clock[0], timezone.utc)

        monkeypatch.setattr(code_files, "datetime", Clock)

        def scan(*_args, **_kwargs):
            clock[0] = expires + offset
            return "infected", 1, "{}"

        monkeypatch.setattr(project_source_service, "_scan_quarantined_zip", scan)
        with SpooledTemporaryFile() as file:
            file.write(raw.getvalue())
            file.seek(0)
            upload = UploadFile(file, filename="a.zip")
            if offset >= 0:
                with pytest.raises(AuthError):
                    projects.upload_audit_source_archive(7, upload, db, user)
            else:
                projects.upload_audit_source_archive(7, upload, db, user)
    with factory() as db:
        assert db.query(ProjectSourceArchive).count() == (1 if offset < 0 else 0)
    engine.dispose()


@pytest.mark.parametrize("kind", ["file", "archive", "quarantine", "direct_quarantine"])
@pytest.mark.parametrize("expires", [False, True])
def test_direct_upload_rechecks_reviewer_entitlement(tmp_path, monkeypatch, kind, expires):
    load_all_models()
    engine = create_engine(f"sqlite:///{tmp_path / 'reviewer.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    current = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    clock = [current]
    monkeypatch.setattr(settings, "billing_entitlement_enforcement_ready", True)
    monkeypatch.setattr(billing_entitlement_service, "_now", lambda: clock[0])
    with factory.begin() as db:
        db.add(User(id=1, username="reviewer", password="x", role="reviewer", status=1, token_version=0))
        db.add(Project(id=7, user_id=1, project_name="source", status="active"))
        role = Role(name="审查员", code="reviewer", status="active")
        permission = Permission(name="上传", code=PermissionCode.FILE_UPLOAD, module="file")
        db.add_all([role, permission])
        db.flush()
        db.add_all([UserRole(user_id=1, role_id=role.id), RolePermission(role_id=role.id, permission_id=permission.id)])
        db.add(BillingOrder(order_no="order", user_id=1, product="reviewer_month", quantity=1,
                            channel="alipay", amount_fen=100, status="paid", payment_status="paid"))
        db.add(BillingPaymentEvent(channel="alipay", trade_id="trade", order_no="order", amount_fen=100))
        sub = BillingSubscription(owner_type="user", owner_id=1, product="reviewer",
                                  anchor_at=current - timedelta(days=30), last_end_at=current, next_offset=1)
        db.add(sub)
        db.flush()
        db.add(BillingSubscriptionPeriod(subscription_id=sub.id, order_no="order",
                                         starts_at=current - timedelta(days=30),
                                         ends_at=current - timedelta(days=7) + timedelta(seconds=1),
                                         credit_milli=5_000_000, seats=1))

    def advance(*_args, **_kwargs):
        if expires:
            clock[0] = current + timedelta(seconds=2)

    if kind == "file":
        monkeypatch.setattr(code_file_service, "_validate_upload_security", advance)
        raw, filename = b"print(1)", "a.py"
    else:
        buffer = BytesIO()
        with ZipFile(buffer, "w") as archive:
            archive.writestr("a.py", "print(1)")
        raw, filename = buffer.getvalue(), "a.zip"
        if kind == "archive":
            monkeypatch.setattr(code_file_service, "_validate_upload_security", lambda *_a, **_k: None)
            monkeypatch.setattr(code_file_service, "_scan_extracted_file", advance)
        else:
            def scan_quarantined(*_args, **_kwargs):
                advance()
                return "infected", 1, "{}"

            monkeypatch.setattr(project_source_service, "_scan_quarantined_zip", scan_quarantined)
            if kind == "quarantine":
                def infected(*_args, **_kwargs):
                    raise code_file_service.MaliciousUploadError("样本", "test")
                monkeypatch.setattr(code_file_service, "_validate_upload_security", infected)

    with factory() as db, SpooledTemporaryFile() as file:
        user = authenticate_access_token(create_access_token(1, "reviewer"), db)
        file.write(raw)
        file.seek(0)
        upload = UploadFile(file, filename=filename)

        def call():
            if kind == "direct_quarantine":
                return projects.upload_audit_source_archive(7, upload, db, user)
            return code_files.upload_code(7, upload, None, None, db, user)

        if expires:
            with pytest.raises(PermissionError):
                call()
        else:
            call()
    with factory() as db:
        count = db.query(CodeFile).count() + db.query(ProjectSourceArchive).count()
        assert count == (0 if expires else 1)
    engine.dispose()


@pytest.mark.parametrize("kind", ["file", "archive", "quarantine"])
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_single_upload_rechecks_token_expiry_before_commit(tmp_path, monkeypatch, kind, offset):
    load_all_models()
    engine = create_engine(f"sqlite:///{tmp_path / 'single.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with factory.begin() as db:
        db.add(User(id=1, username="owner", password="x", role="admin", status=1, token_version=0))
        db.add(Project(id=7, user_id=1, project_name="source", status="active"))
    with factory() as db:
        user = authenticate_access_token(create_access_token(1, "admin"), db)
        expires = user._authenticated_token_expires_at
        clock = [expires - 1]
        monkeypatch.setattr(project_member_service, "time", lambda: clock[0])

        class Clock:
            @staticmethod
            def now(_tz=None):
                return datetime.fromtimestamp(clock[0], timezone.utc)

        monkeypatch.setattr(code_files, "datetime", Clock)

        def advance(*_args, **_kwargs):
            clock[0] = expires + offset

        if kind == "file":
            monkeypatch.setattr(code_file_service, "_validate_upload_security", advance)
            raw, filename = b"print(1)", "a.py"
        else:
            buffer = BytesIO()
            with ZipFile(buffer, "w") as archive:
                archive.writestr("a.py", "print(1)")
            raw, filename = buffer.getvalue(), "a.zip"
            if kind == "archive":
                monkeypatch.setattr(code_file_service, "_validate_upload_security", lambda *_a, **_k: None)
                monkeypatch.setattr(code_file_service, "_scan_extracted_file", advance)
            else:
                def infected(*_args, **_kwargs):
                    raise code_file_service.MaliciousUploadError("样本", "test")

                def scan_quarantined(*_args, **_kwargs):
                    advance()
                    return "infected", 1, "{}"

                monkeypatch.setattr(code_file_service, "_validate_upload_security", infected)
                monkeypatch.setattr(project_source_service, "_scan_quarantined_zip", scan_quarantined)
        with SpooledTemporaryFile() as file:
            file.write(raw)
            file.seek(0)
            upload = UploadFile(file, filename=filename)
            if offset >= 0:
                with pytest.raises((AuthError, ValueError)):
                    code_files.upload_code(7, upload, None, None, db, user)
            else:
                code_files.upload_code(7, upload, None, None, db, user)
    with factory() as db:
        count = db.query(CodeFile).count() + db.query(ProjectSourceArchive).count()
        assert count == (1 if offset < 0 else 0)
    engine.dispose()
