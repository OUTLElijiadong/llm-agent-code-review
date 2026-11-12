"""Real MySQL checks for commit-time direct-upload authentication fences."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from io import BytesIO
from tempfile import SpooledTemporaryFile
from threading import Event
from zipfile import ZipFile

import pytest
import test_fresh_database_chain as chain
from sqlalchemy.orm import sessionmaker
from starlette.datastructures import UploadFile
from test_mysql_folder_upload_expiry import _identity

from app.api.v1 import code_files, projects
from app.core.config import settings
from app.core.dependencies import authenticate_access_token
from app.core.exceptions import AuthError, PermissionError
from app.core.security import create_access_token
from app.models.code_file import CodeFile
from app.models.project_source_archive import ProjectSourceArchive
from app.models.user import User
from app.services import billing_entitlement_service, code_file_service, project_member_service, project_source_service

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


def _payload(kind, monkeypatch, advance):
    if kind == "file":
        monkeypatch.setattr(code_file_service, "_validate_upload_security", advance)
        return b"print(1)", "a.py"
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("a.py", "print(1)")
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
    return buffer.getvalue(), "a.zip"


def _call(kind, db, user, project_id, upload):
    if kind == "direct_quarantine":
        return projects.upload_audit_source_archive(project_id, upload, db, user)
    return code_files.upload_code(project_id, upload, None, None, db, user)


@pytest.mark.parametrize("kind", ["file", "archive", "quarantine", "direct_quarantine"])
@pytest.mark.parametrize("expires", [False, True])
def test_mysql_direct_upload_token_expiry(fresh_chain, monkeypatch, kind, expires):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    identity, project_id = _identity(factory)
    with factory() as db, SpooledTemporaryFile() as file:
        user = authenticate_access_token(create_access_token(identity.user_id, "admin"), db)
        token_expiry = user._authenticated_token_expires_at
        clock = [token_expiry - 1]
        monkeypatch.setattr(project_member_service, "time", lambda: clock[0])

        class Clock:
            @staticmethod
            def now(_tz=None):
                return datetime.fromtimestamp(clock[0], timezone.utc)

        monkeypatch.setattr(code_files, "datetime", Clock)

        def advance(*_args, **_kwargs):
            clock[0] = token_expiry if expires else token_expiry - 1

        raw, filename = _payload(kind, monkeypatch, advance)
        file.write(raw)
        file.seek(0)
        upload = UploadFile(file, filename=filename)
        if expires:
            with pytest.raises(AuthError):
                _call(kind, db, user, project_id, upload)
        else:
            _call(kind, db, user, project_id, upload)
    with factory() as db:
        count = db.query(CodeFile).filter_by(project_id=project_id).count()
        count += db.query(ProjectSourceArchive).filter_by(project_id=project_id).count()
        assert count == (0 if expires else 1)


@pytest.mark.parametrize("kind", ["file", "archive", "quarantine", "direct_quarantine"])
@pytest.mark.parametrize("expires", [False, True])
def test_mysql_direct_upload_reviewer_expiry(fresh_chain, monkeypatch, kind, expires):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    current = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    clock = [current]
    monkeypatch.setattr(settings, "billing_entitlement_enforcement_ready", True)
    monkeypatch.setattr(billing_entitlement_service, "_now", lambda: clock[0])
    identity, project_id = _identity(factory, reviewer=True, now=current)

    def advance(*_args, **_kwargs):
        if expires:
            clock[0] = current + timedelta(seconds=2)

    raw, filename = _payload(kind, monkeypatch, advance)
    with factory() as db, SpooledTemporaryFile() as file:
        user = authenticate_access_token(create_access_token(identity.user_id, "reviewer"), db)
        file.write(raw)
        file.seek(0)
        upload = UploadFile(file, filename=filename)
        if expires:
            with pytest.raises(PermissionError):
                _call(kind, db, user, project_id, upload)
        else:
            _call(kind, db, user, project_id, upload)
    with factory() as db:
        count = db.query(CodeFile).filter_by(project_id=project_id).count()
        count += db.query(ProjectSourceArchive).filter_by(project_id=project_id).count()
        assert count == (0 if expires else 1)


def test_mysql_single_quarantine_reauthorizes_after_rollback(fresh_chain, monkeypatch):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    identity, project_id = _identity(factory)
    token = create_access_token(identity.user_id, "admin")
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
            user = authenticate_access_token(token, db)
            file.write(buffer.getvalue())
            file.seek(0)
            with pytest.raises(PermissionError):
                code_files.upload_code(project_id, UploadFile(file, filename="a.zip"), None, None, db, user)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            worker = pool.submit(upload)
            assert rolled_back.wait(10)
            with factory.begin() as db:
                user = db.query(User).filter_by(id=identity.user_id).with_for_update().one()
                user.token_version += 1
            release.set()
            worker.result(timeout=10)
        with factory() as db:
            assert db.query(CodeFile).filter_by(project_id=project_id).count() == 0
            assert db.query(ProjectSourceArchive).filter_by(project_id=project_id).count() == 0
    finally:
        release.set()
