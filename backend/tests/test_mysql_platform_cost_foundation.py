"""Explicit disposable-MySQL races; never drops a database or connects without an opt-in URL."""

import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timedelta
from threading import Barrier, Event

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.platform_cost_center import (
    PlatformCostAttempt,
    PlatformCostBase,
    PlatformCostBudget,
    PlatformCostCenter,
)
from app.models.user import User
from app.services import platform_cost_center as platform
from app.services.billing_ledger_service import BillingError
from app.services.billing_rate_catalog_service import seed_test_baseline


def _reserve(db, actor_id):
    return platform.reserve_model_attempt(
        db, actor_id=actor_id, attempt_key=platform.new_attempt_key(), model_name='deepseek-flash',
        provider_name='deepseek', base_url='https://api.deepseek.com/v1', credential_source='system',
        payload={'model': 'deepseek-flash', 'input': 'hello', 'max_output_tokens': 100},
    )


def _must_wait(future):
    with pytest.raises(FutureTimeout):
        future.result(timeout=0.3)


def test_platform_center_and_budget_mysql_lock_order(monkeypatch):
    raw = os.environ.get('PRISM_PLATFORM_COST_TEST_MYSQL_URL', '')
    if not raw:
        pytest.skip('requires an explicitly supplied disposable empty MySQL database')
    url = make_url(raw)
    container_id = os.environ.get('PRISM_PLATFORM_COST_TEST_CONTAINER_ID', '')
    nonce = os.environ.get('PRISM_PLATFORM_COST_TEST_NONCE', '')
    if (url.get_backend_name() != 'mysql' or url.host != '127.0.0.1'
            or not re.fullmatch(r'prism_platform_cost_test_[0-9a-f]{10}', url.database or '')
            or not re.fullmatch(r'[0-9a-f]{64}', container_id)
            or not re.fullmatch(r'[0-9a-f]{64}', nonce)):
        pytest.fail('refusing non-disposable MySQL target')
    identity = subprocess.run(
        ['docker', 'inspect', '--format', '{{.Id}} {{index .Config.Labels "prism.task"}} {{.State.Running}}',
         container_id], capture_output=True, text=True, check=False, timeout=5,
    )
    port = subprocess.run(['docker', 'port', container_id, '3306/tcp'], capture_output=True,
                          text=True, check=False, timeout=5)
    if (identity.returncode != 0 or identity.stdout.strip() != f'{container_id} platform-cost-isolated true'
            or port.returncode != 0 or port.stdout.strip() != f'127.0.0.1:{url.port}'):
        pytest.fail('refusing unverified MySQL container or port')
    engine = create_engine(url, pool_pre_ping=True)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        inspector = inspect(engine)
        if inspector.get_table_names() != ['_prism_platform_cost_test_identity'] or inspector.get_view_names():
            pytest.fail('refusing database without exclusive test marker')
        with engine.connect() as db:
            markers = db.execute(text('SELECT nonce FROM _prism_platform_cost_test_identity')).scalars().all()
        if markers != [nonce]:
            pytest.fail('refusing database with mismatched test marker')
        Base.metadata.create_all(bind=engine)
        PlatformCostBase.metadata.create_all(bind=engine)
        now = datetime.utcnow().replace(microsecond=0)
        with factory.begin() as db:
            seed_test_baseline(db)
            db.add(PlatformCostCenter(id=1, status='active'))
            db.flush()
            db.add_all([User(id=501, username='platform-admin-1', password='x', role='admin', status=1),
                        User(id=502, username='platform-admin-2', password='x', role='admin', status=1),
                        PlatformCostBudget(id=1, period_start=now - timedelta(days=1),
                                           period_end=now + timedelta(days=1), limit_cny_micros=1_000_000,
                                           reserved_cny_micros=0, spent_cny_micros=0, status='active')])
        monkeypatch.setattr(platform, '_FOUNDATION_ENABLED', True)
        reached = Event()
        original_auth = platform._require_admin

        def observed_auth(db, actor_id):
            original_auth(db, actor_id)
            if actor_id == 502:
                reached.set()

        monkeypatch.setattr(platform, '_require_admin', observed_auth)

        def reserve_second():
            with factory() as db:
                try:
                    _reserve(db, 502)
                    db.commit()
                    return 'reserved'
                except BillingError:
                    db.rollback()
                    return 'rejected'

        # A committed disable wins over a waiting reservation.
        with factory() as blocker, ThreadPoolExecutor(max_workers=1) as pool:
            center = blocker.query(PlatformCostCenter).filter_by(id=1).with_for_update().one()
            center.status = 'disabled'
            blocker.flush()
            future = pool.submit(reserve_second)
            assert reached.wait(5)
            _must_wait(future)
            blocker.commit()
            assert future.result(timeout=5) == 'rejected'
        with factory.begin() as db:
            center = db.query(PlatformCostCenter).filter_by(id=1).with_for_update().one()
            center.status = 'active'

        # A reservation holding the center finishes before a later disable.
        held, release, disabling = Event(), Event(), Event()

        def reserve_first():
            with factory() as db:
                attempt = _reserve(db, 501)
                held.set()
                assert release.wait(5)
                amount = attempt.reserved_cny_micros
                db.commit()
                return amount

        def disable_center():
            with factory.begin() as db:
                disabling.set()
                center = db.query(PlatformCostCenter).filter_by(id=1).with_for_update().one()
                center.status = 'disabled'

        with ThreadPoolExecutor(max_workers=2) as pool:
            reservation = pool.submit(reserve_first)
            assert held.wait(5)
            disable = pool.submit(disable_center)
            assert disabling.wait(5)
            _must_wait(disable)
            release.set()
            reserved_amount = reservation.result(timeout=5)
            disable.result(timeout=5)
        assert reserved_amount > 0
        with factory() as db:
            assert db.get(PlatformCostCenter, 1).status == 'disabled'
            with pytest.raises(BillingError, match='成本中心'):
                _reserve(db, 501)
            db.rollback()
            assert db.query(PlatformCostAttempt).count() == 1
        with factory.begin() as db:
            db.query(PlatformCostCenter).filter_by(id=1).with_for_update().one().status = 'active'

        # Waiting on the budget lock must not reuse the stale pre-wait clock.
        clock = {'now': datetime.utcnow().replace(microsecond=0)}
        monkeypatch.setattr(platform, '_now', lambda: clock['now'])
        with factory.begin() as db:
            row = db.query(PlatformCostBudget).filter_by(id=1).with_for_update().one()
            row.period_end = clock['now'] + timedelta(seconds=5)
        rate_seen = Event()
        original_cost = platform._cost_snapshot

        def observed_cost(db, model_name, at):
            value = original_cost(db, model_name, at)
            rate_seen.set()
            return value

        monkeypatch.setattr(platform, '_cost_snapshot', observed_cost)
        reached.clear()
        with factory() as blocker, ThreadPoolExecutor(max_workers=1) as pool:
            blocker.query(PlatformCostBudget).filter_by(id=1).with_for_update().one()
            future = pool.submit(reserve_second)
            assert rate_seen.wait(5)
            _must_wait(future)
            clock['now'] += timedelta(seconds=6)
            blocker.commit()
            assert future.result(timeout=5) == 'rejected'
        with factory() as db:
            assert db.query(PlatformCostAttempt).count() == 1
            assert db.get(PlatformCostBudget, 1).reserved_cny_micros == reserved_amount

        # A compliant budget writer must hold the same singleton lock. The
        # reservation then sees the committed overlap via a current read.
        with factory.begin() as db:
            db.query(PlatformCostCenter).filter_by(id=1).with_for_update().one()
            db.query(PlatformCostBudget).filter_by(id=1).with_for_update().one().period_end = (
                clock['now'] + timedelta(days=1)
            )
        reached.clear()
        with factory() as blocker, ThreadPoolExecutor(max_workers=1) as pool:
            blocker.query(PlatformCostCenter).filter_by(id=1).with_for_update().one()
            blocker.add(PlatformCostBudget(id=2, center_id=1, period_start=clock['now'] - timedelta(hours=1),
                                           period_end=clock['now'] + timedelta(hours=1), limit_cny_micros=10_000,
                                           reserved_cny_micros=0, spent_cny_micros=0, status='active'))
            blocker.flush()
            future = pool.submit(reserve_second)
            assert reached.wait(5)
            _must_wait(future)
            blocker.commit()
            assert future.result(timeout=5) == 'rejected'

        # Two authenticated admins compete for the final single-call budget.
        with factory.begin() as db:
            db.query(PlatformCostCenter).filter_by(id=1).with_for_update().one()
            db.query(PlatformCostBudget).filter_by(id=2).with_for_update().one().status = 'disabled'
            row = db.query(PlatformCostBudget).filter_by(id=1).with_for_update().one()
            row.limit_cny_micros = reserved_amount + 1_500
        barrier = Barrier(2, timeout=5)

        def compete(actor_id):
            with factory() as db:
                barrier.wait()
                try:
                    _reserve(db, actor_id)
                    db.commit()
                    return 'reserved'
                except BillingError:
                    db.rollback()
                    return 'rejected'

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(compete, (501, 502)))
        assert sorted(results) == ['rejected', 'reserved']
        with factory() as db:
            row = db.get(PlatformCostBudget, 1)
            assert row.reserved_cny_micros <= row.limit_cny_micros
            assert db.query(PlatformCostAttempt).count() == 2
    finally:
        engine.dispose()
