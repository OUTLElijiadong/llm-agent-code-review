"""Malformed uploads must fail closed without logging the request body."""

from time import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.api.v1 import code_files, wechat_fund_admin
from app.core import error_handlers
from app.core.database import get_db
from app.core.dependencies import get_current_user, require_super_admin


@pytest.fixture
def folder_auth_overrides():
    engine = create_engine('sqlite://')
    yield {
        get_db: lambda: SimpleNamespace(get_bind=lambda: engine),
        get_current_user: lambda: SimpleNamespace(
            id=1, _authenticated_token_version=0, _authenticated_token_expires_at=int(time()) + 60,
        ),
    }
    engine.dispose()


@pytest.mark.parametrize(
    ('headers', 'body'),
    [
        ({}, b''),
        ({'content-type': 'application/json'}, b'{"sensitive":"test-secret"}'),
        ({'content-type': 'multipart/form-data; boundary=abc'}, b'bad-test-secret'),
        ({'content-type': 'multipart/form-data; boundary='}, b'bad-test-secret'),
        ({'content-type': 'multipart/form-data; boundary=' + 'x' * 71}, b'bad-test-secret'),
        ({'content-type': 'multipart/form-data; boundary=' + 'x' * 257}, b'bad-test-secret'),
        ({'content-type': 'multipart/form-data; boundary=abc; BOUNDARY=abc'}, b'bad-test-secret'),
    ],
)
@pytest.mark.parametrize('path', ['/api/code-files/upload-folder', '/api/billing/admin/fund-statements/import'])
def test_invalid_multipart_is_client_error_without_body_logging(monkeypatch, caplog, path, headers, body,
                                                                folder_auth_overrides):
    unexpected_logs = []
    monkeypatch.setattr(error_handlers.logger, 'exception', lambda *args, **kwargs: unexpected_logs.append(args))
    app = FastAPI()
    error_handlers.register_handlers(app)
    app.include_router(code_files.router, prefix='/api/code-files')
    app.include_router(wechat_fund_admin.router, prefix='/api')
    folder_route = next(route for route in code_files.router.routes if route.path == '/upload-folder')
    for dependency in folder_route.dependant.dependencies:
        app.dependency_overrides[dependency.call] = lambda: object()
    app.dependency_overrides.update(folder_auth_overrides)
    app.dependency_overrides[require_super_admin] = lambda: object()
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(path, headers=headers, content=body)
    assert response.status_code in (400, 415, 422)
    assert 'test-secret' not in response.text
    assert 'test-secret' not in caplog.text
    assert not unexpected_logs


@pytest.mark.parametrize('path', ['/api/code-files/upload-folder', '/api/billing/admin/fund-statements/import'])
def test_duplicate_content_type_fields_fail_closed(path, folder_auth_overrides):
    app = FastAPI()
    error_handlers.register_handlers(app)
    app.include_router(code_files.router, prefix='/api/code-files')
    app.include_router(wechat_fund_admin.router, prefix='/api')
    folder_route = next(route for route in code_files.router.routes if route.path == '/upload-folder')
    for dependency in folder_route.dependant.dependencies:
        app.dependency_overrides[dependency.call] = lambda: object()
    app.dependency_overrides.update(folder_auth_overrides)
    app.dependency_overrides[require_super_admin] = lambda: object()
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(path, headers=[('Content-Type', 'multipart/form-data; boundary=abc'),
                                              ('content-type', 'multipart/form-data; boundary=xyz')], content=b'')
    assert response.status_code == 400


@pytest.mark.parametrize('path', ['/api/code-files/upload', '/api/code-files/upload-folder'])
def test_valid_code_uploads_keep_their_contract(monkeypatch, path, folder_auth_overrides):
    app = FastAPI()
    error_handlers.register_handlers(app)
    app.include_router(code_files.router, prefix='/api/code-files')
    route_path = path.removeprefix('/api/code-files')
    route = next(route for route in code_files.router.routes if route.path == route_path)
    for dependency in route.dependant.dependencies:
        app.dependency_overrides[dependency.call] = lambda: object()
    app.dependency_overrides.update(folder_auth_overrides)
    user = folder_auth_overrides[get_current_user]()
    monkeypatch.setattr(
        code_files,
        "_authorize_folder_actor",
        lambda _db, _actor: code_files._FolderAuthorization(user, None, True, object()),
    )
    seen = []

    def upload_one(**kwargs):
        seen.append(kwargs['upload_file'].file.read())
        return 1, 'python', 1

    def upload_folder(_db, _user, project_id, files, **_kwargs):
        assert project_id == 7
        seen.extend(item.file.read() for item in files)
        return {'success_count': len(files), 'fail_count': 0, 'files': [], 'errors': []}

    monkeypatch.setattr(code_files.code_file_service, 'upload', upload_one)
    monkeypatch.setattr(code_files, '_upload_folder_items', upload_folder)
    name = 'files' if path.endswith('upload-folder') else 'file'
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(path, data={'project_id': '7'}, files={name: ('a.py', b'content', 'text/plain')})
    assert response.status_code == 200
    assert seen == [b'content']
