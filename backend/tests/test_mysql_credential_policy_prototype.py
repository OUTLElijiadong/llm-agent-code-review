"""Prototype against real config rows and a guarded disposable MySQL database."""

import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Barrier, Event

import pytest
from sqlalchemy.exc import DBAPIError
from test_mysql_byok_fallback_races import _create_user
from test_mysql_byok_fallback_races import isolated_mysql as _guarded_mysql

from app.models.api_config import UserApiConfig
from app.models.credential_policy_prototype import CredentialPolicyBase, CredentialPolicyPrototype
from app.models.system_config import SystemConfig
from app.models.user import User
from app.schemas.api_config import ApiConfigSaveIn
from app.services import api_config_service, system_config_service
from app.services import credential_policy_prototype as policy


@pytest.fixture(name="mysql_factory")
def _mysql_factory(monkeypatch):
    yield from _guarded_mysql.__wrapped__(monkeypatch)


def _setup(factory, monkeypatch, kind):
    CredentialPolicyBase.metadata.create_all(factory.kw["bind"])
    monkeypatch.setattr(policy, "_EXPERIMENT_ENABLED", True)
    if kind == "user":
        owner_id = _create_user(factory, ciphertext="original")
    else:
        owner_id = 0
        SystemConfig.__table__.create(bind=factory.kw["bind"], checkfirst=True)
        # This row is owned by earlier parameter cases in the marker-verified,
        # disposable database; never delete arbitrary or production records.
        with factory.begin() as db:
            db.query(CredentialPolicyPrototype).filter_by(kind="global", owner_id=0).delete()
        with factory() as db:
            system_config_service._set_raw(db, system_config_service.LLM_KEY, json.dumps({
                "active": True, "api_key_enc": "original", "model": "original-model", "other": "keep",
            }))
    with factory.begin() as db:
        snapshot = policy.initialize(db, kind=kind, owner_id=owner_id)
    return owner_id, snapshot


@pytest.mark.parametrize("kind", ["user", "global"])
@pytest.mark.parametrize("stale_action", ["failure", "rotation"])
def test_new_config_commit_fences_old_failure_or_rotation(mysql_factory, monkeypatch, kind, stale_action):
    owner_id, initial = _setup(mysql_factory, monkeypatch, kind)
    selected, resume = Event(), Event()

    def stale_worker():
        with mysql_factory() as db:
            observed = policy.read(db, kind=kind, owner_id=owner_id)
            db.commit()  # Keep the observed version, release its database locks.
            selected.set()
            if not resume.wait(10):
                raise RuntimeError("new configuration did not commit")
            with pytest.raises(policy.PolicyConflict):
                if stale_action == "failure":
                    policy.mark_failed(db, observed)
                else:
                    policy.rotate_key(db, observed, ciphertext="old-rotated")

    with ThreadPoolExecutor(max_workers=1) as executor:
        stale = executor.submit(stale_worker)
        try:
            assert selected.wait(10)
            with mysql_factory.begin() as db:
                fresh = policy.replace_key(db, initial, ciphertext="new")
        finally:
            resume.set()
        stale.result(timeout=10)
    with mysql_factory() as db:
        assert policy.read(db, kind=kind, owner_id=owner_id) == fresh
        if kind == "user":
            row = db.query(UserApiConfig).filter_by(user_id=owner_id).one()
            assert row.is_active and row.api_key_enc == "new"
        else:
            data = json.loads(db.query(SystemConfig).filter_by(config_key="llm_provider").one().config_value)
            assert data["active"] and data["api_key_enc"] == "new" and data["other"] == "keep"


