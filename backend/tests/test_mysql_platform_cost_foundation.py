"""Explicit disposable-MySQL races; never drops a database or connects without an opt-in URL."""

import hashlib
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
from sqlalchemy.exc import DBAPIError
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

        # Detached owner experiment uses the same exclusive marker database.
        from app.models.agent_response_run import AgentResponseRun
        from app.models.platform_cost_center import PlatformAttemptOwner
        from app.services import platform_cost_ownership as owner

        with factory.begin() as db:
            db.query(PlatformCostCenter).filter_by(id=1).with_for_update().one()
            db.query(PlatformCostBudget).filter_by(id=1).with_for_update().one().limit_cny_micros = 1_000_000
        with factory.begin() as db:
            run_pk, run_secret = owner.create_owned_run(db, actor_id=501, run_id='mysql-owner-run',
                                                        session_key='admin-session')
        # Database-level identity protection, including a case-only edit under
        # MySQL's usual case-insensitive collation and REPLACE semantics.
        for statement in (
            'DELETE FROM agent_response_run WHERE id=:pk',
            'DELETE FROM platform_cost_run_owner WHERE run_pk=:pk',
            "UPDATE agent_response_run SET run_id='MYSQL-OWNER-RUN' WHERE id=:pk",
            'UPDATE agent_response_run SET user_id=502 WHERE id=:pk',
            'UPDATE platform_cost_run_owner SET actor_id=502 WHERE run_pk=:pk',
            'REPLACE INTO platform_cost_run_owner (id,run_pk,actor_id,generation,owner_digest,status) '
            'SELECT id,run_pk,actor_id,generation,owner_digest,status FROM platform_cost_run_owner WHERE run_pk=:pk',
        ):
            with factory() as db:
                with pytest.raises(DBAPIError):
                    db.execute(text(statement), {'pk': run_pk})
                db.rollback()
                assert db.get(AgentResponseRun, run_pk).user_id == 501
        with factory() as db:
            connection = db.connection()
            try:
                connection.exec_driver_sql('SET FOREIGN_KEY_CHECKS=0')
                with pytest.raises(DBAPIError):
                    connection.execute(text('DELETE FROM agent_response_run WHERE id=:pk'), {'pk': run_pk})
                with pytest.raises(DBAPIError):
                    connection.execute(text(
                        'REPLACE INTO agent_response_run '
                        '(id,run_id,user_id,surface,session_key,status,checkpoint_json,version,create_time,'
                        'update_time) '
                        "VALUES (:pk,'replacement',501,'admin','admin-session','running','{}',1,NOW(),NOW())"
                    ), {'pk': run_pk})
            finally:
                connection.exec_driver_sql('SET FOREIGN_KEY_CHECKS=1')
                db.rollback()
        key = platform.new_attempt_key()

        def owned_reserve(db, actor=501, pk=run_pk, secret=run_secret, attempt_key=key):
            return owner.reserve_owned_attempt(
                db, actor_id=actor, run_pk=pk, session_key='admin-session', run_secret=secret,
                attempt_key=attempt_key, model_name='deepseek-flash', provider_name='deepseek',
                base_url='https://api.deepseek.com/v1', credential_source='system',
                payload={'model': 'deepseek-flash', 'input': 'hello', 'max_output_tokens': 100},
            )

        barrier = Barrier(2, timeout=5)

        def owner_compete(_):
            with factory() as db:
                barrier.wait()
                try:
                    attempt, secret = owned_reserve(db)
                    result = (attempt.id, secret, attempt.reserved_cny_micros)
                    db.commit()
                    return result
                except BillingError:
                    db.rollback()
                    return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            winners = [value for value in pool.map(owner_compete, range(2)) if value is not None]
        assert len(winners) == 1
        attempt_id, attempt_secret, attempt_held = winners[0]
        with factory() as db:
            held_before_cancel = db.query(PlatformCostBudget).filter_by(id=1).one().reserved_cny_micros
            assert db.query(PlatformAttemptOwner).count() == 1

        # A prior uncommitted permission revocation serializes before reservation.
        started = Event()

        def revoked_reserve():
            with factory() as db:
                started.set()
                try:
                    owned_reserve(db, attempt_key=platform.new_attempt_key())
                    db.commit()
                    return 'reserved'
                except BillingError:
                    db.rollback()
                    return 'rejected'

        with factory() as blocker, ThreadPoolExecutor(max_workers=1) as pool:
            blocker.query(User).filter_by(id=501).with_for_update().one().status = 0
            blocker.flush()
            future = pool.submit(revoked_reserve)
            assert started.wait(5)
            _must_wait(future)
            blocker.commit()
            assert future.result(timeout=5) == 'rejected'
        with factory.begin() as db:
            db.query(User).filter_by(id=501).with_for_update().one().status = 1

        # When reservation owns authorization locks first, revocation waits;
        # once revoked, the old owner cannot write unknown and the hold remains.
        with factory.begin() as db:
            auth_run, auth_secret = owner.create_owned_run(db, actor_id=501, run_id='mysql-reserve-revoke',
                                                           session_key='admin-session')
        authorized = Event()
        finish_authorized = Event()
        revoking = Event()

        def reserve_before_revoke():
            with factory() as db:
                attempt, secret = owned_reserve(db, pk=auth_run, secret=auth_secret,
                                                attempt_key=platform.new_attempt_key())
                value = (attempt.id, secret, attempt.reserved_cny_micros)
                authorized.set()
                assert finish_authorized.wait(5)
                db.commit()
                return value

        def revoke_waiting():
            with factory.begin() as db:
                revoking.set()
                db.query(User).filter_by(id=501).with_for_update().one().status = 0

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(reserve_before_revoke)
            assert authorized.wait(5)
            second = pool.submit(revoke_waiting)
            assert revoking.wait(5)
            _must_wait(second)
            finish_authorized.set()
            auth_attempt, auth_attempt_secret, auth_held = first.result(timeout=5)
            second.result(timeout=5)
        with factory() as db:
            with pytest.raises(BillingError, match='管理员'):
                owner.mark_owned_attempt_unknown(db, actor_id=501, run_pk=auth_run, session_key='admin-session',
                                                 run_secret=auth_secret, attempt_id=auth_attempt,
                                                 attempt_secret=auth_attempt_secret)
            db.rollback()
            assert db.get(PlatformCostAttempt, auth_attempt).status == 'reserved'
        with factory.begin() as db:
            db.query(User).filter_by(id=501).with_for_update().one().status = 1

        barrier = Barrier(2, timeout=5)

        def cancel_or_unknown(action):
            with factory() as db:
                barrier.wait()
                try:
                    if action == 'cancel':
                        owner.cancel_owned_run(db, actor_id=501, run_pk=run_pk, session_key='admin-session')
                    else:
                        owner.mark_owned_attempt_unknown(db, actor_id=501, run_pk=run_pk,
                                                         session_key='admin-session', run_secret=run_secret,
                                                         attempt_id=attempt_id, attempt_secret=attempt_secret)
                    db.commit()
                    return 'ok'
                except BillingError:
                    db.rollback()
                    return 'rejected'

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(cancel_or_unknown, ('cancel', 'unknown')))
        assert results[0] == 'ok' and results[1] in {'ok', 'rejected'}
        with factory() as db:
            assert db.get(AgentResponseRun, run_pk).status == 'cancelled'
            assert db.get(PlatformCostAttempt, attempt_id).status == 'unknown'
            budget = db.query(PlatformCostBudget).filter_by(id=1).one()
            assert budget.reserved_cny_micros == held_before_cancel + auth_held and attempt_held > 0
        for statement in (
            'UPDATE platform_cost_attempt SET actor_id=502 WHERE id=:attempt',
            'UPDATE platform_cost_attempt SET reserved_cny_micros=reserved_cny_micros+1 WHERE id=:attempt',
            "UPDATE platform_cost_attempt SET status='reserved' WHERE id=:attempt",
            'DELETE FROM platform_cost_attempt WHERE id=:attempt',
            'DELETE FROM platform_cost_attempt_owner WHERE attempt_id=:attempt',
        ):
            with factory() as db:
                with pytest.raises(DBAPIError):
                    db.execute(text(statement), {'attempt': attempt_id})
                db.rollback()
                assert db.get(PlatformCostAttempt, attempt_id).status == 'unknown'

        # Hold reservation locks while cancellation waits, then check the reverse order.
        with factory.begin() as db:
            race_run, race_secret = owner.create_owned_run(db, actor_id=501, run_id='mysql-reserve-cancel',
                                                            session_key='admin-session')
        reserved = Event()
        allow_commit = Event()
        cancelling = Event()

        def reserve_before_cancel():
            with factory() as db:
                attempt, secret = owned_reserve(db, pk=race_run, secret=race_secret,
                                                attempt_key=platform.new_attempt_key())
                result = (attempt.id, secret)
                reserved.set()
                assert allow_commit.wait(5)
                db.commit()
                return result

        def cancel_waiting():
            with factory.begin() as db:
                cancelling.set()
                owner.cancel_owned_run(db, actor_id=501, run_pk=race_run, session_key='admin-session')

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(reserve_before_cancel)
            assert reserved.wait(5)
            second = pool.submit(cancel_waiting)
            assert cancelling.wait(5)
            _must_wait(second)
            allow_commit.set()
            race_attempt, _ = first.result(timeout=5)
            second.result(timeout=5)
        with factory() as db:
            assert db.get(PlatformCostAttempt, race_attempt).status == 'unknown'
            with pytest.raises(BillingError):
                owned_reserve(db, pk=race_run, secret=race_secret, attempt_key=platform.new_attempt_key())
            db.rollback()

        # Distinct admins and runs serialize at the common budget, not only at a run lock.
        with factory.begin() as db:
            first_run, first_secret = owner.create_owned_run(db, actor_id=501, run_id='mysql-budget-run-1',
                                                              session_key='admin-session')
        with factory.begin() as db:
            second_run, second_secret = owner.create_owned_run(db, actor_id=502, run_id='mysql-budget-run-2',
                                                                session_key='admin-session')
        with factory.begin() as db:
            db.query(PlatformCostCenter).filter_by(id=1).with_for_update().one()
            budget = db.query(PlatformCostBudget).filter_by(id=1).with_for_update().one()
            budget.limit_cny_micros = budget.reserved_cny_micros + 1_500
        barrier = Barrier(2, timeout=5)

        def distinct_run_reserve(args):
            actor, pk, secret = args
            with factory() as db:
                barrier.wait()
                try:
                    owned_reserve(db, actor=actor, pk=pk, secret=secret, attempt_key=platform.new_attempt_key())
                    db.commit()
                    return 'reserved'
                except BillingError:
                    db.rollback()
                    return 'rejected'

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(distinct_run_reserve,
                                     ((501, first_run, first_secret), (502, second_run, second_secret))))
        assert sorted(outcomes) == ['rejected', 'reserved']
        with factory() as db:
            budget = db.query(PlatformCostBudget).filter_by(id=1).one()
            assert budget.reserved_cny_micros <= budget.limit_cny_micros
        # Synthetic flat-price settlement: identical concurrent receipts may
        # settle once, while cancellation and unknown remain fail-closed.
        from app.models.platform_cost_center import PlatformCostSettlement

        monkeypatch.setattr(owner, '_SYNTHETIC_SETTLEMENT_ENABLED', True)
        with factory.begin() as db:
            db.query(PlatformCostCenter).filter_by(id=1).with_for_update().one()
            db.query(PlatformCostBudget).filter_by(id=1).with_for_update().one().limit_cny_micros = 1_000_000
        with factory.begin() as db:
            settle_run, settle_secret = owner.create_owned_run(db, actor_id=501, run_id='mysql-settlement',
                                                               session_key='admin-session')
        with factory.begin() as db:
            attempt, attempt_secret = owned_reserve(db, pk=settle_run, secret=settle_secret,
                                                    attempt_key=platform.new_attempt_key())
            settle_id, settle_held = attempt.id, attempt.reserved_cny_micros
        with factory() as db:
            budget = db.query(PlatformCostBudget).filter_by(id=1).one()
            before_reserved, before_spent = budget.reserved_cny_micros, budget.spent_cny_micros
        settle_args = dict(actor_id=501, run_pk=settle_run, session_key='admin-session', run_secret=settle_secret,
                           attempt_id=settle_id, attempt_secret=attempt_secret,
                           receipt_digest=hashlib.sha256(b'mysql-settlement').digest(),
                           input_tokens=10, output_tokens=20)
        barrier = Barrier(2, timeout=5)

        def settle_same(_):
            with factory.begin() as db:
                barrier.wait()
                return owner.settle_owned_attempt(db, **settle_args)

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert list(pool.map(settle_same, range(2))) == [180, 180]
        with factory() as db:
            budget = db.query(PlatformCostBudget).filter_by(id=1).one()
            assert (budget.reserved_cny_micros, budget.spent_cny_micros) == (
                before_reserved - settle_held, before_spent + 180,
            )
            assert db.query(PlatformCostSettlement).filter_by(attempt_id=settle_id).count() == 1
            with pytest.raises(BillingError):
                owner.settle_owned_attempt(db, **(settle_args | {'input_tokens': 11}))
            db.rollback()

        with factory.begin() as db:
            cancel_run, cancel_secret = owner.create_owned_run(db, actor_id=501, run_id='mysql-settle-cancel',
                                                               session_key='admin-session')
        with factory.begin() as db:
            attempt, cancel_attempt_secret = owned_reserve(db, pk=cancel_run, secret=cancel_secret,
                                                           attempt_key=platform.new_attempt_key())
            cancel_attempt_id, cancel_held = attempt.id, attempt.reserved_cny_micros
        with factory() as db:
            budget = db.query(PlatformCostBudget).filter_by(id=1).one()
            cancel_reserved, cancel_spent = budget.reserved_cny_micros, budget.spent_cny_micros
        barrier = Barrier(2, timeout=5)

        def cancel_or_settle(action):
            with factory() as db:
                barrier.wait()
                try:
                    if action == 'cancel':
                        owner.cancel_owned_run(db, actor_id=501, run_pk=cancel_run, session_key='admin-session')
                    else:
                        owner.settle_owned_attempt(
                            db, actor_id=501, run_pk=cancel_run, session_key='admin-session',
                            run_secret=cancel_secret, attempt_id=cancel_attempt_id,
                            attempt_secret=cancel_attempt_secret,
                            receipt_digest=hashlib.sha256(b'cancel-settle').digest(),
                            input_tokens=10, output_tokens=20,
                        )
                    db.commit()
                    return 'ok'
                except BillingError:
                    db.rollback()
                    return 'rejected'

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcome = list(pool.map(cancel_or_settle, ('cancel', 'settle')))
        assert outcome[0] == 'ok' and outcome[1] in {'ok', 'rejected'}
        with factory() as db:
            budget = db.query(PlatformCostBudget).filter_by(id=1).one()
            attempt = db.get(PlatformCostAttempt, cancel_attempt_id)
            if outcome[1] == 'ok':
                assert attempt.status == 'settled'
                assert (budget.reserved_cny_micros, budget.spent_cny_micros) == (
                    cancel_reserved-cancel_held, cancel_spent+180,
                )
            else:
                assert attempt.status == 'unknown'
                assert (budget.reserved_cny_micros, budget.spent_cny_micros) == (cancel_reserved, cancel_spent)

        # Different administrators/runs share the budget. An unknown writer
        # holds one pending row while another owner's settlement checks all holds.
        with factory.begin() as db:
            cross_run, cross_secret = owner.create_owned_run(db, actor_id=501, run_id='mysql-cross-settle',
                                                             session_key='admin-session')
        with factory.begin() as db:
            other_run, other_secret = owner.create_owned_run(db, actor_id=502, run_id='mysql-cross-unknown',
                                                             session_key='admin-session')
        with factory.begin() as db:
            cross_attempt, cross_attempt_secret = owned_reserve(db, pk=cross_run, secret=cross_secret,
                                                                attempt_key=platform.new_attempt_key())
            cross_id = cross_attempt.id
        with factory.begin() as db:
            other_attempt, other_attempt_secret = owned_reserve(db, actor=502, pk=other_run, secret=other_secret,
                                                                attempt_key=platform.new_attempt_key())
            other_id = other_attempt.id
        held_unknown = Event()
        release_unknown = Event()
        waiting_settle = Event()

        def hold_unknown():
            with factory() as db:
                owner.mark_owned_attempt_unknown(db, actor_id=502, run_pk=other_run, session_key='admin-session',
                                                 run_secret=other_secret, attempt_id=other_id,
                                                 attempt_secret=other_attempt_secret)
                held_unknown.set()
                assert release_unknown.wait(5)
                db.commit()

        def settle_cross():
            with factory.begin() as db:
                waiting_settle.set()
                return owner.settle_owned_attempt(
                    db, actor_id=501, run_pk=cross_run, session_key='admin-session', run_secret=cross_secret,
                    attempt_id=cross_id, attempt_secret=cross_attempt_secret,
                    receipt_digest=hashlib.sha256(b'cross-unknown').digest(), input_tokens=10, output_tokens=20,
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            blocker = pool.submit(hold_unknown)
            assert held_unknown.wait(5)
            waiter = pool.submit(settle_cross)
            assert waiting_settle.wait(5)
            _must_wait(waiter)
            release_unknown.set()
            blocker.result(timeout=5)
            assert waiter.result(timeout=5) == 180
        with factory() as db:
            assert db.get(PlatformCostAttempt, cross_id).status == 'settled'
            assert db.get(PlatformCostAttempt, other_id).status == 'unknown'
        with factory.begin() as db:
            next_attempt, next_secret = owned_reserve(db, pk=cross_run, secret=cross_secret,
                                                       attempt_key=platform.new_attempt_key())
            next_id = next_attempt.id
        with factory.begin() as db:
            other_next, other_next_secret = owned_reserve(db, actor=502, pk=other_run, secret=other_secret,
                                                           attempt_key=platform.new_attempt_key())
            other_next_id = other_next.id
        held_settlement = Event()
        release_settlement = Event()
        waiting_unknown = Event()

        def hold_settlement():
            with factory() as db:
                value = owner.settle_owned_attempt(
                    db, actor_id=501, run_pk=cross_run, session_key='admin-session', run_secret=cross_secret,
                    attempt_id=next_id, attempt_secret=next_secret,
                    receipt_digest=hashlib.sha256(b'cross-settlement-first').digest(),
                    input_tokens=10, output_tokens=20,
                )
                held_settlement.set()
                assert release_settlement.wait(5)
                db.commit()
                return value

        def unknown_after_settle():
            with factory.begin() as db:
                waiting_unknown.set()
                owner.mark_owned_attempt_unknown(db, actor_id=502, run_pk=other_run, session_key='admin-session',
                                                 run_secret=other_secret, attempt_id=other_next_id,
                                                 attempt_secret=other_next_secret)

        with ThreadPoolExecutor(max_workers=2) as pool:
            blocker = pool.submit(hold_settlement)
            assert held_settlement.wait(5)
            waiter = pool.submit(unknown_after_settle)
            assert waiting_unknown.wait(5)
            _must_wait(waiter)
            release_settlement.set()
            assert blocker.result(timeout=5) == 180
            waiter.result(timeout=5)
        with factory() as db:
            assert db.get(PlatformCostAttempt, next_id).status == 'settled'
            assert db.get(PlatformCostAttempt, other_next_id).status == 'unknown'

        # A corrupted aggregate that still covers one hold must not consume
        # the other pending hold. Restore the deliberately changed test value.
        with factory.begin() as db:
            bad_first, bad_first_secret = owned_reserve(db, pk=cross_run, secret=cross_secret,
                                                         attempt_key=platform.new_attempt_key())
            bad_id, bad_held = bad_first.id, bad_first.reserved_cny_micros
        with factory.begin() as db:
            owned_reserve(db, pk=cross_run, secret=cross_secret, attempt_key=platform.new_attempt_key())
        with factory.begin() as db:
            db.query(PlatformCostCenter).filter_by(id=1).with_for_update().one()
            budget = db.query(PlatformCostBudget).filter_by(id=1).with_for_update().one()
            budget.reserved_cny_micros -= bad_held
        with factory() as db:
            with pytest.raises(BillingError, match='不一致'):
                owner.settle_owned_attempt(db, actor_id=501, run_pk=cross_run, session_key='admin-session',
                                           run_secret=cross_secret, attempt_id=bad_id, attempt_secret=bad_first_secret,
                                           receipt_digest=hashlib.sha256(b'corrupt-mysql').digest(),
                                           input_tokens=0, output_tokens=0)
            db.commit()
            assert db.get(PlatformCostAttempt, bad_id).status == 'reserved'
            assert db.query(PlatformCostSettlement).filter_by(attempt_id=bad_id).count() == 0
        with factory.begin() as db:
            db.query(PlatformCostCenter).filter_by(id=1).with_for_update().one()
            db.query(PlatformCostBudget).filter_by(id=1).with_for_update().one().reserved_cny_micros += bad_held
        with pytest.raises(RuntimeError, match='已存在'):
            PlatformCostBase.metadata.create_all(engine)
        with pytest.raises(RuntimeError, match='禁止自动删除'):
            PlatformCostBase.metadata.drop_all(engine)
        with factory() as db:
            assert db.get(AgentResponseRun, run_pk).status == 'cancelled'
            assert db.get(PlatformCostAttempt, attempt_id).status == 'unknown'
    finally:
        engine.dispose()
