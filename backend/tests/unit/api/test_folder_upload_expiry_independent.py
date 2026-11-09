"""A directory file cannot commit after its authenticated token has expired."""

from datetime import datetime, timedelta, timezone
from io import BytesIO
from tempfile import SpooledTemporaryFile
from zipfile import ZipFile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.datastructures import UploadFile

from app.api.v1 import code_files
from app.core.config import settings
from app.core.database import Base
from app.core.dependencies import authenticate_access_token
from app.core.exceptions import AuthError, PermissionError
from app.core.permission_codes import PermissionCode
from app.core.security import create_access_token
from app.models.billing_ledger import BillingOrder, BillingPaymentEvent, BillingSubscription, BillingSubscriptionPeriod
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.project_source_archive import ProjectSourceArchive
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.user import User
from app.services import billing_entitlement_service, code_file_service, project_source_service


@pytest.mark.parametrize("kind", ["file", "archive", "quarantine"])
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_token_expiry_during_scan_cannot_commit_file(monkeypatch, tmp_path, kind, offset):
    engine = create_engine(f"sqlite:///{tmp_path / 'expiry.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with factory.begin() as db:
        db.add(User(id=1, username="owner", password="x", role="admin", status=1, token_version=0))
        db.add(Project(id=7, user_id=1, project_name="source", status="active"))
    with factory() as db:
        user = authenticate_access_token(create_access_token(1, "admin"), db)
        actor = code_files._snapshot_folder_actor(user)

    clock = [actor.token_expires_at - 1]

    class Clock:
        @staticmethod
        def now(_tz=None):
            return datetime.fromtimestamp(clock[0], timezone.utc)

    monkeypatch.setattr(code_files, "datetime", Clock)

    def scan(*_args, **_kwargs):
        clock[0] = actor.token_expires_at + offset

    if kind == "file":
        monkeypatch.setattr(code_file_service, "_validate_upload_security", scan)
        payload, filename = b"print(1)", "a.py"
    else:
        buffer = BytesIO()
        with ZipFile(buffer, "w") as archive:
            archive.writestr("a.py", "print(1)")
        payload, filename = buffer.getvalue(), "a.zip"
        if kind == "archive":
            monkeypatch.setattr(code_file_service, "_validate_upload_security", lambda *_a, **_k: None)
            monkeypatch.setattr(code_file_service, "_scan_extracted_file", scan)
        else:
            def infected(*_args, **_kwargs):
                raise code_file_service.MaliciousUploadError("恶意样本", "test")

            def scan_quarantined(*_args, **_kwargs):
                scan()
                return "infected", 1, "{}"

            monkeypatch.setattr(code_file_service, "_validate_upload_security", infected)
            monkeypatch.setattr(project_source_service, "_scan_quarantined_zip", scan_quarantined)
    with SpooledTemporaryFile() as file:
        file.write(payload)
        file.seek(0)
        with factory() as db:
            try:
                result = code_files._upload_folder_items(
                    db, None, 7, [UploadFile(file, filename=filename)], actor=actor,
                )
            except (AuthError, PermissionError):
                result = None
        with factory() as db:
            total = db.query(CodeFile).count() + db.query(ProjectSourceArchive).count()
            assert total == (0 if offset >= 0 else 1)
        assert result is not None
        assert result["success_count"] == (0 if offset >= 0 else 1)
    engine.dispose()


@pytest.mark.parametrize("expires", [True, False])
def test_reviewer_entitlement_expiry_during_scan_cannot_commit(monkeypatch, tmp_path, expires):
    engine = create_engine(f"sqlite:///{tmp_path / 'entitlement.db'}", connect_args={"check_same_thread": False})
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
    with factory() as db:
        user = authenticate_access_token(create_access_token(1, "reviewer"), db)
        actor = code_files._snapshot_folder_actor(user)
        assert billing_entitlement_service.has_reviewer_entitlement(db, 1)

    def scan(db, *_args, **_kwargs):
        assert billing_entitlement_service.has_reviewer_entitlement(db, 1)
        if expires:
            clock[0] = current + timedelta(seconds=2)
            assert not billing_entitlement_service.has_reviewer_entitlement(db, 1)

    monkeypatch.setattr(code_file_service, "_validate_upload_security", scan)
    with SpooledTemporaryFile() as file:
        file.write(b"print(1)")
        file.seek(0)
        with factory() as db:
            try:
                result = code_files._upload_folder_items(db, None, 7, [UploadFile(file, filename="a.py")], actor=actor)
            except (AuthError, PermissionError):
                result = None
        with factory() as db:
            assert db.query(CodeFile).count() == (0 if expires else 1)
        assert result is not None
        assert result["success_count"] == (0 if expires else 1)
    engine.dispose()


def test_committed_scan_transaction_cannot_reuse_old_authorization(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'transaction.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with factory.begin() as db:
        db.add(User(id=1, username="owner", password="x", role="admin", status=1, token_version=0))
        db.add(Project(id=7, user_id=1, project_name="source", status="active"))
    with factory() as db:
        actor = code_files._snapshot_folder_actor(authenticate_access_token(create_access_token(1, "admin"), db))

    monkeypatch.setattr(code_file_service, "_validate_upload_security", lambda db, *_a, **_k: db.commit())
    with SpooledTemporaryFile() as file:
        file.write(b"print(1)")
        file.seek(0)
        with factory() as db:
            result = code_files._upload_folder_items(db, None, 7, [UploadFile(file, filename="a.py")], actor=actor)
        with factory() as db:
            assert db.query(CodeFile).count() == 0
        assert result["success_count"] == 0
    engine.dispose()
