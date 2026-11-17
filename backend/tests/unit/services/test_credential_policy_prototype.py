"""Detached policy is transactionally coupled to actual configuration tables."""

import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.api_config import UserApiConfig
from app.models.credential_policy_prototype import CredentialPolicyBase
from app.models.system_config import SystemConfig
from app.models.user import User
from app.services import credential_policy_prototype as policy


@pytest.fixture
def factory(db, monkeypatch):
    CredentialPolicyBase.metadata.create_all(db.get_bind())
    monkeypatch.setattr(policy, "_EXPERIMENT_ENABLED", True)
    return sessionmaker(bind=db.get_bind(), expire_on_commit=False, autoflush=False)


def _setup(factory, kind, owner_id, *, active=True):
    with factory.begin() as db:
        if kind == "user":
            db.add(User(id=owner_id, username=f"policy-{owner_id}", password="test-only", role="user", status=1))
            db.flush()
            db.add(UserApiConfig(user_id=owner_id, provider="deepseek", base_url="https://api.deepseek.com/v1",
                                 model="deepseek-v4-flash", is_active=active, api_key_enc="old"))
        else:
            db.add(SystemConfig(config_key="llm_provider", config_value=json.dumps({
                "active": active, "model": "old", "api_key_enc": "old", "provider": "deepseek",
                "base_url": "https://api.deepseek.com/v1", "other": "keep",
            })))
    with factory.begin() as db:
        return policy.initialize(db, kind=kind, owner_id=owner_id)


def test_disabled_by_default(db):
    with pytest.raises(policy.PolicyError, match="尚未启用"):
        policy.initialize(db, kind="user", owner_id=7)


@pytest.mark.parametrize("kind,owner_id", [("user", 7), ("global", 0)])
def test_stale_failure_and_rotation_cannot_override_real_new_configuration(factory, kind, owner_id):
    original = _setup(factory, kind, owner_id)
    with factory.begin() as db:
        fresh = policy.replace_key(db, original, ciphertext="new")
    with factory.begin() as db:
        with pytest.raises(policy.PolicyConflict):
            policy.mark_failed(db, original)
    with factory.begin() as db:
        with pytest.raises(policy.PolicyConflict):
            policy.rotate_key(db, original, ciphertext="old-rotated")
    with factory() as db:
        assert policy.read(db, kind=kind, owner_id=owner_id) == fresh
        if kind == "user":
            row = db.query(UserApiConfig).filter_by(user_id=owner_id).one()
            assert row.api_key_enc == "new" and row.is_active
        else:
            data = json.loads(db.query(SystemConfig).filter_by(config_key="llm_provider").one().config_value)
            assert data["api_key_enc"] == "new" and data["active"] and data["other"] == "keep"


def test_delete_recreate_and_same_value_aba_cannot_revalidate_old_snapshot(factory):
    initial = _setup(factory, "user", 8)
    with factory.begin() as db:
        system = policy.choose_system(db, initial)
    with factory() as db:
        assert db.query(UserApiConfig).filter_by(user_id=8).one_or_none() is None
    with factory.begin() as db:
        recreated = policy.replace_key(db, system, ciphertext="old", user_config={
            "provider": "deepseek", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-v4-flash",
        })
    assert recreated.revision > initial.revision
    assert recreated.incarnation != initial.incarnation
    with factory.begin() as db:
        with pytest.raises(policy.PolicyConflict):
            policy.mark_failed(db, initial)
    with factory() as db:
        assert db.query(UserApiConfig).filter_by(user_id=8).one().is_active


def test_historical_inactive_unknown_and_selection_do_not_authorize_charges(factory):
    unknown = _setup(factory, "user", 9, active=False)
    assert unknown.state == "unknown"
    with factory.begin() as db:
        system = policy.choose_system(db, unknown)
    assert system.state == "system"
    with pytest.raises(policy.PolicyError, match="不能授权扣费"):
        policy.require_paid_authorization(system)


def test_duplicate_initialization_refuses_existing_policy(factory):
    initial = _setup(factory, "user", 12)
    with factory.begin() as db:
        with pytest.raises(policy.PolicyConflict, match="已存在"):
            policy.initialize(db, kind="user", owner_id=12)
    with factory() as db:
        assert policy.read(db, kind="user", owner_id=12) == initial


def test_existing_key_only_replace_rejects_other_user_config_fields(factory):
    initial = _setup(factory, "user", 16)
    with factory.begin() as db:
        with pytest.raises(policy.PolicyError, match="仅支持更换密钥"):
            policy.replace_key(db, initial, ciphertext="new", user_config={
                "provider": "openai", "base_url": "https://example.invalid", "model": "new-model",
            })
    with factory() as db:
        row = db.query(UserApiConfig).filter_by(user_id=16).one()
        assert row.provider == "deepseek" and row.model == "deepseek-v4-flash" and row.api_key_enc == "old"
        assert policy.read(db, kind="user", owner_id=16) == initial


def test_caller_dirty_state_is_not_flushed_or_committed(factory):
    initial = _setup(factory, "user", 10)
    with factory() as db:
        user = db.query(User).filter_by(id=10).one()
        user.username = "uncommitted-change"
        with pytest.raises(policy.PolicyError, match="未提交"):
            policy.mark_failed(db, initial)
        assert user.username == "uncommitted-change" and user in db.dirty
        db.rollback()
    with factory() as db:
        assert db.query(User).filter_by(id=10).one().username == "policy-10"
        assert db.query(UserApiConfig).filter_by(user_id=10).one().is_active
        assert policy.read(db, kind="user", owner_id=10) == initial


