"""Disabled integration contract for the real user configuration save path."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.api_config import UserApiConfig
from app.models.credential_policy_prototype import CredentialPolicyBase
from app.models.user import User
from app.schemas.api_config import ApiConfigSaveIn
from app.services import api_config_service
from app.services import credential_policy_prototype as policy


@pytest.fixture
def store(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'config.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=[User.__table__, UserApiConfig.__table__])
    CredentialPolicyBase.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(policy, '_EXPERIMENT_ENABLED', True)
    monkeypatch.setattr(api_config_service, '_CREDENTIAL_POLICY_SAVE_EXPERIMENT', True, raising=False)
    monkeypatch.setattr(api_config_service, 'normalize_ai_base_url', lambda value, **_: value)
    monkeypatch.setattr(api_config_service, 'encrypt_api_key', lambda value: f'enc:{value}')
    with sessions.begin() as db:
        db.add(User(id=701, username='policy-user', password='test', role='user', status=1))
    with sessions.begin() as db:
        original = policy.initialize(db, kind='user', owner_id=701)
    try:
        yield engine, sessions, original
    finally:
        engine.dispose()


def _payload(provider='openai', api_key='new-secret', base_url='https://api.example.com', model='new-model'):
    return ApiConfigSaveIn(provider=provider, api_key=api_key, base_url=base_url, model=model)


def test_real_save_persists_all_fields_and_fences_old_snapshot(store):
    _, sessions, original = store
    with sessions() as caller:
        result = api_config_service.save_config(caller, 701, _payload())
    assert (result.provider, result.base_url, result.model, result.is_active) == (
        'openai', 'https://api.example.com', 'new-model', True,
    )
    assert 'new-secret' not in result.api_key_masked
    with sessions() as db:
        row = db.query(UserApiConfig).filter_by(user_id=701).one()
        assert (row.provider, row.base_url, row.model, row.api_key_enc, row.is_active) == (
            'openai', 'https://api.example.com', 'new-model', 'enc:new-secret', True,
        )
    with sessions.begin() as db:
        with pytest.raises(policy.PolicyConflict):
            policy.mark_failed(db, original)
    with sessions() as db:
        updated = policy.read(db, kind='user', owner_id=701)
        assert updated.revision == original.revision + 1 and updated.state == 'active'


def test_real_save_updates_existing_configuration_atomically(store, monkeypatch):
    _, sessions, original = store
    with sessions() as caller:
        api_config_service.save_config(caller, 701, _payload())
    with sessions() as db:
        current = policy.read(db, kind='user', owner_id=701)
    old_implementation = policy.save_current_user_configuration

    def fail_after_flush(*args, **kwargs):
        old_implementation(*args, **kwargs)
        raise RuntimeError('simulated failure after policy and config writes')

    monkeypatch.setattr(policy, 'save_current_user_configuration', fail_after_flush)
    with sessions() as caller, pytest.raises(RuntimeError, match='simulated failure'):
        api_config_service.save_config(caller, 701, _payload('custom', 'later', 'https://next.example.com', 'later'))
    with sessions() as db:
        row = db.query(UserApiConfig).filter_by(user_id=701).one()
        assert (row.provider, row.api_key_enc, row.model) == ('openai', 'enc:new-secret', 'new-model')
    with sessions() as db:
        assert policy.read(db, kind='user', owner_id=701) == current


def test_real_save_rejects_active_caller_transaction_without_flushing(store):
    _, sessions, _ = store
    with sessions() as caller:
        user = caller.query(User).filter_by(id=701).one()
        user.username = 'caller-pending'
        with pytest.raises(policy.PolicyError, match='调用者仍有活动事务'):
            api_config_service.save_config(caller, 701, _payload())
        assert user in caller.dirty and user.username == 'caller-pending'
        caller.rollback()
    with sessions() as db:
        assert db.query(User).filter_by(id=701).one().username == 'policy-user'
        assert db.query(UserApiConfig).filter_by(user_id=701).one_or_none() is None


@pytest.mark.parametrize('flushed', [False, True])
def test_caller_loaded_old_config_cannot_overwrite_saved_config(store, flushed):
    _, sessions, _ = store
    with sessions() as caller:
        api_config_service.save_config(caller, 701, _payload())
    with sessions() as caller:
        old = caller.query(UserApiConfig).filter_by(user_id=701).one()
        old.model = 'caller-stale-model'
        if flushed:
            caller.flush()
        with pytest.raises(policy.PolicyError, match='调用者仍有活动事务'):
            api_config_service.save_config(caller, 701, _payload(model='newer-model'))
        assert old.model == 'caller-stale-model'
        caller.rollback()
    with sessions() as db:
        assert db.query(UserApiConfig).filter_by(user_id=701).one().model == 'new-model'


def test_caller_old_identity_retained_after_commit_is_rejected(store):
    _, sessions, _ = store
    with sessions() as caller:
        api_config_service.save_config(caller, 701, _payload())
    with sessions() as caller:
        old = caller.query(UserApiConfig).filter_by(user_id=701).one()
        caller.commit()
        assert not caller.in_transaction()
        with pytest.raises(policy.PolicyError, match='调用者仍持有配置对象'):
            api_config_service.save_config(caller, 701, _payload(model='newer-model'))
        assert old.model == 'new-model'
    with sessions() as db:
        assert db.query(UserApiConfig).filter_by(user_id=701).one().model == 'new-model'


def test_caller_direct_sql_write_does_not_self_block_or_commit(store):
    _, sessions, _ = store
    with sessions() as caller:
        api_config_service.save_config(caller, 701, _payload())
    with sessions() as caller:
        caller.execute(text("UPDATE user_api_config SET model='caller-sql' WHERE user_id=701"))
        with pytest.raises(policy.PolicyError, match='调用者仍有活动事务'):
            api_config_service.save_config(caller, 701, _payload(model='newer-model'))
        assert caller.execute(text('SELECT model FROM user_api_config WHERE user_id=701')).scalar_one() == 'caller-sql'
        caller.rollback()
    with sessions() as db:
        assert db.query(UserApiConfig).filter_by(user_id=701).one().model == 'new-model'


def test_authenticated_session_cannot_directly_enter_experimental_save(store):
    from app.core.dependencies import get_current_user
    from app.core.security import create_access_token

    _, sessions, _ = store
    token = create_access_token(701, 'user')
    with sessions() as caller:
        user = get_current_user(authorization=f'Bearer {token}', db=caller)
        with pytest.raises(policy.PolicyError, match='调用者仍有活动事务'):
            api_config_service.save_config(caller, user.id, _payload())
    with sessions() as db:
        assert db.query(UserApiConfig).filter_by(user_id=701).one_or_none() is None


def test_actual_authenticated_api_keeps_legacy_path_when_experiment_disabled(store, monkeypatch):
    from app.api.v1.api_config import router
    from app.core.database import get_db
    from app.core.security import create_access_token

    _, sessions, _ = store
    monkeypatch.setattr(api_config_service, '_CREDENTIAL_POLICY_SAVE_EXPERIMENT', False)
    app = FastAPI()
    app.include_router(router, prefix='/api')

    def isolated_db():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = isolated_db
    token = create_access_token(701, 'user')
    with TestClient(app) as client:
        response = client.put('/api/api-config', headers={'Authorization': f'Bearer {token}'},
                              json=_payload().model_dump())
    assert response.status_code == 200
    assert response.json()['data']['model'] == 'new-model'
    with sessions() as db:
        assert db.query(UserApiConfig).filter_by(user_id=701).one().model == 'new-model'


def test_real_save_rejects_connection_bound_session(store):
    engine, sessions, _ = store
    with engine.connect() as connection:
        bound_sessions = sessionmaker(bind=connection, autoflush=False)
        with bound_sessions() as caller, pytest.raises(policy.PolicyError, match='独立数据库引擎'):
            api_config_service.save_config(caller, 701, _payload())
    with sessions() as db:
        assert db.query(UserApiConfig).filter_by(user_id=701).one_or_none() is None
