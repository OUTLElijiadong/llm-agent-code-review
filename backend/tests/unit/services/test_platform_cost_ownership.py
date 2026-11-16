"""Detached platform ownership contracts; no production sender or migration."""

import hashlib
from datetime import datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.agent_response_run import AgentResponseRun
from app.models.platform_cost_center import (
    PlatformAttemptOwner,
    PlatformCostAttempt,
    PlatformCostBase,
    PlatformCostBudget,
    PlatformCostCenter,
    PlatformCostSettlement,
    PlatformRunOwner,
)
from app.services import platform_cost_center as platform
from app.services import platform_cost_ownership as owner
from app.services.billing_ledger_service import BillingError


@pytest.fixture(autouse=True)
def detached_tables(db):
    db.connection().exec_driver_sql('PRAGMA foreign_keys=ON')
    assert db.connection().exec_driver_sql('PRAGMA foreign_keys').scalar_one() == 1
    db.commit()
    PlatformCostBase.metadata.create_all(db.get_bind())
    now = datetime.utcnow()
    db.add(PlatformCostCenter(id=1, status='active'))
    db.flush()
    db.add(PlatformCostBudget(period_start=now - timedelta(days=1), period_end=now + timedelta(days=1),
                              limit_cny_micros=1_000_000, reserved_cny_micros=0, spent_cny_micros=0,
                              status='active'))
    db.commit()


@pytest.fixture
def synthetic_settlement(monkeypatch):
    monkeypatch.setattr(owner, '_SYNTHETIC_SETTLEMENT_ENABLED', True)


def create(db, actor, run_id='platform-owned-run'):
    return owner.create_owned_run(db, actor_id=actor, run_id=run_id, session_key='admin-session')


def reserve(db, actor, run, run_secret, key=None):
    return owner.reserve_owned_attempt(
        db, actor_id=actor, run_pk=run, session_key='admin-session', run_secret=run_secret,
        attempt_key=key or platform.new_attempt_key(), model_name='deepseek-flash',
        provider_name='deepseek', base_url='https://api.deepseek.com/v1', credential_source='system',
        payload={'model': 'deepseek-flash', 'input': 'hello', 'max_output_tokens': 100},
    )


def test_owner_default_closed(db, admin_user):
    with pytest.raises(BillingError, match='启用'):
        create(db, admin_user.id)
    assert db.query(AgentResponseRun).count() == 0


