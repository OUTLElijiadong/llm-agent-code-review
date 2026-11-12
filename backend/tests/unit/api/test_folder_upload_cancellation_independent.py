from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from tempfile import SpooledTemporaryFile
from time import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.datastructures import FormData, Headers, UploadFile

from app.api.v1 import code_files
from app.core import error_handlers
from app.core.database import Base, get_db
from app.core.dependencies import authenticate_access_token
from app.core.exceptions import PermissionError as AppPermissionError
from app.core.exceptions import ServiceUnavailableError
from app.core.security import create_access_token
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.user import User
from app.services import code_file_service


def multipart_request(*files):
    body = b'--abc\r\nContent-Disposition: form-data; name="project_id"\r\n\r\n7\r\n'
    for name, data in files:
        body += (b'--abc\r\nContent-Disposition: form-data; name="files"; filename="'
                 + name.encode() + b'"\r\nContent-Type: text/plain\r\n\r\n' + data + b'\r\n')
    body += b'--abc--\r\n'

    async def stream():
        yield body

    return SimpleNamespace(headers=Headers({"content-type": "multipart/form-data; boundary=abc"}), stream=stream)


@pytest.mark.asyncio
async def test_cancelled_folder_request_does_not_close_worker_file(monkeypatch):
    started, release, completed = threading.Event(), threading.Event(), threading.Event()
    observed = {}

    def worker(_engine, _actor, _project_id, files, _cancelled):
        started.set()
        try:
            assert release.wait(5)
            observed["content"] = files[0].file.read()
        except Exception as exc:
            observed["error"] = exc
        finally:
            completed.set()
        return {"success_count": 1, "fail_count": 0, "files": [], "errors": []}

    monkeypatch.setattr(code_files, "_run_owned_folder_upload", worker)
    request = multipart_request(("a.py", b"hello"))
    engine = create_engine("sqlite://")
    db = SimpleNamespace(get_bind=lambda: engine)
    user = SimpleNamespace(id=1, _authenticated_token_version=0, _authenticated_token_expires_at=int(time()) + 60)
    task = asyncio.create_task(code_files.upload_folder(request, db=db, user=user))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        assert await asyncio.to_thread(completed.wait, 5)
        engine.dispose()
    assert observed == {"content": b"hello"}


