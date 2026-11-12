"""Directory authorization must still be valid immediately before each commit."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from io import BytesIO
from tempfile import SpooledTemporaryFile
from threading import Event
from time import monotonic, sleep
from uuid import uuid4
from zipfile import ZipFile

import pytest
import test_fresh_database_chain as chain
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from starlette.datastructures import UploadFile

from app.api.v1 import code_files
from app.core.config import settings
from app.core.dependencies import authenticate_access_token
from app.core.permission_codes import PermissionCode
from app.core.security import create_access_token
from app.models.billing_ledger import BillingOrder, BillingPaymentEvent, BillingSubscription, BillingSubscriptionPeriod
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.project_source_archive import ProjectSourceArchive
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.user import User
from app.services import billing_entitlement_service, code_file_service, project_source_service

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


def _identity(factory, *, reviewer=False, now=None):
    suffix = uuid4().hex[:12]
    with factory.begin() as db:
        user = User(username=f"expiry_{suffix}", password="x", role="reviewer" if reviewer else "admin",
                    status=1, token_version=0)
        db.add(user)
        db.flush()
        project = Project(user_id=user.id, project_name=f"expiry_{suffix}", status="active")
        db.add(project)
        if reviewer:
            role = db.query(Role).filter_by(code="reviewer", status="active").one()
            perm = db.query(Permission).filter_by(code=PermissionCode.FILE_UPLOAD).one()
            if not db.query(RolePermission.id).filter_by(role_id=role.id, permission_id=perm.id).first():
                db.add(RolePermission(role_id=role.id, permission_id=perm.id))
            db.add(UserRole(user_id=user.id, role_id=role.id))
            order_no = f"exp-{suffix}"
            db.add(BillingOrder(order_no=order_no, user_id=user.id, product="reviewer_month", quantity=1,
                                channel="alipay", amount_fen=100, status="paid", payment_status="paid"))
            db.add(BillingPaymentEvent(channel="alipay", trade_id=f"trade-{suffix}", order_no=order_no,
                                       amount_fen=100))
            sub = BillingSubscription(owner_type="user", owner_id=user.id, product="reviewer",
                                      anchor_at=now - timedelta(days=30), last_end_at=now, next_offset=1)
            db.add(sub)
            db.flush()
            db.add(BillingSubscriptionPeriod(subscription_id=sub.id, order_no=order_no,
                                             starts_at=now - timedelta(days=30),
                                             ends_at=now - timedelta(days=7) + timedelta(seconds=1),
                                             credit_milli=5_000_000, seats=1))
        db.flush()
        user_id, project_id = user.id, project.id
    with factory() as db:
        token = create_access_token(user_id, "reviewer" if reviewer else "admin")
        actor = code_files._snapshot_folder_actor(authenticate_access_token(token, db))
    return actor, project_id


@pytest.mark.parametrize("kind", ["file", "archive", "quarantine"])
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_token_expiry_at_scan_boundary_mysql(fresh_chain, monkeypatch, kind, offset):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    actor, project_id = _identity(factory)
    clock = [actor.token_expires_at - 1]

    class Clock:
        @staticmethod
        def now(_tz=None):
            return datetime.fromtimestamp(clock[0], timezone.utc)

    monkeypatch.setattr(code_files, "datetime", Clock)

    def advance(*_args, **_kwargs):
        clock[0] = actor.token_expires_at + offset

    if kind == "file":
        payload, filename = b"print(1)", "a.py"
        monkeypatch.setattr(code_file_service, "_validate_upload_security", advance)
    else:
        buffer = BytesIO()
        with ZipFile(buffer, "w") as archive:
            archive.writestr("a.py", "print(1)")
        payload, filename = buffer.getvalue(), "a.zip"
        if kind == "archive":
            monkeypatch.setattr(code_file_service, "_validate_upload_security", lambda *_a, **_k: None)
            monkeypatch.setattr(code_file_service, "_scan_extracted_file", advance)
        else:
            def infected(*_args, **_kwargs):
                raise code_file_service.MaliciousUploadError("恶意样本", "test")

            def quarantine_scan(*_args, **_kwargs):
                advance()
                return "infected", 1, "{}"

            monkeypatch.setattr(code_file_service, "_validate_upload_security", infected)
            monkeypatch.setattr(project_source_service, "_scan_quarantined_zip", quarantine_scan)
    with SpooledTemporaryFile() as file:
        file.write(payload)
        file.seek(0)
        with factory() as db:
            result = code_files._upload_folder_items(db, None, project_id, [UploadFile(file, filename=filename)],
                                                     actor=actor)
        with factory() as db:
            total = db.query(CodeFile).filter_by(project_id=project_id).count()
            total += db.query(ProjectSourceArchive).filter_by(project_id=project_id).count()
        assert total == (0 if offset >= 0 else 1)
        assert result["success_count"] == (0 if offset >= 0 else 1)


@pytest.mark.parametrize("offset", [0, 1, 2])
def test_reviewer_entitlement_grace_boundary_mysql(fresh_chain, monkeypatch, offset):
    monkeypatch.setattr(settings, "billing_entitlement_enforcement_ready", True)
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    clock = [now]
    monkeypatch.setattr(billing_entitlement_service, "_now", lambda: clock[0])
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    actor, project_id = _identity(factory, reviewer=True, now=now)

    def scan(db, *_args, **_kwargs):
        assert billing_entitlement_service.has_reviewer_entitlement(db, actor.user_id)
        clock[0] = now + timedelta(seconds=offset)

    monkeypatch.setattr(code_file_service, "_validate_upload_security", scan)
    with SpooledTemporaryFile() as file:
        file.write(b"print(1)")
        file.seek(0)
        with factory() as db:
            result = code_files._upload_folder_items(db, None, project_id, [UploadFile(file, filename="a.py")],
                                                     actor=actor)
        with factory() as db:
            count = db.query(CodeFile).filter_by(project_id=project_id).count()
        assert count == (1 if offset == 0 else 0)
        assert result["success_count"] == count


def test_expiry_while_scan_locked_rolls_back_before_concurrent_logout(fresh_chain, monkeypatch):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    actor, project_id = _identity(factory)
    clock = [actor.token_expires_at - 1]
    scanning, release, revoke_started = Event(), Event(), Event()
    revoke_connection = []

    class Clock:
        @staticmethod
        def now(_tz=None):
            return datetime.fromtimestamp(clock[0], timezone.utc)

    def scan(*_args, **_kwargs):
        scanning.set()
        assert release.wait(15)
        clock[0] = actor.token_expires_at

    monkeypatch.setattr(code_files, "datetime", Clock)
    monkeypatch.setattr(code_file_service, "_validate_upload_security", scan)

    def upload():
        with SpooledTemporaryFile() as file, factory() as db:
            file.write(b"print(1)")
            file.seek(0)
            return code_files._upload_folder_items(db, None, project_id, [UploadFile(file, filename="a.py")],
                                                   actor=actor)

    def revoke():
        with factory.begin() as db:
            revoke_connection.append(db.execute(text("SELECT CONNECTION_ID()")).scalar_one())
            revoke_started.set()
            current = db.query(User).filter_by(id=actor.user_id).with_for_update().one()
            current.token_version += 1

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(upload)
            assert scanning.wait(10)
            second = pool.submit(revoke)
            assert revoke_started.wait(10)
            deadline = monotonic() + 8
            waiting = False
            while monotonic() < deadline:
                with fresh_chain.connect() as connection:
                    waiting = bool(connection.execute(text(
                        "SELECT 1 FROM performance_schema.data_lock_waits w "
                        "JOIN performance_schema.threads t ON t.THREAD_ID = w.REQUESTING_THREAD_ID "
                        "WHERE t.PROCESSLIST_ID = :connection_id LIMIT 1"
                    ), {"connection_id": revoke_connection[0]}).first())
                if waiting:
                    break
                sleep(0.05)
            assert waiting, "登出必须等待扫描事务的账号授权锁"
            release.set()
            assert first.result(timeout=10)["success_count"] == 0
            second.result(timeout=10)
        with factory() as db:
            assert db.query(CodeFile).filter_by(project_id=project_id).count() == 0
            assert db.get(User, actor.user_id).token_version == actor.token_version + 1
    finally:
        release.set()


def test_quarantine_reauthorizes_after_rollback_before_writing(fresh_chain, monkeypatch):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    actor, project_id = _identity(factory)
    rolled_back, release = Event(), Event()
    original = code_files._FolderCommitGuard.after_rollback

    def infected(*_args, **_kwargs):
        raise code_file_service.MaliciousUploadError("恶意样本", "test")

    def pause_after_rollback(guard):
        rolled_back.set()
        assert release.wait(10)
        original(guard)

    monkeypatch.setattr(code_file_service, "_validate_upload_security", infected)
    monkeypatch.setattr(code_files._FolderCommitGuard, "after_rollback", pause_after_rollback)
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("a.py", "print(1)")

    def upload():
        with SpooledTemporaryFile() as file, factory() as db:
            file.write(buffer.getvalue())
            file.seek(0)
            return code_files._upload_folder_items(db, None, project_id, [UploadFile(file, filename="a.zip")],
                                                   actor=actor)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            worker = pool.submit(upload)
            assert rolled_back.wait(10)
            with factory.begin() as db:
                user = db.query(User).filter_by(id=actor.user_id).with_for_update().one()
                user.token_version += 1
            release.set()
            assert worker.result(timeout=10)["success_count"] == 0
        with factory() as db:
            assert db.query(CodeFile).filter_by(project_id=project_id).count() == 0
            assert db.query(ProjectSourceArchive).filter_by(project_id=project_id).count() == 0
    finally:
        release.set()