def test_disabled_sqlite_foreign_keys_reject_owner_operations(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.database import Base

    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    engine = create_engine(f'sqlite:///{tmp_path / "no-foreign-keys.sqlite"}')
    Base.metadata.create_all(engine)
    PlatformCostBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    try:
        with factory() as session:
            assert session.connection().exec_driver_sql('PRAGMA foreign_keys').scalar_one() == 0
            with pytest.raises(BillingError, match='外键'):
                create(session, 501)
            session.rollback()
            assert session.query(PlatformRunOwner).count() == 0
    finally:
        engine.dispose()


def test_creation_and_reservation_are_outer_transaction_owned(db, admin_user, monkeypatch):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    run_pk, secret = create(db, admin_user.id)
    assert repr(secret).find(secret._value.hex()) == -1
    db.rollback()
    assert db.query(AgentResponseRun).count() == db.query(PlatformRunOwner).count() == 0
    run_pk, secret = create(db, admin_user.id)
    db.commit()
    reserve(db, admin_user.id, run_pk, secret)
    db.rollback()
    assert db.query(PlatformCostAttempt).count() == db.query(PlatformAttemptOwner).count() == 0
    assert db.query(PlatformCostBudget).one().reserved_cny_micros == 0


def test_both_owner_proofs_and_live_generation_required(db, admin_user, monkeypatch):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    run_pk, run_secret = create(db, admin_user.id)
    db.commit()
    attempt, attempt_secret = reserve(db, admin_user.id, run_pk, run_secret)
    attempt_id = attempt.id
    db.commit()
    other_run, other_run_secret = create(db, admin_user.id, run_id='different-run')
    db.commit()
    _, other_attempt_secret = reserve(db, admin_user.id, other_run, other_run_secret)
    db.commit()
    for wrong_run, wrong_attempt in ((other_run_secret, attempt_secret), (run_secret, other_attempt_secret)):
        with pytest.raises(BillingError):
            owner.mark_owned_attempt_unknown(db, actor_id=admin_user.id, run_pk=run_pk, session_key='admin-session',
                                             run_secret=wrong_run, attempt_id=attempt_id,
                                             attempt_secret=wrong_attempt)
        db.rollback()
    row = db.query(PlatformRunOwner).filter_by(run_pk=run_pk).one()
    row.generation += 1
    db.commit()
    with pytest.raises(BillingError):
        reserve(db, admin_user.id, run_pk, run_secret)
    db.rollback()
    with pytest.raises(BillingError):
        owner.mark_owned_attempt_unknown(db, actor_id=admin_user.id, run_pk=run_pk, session_key='admin-session',
                                         run_secret=run_secret, attempt_id=attempt_id,
                                         attempt_secret=attempt_secret)
    db.rollback()
    assert db.get(PlatformCostAttempt, attempt_id).status == 'reserved'


def test_revoked_permission_and_dirty_callers_fail_without_write(db, admin_user, monkeypatch):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    run_pk, secret = create(db, admin_user.id)
    db.commit()
    admin_user.status = 0
    with pytest.raises(BillingError, match='未提交'):
        reserve(db, admin_user.id, run_pk, secret)
    assert admin_user.status == 0 and admin_user in db.dirty
    db.commit()
    with pytest.raises(BillingError, match='管理员'):
        reserve(db, admin_user.id, run_pk, secret)
    db.rollback()
    assert db.query(PlatformCostAttempt).count() == 0
    assert db.query(PlatformCostBudget).one().reserved_cny_micros == 0


def test_wrong_session_cannot_reserve_or_cancel(db, admin_user, monkeypatch):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    run_pk, run_secret = create(db, admin_user.id)
    db.commit()
    with pytest.raises(BillingError, match='归属'):
        owner.reserve_owned_attempt(db, actor_id=admin_user.id, run_pk=run_pk, session_key='other-session',
                                    run_secret=run_secret, attempt_key=platform.new_attempt_key(),
                                    model_name='deepseek-flash', provider_name='deepseek',
                                    base_url='https://api.deepseek.com/v1', credential_source='system',
                                    payload={'model': 'deepseek-flash', 'input': 'hello', 'max_output_tokens': 100})
    db.rollback()
    with pytest.raises(BillingError, match='归属'):
        owner.cancel_owned_run(db, actor_id=admin_user.id, run_pk=run_pk, session_key='other-session')
    db.rollback()
    assert db.get(AgentResponseRun, run_pk).status == 'running'


@pytest.mark.parametrize('checkpoint', ['{invalid', '[]', 'null', '{"stop_requested": "false"}',
                                       '{"stop_requested": 1}', '{"status": "cancelled"}',
                                       '{"stop_requested": true, "stop_requested": false}',
                                       '{"status": "running", "nested": [NaN]}',
                                       '{"status": "running", "extra": Infinity}',
                                       '{"status": "running", "extra": -Infinity}', '{}',
                                       '{"stop_requested": false}', '{"status": "running", "x": 1e400}',
                                       '{"status": "running", "x": [-1e400]}'])
def test_unreadable_or_ambiguous_stop_intent_fails_closed(db, admin_user, monkeypatch, checkpoint):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    run_pk, run_secret = create(db, admin_user.id)
    db.commit()
    db.get(AgentResponseRun, run_pk).checkpoint_json = checkpoint
    db.commit()
    with pytest.raises(BillingError):
        reserve(db, admin_user.id, run_pk, run_secret)
    db.rollback()
    assert db.query(PlatformCostAttempt).count() == 0
    assert db.query(PlatformCostBudget).one().reserved_cny_micros == 0


def test_other_admin_cannot_use_run_or_cancel_it(db, admin_user, monkeypatch):
    from app.models.user import User

    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    other = User(username='other-owner-admin', password='x', role='admin', status=1)
    db.add(other)
    db.commit()
    run_pk, run_secret = create(db, admin_user.id)
    db.commit()
    with pytest.raises(BillingError, match='归属'):
        reserve(db, other.id, run_pk, run_secret)
    db.rollback()
    with pytest.raises(BillingError, match='归属'):
        owner.cancel_owned_run(db, actor_id=other.id, run_pk=run_pk, session_key='admin-session')
    db.rollback()
    assert db.get(AgentResponseRun, run_pk).status == 'running'
    assert db.query(PlatformCostAttempt).count() == 0


def test_ambiguous_commit_does_not_allow_same_attempt_to_reserve_again(db, admin_user, monkeypatch):
    """Simulate acknowledgement loss after commit; this does not simulate HTTP."""
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    run_pk, run_secret = create(db, admin_user.id)
    db.commit()
    key = platform.new_attempt_key()
    attempt, _ = reserve(db, admin_user.id, run_pk, run_secret, key=key)
    held = attempt.reserved_cny_micros
    commit = db.commit

    def commit_then_lose_ack():
        commit()
        raise ConnectionError('simulated lost commit acknowledgement')

    monkeypatch.setattr(db, 'commit', commit_then_lose_ack)
    with pytest.raises(ConnectionError):
        db.commit()
    monkeypatch.setattr(db, 'commit', commit)
    db.expire_all()
    with pytest.raises(BillingError):
        reserve(db, admin_user.id, run_pk, run_secret, key=key)
    db.rollback()
    assert db.query(PlatformCostAttempt).count() == db.query(PlatformAttemptOwner).count() == 1
    assert db.query(PlatformCostBudget).one().reserved_cny_micros == held


def test_cancel_fences_owner_and_preserves_unknown_hold(db, admin_user, monkeypatch):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    run_pk, run_secret = create(db, admin_user.id)
    db.commit()
    attempt, attempt_secret = reserve(db, admin_user.id, run_pk, run_secret)
    attempt_id, held = attempt.id, attempt.reserved_cny_micros
    db.commit()
    owner.cancel_owned_run(db, actor_id=admin_user.id, run_pk=run_pk, session_key='admin-session')
    db.commit()
    assert db.get(AgentResponseRun, run_pk).version == 2
    with pytest.raises(BillingError):
        owner.mark_owned_attempt_unknown(db, actor_id=admin_user.id, run_pk=run_pk, session_key='admin-session',
                                         run_secret=run_secret, attempt_id=attempt_id,
                                         attempt_secret=attempt_secret)
    db.rollback()
    assert db.get(PlatformCostAttempt, attempt_id).status == 'unknown'
    budget = db.query(PlatformCostBudget).one()
    assert (budget.reserved_cny_micros, budget.spent_cny_micros) == (held, 0)


def test_existing_run_is_never_adopted(db, admin_user, monkeypatch):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    db.add(AgentResponseRun(run_id='existing', user_id=admin_user.id, surface='admin',
                            session_key='admin-session', status='running', checkpoint_json='{}', version=1))
    db.commit()
    with pytest.raises(BillingError, match='已存在'):
        create(db, admin_user.id, run_id='existing')
    assert db.query(PlatformRunOwner).count() == 0
    assert db.query(AgentResponseRun).count() == 1


def test_recreated_run_with_same_primary_key_cannot_reuse_old_owner(db, admin_user, monkeypatch):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    run_pk, run_secret = create(db, admin_user.id)
    db.commit()
    with pytest.raises(IntegrityError):
        db.connection().exec_driver_sql('DELETE FROM agent_response_run WHERE id = ?', (run_pk,))
    db.rollback()
    assert db.get(AgentResponseRun, run_pk).run_id == 'platform-owned-run'
    attempt, _ = reserve(db, admin_user.id, run_pk, run_secret)
    assert attempt.status == 'reserved'
    db.rollback()


def test_parent_delete_and_replace_rejected_even_when_sqlite_foreign_keys_are_off(db, admin_user, monkeypatch):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    run_pk, _ = create(db, admin_user.id)
    db.commit()
    connection = db.connection()
    connection.exec_driver_sql('PRAGMA foreign_keys=OFF')
    assert connection.exec_driver_sql('PRAGMA foreign_keys').scalar_one() == 0
    with pytest.raises(IntegrityError):
        connection.exec_driver_sql('DELETE FROM agent_response_run WHERE id=?', (run_pk,))
    with pytest.raises(IntegrityError):
        connection.exec_driver_sql('INSERT OR REPLACE INTO agent_response_run SELECT * '
                                   'FROM agent_response_run WHERE id=?', (run_pk,))
    db.rollback()
    db.connection().exec_driver_sql('PRAGMA foreign_keys=ON')
    db.commit()
    assert db.get(AgentResponseRun, run_pk).run_id == 'platform-owned-run'


@pytest.mark.parametrize('sql', [
    'DELETE FROM platform_cost_run_owner',
    'INSERT OR REPLACE INTO platform_cost_run_owner (id,run_pk,actor_id,generation,owner_digest,status) '
    'SELECT id,run_pk,actor_id,generation,owner_digest,status FROM platform_cost_run_owner',
    'UPDATE platform_cost_run_owner SET actor_id = actor_id + 1',
    'UPDATE platform_cost_run_owner SET run_pk = run_pk + 1',
    "UPDATE platform_cost_run_owner SET owner_digest = zeroblob(32)",
    "UPDATE agent_response_run SET run_id = 'another-run'",
    "UPDATE agent_response_run SET session_key = 'another-session'",
    "UPDATE agent_response_run SET surface = 'user'",
    'UPDATE agent_response_run SET user_id = user_id + 1',
])
def test_database_direct_changes_cannot_break_owned_run_identity(db, admin_user, monkeypatch, sql):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    run_pk, _ = create(db, admin_user.id)
    db.commit()
    with pytest.raises(IntegrityError):
        db.connection().exec_driver_sql(sql)
    db.rollback()
    assert db.get(AgentResponseRun, run_pk).user_id == admin_user.id
    assert db.query(PlatformRunOwner).filter_by(run_pk=run_pk).count() == 1


def test_database_prevents_owner_resurrection_after_cancellation(db, admin_user, monkeypatch):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    run_pk, _ = create(db, admin_user.id)
    db.commit()
    owner.cancel_owned_run(db, actor_id=admin_user.id, run_pk=run_pk, session_key='admin-session')
    db.commit()
    with pytest.raises(IntegrityError):
        db.connection().exec_driver_sql("UPDATE platform_cost_run_owner SET status='active', generation=1")
    db.rollback()
    row = db.query(PlatformRunOwner).filter_by(run_pk=run_pk).one()
    assert row.status == 'cancelled' and row.generation == 2


def test_database_prevents_deleting_attempt_owner(db, admin_user, monkeypatch):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    run_pk, secret = create(db, admin_user.id)
    db.commit()
    attempt, _ = reserve(db, admin_user.id, run_pk, secret)
    db.commit()
    with pytest.raises(IntegrityError):
        db.connection().exec_driver_sql('DELETE FROM platform_cost_attempt_owner')
    db.rollback()
    with pytest.raises(IntegrityError):
        db.connection().exec_driver_sql(
            'INSERT OR REPLACE INTO platform_cost_attempt_owner (id,attempt_id,run_owner_id,generation,owner_digest) '
            'SELECT id,attempt_id,run_owner_id,generation,owner_digest FROM platform_cost_attempt_owner'
        )
    db.rollback()
    assert db.get(PlatformCostAttempt, attempt.id).status == 'reserved'


@pytest.mark.parametrize('sql', [
    'UPDATE platform_cost_attempt SET actor_id=actor_id+1',
    'UPDATE platform_cost_attempt SET budget_id=budget_id+1',
    'UPDATE platform_cost_attempt SET reserved_cny_micros=reserved_cny_micros+1',
    'UPDATE platform_cost_attempt SET payload_digest=zeroblob(32)',
    "UPDATE platform_cost_attempt SET pricing_version='different'",
    "UPDATE platform_cost_attempt SET status='reserved'",
    'DELETE FROM platform_cost_attempt',
    'INSERT OR REPLACE INTO platform_cost_attempt SELECT * FROM platform_cost_attempt',
])
def test_bound_attempt_snapshot_and_unknown_are_immutable(db, admin_user, monkeypatch, sql):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    run_pk, run_secret = create(db, admin_user.id)
    db.commit()
    attempt, attempt_secret = reserve(db, admin_user.id, run_pk, run_secret)
    attempt_id = attempt.id
    db.commit()
    owner.mark_owned_attempt_unknown(db, actor_id=admin_user.id, run_pk=run_pk, session_key='admin-session',
                                     run_secret=run_secret, attempt_id=attempt_id, attempt_secret=attempt_secret)
    db.commit()
    with pytest.raises(IntegrityError):
        db.connection().exec_driver_sql(sql)
    db.rollback()
    assert db.get(PlatformCostAttempt, attempt_id).status == 'unknown'


def test_sqlite_backup_restores_owned_run_and_database_guards(db, admin_user, monkeypatch, tmp_path):
    import sqlite3

    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    run_pk, secret = create(db, admin_user.id)
    db.commit()
    attempt, _ = reserve(db, admin_user.id, run_pk, secret)
    attempt_id = attempt.id
    db.commit()
    destination = tmp_path / 'owner-backup.sqlite'
    source = db.connection().connection.driver_connection
    with sqlite3.connect(destination) as backup:
        source.backup(backup)
    with sqlite3.connect(destination) as restored:
        restored.execute('PRAGMA foreign_keys=ON')
        assert restored.execute('PRAGMA foreign_key_check').fetchall() == []
        assert restored.execute('SELECT run_pk FROM platform_cost_run_owner').fetchone() == (run_pk,)
        assert restored.execute('SELECT attempt_id FROM platform_cost_attempt_owner').fetchone() == (attempt_id,)
        with pytest.raises(sqlite3.IntegrityError):
            restored.execute('DELETE FROM agent_response_run WHERE id=?', (run_pk,))
        with pytest.raises(sqlite3.IntegrityError):
            restored.execute('DELETE FROM platform_cost_run_owner WHERE run_pk=?', (run_pk,))
        with pytest.raises(sqlite3.IntegrityError):
            restored.execute("UPDATE agent_response_run SET user_id=user_id+1 WHERE id=?", (run_pk,))


def test_automatic_schema_drop_is_blocked_without_altering_unowned_runs(db, admin_user):
    db.add(AgentResponseRun(run_id='unowned', user_id=admin_user.id, surface='admin',
                            session_key='admin-session', status='running',
                            checkpoint_json='{"status": "running"}', version=1))
    db.commit()
    with pytest.raises(RuntimeError, match='禁止自动删除'):
        PlatformCostBase.metadata.drop_all(db.get_bind())
    db.connection().exec_driver_sql("UPDATE agent_response_run SET status='cancelled' WHERE run_id='unowned'")
    db.commit()
    assert db.query(AgentResponseRun).filter_by(run_id='unowned').one().status == 'cancelled'


def test_existing_trigger_collision_rejects_install_without_deleting_existing_trigger(tmp_path):
    from sqlalchemy import create_engine, inspect

    from app.core.database import Base

    engine = create_engine(f'sqlite:///{tmp_path / "existing-trigger.sqlite"}')
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql('CREATE TRIGGER pc_run_identity AFTER UPDATE ON agent_response_run '
                                   'BEGIN SELECT 1; END')
    try:
        with pytest.raises(RuntimeError, match='已有触发器'):
            PlatformCostBase.metadata.create_all(engine)
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT count(*) FROM sqlite_master WHERE type='trigger' "
                                              "AND name='pc_run_identity'").scalar_one() == 1
        assert 'platform_cost_run_owner' not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_unknown_idempotent_and_cancel_rollback_keeps_owner(db, admin_user, monkeypatch):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    run_pk, run_secret = create(db, admin_user.id)
    db.commit()
    attempt, attempt_secret = reserve(db, admin_user.id, run_pk, run_secret)
    attempt_id, held = attempt.id, attempt.reserved_cny_micros
    db.commit()
    owner.cancel_owned_run(db, actor_id=admin_user.id, run_pk=run_pk, session_key='admin-session')
    db.rollback()
    for _ in range(2):
        owner.mark_owned_attempt_unknown(db, actor_id=admin_user.id, run_pk=run_pk, session_key='admin-session',
                                         run_secret=run_secret, attempt_id=attempt_id,
                                         attempt_secret=attempt_secret)
        db.commit()
    owner.cancel_owned_run(db, actor_id=admin_user.id, run_pk=run_pk, session_key='admin-session')
    db.commit()
    assert db.get(PlatformCostAttempt, attempt_id).status == 'unknown'
    budget = db.query(PlatformCostBudget).one()
    assert (budget.reserved_cny_micros, budget.spent_cny_micros) == (held, 0)


