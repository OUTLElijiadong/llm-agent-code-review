"""Real JWT route and controlled revocation order on isolated MySQL only."""

import io
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger
from test_mysql_credential_policy_prototype import _mysql_factory, _setup

from app.api.v1.api_config import router
from app.core.database import get_db
from app.core.error_handlers import register_handlers
from app.core.security import create_access_token
from app.models.api_config import UserApiConfig
from app.models.user import User
from app.services import api_config_service
from app.services import credential_policy_prototype as policy

mysql_factory = _mysql_factory


@pytest.fixture
def route_fixture(mysql_factory, monkeypatch):
    owner_id, initial = _setup(mysql_factory, monkeypatch, 'user')
    monkeypatch.setattr(api_config_service, '_CREDENTIAL_POLICY_SAVE_EXPERIMENT', True)
    monkeypatch.setattr(api_config_service, 'normalize_ai_base_url', lambda value, **_: value)
    monkeypatch.setattr(api_config_service, 'encrypt_api_key', lambda _value: 'synthetic-ciphertext')
    app = FastAPI()
    register_handlers(app)
    app.include_router(router, prefix='/api')

    def isolated_db():
        with mysql_factory() as db:
            yield db

    app.dependency_overrides[get_db] = isolated_db
    token = create_access_token(owner_id, 'user')
    return app, owner_id, initial, token


def _request(app, token):
    with TestClient(app, raise_server_exceptions=False) as client:
        return client.put('/api/api-config', headers={'Authorization': f'Bearer {token}'}, json={
            'provider': 'openai', 'base_url': 'https://api.example.com', 'model': 'route-model',
            'api_key': 'synthetic-key-not-real',
        })


@pytest.mark.parametrize('change', ['token', 'disable'])
def test_revoked_after_initial_auth_before_write_rechecks_current_user(
    route_fixture, mysql_factory, monkeypatch, change,
):
    app, owner_id, initial, token = route_fixture
    before_write, resume = Event(), Event()

    def wait_after_initial_auth(_key):
        before_write.set()
        if not resume.wait(10):
            raise RuntimeError('revocation did not complete')
        return 'synthetic-ciphertext'

    monkeypatch.setattr(api_config_service, 'encrypt_api_key', wait_after_initial_auth)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_request, app, token)
        try:
            assert before_write.wait(10)
            with mysql_factory.begin() as db:
                user = db.query(User).filter_by(id=owner_id).one()
                if change == 'token':
                    user.token_version += 1
                else:
                    user.status = 0
        finally:
            resume.set()
        assert future.result(timeout=10).status_code in {401, 403}
    with mysql_factory() as db:
        row = db.query(UserApiConfig).filter_by(user_id=owner_id).one()
        assert row.api_key_enc == 'original' and row.is_active
        assert policy.read(db, kind='user', owner_id=owner_id) == initial


def test_user_row_lock_serializes_revocation_after_authorized_save(route_fixture, mysql_factory, monkeypatch):
    app, owner_id, initial, token = route_fixture
    authorized, release, revoker_started = Event(), Event(), Event()
    original_save = policy.save_current_user_configuration

    def wait_after_authorized_write(*args, **kwargs):
        result = original_save(*args, **kwargs)
        authorized.set()
        if not release.wait(10):
            raise RuntimeError('revoker did not start')
        return result

    def revoke():
        with mysql_factory.begin() as db:
            user = db.query(User).filter_by(id=owner_id).one()
            user.token_version += 1
            revoker_started.set()
            db.flush()

    monkeypatch.setattr(policy, 'save_current_user_configuration', wait_after_authorized_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        request = pool.submit(_request, app, token)
        try:
            assert authorized.wait(10)
            revoked = pool.submit(revoke)
            assert revoker_started.wait(10)
            with pytest.raises(TimeoutError):
                revoked.result(timeout=0.2)
        finally:
            release.set()
        assert request.result(timeout=10).status_code == 200
        revoked.result(timeout=10)
    with mysql_factory() as db:
        row = db.query(UserApiConfig).filter_by(user_id=owner_id).one()
        assert row.api_key_enc == 'synthetic-ciphertext' and row.model == 'route-model'
        assert policy.read(db, kind='user', owner_id=owner_id).revision == initial.revision + 1
        assert db.query(User).filter_by(id=owner_id).one().token_version == 1


def test_authorized_route_failure_after_write_rolls_back_without_exposing_credentials(
    route_fixture, mysql_factory, monkeypatch,
):
    app, owner_id, initial, token = route_fixture
    original_save = policy.save_current_user_configuration

    def fail_after_write(*args, **kwargs):
        original_save(*args, **kwargs)
        raise RuntimeError('synthetic write failure')

    monkeypatch.setattr(policy, 'save_current_user_configuration', fail_after_write)
    captured = io.StringIO()
    sink_id = logger.add(captured, diagnose=True, backtrace=True)
    try:
        response = _request(app, token)
    finally:
        logger.remove(sink_id)
    assert response.status_code == 503
    assert response.json()['retryable'] is False
    assert 'synthetic-key-not-real' not in response.text and token not in response.text
    assert 'synthetic-key-not-real' not in captured.getvalue() and token not in captured.getvalue()
    with mysql_factory() as db:
        row = db.query(UserApiConfig).filter_by(user_id=owner_id).one()
        assert row.api_key_enc == 'original' and row.is_active
        assert policy.read(db, kind='user', owner_id=owner_id) == initial
