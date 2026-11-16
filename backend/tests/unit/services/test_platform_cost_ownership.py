"""Detached platform ownership contracts; no production sender or migration."""

from datetime import datetime, timedelta

import pytest

from app.models.agent_response_run import AgentResponseRun
from app.models.platform_cost_center import (
    PlatformAttemptOwner,
    PlatformCostAttempt,
    PlatformCostBase,
    PlatformCostBudget,
    PlatformCostCenter,
    PlatformRunOwner,
)
from app.services import platform_cost_center as platform
from app.services import platform_cost_ownership as owner
from app.services.billing_ledger_service import BillingError


@pytest.fixture(autouse=True)
def detached_tables(db):
    PlatformCostBase.metadata.create_all(db.get_bind())
    now = datetime.utcnow()
    db.add(PlatformCostCenter(id=1, status='active'))
    db.add(PlatformCostBudget(period_start=now - timedelta(days=1), period_end=now + timedelta(days=1),
                              limit_cny_micros=1_000_000, reserved_cny_micros=0, spent_cny_micros=0,
                              status='active'))
    db.commit()


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


@pytest.mark.xfail(strict=True, reason='缺少真实 FK 与不可变运行身份，必须禁止上线')
def test_recreated_run_with_same_primary_key_cannot_reuse_old_owner(db, admin_user, monkeypatch):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    run_pk, run_secret = create(db, admin_user.id)
    db.commit()
    db.delete(db.get(AgentResponseRun, run_pk))
    db.commit()
    db.add(AgentResponseRun(id=run_pk, run_id='replacement', user_id=admin_user.id, surface='admin',
                            session_key='admin-session', status='running',
                            checkpoint_json='{"status": "running"}', version=1))
    db.commit()
    with pytest.raises(BillingError):
        reserve(db, admin_user.id, run_pk, run_secret)


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


def test_owned_attempt_concurrent_replay_and_cancel_unknown(tmp_path, monkeypatch):
    """Two physical connections serialize duplicate reservation and cancellation."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    from app.core.database import Base
    from app.models.user import User
    from app.services.billing_rate_catalog_service import seed_test_baseline

    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    engine = create_engine(f'sqlite:///{tmp_path / "owner-race.sqlite"}', poolclass=NullPool,
                           connect_args={'timeout': 5})
    Base.metadata.create_all(engine)
    PlatformCostBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    now = datetime.utcnow()
    try:
        with factory.begin() as setup:
            seed_test_baseline(setup)
            setup.add(User(id=501, username='owner-race', password='x', role='admin', status=1))
            setup.add(PlatformCostCenter(id=1, status='active'))
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