def test_synthetic_settlement_stays_closed_when_foundation_is_enabled(db, admin_user, monkeypatch):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    run_pk, run_secret = create(db, admin_user.id)
    db.commit()
    attempt, attempt_secret = reserve(db, admin_user.id, run_pk, run_secret)
    attempt_id, held = attempt.id, attempt.reserved_cny_micros
    db.commit()
    with pytest.raises(BillingError, match='未启用'):
        owner.settle_owned_attempt(db, actor_id=admin_user.id, run_pk=run_pk, session_key='admin-session',
                                   run_secret=run_secret, attempt_id=attempt_id, attempt_secret=attempt_secret,
                                   receipt_digest=hashlib.sha256(b'closed').digest(), input_tokens=0, output_tokens=0)
    db.commit()
    assert db.query(PlatformCostBudget).one().reserved_cny_micros == held
    assert db.get(PlatformCostAttempt, attempt_id).status == 'reserved'


def test_settlement_uses_frozen_cost_and_replay_is_idempotent(db, admin_user, monkeypatch, synthetic_settlement):
    from app.models.billing_ledger import BillingRateCatalogItem

    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    run_pk, run_secret = create(db, admin_user.id)
    db.commit()
    attempt, attempt_secret = reserve(db, admin_user.id, run_pk, run_secret)
    attempt_id = attempt.id
    db.commit()
    item = db.query(BillingRateCatalogItem).filter_by(model_name='deepseek-flash').one()
    item.provider_input_cny_micros_per_million = 99_000_000
    item.provider_output_cny_micros_per_million = 99_000_000
    db.commit()
    receipt = hashlib.sha256(b'isolation-test-receipt').digest()
    args = dict(actor_id=admin_user.id, run_pk=run_pk, session_key='admin-session',
                run_secret=run_secret, attempt_id=attempt_id, attempt_secret=attempt_secret,
                receipt_digest=receipt, input_tokens=10, output_tokens=20)
    assert owner.settle_owned_attempt(db, **args) == 180
    db.commit()
    assert owner.settle_owned_attempt(db, **args) == 180
    db.commit()
    for changes in ({'input_tokens': 11}, {'receipt_digest': hashlib.sha256(b'other').digest()}):
        with pytest.raises(BillingError):
            owner.settle_owned_attempt(db, **(args | changes))
        db.commit()
    budget = db.query(PlatformCostBudget).one()
    assert (budget.reserved_cny_micros, budget.spent_cny_micros) == (0, 180)
    assert db.get(PlatformCostAttempt, attempt_id).status == 'settled'
    owner.cancel_owned_run(db, actor_id=admin_user.id, run_pk=run_pk, session_key='admin-session')
    db.commit()
    assert db.get(PlatformCostAttempt, attempt_id).status == 'settled'