def test_flushed_caller_change_and_actual_configuration_rollback_together(factory):
    initial = _setup(factory, "user", 11)
    with factory() as db:
        user = db.query(User).filter_by(id=11).one()
        user.username = "flushed-but-uncommitted"
        db.flush()
        with pytest.raises(policy.PolicyError, match="已有物理事务"):
            policy.mark_failed(db, initial)
        assert user.username == "flushed-but-uncommitted"
        db.rollback()
    with factory() as db:
        assert db.query(User).filter_by(id=11).one().username == "policy-11"
        assert db.query(UserApiConfig).filter_by(user_id=11).one().is_active
        assert policy.read(db, kind="user", owner_id=11) == initial


def test_read_only_outer_transaction_rollback_restores_policy_and_real_configuration(factory):
    initial = _setup(factory, "user", 13)
    with factory() as db:
        assert db.query(UserApiConfig).filter_by(user_id=13).one().is_active
        policy.mark_failed(db, initial)
        db.rollback()
    with factory() as db:
        assert db.query(UserApiConfig).filter_by(user_id=13).one().is_active
        assert policy.read(db, kind="user", owner_id=13) == initial


def test_initialization_rollback_does_not_persist_savepoint(factory):
    with factory.begin() as db:
        db.add(User(id=14, username="policy-14", password="test-only", role="user", status=1))
    with factory() as db:
        assert db.query(User).filter_by(id=14).one().username == "policy-14"
        policy.initialize(db, kind="user", owner_id=14)
        db.rollback()
    with factory() as db:
        with pytest.raises(policy.PolicyError, match="尚未初始化"):
            policy.read(db, kind="user", owner_id=14)


def test_direct_sql_caller_write_is_not_committed(factory):
    initial = _setup(factory, "user", 15)
    with factory() as db:
        db.execute(text("UPDATE user SET username = 'sql-uncommitted' WHERE id = 15"))
        with pytest.raises(policy.PolicyError, match="已有物理事务"):
            policy.mark_failed(db, initial)
        assert db.execute(text("SELECT username FROM user WHERE id = 15")).scalar_one() == "sql-uncommitted"
        db.rollback()
    with factory() as db:
        assert db.query(User).filter_by(id=15).one().username == "policy-15"
        assert db.query(UserApiConfig).filter_by(user_id=15).one().is_active
        assert policy.read(db, kind="user", owner_id=15) == initial


def test_raw_sql_savepoint_without_outer_begin_is_rejected(factory):
    initial = _setup(factory, "user", 19)
    with factory() as db:
        db.execute(text("SAVEPOINT caller_savepoint"))
        with pytest.raises(policy.PolicyError, match="已有物理事务"):
            policy.mark_failed(db, initial)
        db.execute(text("RELEASE SAVEPOINT caller_savepoint"))
        db.rollback()
    with factory() as db:
        assert db.query(UserApiConfig).filter_by(user_id=19).one().is_active
        assert policy.read(db, kind="user", owner_id=19) == initial


def test_raw_commit_then_savepoint_cannot_reuse_old_transaction_marker(factory):
    initial = _setup(factory, "user", 20)
    with factory() as db:
        assert policy.read(db, kind="user", owner_id=20) == initial
        db.execute(text("COMMIT"))
        db.execute(text("SAVEPOINT caller_savepoint"))
        with pytest.raises(policy.PolicyError, match="已有物理事务"):
            policy.mark_failed(db, initial)
        db.execute(text("RELEASE SAVEPOINT caller_savepoint"))
        db.rollback()
    with factory() as db:
        assert db.query(UserApiConfig).filter_by(user_id=20).one().is_active


def test_preexisting_nested_transaction_is_rejected_without_committing(factory):
    initial = _setup(factory, "user", 18)
    with factory() as db:
        outer_savepoint = db.begin_nested()
        with pytest.raises(policy.PolicyError, match="调用者嵌套事务"):
            policy.mark_failed(db, initial)
        outer_savepoint.commit()
        db.rollback()
    with factory() as db:
        assert db.query(UserApiConfig).filter_by(user_id=18).one().is_active
        assert policy.read(db, kind="user", owner_id=18) == initial


def test_two_sqlite_connections_serialize_policy_and_actual_config(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'credential-policy.db'}", connect_args={"timeout": 5})
    Base.metadata.create_all(engine, tables=[User.__table__, UserApiConfig.__table__])
    CredentialPolicyBase.metadata.create_all(engine)
    sqlite_factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(policy, "_EXPERIMENT_ENABLED", True)
    initial = _setup(sqlite_factory, "user", 17)
    locked, release, waiting = Event(), Event(), Event()

    def fail_first():
        with sqlite_factory.begin() as db:
            changed = policy.mark_failed(db, initial)
            locked.set()
            if not release.wait(5):
                raise RuntimeError("second writer did not reach the transaction")
            return changed

    def stale_replace():
        with sqlite_factory.begin() as db:
            waiting.set()
            with pytest.raises(policy.PolicyConflict):
                policy.replace_key(db, initial, ciphertext="stale")

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(fail_first)
            try:
                assert locked.wait(5)
                second = executor.submit(stale_replace)
                assert waiting.wait(5)
                with pytest.raises(TimeoutError):
                    second.result(timeout=0.2)
            finally:
                release.set()
            changed = first.result(timeout=5)
            second.result(timeout=5)
        with sqlite_factory() as db:
            assert policy.read(db, kind="user", owner_id=17) == changed
            row = db.query(UserApiConfig).filter_by(user_id=17).one()
            assert row.api_key_enc == "old" and not row.is_active
    finally:
        engine.dispose()
