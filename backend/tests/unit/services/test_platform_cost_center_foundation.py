"""Detached platform budget contracts; no provider request or production integration."""

from datetime import datetime, timedelta

import pytest

from app.models.platform_cost_center import (
    PlatformCostAttempt,
    PlatformCostBase,
    PlatformCostBudget,
    PlatformCostCenter,
)
from app.models.user import User
from app.services import platform_cost_center as platform
from app.services.billing_ledger_service import BillingError


@pytest.fixture(autouse=True)
def platform_tables(db):
    PlatformCostBase.metadata.create_all(bind=db.get_bind())
    db.add(PlatformCostCenter(id=1, status='active'))
    db.commit()


@pytest.fixture
def budget(db):
    now = datetime.utcnow()
    row = PlatformCostBudget(period_start=now - timedelta(days=1), period_end=now + timedelta(days=1),
                             limit_cny_micros=1_000_000, reserved_cny_micros=0, spent_cny_micros=0,
                             status='active')
    db.add(row)
    db.commit()
    return row


def reserve(db, actor_id, key=None, **kwargs):
    if key is None:
        key = platform.new_attempt_key()
    params = dict(actor_id=actor_id, attempt_key=key, model_name='deepseek-flash',
                  provider_name='deepseek', base_url='https://api.deepseek.com/v1',
                  credential_source='system', payload={'model': 'deepseek-flash', 'input': 'hello',
                                                       'max_output_tokens': 100})
    params.update(kwargs)
    return platform.reserve_model_attempt(db, **params)


def test_default_closed_without_budget_or_provider_access(db, admin_user):
    with pytest.raises(BillingError, match='未启用'):
        reserve(db, admin_user.id)


def test_server_role_and_budget_required(db, admin_user, budget, monkeypatch):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    member = User(username='member-cost', password='x', role='user', status=1)
    db.add(member)
    db.commit()
    with pytest.raises(BillingError, match='管理员'):
        reserve(db, member.id)
    budget.status = 'disabled'
    db.flush()
    with pytest.raises(BillingError, match='预算'):
        reserve(db, admin_user.id)
    assert db.query(PlatformCostAttempt).count() == 0


def test_hold_freezes_verified_provider_cost_and_unknown_does_not_release(db, admin_user, budget, monkeypatch):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    key = platform.new_attempt_key()
    attempt = reserve(db, admin_user.id, key=key)
    assert attempt.status == 'reserved' and attempt.reserved_cny_micros > 0
    assert attempt.provider_name == 'deepseek' and attempt.credential_source == 'system'
    assert len(attempt.pricing_version) == 64 and attempt.input_cost_cny_micros_per_million > 0
    # Approved test fixture prices: CNY 2/8 per million tokens, with 106 input
    # and 100 output tokens reserved, equal to 1012 CNY micro-units.
    assert (attempt.input_limit, attempt.output_limit, attempt.reserved_cny_micros) == (106, 100, 1012)
    assert (attempt.input_cost_cny_micros_per_million, attempt.output_cost_cny_micros_per_million) == (
        2_000_000, 8_000_000,
    )
    held = attempt.reserved_cny_micros
    platform.mark_unknown(db, actor_id=admin_user.id, attempt_key=key)
    db.refresh(budget)
    assert attempt.status == 'unknown' and budget.reserved_cny_micros == held and budget.spent_cny_micros == 0
    with pytest.raises(BillingError):
        reserve(db, admin_user.id, key=key)


@pytest.mark.parametrize('changes', [
    {'credential_source': 'user'}, {'credential_source': 'fallback'}, {'provider_name': 'other'},
    {'base_url': 'https://not-deepseek.example/v1'}, {'model_name': 'deepseek-v4-pro'},
    {'payload': {'model': 'deepseek-flash', 'input': 'x', 'max_output_tokens': 0}},
    {'payload': {'model': 'deepseek-flash', 'input': 'x', 'max_output_tokens': 100,
                 'tools': [{'type': 'web_search'}]}},
    {'payload': {'model': 'deepseek-flash', 'input': 'x', 'max_output_tokens': 1,
                 'max_tokens': 100000, 'n': 3}},
    {'payload': {'model': 'deepseek-flash', 'input': 'x', 'max_output_tokens': 100, 'n': 2}},
    {'payload': {'model': 'deepseek-flash', 'input': 'x', 'max_output_tokens': 100, 'max_tokens': 200}},
    {'payload': {'model': 'deepseek-flash', 'input': 'x', 'max_output_tokens': 100, 'unknown_cost': True}},
    {'payload': {'model': 'deepseek-flash', 'input': 'x', 'max_output_tokens': 100,
                 'previous_response_id': 'remote-state'}},
])
def test_untrusted_provider_model_and_payload_fail_without_hold(db, admin_user, budget, monkeypatch, changes):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    with pytest.raises(BillingError):
        reserve(db, admin_user.id, **changes)
    db.refresh(budget)
    assert budget.reserved_cny_micros == 0
    assert db.query(PlatformCostAttempt).count() == 0