def test_failure_after_budget_write_rolls_back_receipt_and_budget(db, admin_user, monkeypatch, synthetic_settlement):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    run_pk, run_secret = create(db, admin_user.id)
    db.commit()
    attempt, attempt_secret = reserve(db, admin_user.id, run_pk, run_secret)
    attempt_id, held = attempt.id, attempt.reserved_cny_micros
    db.commit()
    db.connection().exec_driver_sql("CREATE TRIGGER injected_settlement_failure BEFORE UPDATE ON platform_cost_attempt "
                                    "WHEN NEW.status='settled' BEGIN SELECT RAISE(ABORT, 'injected'); END")
    db.commit()
    with pytest.raises(BillingError):
        owner.settle_owned_attempt(db, actor_id=admin_user.id, run_pk=run_pk, session_key='admin-session',
                                   run_secret=run_secret, attempt_id=attempt_id, attempt_secret=attempt_secret,
                                   receipt_digest=hashlib.sha256(b'injected').digest(),
                                   input_tokens=1, output_tokens=1)
    db.commit()
    budget = db.query(PlatformCostBudget).one()
    assert (budget.reserved_cny_micros, budget.spent_cny_micros) == (held, 0)
    assert db.query(PlatformCostSettlement).count() == 0
    assert db.get(PlatformCostAttempt, attempt_id).status == 'reserved'