@pytest.mark.parametrize("kind", ["user", "global"])
def test_repeatable_read_old_snapshot_and_identity_map_cannot_override_new_config(mysql_factory, monkeypatch, kind):
    owner_id, initial = _setup(mysql_factory, monkeypatch, kind)
    with mysql_factory() as old_db:
        # Plain consistent reads create a repeatable-read snapshot and keep both
        # ORM objects in the identity map; they intentionally take no row locks.
        stale_policy = old_db.query(CredentialPolicyPrototype).filter_by(kind=kind, owner_id=owner_id).one()
        if kind == "user":
            stale_config = old_db.query(UserApiConfig).filter_by(user_id=owner_id).one()
            assert stale_config.api_key_enc == "original"
        else:
            stale_config = old_db.query(SystemConfig).filter_by(config_key="llm_provider").one()
            assert json.loads(stale_config.config_value)["api_key_enc"] == "original"
        assert stale_policy.revision == initial.revision
        with mysql_factory.begin() as new_db:
            fresh = policy.replace_key(new_db, initial, ciphertext="new")
        with pytest.raises(policy.PolicyConflict):
            policy.mark_failed(old_db, initial)
        assert stale_policy.revision == fresh.revision
        if kind == "user":
            assert stale_config.api_key_enc == "new" and stale_config.is_active
        else:
            assert json.loads(stale_config.config_value)["api_key_enc"] == "new"
        old_db.rollback()
    with mysql_factory() as db:
        assert policy.read(db, kind=kind, owner_id=owner_id) == fresh


def test_old_failure_commit_wins_before_waiting_new_save(mysql_factory, monkeypatch):
    owner_id, initial = _setup(mysql_factory, monkeypatch, "user")
    locked, release, waiting = Event(), Event(), Event()

    def old_failure():
        with mysql_factory.begin() as db:
            failed = policy.mark_failed(db, initial)
            locked.set()
            if not release.wait(10):
                raise RuntimeError("waiting writer did not complete")
            return failed

    def save_with_stale_snapshot():
        with mysql_factory.begin() as db:
            waiting.set()
            with pytest.raises(policy.PolicyConflict):
                policy.replace_key(db, initial, ciphertext="new")

    with ThreadPoolExecutor(max_workers=2) as executor:
        old = executor.submit(old_failure)
        try:
            assert locked.wait(10)
            new = executor.submit(save_with_stale_snapshot)
            assert waiting.wait(10)
            with pytest.raises(TimeoutError):
                new.result(timeout=0.2)
        finally:
            release.set()
        failed = old.result(timeout=10)
        new.result(timeout=10)
    with mysql_factory.begin() as db:
        current = policy.read(db, kind="user", owner_id=owner_id)
        assert current == failed and current.state == "failed"
        row = db.query(UserApiConfig).filter_by(user_id=owner_id).one()
        assert not row.is_active and row.api_key_enc == "original"
    # Retry only after a fresh transaction and an explicit new configuration.
    with mysql_factory.begin() as db:
        saved = policy.replace_key(db, failed, ciphertext="new")
    assert saved.state == "active"


def test_real_save_config_fences_stale_failure_and_rejects_caller_dirty(mysql_factory, monkeypatch):
    owner_id, initial = _setup(mysql_factory, monkeypatch, "user")
    monkeypatch.setattr(api_config_service, "_CREDENTIAL_POLICY_SAVE_EXPERIMENT", True)
    monkeypatch.setattr(api_config_service, "normalize_ai_base_url", lambda value, **_: value)
    monkeypatch.setattr(api_config_service, "encrypt_api_key", lambda value: f"enc:{value}")
    with mysql_factory() as caller:
        user = caller.query(User).filter_by(id=owner_id).one()
        original_username = user.username
        user.username = "uncommitted-caller-name"
        with pytest.raises(policy.PolicyError, match="调用者仍有活动事务"):
            api_config_service.save_config(caller, owner_id, ApiConfigSaveIn(
                provider="openai", base_url="https://api.example.com", model="saved-model", api_key="saved-key",
            ))
        assert user in caller.dirty and user.username == "uncommitted-caller-name"
        caller.rollback()
    with mysql_factory() as caller:
        result = api_config_service.save_config(caller, owner_id, ApiConfigSaveIn(
            provider="openai", base_url="https://api.example.com", model="saved-model", api_key="saved-key",
        ))
        assert result.model == "saved-model" and "saved-key" not in result.api_key_masked
    with mysql_factory.begin() as db:
        with pytest.raises(policy.PolicyConflict):
            policy.mark_failed(db, initial)
    with mysql_factory() as db:
        row = db.query(UserApiConfig).filter_by(user_id=owner_id).one()
        assert (row.provider, row.base_url, row.model, row.api_key_enc, row.is_active) == (
            "openai", "https://api.example.com", "saved-model", "enc:saved-key", True,
        )
        assert db.query(User).filter_by(id=owner_id).one().username == original_username
        assert policy.read(db, kind="user", owner_id=owner_id).revision == initial.revision + 1


