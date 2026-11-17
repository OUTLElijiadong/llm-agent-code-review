"""Additional real-database credential races; use only the guarded disposable fixture."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from cryptography.fernet import Fernet
from test_mysql_byok_fallback_races import _create_user
from test_mysql_byok_fallback_races import isolated_mysql as _guarded_mysql

from app.models.api_config import UserApiConfig
from app.models.system_config import SystemConfig
from app.schemas.api_config import ApiConfigSaveIn
from app.services import api_config_service, system_config_service
from app.utils import api_resolver

_REAL_GLOBAL_READ = system_config_service.get_llm_config


@pytest.fixture(name="isolated_mysql")
def _rotation_mysql(monkeypatch):
    yield from _guarded_mysql.__wrapped__(monkeypatch)


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="stale key rotation overwrites a concurrently saved newer user key")
def test_old_user_key_rotation_must_not_replace_new_mysql_key(isolated_mysql, monkeypatch):
    current, previous = "test-current-only-4567890", "test-previous-only-123456"
    monkeypatch.setattr(api_resolver.settings, "api_key_encryption_keys", [current, previous])
    old_ciphertext = Fernet(api_resolver._derive_fernet_key(previous)).encrypt(b"isolated-old-key").decode()
    user_id = _create_user(isolated_mysql, ciphertext=old_ciphertext)
    selected_old, resume_old = Event(), Event()
    original_decrypt = api_resolver.decrypt_api_key_with_metadata

    def hold_old_decryption(value):
        if value == old_ciphertext:
            selected_old.set()
            if not resume_old.wait(10):
                raise RuntimeError("new-key write did not finish in time")
        return original_decrypt(value)

    monkeypatch.setattr(api_resolver, "decrypt_api_key_with_metadata", hold_old_decryption)

    def old_resolution():
        with isolated_mysql() as db:
            return api_resolver.resolve_api_config(db, user_id).source

    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(old_resolution)
        try:
            assert selected_old.wait(10)
            with isolated_mysql() as db:
                api_config_service.save_config(db, user_id, ApiConfigSaveIn(
                    provider="deepseek", base_url="https://api.deepseek.com/v1",
                    model="deepseek-v4-flash", api_key="isolated-new-key",
                ))
        finally:
            resume_old.set()
        assert old.result(timeout=10) == "user"
    with isolated_mysql() as db:
        row = db.query(UserApiConfig).filter_by(user_id=user_id).one()
        assert api_resolver.decrypt_api_key_with_metadata(row.api_key_enc).plaintext == "isolated-new-key"


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="global decode failure becomes inactive and is treated as a normal system fallback")
def test_invalid_global_key_must_not_appear_as_normal_system_source(isolated_mysql, monkeypatch):
    monkeypatch.setattr(system_config_service, "get_llm_config", _REAL_GLOBAL_READ)
    SystemConfig.__table__.create(bind=isolated_mysql.kw["bind"], checkfirst=True)
    data = {"active": True, "provider": "deepseek", "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-v4-flash", "api_key_enc": "invalid-ciphertext"}
    with isolated_mysql() as db:
        system_config_service._set_raw(db, system_config_service.LLM_KEY, json.dumps(data))
    with isolated_mysql() as db:
        config = api_resolver.resolve_api_config(db)
        assert config.billing_credential_source == "fallback"


@pytest.mark.parametrize("cause", ["failure", "rotation"])
@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="stale global security write replaces a concurrently saved global config")
def test_old_global_security_write_must_not_replace_new_config(isolated_mysql, monkeypatch, cause):
    monkeypatch.setattr(system_config_service, "get_llm_config", _REAL_GLOBAL_READ)
    monkeypatch.setattr(system_config_service, "normalize_ai_base_url", lambda value, **_kw: value)
    current, previous = "test-current-only-4567890", "test-previous-only-123456"
    monkeypatch.setattr(api_resolver.settings, "api_key_encryption_keys", [current, previous])
    SystemConfig.__table__.create(bind=isolated_mysql.kw["bind"], checkfirst=True)
    old_ciphertext = (
        "invalid-ciphertext" if cause == "failure"
        else Fernet(api_resolver._derive_fernet_key(previous)).encrypt(b"isolated-old-key").decode()
    )
    data = {"active": True, "provider": "deepseek", "base_url": "https://api.deepseek.com/v1",
            "model": "old-test-model", "api_key_enc": old_ciphertext}
    with isolated_mysql() as db:
        system_config_service._set_raw(db, system_config_service.LLM_KEY, json.dumps(data))
    selected_old, resume_old = Event(), Event()
    original_decrypt = api_resolver.decrypt_api_key_with_metadata

    def hold_old_decryption(value):
        if value == old_ciphertext:
            selected_old.set()
            if not resume_old.wait(10):
                raise RuntimeError("new global configuration did not finish in time")
        return original_decrypt(value)

    monkeypatch.setattr(api_resolver, "decrypt_api_key_with_metadata", hold_old_decryption)

    def old_read():
        with isolated_mysql() as db:
            return system_config_service.get_llm_config(db)

    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(old_read)
        try:
            assert selected_old.wait(10)
            with isolated_mysql() as db:
                system_config_service.update_llm_config(
                    db, provider="deepseek", base_url="https://api.deepseek.com/v1",
                    model="new-test-model", api_key="isolated-new-key", active=True,
                )
        finally:
            resume_old.set()
        old.result(timeout=10)
    with isolated_mysql() as db:
        config = system_config_service.get_llm_config(db)
        assert config["active"] is True
        assert config["model"] == "new-test-model"
        assert config["api_key"] == "isolated-new-key"