def test_budget_mismatch_keeps_frozen_and_does_not_persist_receipt(db, admin_user, monkeypatch, synthetic_settlement):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    run_pk, run_secret = create(db, admin_user.id)
    db.commit()
    attempt, attempt_secret = reserve(db, admin_user.id, run_pk, run_secret)
    attempt_id = attempt.id
    db.commit()
    # Direct budget corruption is separately blocked by deployment/DB permissions,
    # but this primitive must fail closed if it observes an inconsistent total.
    db.query(PlatformCostBudget).one().reserved_cny_micros = 0
    db.commit()
    with pytest.raises(BillingError, match='不一致'):
        owner.settle_owned_attempt(db, actor_id=admin_user.id, run_pk=run_pk, session_key='admin-session',
                                   run_secret=run_secret, attempt_id=attempt_id, attempt_secret=attempt_secret,
                                   receipt_digest=hashlib.sha256(b'bad-budget').digest(),
                                   input_tokens=1, output_tokens=1)
    db.commit()
    assert db.query(PlatformCostSettlement).count() == 0
    assert db.get(PlatformCostAttempt, attempt_id).status == 'reserved'


def test_two_holds_with_corrupt_aggregate_cannot_consume_other_hold(db, admin_user, monkeypatch, synthetic_settlement):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    run_pk, run_secret = create(db, admin_user.id)
    db.commit()
    first, first_secret = reserve(db, admin_user.id, run_pk, run_secret)
    first_id, held = first.id, first.reserved_cny_micros
    db.commit()
    second, _ = reserve(db, admin_user.id, run_pk, run_secret)
    second_id = second.id
    db.commit()
    db.query(PlatformCostBudget).one().reserved_cny_micros = held
    db.commit()
    with pytest.raises(BillingError, match='不一致'):
        owner.settle_owned_attempt(db, actor_id=admin_user.id, run_pk=run_pk, session_key='admin-session',
                                   run_secret=run_secret, attempt_id=first_id, attempt_secret=first_secret,
                                   receipt_digest=hashlib.sha256(b'corrupt-two-holds').digest(),
                                   input_tokens=0, output_tokens=0)
    db.commit()
    assert db.query(PlatformCostSettlement).count() == 0
    assert db.get(PlatformCostAttempt, first_id).status == db.get(PlatformCostAttempt, second_id).status == 'reserved'
    assert db.query(PlatformCostBudget).one().reserved_cny_micros == held