def test_real_save_rejects_preflushed_config_without_waiting_on_own_lock(mysql_factory, monkeypatch):
    owner_id, initial = _setup(mysql_factory, monkeypatch, "user")
    monkeypatch.setattr(api_config_service, "_CREDENTIAL_POLICY_SAVE_EXPERIMENT", True)
    monkeypatch.setattr(api_config_service, "normalize_ai_base_url", lambda value, **_: value)
    monkeypatch.setattr(api_config_service, "encrypt_api_key", lambda value: f"enc:{value}")
    with mysql_factory() as caller:
        row = caller.query(UserApiConfig).filter_by(user_id=owner_id).one()
        row.model = "caller-pending"
        caller.flush()
        with pytest.raises(policy.PolicyError, match="调用者仍有活动事务"):
            api_config_service.save_config(caller, owner_id, ApiConfigSaveIn(
                provider="openai", base_url="https://api.example.com", model="saved-model", api_key="saved-key",
            ))
        assert row.model == "caller-pending"
        caller.rollback()
    with mysql_factory() as db:
        assert db.query(UserApiConfig).filter_by(user_id=owner_id).one().api_key_enc == "original"
        assert policy.read(db, kind="user", owner_id=owner_id) == initial


def test_real_save_waits_for_prior_failure_then_preserves_user_choice(mysql_factory, monkeypatch):
    owner_id, initial = _setup(mysql_factory, monkeypatch, "user")
    monkeypatch.setattr(api_config_service, "_CREDENTIAL_POLICY_SAVE_EXPERIMENT", True)
    monkeypatch.setattr(api_config_service, "normalize_ai_base_url", lambda value, **_: value)
    monkeypatch.setattr(api_config_service, "encrypt_api_key", lambda value: f"enc:{value}")
    locked, release, waiting = Event(), Event(), Event()

    def old_failure():
        with mysql_factory.begin() as db:
            policy.mark_failed(db, initial)
            locked.set()
            if not release.wait(10):
                raise RuntimeError("new save did not start")

    def real_save():
        with mysql_factory() as db:
            waiting.set()
            return api_config_service.save_config(db, owner_id, ApiConfigSaveIn(
                provider="custom", base_url="https://next.example.com", model="next-model", api_key="next-key",
            ))

    with ThreadPoolExecutor(max_workers=2) as executor:
        old = executor.submit(old_failure)
        try:
            assert locked.wait(10)
            new = executor.submit(real_save)
            assert waiting.wait(10)
            with pytest.raises(TimeoutError):
                new.result(timeout=0.2)
        finally:
            release.set()
        old.result(timeout=10)
        assert new.result(timeout=10).model == "next-model"
    with mysql_factory() as db:
        row = db.query(UserApiConfig).filter_by(user_id=owner_id).one()
        assert row.is_active and row.api_key_enc == "enc:next-key" and row.provider == "custom"
        assert policy.read(db, kind="user", owner_id=owner_id).revision == initial.revision + 2


def test_real_save_failure_rolls_back_config_and_version(mysql_factory, monkeypatch):
    owner_id, initial = _setup(mysql_factory, monkeypatch, "user")
    monkeypatch.setattr(api_config_service, "_CREDENTIAL_POLICY_SAVE_EXPERIMENT", True)
    monkeypatch.setattr(api_config_service, "normalize_ai_base_url", lambda value, **_: value)
    monkeypatch.setattr(api_config_service, "encrypt_api_key", lambda value: f"enc:{value}")
    original_save = policy.save_current_user_configuration

    def fail_after_flush(*args, **kwargs):
        original_save(*args, **kwargs)
        raise RuntimeError("failure after both writes")

    monkeypatch.setattr(policy, "save_current_user_configuration", fail_after_flush)
    with mysql_factory() as db, pytest.raises(RuntimeError, match="failure after both writes"):
        api_config_service.save_config(db, owner_id, ApiConfigSaveIn(
            provider="custom", base_url="https://next.example.com", model="next-model", api_key="next-key",
        ))
    with mysql_factory() as db:
        row = db.query(UserApiConfig).filter_by(user_id=owner_id).one()
        assert row.api_key_enc == "original" and row.is_active
        assert policy.read(db, kind="user", owner_id=owner_id) == initial