def test_uncommitted_reservation_rolls_back_with_caller(db, admin_user, budget, monkeypatch):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    reserve(db, admin_user.id)
    db.rollback()
    db.refresh(budget)
    assert budget.reserved_cny_micros == 0
    assert db.query(PlatformCostAttempt).count() == 0


def test_insufficient_budget_and_duplicate_attempt_never_increase_hold(db, admin_user, budget, monkeypatch):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    budget.limit_cny_micros = 100
    db.commit()
    with pytest.raises(BillingError, match='预算不足'):
        reserve(db, admin_user.id)
    db.refresh(budget)
    assert budget.reserved_cny_micros == 0
    budget.limit_cny_micros = 1_000_000
    db.commit()
    key = platform.new_attempt_key()
    attempt = reserve(db, admin_user.id, key=key)
    held = attempt.reserved_cny_micros
    with pytest.raises(BillingError, match='已存在'):
        reserve(db, admin_user.id, key=key,
                payload={'model': 'deepseek-flash', 'input': 'changed', 'max_output_tokens': 100})
    db.refresh(budget)
    assert budget.reserved_cny_micros == held
    assert db.query(PlatformCostAttempt).count() == 1


def test_another_admin_cannot_change_attempt_or_budget(db, admin_user, budget, monkeypatch):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    other = User(username='other-admin-cost', password='x', role='admin', status=1)
    db.add(other)
    db.commit()
    key = platform.new_attempt_key()
    attempt = reserve(db, admin_user.id, key=key)
    held = attempt.reserved_cny_micros
    with pytest.raises(BillingError, match='归属'):
        platform.mark_unknown(db, actor_id=other.id, attempt_key=key)
    db.refresh(budget)
    assert attempt.status == 'reserved' and budget.reserved_cny_micros == held


def test_expired_or_overlapping_budget_fails_closed(db, admin_user, budget, monkeypatch):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    now = datetime.utcnow()
    overlap = PlatformCostBudget(period_start=now - timedelta(hours=1), period_end=now + timedelta(hours=1),
                                 limit_cny_micros=1_000_000, reserved_cny_micros=0, spent_cny_micros=0,
                                 status='active')
    db.add(overlap)
    db.commit()
    with pytest.raises(BillingError, match='重叠'):
        reserve(db, admin_user.id)
    overlap.status = 'disabled'
    budget.period_end = now - timedelta(seconds=1)
    budget.period_start = now - timedelta(days=1)
    db.commit()
    with pytest.raises(BillingError, match='预算'):
        reserve(db, admin_user.id)


def test_two_sqlite_connections_compete_for_final_budget(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.database import Base
    from app.services.billing_rate_catalog_service import seed_test_baseline

    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    engine = create_engine(f'sqlite:///{tmp_path / "platform-race.sqlite"}', connect_args={'timeout': 5})
    Base.metadata.create_all(engine)
    PlatformCostBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    now = datetime.utcnow()
    with factory.begin() as setup:
        seed_test_baseline(setup)
        setup.add(User(id=501, username='race-admin', password='x', role='admin', status=1))
        setup.add(PlatformCostCenter(id=1, status='active'))
        setup.add(PlatformCostBudget(period_start=now - timedelta(days=1), period_end=now + timedelta(days=1),
                                    limit_cny_micros=1_500, reserved_cny_micros=0, spent_cny_micros=0,
                                    status='active'))
    barrier = Barrier(2, timeout=5)

    def run(index):
        with factory() as session:
            barrier.wait()
            try:
                attempt = reserve(session, 501, key=platform.new_attempt_key())
                held = attempt.reserved_cny_micros
                session.commit()
                return ('reserved', held)
            except BillingError:
                session.rollback()
                return ('rejected', 0)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, (1, 2)))
        assert sorted(result[0] for result in results) == ['rejected', 'reserved']
        with factory() as session:
            row = session.query(PlatformCostBudget).one()
            assert row.reserved_cny_micros == sum(result[1] for result in results)
            assert session.query(PlatformCostAttempt).count() == 1
    finally:
        engine.dispose()