def test_settlement_receipt_and_status_are_immutable(db, admin_user, monkeypatch, synthetic_settlement):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    run_pk, run_secret = create(db, admin_user.id)
    db.commit()
    attempt, attempt_secret = reserve(db, admin_user.id, run_pk, run_secret)
    attempt_id = attempt.id
    db.commit()
    owner.settle_owned_attempt(db, actor_id=admin_user.id, run_pk=run_pk, session_key='admin-session',
                               run_secret=run_secret, attempt_id=attempt_id, attempt_secret=attempt_secret,
                               receipt_digest=hashlib.sha256(b'immutable-receipt').digest(),
                               input_tokens=1, output_tokens=1)
    db.commit()
    for sql in ('DELETE FROM platform_cost_settlement',
                'UPDATE platform_cost_settlement SET actual_cny_micros=0',
                'INSERT OR REPLACE INTO platform_cost_settlement SELECT * FROM platform_cost_settlement',
                "UPDATE platform_cost_attempt SET status='unknown'"):
        with pytest.raises(IntegrityError):
            db.connection().exec_driver_sql(sql)
        db.rollback()
    assert db.query(PlatformCostSettlement).count() == 1
    assert db.get(PlatformCostAttempt, attempt_id).status == 'settled'


@pytest.mark.parametrize('change', [
    {'input_tokens': -1}, {'input_tokens': True}, {'input_tokens': 107},
    {'output_tokens': 101}, {'output_tokens': 1.1}, {'receipt_digest': None},
    {'receipt_digest': b'short'},
])
def test_invalid_settlement_does_not_release_even_if_caller_commits(
    db, admin_user, monkeypatch, synthetic_settlement, change
):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    run_pk, run_secret = create(db, admin_user.id)
    db.commit()
    attempt, attempt_secret = reserve(db, admin_user.id, run_pk, run_secret)
    attempt_id, held = attempt.id, attempt.reserved_cny_micros
    db.commit()
    args = dict(actor_id=admin_user.id, run_pk=run_pk, session_key='admin-session',
                run_secret=run_secret, attempt_id=attempt_id, attempt_secret=attempt_secret,
                receipt_digest=hashlib.sha256(b'bad-test-receipt').digest(), input_tokens=1, output_tokens=1)
    with pytest.raises(BillingError):
        owner.settle_owned_attempt(db, **(args | change))
    db.commit()
    budget = db.query(PlatformCostBudget).one()
    assert (budget.reserved_cny_micros, budget.spent_cny_micros) == (held, 0)
    assert db.get(PlatformCostAttempt, attempt_id).status == 'reserved'