def test_real_config_and_policy_share_rollback_with_caller_write(mysql_factory, monkeypatch):
    owner_id, initial = _setup(mysql_factory, monkeypatch, "user")
    with mysql_factory() as db:
        user = db.query(User).filter_by(id=owner_id).one()
        before = user.username
        user.username = "uncommitted-name"
        db.flush()
        policy.mark_failed(db, initial)
        db.rollback()
    with mysql_factory() as db:
        assert db.query(User).filter_by(id=owner_id).one().username == before
        assert db.query(UserApiConfig).filter_by(user_id=owner_id).one().is_active
        assert policy.read(db, kind="user", owner_id=owner_id) == initial


def test_delete_recreate_same_payload_invalidates_old_snapshot(mysql_factory, monkeypatch):
    owner_id, initial = _setup(mysql_factory, monkeypatch, "user")
    with mysql_factory.begin() as db:
        deleted = policy.choose_system(db, initial)
    with mysql_factory.begin() as db:
        recreated = policy.replace_key(db, deleted, ciphertext="original", user_config={
            "provider": "deepseek", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-v4-flash",
        })
    assert recreated.incarnation != initial.incarnation and recreated.revision > initial.revision
    with mysql_factory.begin() as db:
        with pytest.raises(policy.PolicyConflict):
            policy.mark_failed(db, initial)
    with pytest.raises(policy.PolicyError, match="不能授权扣费"):
        policy.require_paid_authorization(recreated)


def test_uncooperative_direct_update_is_detected_but_not_repaired(mysql_factory, monkeypatch):
    owner_id, _ = _setup(mysql_factory, monkeypatch, "user")
    with mysql_factory.begin() as db:
        db.query(UserApiConfig).filter_by(user_id=owner_id).one().api_key_enc = "untracked-change"
    with mysql_factory() as db:
        with pytest.raises(policy.PolicyConflict, match="实际配置与策略版本不一致"):
            policy.read(db, kind="user", owner_id=owner_id)
        assert db.query(UserApiConfig).filter_by(user_id=owner_id).one().api_key_enc == "untracked-change"


def test_concurrent_initializers_create_only_one_policy(mysql_factory, monkeypatch):
    CredentialPolicyBase.metadata.create_all(mysql_factory.kw["bind"])
    monkeypatch.setattr(policy, "_EXPERIMENT_ENABLED", True)
    owner_id = _create_user(mysql_factory, ciphertext="original")
    rendezvous = Barrier(2)

    def initialize():
        try:
            with mysql_factory.begin() as db:
                rendezvous.wait(timeout=10)
                policy.initialize(db, kind="user", owner_id=owner_id)
            return "created"
        except policy.PolicyConflict:
            return "rejected"
        except DBAPIError as exc:
            # Only an observed MySQL lock timeout/deadlock is a retryable
            # transaction failure; unrelated database errors must fail the test.
            if not exc.orig.args or exc.orig.args[0] not in {1205, 1213}:
                raise
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [item.result(timeout=15) for item in (executor.submit(initialize), executor.submit(initialize))]
    assert sorted(outcomes) == ["created", "rejected"]
    with mysql_factory() as db:
        assert policy.read(db, kind="user", owner_id=owner_id).state == "active"
        row = db.query(UserApiConfig).filter_by(user_id=owner_id).one()
        assert row.is_active and row.api_key_enc == "original"


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="legacy writers bypass sidecar revision; ABA cannot be detected")
def test_uncooperative_legacy_writer_aba_is_not_fenced(mysql_factory, monkeypatch):
    owner_id, initial = _setup(mysql_factory, monkeypatch, "user")
    for value in ("different", "original"):
        with mysql_factory.begin() as db:
            db.query(UserApiConfig).filter_by(user_id=owner_id).one().api_key_enc = value
    with mysql_factory() as db:
        assert policy.read(db, kind="user", owner_id=owner_id).revision != initial.revision