def test_budget_expiry_after_wait_never_reserves(db, admin_user, budget, monkeypatch):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    before = datetime.utcnow()
    budget.period_end = before + timedelta(seconds=5)
    db.commit()
    times = iter((before, before + timedelta(seconds=6)))
    monkeypatch.setattr(platform, '_now', lambda: next(times, before + timedelta(seconds=6)))
    with pytest.raises(BillingError, match='预算'):
        reserve(db, admin_user.id)
    db.refresh(budget)
    assert budget.reserved_cny_micros == 0
    assert db.query(PlatformCostAttempt).count() == 0


def test_attempt_identifier_must_not_embed_arbitrary_user_text(db, admin_user, budget, monkeypatch):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    with pytest.raises(BillingError, match='标识'):
        reserve(db, admin_user.id, key='用户原始文本或密钥')
    db.refresh(budget)
    assert budget.reserved_cny_micros == 0
    assert db.query(PlatformCostAttempt).count() == 0


def test_disabled_center_blocks_even_active_budget(db, admin_user, budget, monkeypatch):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    center = db.get(PlatformCostCenter, 1)
    center.status = 'disabled'
    db.commit()
    with pytest.raises(BillingError, match='成本中心'):
        reserve(db, admin_user.id)
    db.refresh(budget)
    assert budget.reserved_cny_micros == 0
    assert db.query(PlatformCostAttempt).count() == 0


@pytest.mark.parametrize('target', ['center', 'user', 'budget'])
def test_dirty_caller_objects_are_not_overwritten(db, admin_user, budget, monkeypatch, target):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    if target == 'center':
        row = db.get(PlatformCostCenter, 1)
        row.status = 'disabled'
    elif target == 'user':
        row = admin_user
        row.status = 0
    else:
        row = budget
        row.status = 'disabled'
    with pytest.raises(BillingError, match='未提交'):
        reserve(db, admin_user.id)
    assert row.status == ('disabled' if target != 'user' else 0)
    assert row in db.dirty
    db.rollback()


@pytest.mark.parametrize('target', ['new', 'deleted', 'role', 'userrole', 'catalog'])
def test_pending_caller_work_is_preserved(db, admin_user, budget, monkeypatch, target):
    from app.models.billing_ledger import BillingRateCatalog
    from app.models.rbac import Role, UserRole

    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    role = Role(name='管理员测试', code='admin', status='active')
    db.add(role)
    db.flush()
    binding = UserRole(user_id=admin_user.id, role_id=role.id)
    db.add(binding)
    db.commit()
    if target == 'new':
        row = User(username='pending-extra', password='x', role='user', status=1)
        db.add(row)
        pending = db.new
    elif target == 'deleted':
        row = db.get(PlatformCostCenter, 1)
        db.delete(row)
        pending = db.deleted
    elif target == 'role':
        row = role
        row.status = 'disabled'
        pending = db.dirty
    elif target == 'userrole':
        row = binding
        db.delete(row)
        pending = db.deleted
    else:
        row = db.query(BillingRateCatalog).one()
        row.change_note = 'pending change'
        pending = db.dirty
    with pytest.raises(BillingError, match='未提交'):
        reserve(db, admin_user.id)
    assert row in pending
    if target == 'role':
        assert role.status == 'disabled'
    if target == 'catalog':
        assert row.change_note == 'pending change'
    db.rollback()


def test_changed_approved_cost_facts_fail_closed(db, admin_user, budget, monkeypatch):
    from app.models.billing_ledger import BillingRateCatalogItem

    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    item = db.query(BillingRateCatalogItem).filter_by(model_name='deepseek-flash').one()
    item.provider_input_cny_micros_per_million = 1
    db.commit()
    with pytest.raises(BillingError):
        reserve(db, admin_user.id)
    db.refresh(budget)
    assert budget.reserved_cny_micros == 0
    assert db.query(PlatformCostAttempt).count() == 0


def test_rate_change_during_lock_wait_rejects_reservation(db, admin_user, budget, monkeypatch):
    monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
    original = platform._cost_snapshot
    calls = []

    def changed(db_session, model, now):
        value = original(db_session, model, now)
        calls.append(value)
        return value if len(calls) == 1 else (value[0], value[1] + 1, value[2])

    monkeypatch.setattr(platform, '_cost_snapshot', changed)
    with pytest.raises(BillingError, match='费率'):
        reserve(db, admin_user.id)
    db.refresh(budget)
    assert len(calls) == 2 and budget.reserved_cny_micros == 0
    assert db.query(PlatformCostAttempt).count() == 0