@pytest.mark.parametrize('interruption', ['unknown', 'cancel', 'revoke', 'generation'])
def test_interrupted_or_stale_owner_cannot_settle(db, admin_user, monkeypatch, synthetic_settlement, interruption):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    run_pk, run_secret = create(db, admin_user.id)
    db.commit()
    attempt, attempt_secret = reserve(db, admin_user.id, run_pk, run_secret)
    attempt_id, held = attempt.id, attempt.reserved_cny_micros
    db.commit()
    if interruption == 'unknown':
        owner.mark_owned_attempt_unknown(db, actor_id=admin_user.id, run_pk=run_pk,
                                         session_key='admin-session', run_secret=run_secret,
                                         attempt_id=attempt_id, attempt_secret=attempt_secret)
    elif interruption == 'cancel':
        owner.cancel_owned_run(db, actor_id=admin_user.id, run_pk=run_pk, session_key='admin-session')
    elif interruption == 'revoke':
        admin_user.status = 0
    else:
        db.query(PlatformRunOwner).filter_by(run_pk=run_pk).one().generation += 1
    db.commit()
    with pytest.raises(BillingError):
        owner.settle_owned_attempt(db, actor_id=admin_user.id, run_pk=run_pk, session_key='admin-session',
                                   run_secret=run_secret, attempt_id=attempt_id, attempt_secret=attempt_secret,
                                   receipt_digest=hashlib.sha256(b'interrupted').digest(),
                                   input_tokens=1, output_tokens=1)
    db.rollback()
    budget = db.query(PlatformCostBudget).one()
    assert (budget.reserved_cny_micros, budget.spent_cny_micros) == (held, 0)


def test_two_connections_settle_once_and_cancel_race(tmp_path, monkeypatch, synthetic_settlement):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    from app.core.database import Base
    from app.models.user import User
    from app.services.billing_rate_catalog_service import seed_test_baseline

    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    engine = create_engine(f'sqlite:///{tmp_path / "settlement-race.sqlite"}', poolclass=NullPool,
                           connect_args={'timeout': 5})

    @event.listens_for(engine, 'connect')
    def enable_fk(connection, _record):
        connection.execute('PRAGMA foreign_keys=ON')

    Base.metadata.create_all(engine)
    PlatformCostBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    now = datetime.utcnow()
    try:
        with factory.begin() as setup:
            seed_test_baseline(setup)
            setup.add(User(id=501, username='settlement-race', password='x', role='admin', status=1))
            setup.add(PlatformCostCenter(id=1, status='active'))
            setup.flush()
            setup.add(PlatformCostBudget(period_start=now-timedelta(days=1), period_end=now+timedelta(days=1),
                                         limit_cny_micros=1_000_000, reserved_cny_micros=0,
                                         spent_cny_micros=0, status='active'))
        with factory.begin() as session:
            run_pk, run_secret = create(session, 501)
        with factory.begin() as session:
            attempt, attempt_secret = reserve(session, 501, run_pk, run_secret)
            attempt_id = attempt.id
        args = dict(actor_id=501, run_pk=run_pk, session_key='admin-session', run_secret=run_secret,
                    attempt_id=attempt_id, attempt_secret=attempt_secret,
                    receipt_digest=hashlib.sha256(b'concurrent-receipt').digest(), input_tokens=10, output_tokens=20)
        barrier = Barrier(2, timeout=5)

        def settle(_):
            with factory.begin() as session:
                barrier.wait()
                return owner.settle_owned_attempt(session, **args)

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert list(pool.map(settle, range(2))) == [180, 180]
        with factory() as session:
            budget = session.query(PlatformCostBudget).one()
            assert (budget.reserved_cny_micros, budget.spent_cny_micros) == (0, 180)
            assert session.query(PlatformCostSettlement).count() == 1

        with factory.begin() as session:
            race_run, race_secret = create(session, 501, run_id='cancel-race')
        with factory.begin() as session:
            race_attempt, race_attempt_secret = reserve(session, 501, race_run, race_secret)
            race_id, held = race_attempt.id, race_attempt.reserved_cny_micros
        barrier = Barrier(2, timeout=5)

        def compete(action):
            with factory() as session:
                barrier.wait()
                try:
                    if action == 'cancel':
                        owner.cancel_owned_run(session, actor_id=501, run_pk=race_run, session_key='admin-session')
                    else:
                        owner.settle_owned_attempt(session, actor_id=501, run_pk=race_run,
                                                   session_key='admin-session', run_secret=race_secret,
                                                   attempt_id=race_id, attempt_secret=race_attempt_secret,
                                                   receipt_digest=hashlib.sha256(b'cancel-race').digest(),
                                                   input_tokens=10, output_tokens=20)
                    session.commit()
                    return 'ok'
                except BillingError:
                    session.rollback()
                    return 'rejected'

        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(compete, ('cancel', 'settle')))
        assert result[0] == 'ok' and result[1] in {'ok', 'rejected'}
        with factory() as session:
            budget = session.query(PlatformCostBudget).one()
            attempt = session.get(PlatformCostAttempt, race_id)
            if result[1] == 'ok':
                assert attempt.status == 'settled' and (budget.reserved_cny_micros, budget.spent_cny_micros) == (0, 360)
            else:
                assert attempt.status == 'unknown'
                assert (budget.reserved_cny_micros, budget.spent_cny_micros) == (held, 180)
    finally:
        engine.dispose()


