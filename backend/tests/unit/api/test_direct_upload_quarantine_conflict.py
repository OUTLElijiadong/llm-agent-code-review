"""Quarantine authorization conflicts must remain retryable to callers."""

from io import BytesIO
from tempfile import SpooledTemporaryFile
from unittest.mock import patch
from zipfile import ZipFile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.datastructures import UploadFile

from app.api.v1 import code_files
from app.core import error_handlers
from app.core.database import Base, get_db
from app.core.dependencies import authenticate_access_token
from app.core.exceptions import ConflictError, ValidationError
from app.core.security import create_access_token
from app.models import load_all_models
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.project_source_archive import ProjectSourceArchive
from app.models.user import User
from app.services import code_file_service, project_source_service


@pytest.mark.parametrize("failure", ["conflict", "unexpected"])
def test_quarantine_conflict_remains_distinct_from_scan_rejection(tmp_path, monkeypatch, failure):
    load_all_models()
    engine = create_engine(f"sqlite:///{tmp_path / 'quarantine.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with factory.begin() as db:
        db.add_all([
            User(id=1, username="owner", password="x", role="admin", status=1, token_version=0),
            Project(id=7, user_id=1, project_name="project", status="active"),
        ])

    raw = BytesIO()
    with ZipFile(raw, "w") as archive:
        archive.writestr("a.py", "print(1)")

    def infected(*_args, **_kwargs):
        raise code_file_service.MaliciousUploadError("扫描拒绝", "test")

    def fail_ingest(*_args, **_kwargs):
        if failure == "conflict":
            raise ConflictError("项目权限正在变更，请刷新后重试", code=40901)
        raise RuntimeError("不应向用户暴露的内部故障")

    monkeypatch.setattr(code_file_service, "_validate_upload_security", infected)
    monkeypatch.setattr(project_source_service, "ingest_source_archive_bytes", fail_ingest)
    with factory() as db, SpooledTemporaryFile() as file:
        user = authenticate_access_token(create_access_token(1, "admin"), db)
        file.write(raw.getvalue())
        file.seek(0)
        with patch.object(db, "rollback", wraps=db.rollback) as rollback:
            expected = ConflictError if failure == "conflict" else ValidationError
            with pytest.raises(expected) as caught:
                code_files.upload_code(7, UploadFile(file, filename="a.zip"), None, None, db, user)
            assert rollback.call_count >= 2
        if failure == "conflict":
            assert caught.value.code == 40901
        else:
            assert str(caught.value) == "扫描拒绝"
    with factory() as db:
        assert db.query(CodeFile).count() == 0
        assert db.query(ProjectSourceArchive).count() == 0
    engine.dispose()


def test_authenticated_http_quarantine_lock_conflict_is_409(tmp_path, monkeypatch):
    load_all_models()
    engine = create_engine(f"sqlite:///{tmp_path / 'http.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with factory.begin() as db:
        db.add_all([
            User(id=1, username="owner", password="x", role="admin", status=1, token_version=0),
            Project(id=7, user_id=1, project_name="project", status="active"),
        ])

    def infected(*_args, **_kwargs):
        raise code_file_service.MaliciousUploadError("扫描拒绝", "test")

    def conflict(*_args, **_kwargs):
        raise ConflictError("项目权限正在变更，请刷新后重试", code=40901)

    monkeypatch.setattr(code_file_service, "_validate_upload_security", infected)
    monkeypatch.setattr(project_source_service, "ingest_source_archive_bytes", conflict)
    app = FastAPI()
    error_handlers.register_handlers(app)
    app.include_router(code_files.router, prefix="/api/code-files")

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        response = client.post(
            "/api/code-files/upload", headers={"Authorization": f"Bearer {create_access_token(1, 'admin')}"},
            data={"project_id": "7"}, files={"file": ("a.zip", b"synthetic archive")},
        )
    assert response.status_code == 409
    assert response.json()["code"] == 40901
    with factory() as db:
        assert db.query(CodeFile).count() == 0
        assert db.query(ProjectSourceArchive).count() == 0
    engine.dispose()
