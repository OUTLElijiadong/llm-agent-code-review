"""Opt-in races against a uniquely marked disposable localhost MySQL container.

No network model calls, production databases, automatic database deletion, or
application migrations. The two strict xfails specify still-unimplemented safety.
"""

import os
import re
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.api_config import UserApiConfig
from app.models.user import User
from app.schemas.api_config import ApiConfigSaveIn
from app.services import api_config_service, system_config_service
from app.utils import api_resolver


@pytest.fixture
def isolated_mysql(monkeypatch):
    raw = os.environ.get("PRISM_BYOK_TEST_MYSQL_URL", "")
    container_id = os.environ.get("PRISM_BYOK_TEST_CONTAINER_ID", "")
    nonce = os.environ.get("PRISM_BYOK_TEST_MARKER", "")
    if not raw:
        pytest.skip("explicit isolated BYOK MySQL test environment not provided")
    url = make_url(raw)
    if (url.get_backend_name() != "mysql" or url.host != "127.0.0.1" or not url.port
            or not re.fullmatch(r"[0-9a-f]{64}", container_id)
            or not re.fullmatch(r"[0-9a-f]{32}", nonce)
            or url.database != f"prism_byok_{nonce}"):
        pytest.fail("refusing unverified database configuration")
    identity = subprocess.run(
        ["docker", "inspect", "--format", '{{.Id}} {{index .Config.Labels "prism.task"}} {{.State.Running}}',
         container_id], capture_output=True, text=True, check=False, timeout=5,
    )
    port = subprocess.run(["docker", "port", container_id, "3306/tcp"], capture_output=True,
                          text=True, check=False, timeout=5)
    if (identity.returncode != 0 or identity.stdout.strip() != f"{container_id} byok-race-isolated true"
            or port.returncode != 0 or port.stdout.strip() != f"127.0.0.1:{url.port}"):
        pytest.fail("refusing unverified container identity or port")
    engine = create_engine(raw, pool_pre_ping=True, isolation_level="REPEATABLE READ")
    try:
        with engine.connect() as conn:
            markers = conn.execute(text("SELECT nonce FROM prism_test_identity")).scalars().all()
        if markers != [nonce]:
            pytest.fail("refusing database with mismatched marker")
        Base.metadata.create_all(engine, tables=[User.__table__, UserApiConfig.__table__])
        monkeypatch.setattr(api_resolver.settings, "enforce_ai_base_url_dns_check", False)
        monkeypatch.setattr(api_resolver.settings, "deepseek_api_key", "isolated-system-key")
        monkeypatch.setattr(api_resolver.settings, "deepseek_base_url", "https://api.deepseek.com/v1")
        monkeypatch.setattr(api_resolver.settings, "deepseek_model", "deepseek-v4-flash")
        monkeypatch.setattr(system_config_service, "get_llm_config", lambda _db: None)
        monkeypatch.setattr(api_config_service, "normalize_ai_base_url", lambda value, **_kw: value)
        yield sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    finally:
        engine.dispose()


def _create_user(factory, *, active=True, ciphertext="invalid-ciphertext"):
    user_id = int(uuid.uuid4().hex[:10], 16)
    with factory.begin() as db:
        db.add(User(id=user_id, username=f"byok-{user_id}", password="test-only", role="user", status=1))
        db.flush()
        db.add(UserApiConfig(user_id=user_id, provider="deepseek", base_url="https://api.deepseek.com/v1",
                             model="deepseek-v4-flash", is_active=active, api_key_enc=ciphertext))
    return user_id


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="automatic deactivation has no persisted fallback reason")
def test_automatic_key_failure_stays_nonbillable_across_mysql_sessions(isolated_mysql):
    user_id = _create_user(isolated_mysql)
    with isolated_mysql() as db:
        first = api_resolver.resolve_api_config(db, user_id)
        assert first.billing_credential_source == "fallback"
    with isolated_mysql() as db:
        second = api_resolver.resolve_api_config(db, user_id)
        assert second.billing_credential_source == "fallback"


def test_explicit_delete_restores_system_source_across_mysql_sessions(isolated_mysql):
    encrypted = api_resolver.encrypt_api_key("isolated-user-key")
    user_id = _create_user(isolated_mysql, ciphertext=encrypted)
    with isolated_mysql() as db:
        assert api_resolver.resolve_api_config(db, user_id).billing_credential_source == "user"
        api_config_service.delete_config(db, user_id)
    with isolated_mysql() as db:
        assert db.query(UserApiConfig).filter_by(user_id=user_id).one_or_none() is None
        assert api_resolver.resolve_api_config(db, user_id).billing_credential_source == "system"


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="old decrypt failure can deactivate a concurrently saved newer key")
def test_stale_decrypt_failure_must_not_disable_new_mysql_key(isolated_mysql, monkeypatch):
    user_id = _create_user(isolated_mysql)
    selected_old = Event()
    resume_old = Event()
    original_decrypt = api_resolver.decrypt_api_key_with_metadata

    def hold_old_decryption(value):
        if value == "invalid-ciphertext":
            selected_old.set()
            if not resume_old.wait(10):
                raise RuntimeError("new-key write did not finish in time")
        return original_decrypt(value)

    monkeypatch.setattr(api_resolver, "decrypt_api_key_with_metadata", hold_old_decryption)

    def old_resolution():
        with isolated_mysql() as db:
            return api_resolver.resolve_api_config(db, user_id).billing_credential_source

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
        assert old.result(timeout=10) == "fallback"
    with isolated_mysql() as db:
        row = db.query(UserApiConfig).filter_by(user_id=user_id).one()
        assert api_resolver.decrypt_api_key_with_metadata(row.api_key_enc).plaintext == "isolated-new-key"
        assert row.is_active is True