def test_owned_attempt_concurrent_replay_and_cancel_unknown(tmp_path, monkeypatch):
    """Two physical connections serialize duplicate reservation and cancellation."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    from app.core.database import Base
    from app.models.user import User
    from app.services.billing_rate_catalog_service import seed_test_baseline

    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    engine = create_engine(f'sqlite:///{tmp_path / "owner-race.sqlite"}', poolclass=NullPool,
                           connect_args={'timeout': 5})
    @event.listens_for(engine, 'connect')
    def enable_foreign_keys(connection, _record):
        connection.execute('PRAGMA foreign_keys=ON')

    Base.metadata.create_all(engine)
    PlatformCostBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    now = datetime.utcnow()
    try:
        with factory.begin() as setup:
            seed_test_baseline(setup)
            setup.add(User(id=501, username='owner-race', password='x', role='admin', status=1))
            setup.add(PlatformCostCenter(id=1, status='active'))
            setup.flush()
            setup.add(PlatformCostBudget(period_start=now - timedelta(days=1), period_end=now + timedelta(days=1),
                                         limit_cny_micros=1_000_000, reserved_cny_micros=0,
                                         spent_cny_micros=0, status='active'))
        with factory.begin() as session:
            run_pk, run_secret = create(session, 501)
        attempt_key = platform.new_attempt_key()
        barrier = Barrier(2, timeout=5)

        def compete(_):
            with factory() as session:
                barrier.wait()
                try:
                    attempt, secret = reserve(session, 501, run_pk, run_secret, key=attempt_key)
                    value = (attempt.id, secret)
                    session.commit()
                    return value
                except BillingError:
                    session.rollback()
                    return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(compete, range(2)))
        winners = [item for item in results if item is not None]
        assert len(winners) == 1
        attempt_id, attempt_secret = winners[0]
        with factory() as session:
            held = session.query(PlatformCostBudget).one().reserved_cny_micros
            assert held > 0 and session.query(PlatformCostAttempt).count() == 1
            assert session.query(PlatformAttemptOwner).count() == 1

        barrier = Barrier(2, timeout=5)

        def finish(kind):
            with factory() as session:
                barrier.wait()
                try:
                    if kind == 'cancel':
                        owner.cancel_owned_run(session, actor_id=501, run_pk=run_pk, session_key='admin-session')
                    else:
                        owner.mark_owned_attempt_unknown(session, actor_id=501, run_pk=run_pk,
                                                         session_key='admin-session', run_secret=run_secret,
                                                         attempt_id=attempt_id, attempt_secret=attempt_secret)
                    session.commit()
                    return 'ok'
                except BillingError:
                    session.rollback()
                    return 'rejected'

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(finish, ('cancel', 'unknown')))
        assert outcomes[0] == 'ok' and outcomes[1] in {'ok', 'rejected'}
        with factory() as session:
            assert session.get(PlatformCostAttempt, attempt_id).status == 'unknown'
            assert session.get(AgentResponseRun, run_pk).status == 'cancelled'
            budget = session.query(PlatformCostBudget).one()
            assert (budget.reserved_cny_micros, budget.spent_cny_micros) == (held, 0)
    finally:
        engine.dispose()
