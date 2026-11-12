"""Real MySQL RR folder worker authorization and cancellation tests."""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest
import test_fresh_database_chain as chain
from sqlalchemy.orm import sessionmaker
from starlette.datastructures import Headers

from app.api.v1 import code_files
from app.core.dependencies import authenticate_access_token
from app.core.exceptions import PermissionError as AppPermissionError
from app.core.security import create_access_token
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.user import User
from app.services import code_file_service

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


def _request(project_id: int):
    body = (b'--abc\r\nContent-Disposition: form-data; name="project_id"\r\n\r\n'
            + str(project_id).encode() + b'\r\n'
            + b'--abc\r\nContent-Disposition: form-data; name="files"; filename="a.py"\r\n\r\na=1\r\n'
            + b'--abc\r\nContent-Disposition: form-data; name="files"; filename="b.py"\r\n\r\nb=2\r\n'
            + b'--abc--\r\n')

    async def stream():
        yield body

    return SimpleNamespace(headers=Headers({"content-type": "multipart/form-data; boundary=abc"}), stream=stream)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "logout"])
async def test_folder_partial_commit_then_cancel_or_logout(fresh_chain, monkeypatch, action):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    name = f"folder_{uuid4().hex[:18]}"
    with factory.begin() as db:
        actor = User(username=name, password="x", role="admin", status=1, token_version=0)
        db.add(actor)
        db.flush()
        project = Project(user_id=actor.id, project_name=name, status="active")
        db.add(project)
        db.flush()
        user_id, project_id = actor.id, project.id

    request_db = factory()
    user = authenticate_access_token(create_access_token(user_id, "admin"), request_db)
    started, release, done = threading.Event(), threading.Event(), threading.Event()
    original_create = code_file_service._create_file
    original_worker = code_files._run_owned_folder_upload

    def hold_first(*args, **kwargs):
        result = original_create(*args, **kwargs)
        started.set()
        assert release.wait(8)
        return result

    def mark_done(*args, **kwargs):
        try:
            return original_worker(*args, **kwargs)
        finally:
            done.set()

    monkeypatch.setattr(code_file_service, "_validate_upload_security", lambda *_a, **_kw: None)
    monkeypatch.setattr(code_file_service, "_create_file", hold_first)
    monkeypatch.setattr(code_files, "_run_owned_folder_upload", mark_done)
    task = asyncio.create_task(code_files.upload_folder(_request(project_id), db=request_db, user=user))
    try:
        assert await asyncio.to_thread(started.wait, 8)
        if action == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            request_db.close()
        else:
            with factory.begin() as db:
                current = db.get(User, user_id)
                current.token_version += 1
        release.set()
        if action == "logout":
            with pytest.raises(AppPermissionError):
                await task
        assert await asyncio.to_thread(done.wait, 8)
        with factory() as db:
            names = [row.file_name for row in db.query(CodeFile).filter_by(project_id=project_id).all()]
        assert names == ["a.py"]
    finally:
        release.set()
        request_db.close()


@pytest.mark.asyncio
async def test_project_owner_change_after_actor_snapshot_denies_upload(fresh_chain, monkeypatch):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    suffix = uuid4().hex[:16]
    with factory.begin() as db:
        role = db.query(Role).filter_by(code="user").one()
        permission = db.query(Permission).filter_by(code="file:upload").one()
        if not db.query(RolePermission.id).filter_by(role_id=role.id, permission_id=permission.id).first():
            db.add(RolePermission(role_id=role.id, permission_id=permission.id))
        actor = User(username=f"folder_{suffix}", password="x", role="user", status=1, token_version=0)
        other = User(username=f"other_{suffix}", password="x", role="user", status=1, token_version=0)
        db.add_all([actor, other])
        db.flush()
        db.add(UserRole(user_id=actor.id, role_id=role.id))
        project = Project(user_id=actor.id, project_name=f"folder_{suffix}", status="active")
        db.add(project)
        db.flush()
        actor_id, other_id, project_id = actor.id, other.id, project.id
    request_db = factory()
    user = authenticate_access_token(create_access_token(actor_id, "user"), request_db)
    ready, release = threading.Event(), threading.Event()
    original_auth = code_files._authorize_folder_actor

    def pause_after_auth(*args, **kwargs):
        result = original_auth(*args, **kwargs)
        ready.set()
        assert release.wait(8)
        return result

    monkeypatch.setattr(code_files, "_authorize_folder_actor", pause_after_auth)
    monkeypatch.setattr(code_file_service, "_validate_upload_security", lambda *_a, **_kw: None)
    task = asyncio.create_task(code_files.upload_folder(_request(project_id), db=request_db, user=user))
    try:
        assert await asyncio.to_thread(ready.wait, 8)
        with factory.begin() as db:
            project = db.get(Project, project_id)
            project.user_id = other_id
        release.set()
        response = await task
        assert response.data["success_count"] == 0
        assert response.data["fail_count"] == 2
        with factory() as db:
            assert db.query(CodeFile).filter_by(project_id=project_id).count() == 0
    finally:
        release.set()
        request_db.close()
