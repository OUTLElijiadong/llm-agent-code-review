"""Real MySQL lock-order regressions for team authorization and cancellation."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event, get_ident
from time import monotonic, sleep

import pytest
import test_fresh_database_chain as chain
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker
from test_mysql_enterprise_group_shared_lock import _install_live_snapshots, _ready, _seed

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.models.enterprise_agent_team import EnterpriseAgentTeam
from app.models.enterprise_chat import EnterpriseAssistantRun
from app.models.enterprise_org import Enterprise, EnterpriseGroup
from app.models.user import User
from app.services import auth_service, user_service
from app.services import enterprise_agent_team_runtime_service as runtime
from app.services import enterprise_assistant_runtime_service as parent_runtime
from app.services import enterprise_org_service as org

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


def _pause_after_run_lock(engine, worker_id, held, release):
    def after(_conn, _cursor, sql, _params, _context, _many):
        if (worker_id and get_ident() == worker_id[0] and not held.is_set()
                and 'FROM ENTERPRISE_ASSISTANT_RUN' in sql.upper() and 'FOR UPDATE' in sql.upper()):
            held.set()
            assert release.wait(15)
    event.listen(engine, 'after_cursor_execute', after)
    return after


def test_group_revocation_wins_before_team_membership_lock(fresh_chain, monkeypatch):
    """A team holding the parent run must not hold the member before company authorization."""
    real_snapshot = parent_runtime._live_role_snapshot
    _ready(monkeypatch)
    monkeypatch.setattr(parent_runtime, '_live_role_snapshot', real_snapshot)
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, group, owner, users, tasks, teams = _seed(factory)
    _install_live_snapshots(factory, company, teams, legacy=True)
    held, release, revoked = Event(), Event(), Event()
    worker_id = []
    listener = _pause_after_run_lock(fresh_chain, worker_id, held, release)

    def team():
        worker_id.append(get_ident())
        with factory.begin() as db:
            try:
                runtime._lock_live_context(db, enterprise_id=company, task_id=tasks[0], lock_company=False)
            except (ForbiddenError, NotFoundError):
                return 'denied'
            return 'allowed'

    def revoke():
        with factory.begin() as db:
            org.remove_group_member(db, company, group, owner, users[0])
        revoked.set()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(team)
            assert held.wait(15)
            b = pool.submit(revoke)
            # The writer cannot be waiting on a member lock acquired before
            # the run; it must finish while the reader is still paused.
            completed = revoked.wait(3)
            release.set()
            b.result(timeout=20)
            result = a.result(timeout=20)
            assert completed, '组撤权等待团队在父运行锁之前持有的成员锁'
            assert result == 'denied'
    finally:
        release.set()
        event.remove(fresh_chain, 'after_cursor_execute', listener)


def test_team_cancel_and_worker_take_parent_before_company(fresh_chain, monkeypatch):
    """The cancellation cannot hold the company while waiting for a parent run."""
    real_snapshot = parent_runtime._live_role_snapshot
    _ready(monkeypatch)
    monkeypatch.setattr(parent_runtime, '_live_role_snapshot', real_snapshot)
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, _group, owner, _users, tasks, teams = _seed(factory)
    _install_live_snapshots(factory, company, teams, legacy=True)
    held, release, started = Event(), Event(), Event()
    worker_id, cancel_cid = [], []
    listener = _pause_after_run_lock(fresh_chain, worker_id, held, release)

    def team():
        worker_id.append(get_ident())
        with factory.begin() as db:
            context = runtime._lock_live_context(db, enterprise_id=company, task_id=tasks[0], lock_company=False)
            return context['team'].id

    def cancelling():
        def before(conn, _cursor, sql, _params, _context, _many):
            if get_ident() == cancel_thread[0] and 'FOR UPDATE' in sql.upper() and not cancel_cid:
                cancel_cid.append(conn.connection.driver_connection.thread_id())
                started.set()
        cancel_thread.append(get_ident())
        event.listen(fresh_chain, 'before_cursor_execute', before)
        try:
            return runtime.cancel_team(factory, enterprise_id=company, team_id=teams[0], actor_user_id=owner)
        finally:
            event.remove(fresh_chain, 'before_cursor_execute', before)

    cancel_thread = []
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(team)
            assert held.wait(15)
            b = pool.submit(cancelling)
            assert started.wait(15)
            deadline = monotonic() + 8
            waiting_table = None
            while monotonic() < deadline:
                with fresh_chain.connect() as observer:
                    waiting_table = observer.execute(text(
                        'SELECT l.OBJECT_NAME FROM performance_schema.data_lock_waits w '
                        'JOIN performance_schema.threads t ON t.THREAD_ID=w.REQUESTING_THREAD_ID '
                        'JOIN performance_schema.data_locks l ON l.ENGINE_LOCK_ID=w.REQUESTING_ENGINE_LOCK_ID '
                        'WHERE t.PROCESSLIST_ID=:cid LIMIT 1'
                    ), {'cid': cancel_cid[0]}).scalar_one_or_none()
                if waiting_table:
                    break
                sleep(0.05)
            release.set()
            assert a.result(timeout=20) == teams[0]
            assert b.result(timeout=20)['status'] == 'cancelled'
            assert waiting_table == 'enterprise_assistant_run'
    finally:
        release.set()
        event.remove(fresh_chain, 'after_cursor_execute', listener)


def test_real_retry_and_team_do_not_invert_parent_company_locks(fresh_chain, monkeypatch):
    """The real retry must wait for the parent, not hold company and block the team."""
    real_snapshot = parent_runtime._live_role_snapshot
    _ready(monkeypatch)
    monkeypatch.setattr(parent_runtime, '_live_role_snapshot', real_snapshot)
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, _group, _owner, users, tasks, teams = _seed(factory)
    _install_live_snapshots(factory, company, teams, legacy=True)
    with factory() as db:
        team_row = db.get(EnterpriseAgentTeam, teams[0])
        run = db.get(EnterpriseAssistantRun, team_row.assistant_run_id)
        conversation, public_id = int(run.conversation_id), str(run.public_id)
    held, release, started = Event(), Event(), Event()
    worker_id, retry_thread, retry_cid = [], [], []
    listener = _pause_after_run_lock(fresh_chain, worker_id, held, release)

    def team():
        worker_id.append(get_ident())
        with factory.begin() as db:
            context = runtime._lock_live_context(db, enterprise_id=company, task_id=tasks[0], lock_company=False)
            return context['team'].id

    def retry():
        def before(conn, _cursor, sql, _params, _context, _many):
            if (get_ident() == retry_thread[0] and 'FOR UPDATE' in sql.upper() and not retry_cid):
                retry_cid.append(conn.connection.driver_connection.thread_id())
                started.set()
        retry_thread.append(get_ident())
        event.listen(fresh_chain, 'before_cursor_execute', before)
        try:
            with factory() as db:
                try:
                    asyncio.run(parent_runtime.retry_run(
                        db, enterprise_id=company, user_id=users[0], conversation_id=conversation,
                        run_public_id=public_id,
                    ))
                except ConflictError:
                    # A completed parent cannot be retried; the normal refusal
                    # rather than a deadlock is the expected outcome.
                    return 'not_retryable'
        finally:
            event.remove(fresh_chain, 'before_cursor_execute', before)
        return 'unexpected'

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(team)
            assert held.wait(15)
            b = pool.submit(retry)
            assert started.wait(15)
            deadline, waiting_table = monotonic() + 8, None
            while monotonic() < deadline:
                with fresh_chain.connect() as observer:
                    waiting_table = observer.execute(text(
                        'SELECT l.OBJECT_NAME FROM performance_schema.data_lock_waits w '
                        'JOIN performance_schema.threads t ON t.THREAD_ID=w.REQUESTING_THREAD_ID '
                        'JOIN performance_schema.data_locks l ON l.ENGINE_LOCK_ID=w.REQUESTING_ENGINE_LOCK_ID '
                        'WHERE t.PROCESSLIST_ID=:cid LIMIT 1'
                    ), {'cid': retry_cid[0]}).scalar_one_or_none()
                if waiting_table:
                    break
                sleep(0.05)
            release.set()
            assert a.result(timeout=20) == teams[0]
            assert b.result(timeout=20) == 'not_retryable'
            assert waiting_table == 'enterprise_assistant_run'
    finally:
        release.set()
        event.remove(fresh_chain, 'after_cursor_execute', listener)


def test_real_retry_first_does_not_deadlock_team(fresh_chain, monkeypatch):
    """A retry holding the parent first finishes before the team takes it."""
    real_snapshot = parent_runtime._live_role_snapshot
    _ready(monkeypatch)
    monkeypatch.setattr(parent_runtime, '_live_role_snapshot', real_snapshot)
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, _group, _owner, users, tasks, teams = _seed(factory)
    _install_live_snapshots(factory, company, teams, legacy=True)
    with factory() as db:
        team_row = db.get(EnterpriseAgentTeam, teams[0])
        run = db.get(EnterpriseAssistantRun, team_row.assistant_run_id)
        conversation, public_id = int(run.conversation_id), str(run.public_id)
    held, release, started = Event(), Event(), Event()
    retry_thread, worker_thread, worker_cid = [], [], []
    listener = _pause_after_run_lock(fresh_chain, retry_thread, held, release)

    def retry():
        retry_thread.append(get_ident())
        with factory() as db:
            try:
                asyncio.run(parent_runtime.retry_run(
                    db, enterprise_id=company, user_id=users[0], conversation_id=conversation,
                    run_public_id=public_id,
                ))
            except ConflictError:
                return 'not_retryable'
        return 'unexpected'

    def before(conn, _cursor, sql, _params, _context, _many):
        if (worker_thread and get_ident() == worker_thread[0]
                and 'FROM ENTERPRISE_ASSISTANT_RUN' in sql.upper() and 'FOR UPDATE' in sql.upper()
                and not worker_cid):
            worker_cid.append(conn.connection.driver_connection.thread_id())
            started.set()

    def team():
        worker_thread.append(get_ident())
        with factory.begin() as db:
            context = runtime._lock_live_context(db, enterprise_id=company, task_id=tasks[0], lock_company=False)
            return context['team'].id

    event.listen(fresh_chain, 'before_cursor_execute', before)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(retry)
            assert held.wait(15)
            b = pool.submit(team)
            assert started.wait(15)
            deadline, waiting_table = monotonic() + 8, None
            while monotonic() < deadline:
                with fresh_chain.connect() as observer:
                    waiting_table = observer.execute(text(
                        'SELECT l.OBJECT_NAME FROM performance_schema.data_lock_waits w '
                        'JOIN performance_schema.threads t ON t.THREAD_ID=w.REQUESTING_THREAD_ID '
                        'JOIN performance_schema.data_locks l ON l.ENGINE_LOCK_ID=w.REQUESTING_ENGINE_LOCK_ID '
                        'WHERE t.PROCESSLIST_ID=:cid LIMIT 1'
                    ), {'cid': worker_cid[0]}).scalar_one_or_none()
                if waiting_table:
                    break
                sleep(0.05)
            release.set()
            assert a.result(timeout=20) == 'not_retryable'
            assert b.result(timeout=20) == teams[0]
            assert waiting_table == 'enterprise_assistant_run'
    finally:
        release.set()
        event.remove(fresh_chain, 'after_cursor_execute', listener)
        event.remove(fresh_chain, 'before_cursor_execute', before)


@pytest.mark.parametrize('change', [
    'remove_member', 'disable_company', 'disable_user', 'disable_group', 'logout_user',
])
def test_team_first_fences_revocation_and_next_execution_rechecks(fresh_chain, monkeypatch, change):
    """A committed access change is checked before the next team operation."""
    real_snapshot = parent_runtime._live_role_snapshot
    _ready(monkeypatch)
    monkeypatch.setattr(parent_runtime, '_live_role_snapshot', real_snapshot)
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, group, owner, users, tasks, teams = _seed(factory)
    _install_live_snapshots(factory, company, teams, legacy=True)
    held, release, started, finished = Event(), Event(), Event(), Event()
    writer_cid = []

    def team():
        with factory.begin() as db:
            runtime._lock_live_context(db, enterprise_id=company, task_id=tasks[0], lock_company=False)
            held.set()
            assert release.wait(15)

    def change_authority():
        with factory.begin() as db:
            writer_cid.append(db.execute(text('SELECT CONNECTION_ID()')).scalar_one())
            started.set()
            if change == 'remove_member':
                org.remove_group_member(db, company, group, owner, users[0])
            elif change == 'disable_company':
                db.query(Enterprise).filter_by(id=company).with_for_update().one().status = 'suspended'
            elif change == 'disable_user':
                user_service.toggle_status(db, users[0], 0)
            elif change == 'logout_user':
                auth_service.logout(db, db.get(User, users[0]))
            else:
                db.query(EnterpriseGroup).filter_by(id=group).with_for_update().one().status = 'inactive'
        finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(team)
        try:
            assert held.wait(15)
            b = pool.submit(change_authority)
            assert started.wait(15)
            deadline, waiting_table = monotonic() + 8, None
            while monotonic() < deadline:
                with fresh_chain.connect() as observer:
                    waiting_table = observer.execute(text(
                        'SELECT l.OBJECT_NAME FROM performance_schema.data_lock_waits w '
                        'JOIN performance_schema.threads t ON t.THREAD_ID=w.REQUESTING_THREAD_ID '
                        'JOIN performance_schema.data_locks l ON l.ENGINE_LOCK_ID=w.REQUESTING_ENGINE_LOCK_ID '
                        'WHERE t.PROCESSLIST_ID=:cid LIMIT 1'
                    ), {'cid': writer_cid[0]}).scalar_one_or_none()
                if waiting_table:
                    break
                sleep(0.05)
            assert waiting_table in {'enterprise', 'enterprise_member', 'enterprise_group', 'user'}
            assert not finished.is_set()
        finally:
            release.set()
        a.result(timeout=20)
        b.result(timeout=20)
    with factory.begin() as db:
        if change == 'logout_user':
            # Legacy v0 background work is bound to the account and current
            # roles, not to the login JWT. Logout alone does not stop it.
            assert runtime._lock_live_context(
                db, enterprise_id=company, task_id=tasks[0], lock_company=False,
            )['team'].id == teams[0]
        else:
            with pytest.raises((ForbiddenError, NotFoundError)):
                runtime._lock_live_context(db, enterprise_id=company, task_id=tasks[0], lock_company=False)