@pytest.mark.asyncio
async def test_cancel_after_first_commit_stops_next_file_and_worker_owns_session(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'folder.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with factory.begin() as db:
        db.add(User(id=1, username="owner", password="x", role="admin", status=1, token_version=0))
        db.add(Project(id=7, user_id=1, project_name="source", status="active"))
    request_db = factory()
    user = authenticate_access_token(create_access_token(1, "admin"), request_db)
    started, release, completed = threading.Event(), threading.Event(), threading.Event()
    original = code_file_service._create_file
    original_worker = code_files._run_owned_folder_upload

    def hold_after_first(*args, **kwargs):
        result = original(*args, **kwargs)
        started.set()
        assert release.wait(5)
        return result

    def mark_completed(*args, **kwargs):
        try:
            return original_worker(*args, **kwargs)
        finally:
            completed.set()

    monkeypatch.setattr(code_file_service, "_validate_upload_security", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(code_file_service, "_create_file", hold_after_first)
    monkeypatch.setattr(code_files, "_run_owned_folder_upload", mark_completed)
    task = asyncio.create_task(code_files.upload_folder(
        multipart_request(("a.py", b"a=1"), ("b.py", b"b=2")), db=request_db, user=user,
    ))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        request_db.close()
    finally:
        release.set()
        assert await asyncio.to_thread(completed.wait, 5)
    # The first file was already committed; cancellation stops subsequent files.
    with factory() as db:
        names = [row.file_name for row in db.query(CodeFile).all()]
    assert names == ["a.py"]
    engine.dispose()


@pytest.mark.asyncio
async def test_queued_cancel_closes_owned_form_when_worker_never_starts(monkeypatch):
    executor = ThreadPoolExecutor(max_workers=1)
    blocker_started, release = threading.Event(), threading.Event()
    blocker = executor.submit(lambda: (blocker_started.set(), release.wait(5)))
    assert await asyncio.to_thread(blocker_started.wait, 3)
    monkeypatch.setattr(code_files, "_folder_upload_executor", executor)
    captured = []
    original_close = code_files._close_folder_form

    def capture_close(form):
        captured.extend(item.file for _, item in form.multi_items() if hasattr(item, "file"))
        original_close(form)

    monkeypatch.setattr(code_files, "_close_folder_form", capture_close)
    engine = create_engine("sqlite://")
    db = SimpleNamespace(get_bind=lambda: engine)
    user = SimpleNamespace(id=1, _authenticated_token_version=0, _authenticated_token_expires_at=int(time()) + 60)
    task = asyncio.create_task(code_files.upload_folder(multipart_request(("a.py", b"x")), db=db, user=user))
    try:
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(100):
            if captured:
                break
            await asyncio.sleep(0.01)
        assert captured and all(file.closed for file in captured)
    finally:
        release.set()
        await asyncio.to_thread(blocker.result, 5)
        executor.shutdown(wait=True)
        engine.dispose()


@pytest.mark.asyncio
async def test_cancelling_wrapped_future_cannot_close_running_worker_files(monkeypatch):
    started, release, done = threading.Event(), threading.Event(), threading.Event()
    observed = {}
    wrapped_futures = []
    original_wrap = asyncio.wrap_future

    def capture_wrap(future, *args, **kwargs):
        wrapped = original_wrap(future, *args, **kwargs)
        wrapped_futures.append(wrapped)
        return wrapped

    def worker(_engine, _actor, _project_id, files, _cancelled):
        started.set()
        try:
            assert release.wait(5)
            observed["content"] = files[0].file.read()
        except Exception as exc:
            observed["error"] = exc
        finally:
            done.set()
        return {"success_count": 1, "fail_count": 0, "files": [], "errors": []}

    monkeypatch.setattr(code_files, "asyncio", SimpleNamespace(
        wrap_future=capture_wrap, shield=asyncio.shield, CancelledError=asyncio.CancelledError,
    ))
    monkeypatch.setattr(code_files, "_run_owned_folder_upload", worker)
    engine = create_engine("sqlite://")
    db = SimpleNamespace(get_bind=lambda: engine)
    user = SimpleNamespace(id=1, _authenticated_token_version=0, _authenticated_token_expires_at=int(time()) + 60)
    task = asyncio.create_task(code_files.upload_folder(multipart_request(("a.py", b"hello")), db=db, user=user))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        assert wrapped_futures
        wrapped_futures[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        assert await asyncio.to_thread(done.wait, 5)
        engine.dispose()
    assert observed == {"content": b"hello"}


@pytest.mark.asyncio
async def test_bounded_upload_capacity_is_released_after_cancelled_worker_finishes(monkeypatch):
    started, release, done = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(code_files, "_folder_upload_capacity", threading.BoundedSemaphore(1))

    def worker(_engine, _actor, _project_id, files, _cancelled):
        started.set()
        try:
            assert release.wait(5)
            assert len(files) == 1000
            assert files[0].file.read() == b"x"
            assert files[-1].file.read() == b"x"
        finally:
            done.set()
        return {"success_count": 0, "fail_count": 0, "files": [], "errors": []}

    monkeypatch.setattr(code_files, "_run_owned_folder_upload", worker)
    engine = create_engine("sqlite://")
    db = SimpleNamespace(get_bind=lambda: engine)
    user = SimpleNamespace(id=1, _authenticated_token_version=0, _authenticated_token_expires_at=int(time()) + 60)
    task = asyncio.create_task(code_files.upload_folder(
        multipart_request(*[(f"{index}.py", b"x") for index in range(1000)]), db=db, user=user,
    ))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        with pytest.raises(ServiceUnavailableError):
            await code_files.upload_folder(multipart_request(("other.py", b"x")), db=db, user=user)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        assert await asyncio.to_thread(done.wait, 5)
        # The concurrent-future cleanup may finish just after worker return.
        for _ in range(100):
            if code_files._folder_upload_capacity.acquire(blocking=False):
                code_files._folder_upload_capacity.release()
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("已取消上传未释放容量")
        engine.dispose()


@pytest.mark.asyncio
async def test_cancel_during_validation_cleanup_still_closes_all_form_files(monkeypatch):
    first, second = SpooledTemporaryFile(max_size=1), SpooledTemporaryFile(max_size=1)
    first.write(b"first")
    second.write(b"second")
    started, release = threading.Event(), threading.Event()
    original_close = first.close

    def slow_close():
        started.set()
        assert release.wait(5)
        original_close()

    first.close = slow_close
    form = FormData([
        ("project_id", "invalid"),
        ("files", UploadFile(first, filename="first.py")),
        ("files", UploadFile(second, filename="second.py")),
    ])

    async def parse(*_args, **_kwargs):
        return form

    monkeypatch.setattr(code_files, "parse_limited_multipart", parse)
    engine = create_engine("sqlite://")
    db = SimpleNamespace(get_bind=lambda: engine)
    user = SimpleNamespace(id=1, _authenticated_token_version=0, _authenticated_token_expires_at=int(time()) + 60)
    task = asyncio.create_task(code_files.upload_folder(multipart_request(), db=db, user=user))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        for _ in range(100):
            if first.closed and second.closed:
                break
            await asyncio.sleep(0.01)
        engine.dispose()
    assert first.closed and second.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["token", "disabled", "role"])
async def test_each_file_rechecks_live_actor_before_next_commit(monkeypatch, tmp_path, revocation):
    engine = create_engine(f"sqlite:///{tmp_path / 'actor.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with factory.begin() as db:
        db.add(User(id=1, username="owner", password="x", role="admin", status=1, token_version=0))
        db.add(Project(id=7, user_id=1, project_name="source", status="active"))
    request_db = factory()
    user = authenticate_access_token(create_access_token(1, "admin"), request_db)
    first_committed, release = threading.Event(), threading.Event()
    original = code_file_service._create_file

    def hold_after_first(*args, **kwargs):
        result = original(*args, **kwargs)
        first_committed.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(code_file_service, "_validate_upload_security", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(code_file_service, "_create_file", hold_after_first)
    task = asyncio.create_task(code_files.upload_folder(
        multipart_request(("a.py", b"a=1"), ("b.py", b"b=2")), db=request_db, user=user,
    ))
    try:
        assert await asyncio.to_thread(first_committed.wait, 5)
        with factory.begin() as db:
            actor = db.get(User, 1)
            if revocation == "token":
                actor.token_version += 1
            elif revocation == "disabled":
                actor.status = 0
            else:
                actor.role = "user"
        release.set()
        with pytest.raises(AppPermissionError):
            await task
        with factory() as db:
            assert [row.file_name for row in db.query(CodeFile).all()] == ["a.py"]
    finally:
        release.set()
        request_db.close()
        engine.dispose()


def test_folder_upload_normal_path_uses_real_jwt_and_independent_session(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'normal.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with factory.begin() as db:
        db.add(User(id=1, username="owner", password="x", role="admin", status=1, token_version=0))
        db.add(Project(id=7, user_id=1, project_name="source", status="active"))
    app = FastAPI()
    error_handlers.register_handlers(app)
    app.include_router(code_files.router, prefix="/api/code-files")

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(code_file_service, "_validate_upload_security", lambda *_args, **_kwargs: None)
    with TestClient(app) as client:
        result = client.post(
            "/api/code-files/upload-folder", headers={"Authorization": f"Bearer {create_access_token(1, 'admin')}"},
            data={"project_id": "7"}, files=[("files", ("a.py", b"a=1")), ("files", ("b.py", b"b=2"))],
        )
    assert result.status_code == 200
    assert result.json()["data"]["success_count"] == 2
    with factory() as db:
        assert [row.file_name for row in db.query(CodeFile).order_by(CodeFile.id)] == ["a.py", "b.py"]
    engine.dispose()
