"""Real JWT and FastAPI entry point for the disabled credential-save experiment."""

import io
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.v1.api_config import router
from app.core.database import Base, get_db
from app.core.error_handlers import register_handlers
from app.core.security import create_access_token
from app.models.api_config import UserApiConfig
from app.models.credential_policy_prototype import CredentialPolicyBase
from app.models.user import User
from app.services import api_config_service
from app.services import credential_policy_prototype as policy


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'route.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=[User.__table__, UserApiConfig.__table__])
    CredentialPolicyBase.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(policy, '_EXPERIMENT_ENABLED', True)
    monkeypatch.setattr(api_config_service, '_CREDENTIAL_POLICY_SAVE_EXPERIMENT', True)
    monkeypatch.setattr(api_config_service, 'normalize_ai_base_url', lambda value, **_: value)
    monkeypatch.setattr(api_config_service, 'encrypt_api_key', lambda _value: 'synthetic-ciphertext')
    with sessions.begin() as db:
        db.add(User(id=702, username='route-user', password='test', role='user', status=1, token_version=0))
    with sessions.begin() as db:
        original = policy.initialize(db, kind='user', owner_id=702)
    app = FastAPI()
    register_handlers(app)
    app.include_router(router, prefix='/api')

    def isolated_db():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = isolated_db
    try:
        yield app, sessions, original
    finally:
        engine.dispose()


def _payload():
    return {'provider': 'custom', 'base_url': 'https://api.example.com',
            'model': 'synthetic-model', 'api_key': 'synthetic-secret-not-real-458'}


def test_actual_jwt_route_saves_real_config_and_version(setup):
    app, sessions, original = setup
    token = create_access_token(702, 'user')
    with TestClient(app, raise_server_exceptions=False) as client:
        result = client.put('/api/api-config', headers={'Authorization': f'Bearer {token}'}, json=_payload())
    assert result.status_code == 200
    assert result.json()['data']['model'] == 'synthetic-model'
    assert _payload()['api_key'] not in result.text and token not in result.text
    with sessions() as db:
        row = db.query(UserApiConfig).filter_by(user_id=702).one()
        assert row.api_key_enc == 'synthetic-ciphertext' and row.is_active
    with sessions() as db:
        assert policy.read(db, kind='user', owner_id=702).revision == original.revision + 1


@pytest.mark.parametrize('revoke_kind', ['token', 'disable'])
def test_actual_route_rechecks_auth_inside_write_transaction(setup, monkeypatch, revoke_kind):
    app, sessions, original = setup
    token = create_access_token(702, 'user')

    def revoke_after_original_auth(_key):
        with sessions.begin() as db:
            user = db.query(User).filter_by(id=702).one()
            if revoke_kind == 'token':
                user.token_version += 1
            else:
                user.status = 0
        return 'synthetic-ciphertext'

    monkeypatch.setattr(api_config_service, 'encrypt_api_key', revoke_after_original_auth)
    with TestClient(app, raise_server_exceptions=False) as client:
        result = client.put('/api/api-config', headers={'Authorization': f'Bearer {token}'}, json=_payload())
    assert result.status_code in {401, 403}
    with sessions() as db:
        assert db.query(UserApiConfig).filter_by(user_id=702).one_or_none() is None
    with sessions() as db:
        assert policy.read(db, kind='user', owner_id=702) == original


def test_write_failure_rolls_back_without_logging_key_or_token(setup, monkeypatch):
    app, sessions, original = setup
    token = create_access_token(702, 'user')
    original_save = policy.save_current_user_configuration

    def fail_after_write(*args, **kwargs):
        original_save(*args, **kwargs)
        raise RuntimeError('synthetic error')

    monkeypatch.setattr(policy, 'save_current_user_configuration', fail_after_write)
    sink = io.StringIO()
    sink_id = logger.add(sink, diagnose=True, backtrace=True)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            result = client.put('/api/api-config', headers={'Authorization': f'Bearer {token}'}, json=_payload())
    finally:
        logger.remove(sink_id)
    assert result.status_code == 503
    assert _payload()['api_key'] not in result.text and token not in result.text
    assert _payload()['api_key'] not in sink.getvalue() and token not in sink.getvalue()
    with sessions() as db:
        assert db.query(UserApiConfig).filter_by(user_id=702).one_or_none() is None
    with sessions() as db:
        assert policy.read(db, kind='user', owner_id=702) == original


def test_expiry_before_commit_rejects_and_rolls_back(setup, monkeypatch):
    app, sessions, original = setup
    now = int(time.time())
    token = create_access_token(702, 'user', expires_at=now + 60)
    timestamps = iter([now, now + 61])
    monkeypatch.setattr(api_config_service, 'time', SimpleNamespace(time=lambda: next(timestamps)))
    with TestClient(app, raise_server_exceptions=False) as client:
        result = client.put('/api/api-config', headers={'Authorization': f'Bearer {token}'}, json=_payload())
    assert result.status_code == 401
    with sessions() as db:
        assert db.query(UserApiConfig).filter_by(user_id=702).one_or_none() is None
    with sessions() as db:
        assert policy.read(db, kind='user', owner_id=702) == original
