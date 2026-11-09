"""MySQL lock ordering for personal Agent send authorization."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event, get_ident
from time import monotonic, sleep
from uuid import uuid4

import httpx
import pytest
import test_fresh_database_chain as chain
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker

from app.core.exceptions import ConflictError, NotFoundError
from app.models.billing_ledger import BillingCreditBatch, BillingReservation
from app.models.personal_agent import PersonalAgentRun
from app.models.system_config import SystemConfig
from app.models.user import User
from app.services import billing_ledger_service as ledger
from app.services import billing_rate_catalog_service as catalog
from app.services import personal_agent_execution_service as execution
from app.services import personal_agent_publication_service as publication
from app.services import personal_agent_service, system_config_service, user_service
from app.utils.api_resolver import ApiConfig

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


def test_mysql_distinct_personal_publications_do_not_deadlock_on_missing_keys(fresh_chain):
    from threading import Barrier

    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    suffix = uuid4().hex[:16]
    with factory.begin() as db:
        users = [User(username=f"pa-publish-gap-{suffix}-{index}", password="x", role="user", status=1)
                 for index in range(2)]
        db.add_all(users)
        db.flush()
        user_ids = [int(user.id) for user in users]
    agent_ids = []
    for user_id in user_ids:
        with factory() as db:
            agent_ids.append(personal_agent_service.create(db, user_id, "Agent", "提示", f"create-{suffix}").id)
    barrier = Barrier(2, timeout=10)

    def after(_conn, _cursor, statement, _params, _ctx, _many):
        sql = statement.lower()
        if "from personal_agent_publication" in sql and "request_key" in sql and "where" in sql:
            barrier.wait()

    def publish(pair):
        user_id, agent_id = pair
        with factory() as db:
            return publication.submit(db, owner_id=user_id, agent_id=agent_id,
                                      expected_version=1, request_key=f"publish-{suffix}").id

    event.listen(fresh_chain, "after_cursor_execute", after)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(publish, zip(user_ids, agent_ids)))
    finally:
        event.remove(fresh_chain, "after_cursor_execute", after)
    assert len(set(ids)) == 2


@pytest.mark.parametrize("same_key", [True, False])
def test_mysql_old_snapshot_personal_publication_is_idempotent_and_version_unique(fresh_chain, same_key):
    from threading import Barrier

    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    suffix = uuid4().hex[:16]
    with factory.begin() as db:
        user = User(username=f"pa-publish-old-{suffix}", password="x", role="user", status=1)
        db.add(user)
        db.flush()
        user_id = int(user.id)
    with factory() as db:
        agent_id = personal_agent_service.create(db, user_id, "Agent", "提示", f"create-{suffix}").id
    barrier = Barrier(2, timeout=10)

    def publish(index):
        with factory() as db:
            # Establish a consistent read before either transaction locks the owner.
            assert db.query(User).filter_by(id=user_id).one().status == 1
            barrier.wait()
            try:
                key = f"publish-{suffix}" if same_key else f"publish-{suffix}-{index}"
                return publication.submit(db, owner_id=user_id, agent_id=agent_id,
                                          expected_version=1, request_key=key).id
            except ConflictError:
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, range(2)))
    if same_key:
        assert len(set(results)) == 1 and results[0] != "conflict"
    else:
        assert results.count("conflict") == 1
    with factory() as db:
        rows = db.query(publication.PersonalAgentPublication).filter_by(agent_id=agent_id).all()
        assert len(rows) == 1 and rows[0].status == "pending"


@pytest.mark.parametrize("same_key", [True, False])
def test_mysql_old_snapshot_sees_current_publication_decision(fresh_chain, same_key):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    suffix = uuid4().hex[:16]
    with factory.begin() as db:
        user = User(username=f"pa-publish-stale-{suffix}", password="x", role="user", status=1)
        db.add(user)
        db.flush()
        user_id = int(user.id)
        admin = db.query(User).filter_by(username="admin", role="super_admin").one()
        admin_id, token_version = int(admin.id), int(admin.token_version or 0)
    with factory() as db:
        agent_id = personal_agent_service.create(db, user_id, "Agent", "提示", f"create-{suffix}").id
    with factory() as db:
        pub_id = publication.submit(db, owner_id=user_id, agent_id=agent_id,
                                    expected_version=1, request_key=f"publish-{suffix}").id
    with factory() as stale:
        assert stale.query(publication.PersonalAgentPublication).filter_by(id=pub_id).one().status == "pending"
        with factory() as current:
            publication.decide(current, actor_id=admin_id, token_version=token_version,
                               publication_id=pub_id, approve=False, reason="拒绝")
        key = f"publish-{suffix}" if same_key else f"retry-{suffix}"
        result = publication.submit(stale, owner_id=user_id, agent_id=agent_id, expected_version=1, request_key=key)
        if same_key:
            assert result.id == pub_id and result.status == "rejected"
        else:
            assert result.id != pub_id and result.status == "pending"


def test_mysql_withdraw_refreshes_preloaded_publication_after_approval(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    suffix = uuid4().hex[:16]
    with factory.begin() as db:
        assert db.execute(text("SELECT @@transaction_isolation")).scalar_one() == "REPEATABLE-READ"
        user = User(username=f"pa-withdraw-stale-{suffix}", password="x", role="user", status=1)
        db.add(user)
        db.flush()
        user_id = int(user.id)
        admin = db.query(User).filter_by(username="admin", role="super_admin").one()
        admin_id, token_version = int(admin.id), int(admin.token_version or 0)
    with factory() as db:
        agent_id = personal_agent_service.create(db, user_id, "Agent", "提示", f"create-{suffix}").id
    with factory() as db:
        pub_id = publication.submit(db, owner_id=user_id, agent_id=agent_id,
                                    expected_version=1, request_key=f"publish-{suffix}").id
    with factory() as stale:
        held = stale.query(publication.PersonalAgentPublication).filter_by(id=pub_id).one()
        assert held.status == "pending"
        with factory() as current:
            publication.decide(current, actor_id=admin_id, token_version=token_version,
                               publication_id=pub_id, approve=True, reason="通过")
        with pytest.raises(ConflictError):
            publication.withdraw(stale, owner_id=user_id, publication_id=pub_id)
        assert held.status == "approved"
    with factory() as db:
        assert db.query(publication.PersonalAgentPublication).filter_by(id=pub_id).one().status == "approved"


def _setup(engine, monkeypatch):
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    suffix = uuid4().hex[:16]
    with factory.begin() as db:
        user = User(username=f"pa-owner-{suffix}", password="x", role="user", status=1, token_version=0)
        db.add(user)
        config = db.query(SystemConfig).filter_by(config_key="llm_provider").first()
        if config is None:
            config = SystemConfig(config_key="llm_provider", config_value='{"active":false}')
            db.add(config)
        else:
            config.config_value = '{"active":false}'
        db.flush()
        user_id = int(user.id)
        wallet = ledger.get_or_create_wallet(db, "user", user_id)
        ledger.grant(db, wallet_id=wallet.id, grant_key=f"pa-{suffix}", kind="purchase", amount_milli=2_000_000)
    with factory() as db:
        agent_id = personal_agent_service.create(db, user_id, "助手", "简洁回答", f"agent-{suffix}").id
    monkeypatch.setattr(execution.settings, "billing_commercial_metering_ready", True)
    monkeypatch.setattr(execution, "resolve_api_config", lambda _db: ApiConfig(
        api_key="sk-test", base_url="https://api.deepseek.com", model="deepseek-flash", source="system",
    ))
    # Migration 105 intentionally leaves old catalog cost facts disabled.  The
    # eligibility check is a test stub; all ledger and DB locks are real.
    monkeypatch.setattr(catalog, "_require_current_model_facts", lambda *_a, **_kw: None)
    sends = []
    monkeypatch.setattr(execution, "_send_once", lambda _config, _payload: (
        sends.append(1) or {"choices": [{"finish_reason": "stop", "message": {"content": "好"}}],
                            "usage": {"prompt_tokens": 12, "completion_tokens": 7}}
    ))
    return factory, user_id, agent_id, sends


def _observe_wait(engine, contender_thread, expected_table, hit):
    observed = []
    def before(conn, _cursor, statement, _params, _ctx, _many):
        if (contender_thread and get_ident() == contender_thread[0]
                and expected_table in statement.lower() and
                ("FOR UPDATE" in statement.upper() or statement.lstrip().upper().startswith("UPDATE"))):
            if not observed:
                observed.append(conn.connection.driver_connection.thread_id())
                hit.set()
    return before, observed


def _wait_for_lock(engine, observed):
    deadline = monotonic() + 5
    while monotonic() < deadline:
        with engine.connect() as conn:
            waiting = conn.execute(text(
                "SELECT COUNT(*) FROM performance_schema.data_lock_waits w "
                "JOIN performance_schema.threads t ON t.thread_id=w.requesting_thread_id "
                "WHERE t.processlist_id=:id"
            ), {"id": observed[0]}).scalar_one()
        if waiting:
            return
        sleep(0.02)
    pytest.fail("未观察到真实 MySQL 锁等待")


@pytest.mark.parametrize("winner", ["config", "run"])
def test_mysql_config_update_serializes_with_send_marker(fresh_chain, monkeypatch, winner):
    factory, user_id, agent_id, sends = _setup(fresh_chain, monkeypatch)
    locked, release, hit = Event(), Event(), Event()
    worker_thread, contender_thread = [], []
    before, observed = _observe_wait(fresh_chain, contender_thread, "system_config", hit)

    def after(_conn, _cursor, statement, _params, _ctx, _many):
        if worker_thread and get_ident() == worker_thread[0] and "system_config" in statement.lower():
            upper = statement.upper()
            if (winner == "run" and "FOR UPDATE" in upper) or (winner == "config" and upper.startswith("UPDATE")):
                locked.set()
                assert release.wait(10)

    def run():
        try:
            return execution.execute(factory, owner_id=user_id, agent_id=agent_id, version_number=1,
                                     request_key="test-run", input_text="你好")
        except ledger.BillingError:
            return "config-rejected"

    def change_config():
        with factory() as db:
            system_config_service._set_raw(db, "llm_provider", '{"active":false,"changed":true}')
        return "changed"

    first_fn, second_fn = (change_config, run) if winner == "config" else (run, change_config)
    def first():
        worker_thread.append(get_ident())
        return first_fn()
    def second():
        contender_thread.append(get_ident())
        return second_fn()
    event.listen(fresh_chain, "before_cursor_execute", before)
    event.listen(fresh_chain, "after_cursor_execute", after)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(first)
            assert locked.wait(10)
            try:
                two = pool.submit(second)
                assert hit.wait(10)
                _wait_for_lock(fresh_chain, observed)
            finally:
                release.set()
            first_result, second_result = one.result(timeout=20), two.result(timeout=20)
    finally:
        event.remove(fresh_chain, "before_cursor_execute", before)
        event.remove(fresh_chain, "after_cursor_execute", after)
    if winner == "config":
        assert first_result == "changed" and second_result == "config-rejected" and not sends
        with factory() as db:
            assert db.query(PersonalAgentRun).filter_by(owner_id=user_id).count() == 0
            assert db.query(BillingReservation).filter_by(user_id=user_id).count() == 0
    else:
        assert first_result["status"] == "succeeded" and second_result == "changed" and sends == [1]
        with factory() as db:
            assert db.query(BillingReservation).filter_by(user_id=user_id).one().status == "settled"


@pytest.mark.parametrize("winner", ["disable", "run"])
def test_mysql_user_disable_serializes_with_send_marker(fresh_chain, monkeypatch, winner):
    factory, user_id, agent_id, sends = _setup(fresh_chain, monkeypatch)
    locked, release, hit, config_locked, disabled = Event(), Event(), Event(), Event(), Event()
    worker_thread, contender_thread = [], []
    before, observed = _observe_wait(fresh_chain, contender_thread, "user", hit)

    def after(_conn, _cursor, statement, _params, _ctx, _many):
        if worker_thread and get_ident() == worker_thread[0]:
            upper = statement.upper()
            if "SYSTEM_CONFIG" in upper and "FOR UPDATE" in upper:
                config_locked.set()
            is_user_lock = "FOR UPDATE" in upper and ("FROM USER" in upper or "FROM `USER`" in upper)
            if is_user_lock and (winner == "disable" or config_locked.is_set()):
                locked.set()
                assert release.wait(10)

    if winner == "run":
        def send_after_disable(_config, _payload):
            sends.append(1)
            assert disabled.wait(10)
            return {"choices": [{"finish_reason": "stop", "message": {"content": "好"}}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 7}}
        monkeypatch.setattr(execution, "_send_once", send_after_disable)

    def run():
        try:
            return execution.execute(factory, owner_id=user_id, agent_id=agent_id, version_number=1,
                                     request_key="test-run", input_text="你好", token_version=0)
        except NotFoundError:
            return "user-unavailable"

    def disable():
        with factory() as db:
            user_service.toggle_status(db, user_id, 0)
        disabled.set()
        return "disabled"

    first_fn, second_fn = (disable, run) if winner == "disable" else (run, disable)
    def first():
        worker_thread.append(get_ident())
        return first_fn()
    def second():
        contender_thread.append(get_ident())
        return second_fn()
    event.listen(fresh_chain, "before_cursor_execute", before)
    event.listen(fresh_chain, "after_cursor_execute", after)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(first)
            assert locked.wait(10)
            try:
                two = pool.submit(second)
                assert hit.wait(10)
                _wait_for_lock(fresh_chain, observed)
            finally:
                release.set()
            one.result(timeout=20)
            two.result(timeout=20)
    finally:
        event.remove(fresh_chain, "before_cursor_execute", before)
        event.remove(fresh_chain, "after_cursor_execute", after)
    with factory() as db:
        reservations = db.query(BillingReservation).filter_by(user_id=user_id).all()
        if winner == "disable":
            assert not sends and not reservations
        else:
            assert len(sends) == 1 and len(reservations) == 1
            assert reservations[0].status == "settled"
            run = db.query(PersonalAgentRun).filter_by(owner_id=user_id).one()
            assert run.status == "result_unavailable" and run.result_text is None
        if reservations:
            batch = db.query(BillingCreditBatch).filter_by(wallet_id=reservations[0].personal_wallet_id).one()
            assert batch.reserved_milli == 0


@pytest.mark.parametrize("same_key", [True, False])
def test_mysql_overlapping_requests_send_once_and_settle_one_hold(fresh_chain, monkeypatch, same_key):
    real_send_once = execution._send_once
    factory, user_id, agent_id, _ = _setup(fresh_chain, monkeypatch)
    monkeypatch.setattr(execution, "_send_once", real_send_once)
    client_cls = httpx.AsyncClient
    permit_locked, release_permit, send_started, release_response, hit = (Event() for _ in range(5))
    first_thread, second_thread, sends = [], [], []
    before, observed = _observe_wait(fresh_chain, second_thread, "system_config", hit)

    def after(_conn, _cursor, statement, _params, _ctx, _many):
        if (first_thread and get_ident() == first_thread[0] and "system_config" in statement.lower()
                and "FOR UPDATE" in statement.upper() and not permit_locked.is_set()):
            permit_locked.set()
            assert release_permit.wait(10)

    async def handler(request):
        sends.append((request.method, str(request.url), request.content))
        send_started.set()
        assert await asyncio.to_thread(release_response.wait, 10)
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"content": "好"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 7},
        })

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(execution.httpx, "AsyncClient", lambda **kw: client_cls(transport=transport, **kw))

    def request(first):
        (first_thread if first else second_thread).append(get_ident())
        try:
            return execution.execute(factory, owner_id=user_id, agent_id=agent_id, version_number=1,
                                     request_key="same" if first or same_key else "other", input_text="你好")
        except ConflictError:
            return "conflict"

    event.listen(fresh_chain, "before_cursor_execute", before)
    event.listen(fresh_chain, "after_cursor_execute", after)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(request, True)
            assert permit_locked.wait(10)
            second = pool.submit(request, False)
            try:
                assert hit.wait(10)
                _wait_for_lock(fresh_chain, observed)
            finally:
                release_permit.set()
            try:
                assert send_started.wait(10)
                second_result = second.result(timeout=20)
            finally:
                release_response.set()
            first_result = first.result(timeout=20)
    finally:
        release_permit.set()
        release_response.set()
        event.remove(fresh_chain, "before_cursor_execute", before)
        event.remove(fresh_chain, "after_cursor_execute", after)
    assert first_result["status"] == "succeeded"
    if same_key:
        assert second_result["id"] == first_result["id"] and second_result["status"] == "running"
    else:
        assert second_result == "conflict"
    assert len(sends) == 1 and sends[0][0:2] == ("POST", "https://api.deepseek.com/v1/chat/completions")
    with factory() as db:
        runs = db.query(PersonalAgentRun).filter_by(owner_id=user_id).all()
        reservations = db.query(BillingReservation).filter_by(user_id=user_id).all()
        assert len(runs) == len(reservations) == 1
        assert runs[0].status == "succeeded" and reservations[0].status == "settled"
        assert (reservations[0].input_tokens, reservations[0].output_tokens) == (12, 7)
        batch = db.query(BillingCreditBatch).filter_by(wallet_id=reservations[0].personal_wallet_id).one()
        assert batch.reserved_milli == 0 and batch.remaining_milli == 2_000_000 - reservations[0].actual_milli


@pytest.mark.parametrize("winner", ["revoke", "run"])
def test_mysql_publication_revoke_serializes_with_send_permit(fresh_chain, monkeypatch, winner):
    factory, author_id, agent_id, sends = _setup(fresh_chain, monkeypatch)
    with factory.begin() as db:
        admin = db.query(User).filter_by(username="admin", role="super_admin").one()
        admin_id, token_version = admin.id, int(admin.token_version or 0)
        caller = User(username=f"pa-caller-{uuid4().hex[:16]}", password="x", role="user", status=1)
        db.add(caller)
        db.flush()
        caller_id = caller.id
        wallet = ledger.get_or_create_wallet(db, "user", caller_id)
        ledger.grant(db, wallet_id=wallet.id, grant_key=f"pa-call-{uuid4().hex}",
                     kind="purchase", amount_milli=2_000_000)
    with factory() as db:
        pub_id = publication.submit(db, owner_id=author_id, agent_id=agent_id,
                                    expected_version=1, request_key="publish").id
    with factory() as db:
        publication.decide(db, actor_id=admin_id, token_version=token_version,
                           publication_id=pub_id, approve=True, reason="ok")
    locked, release, hit, revoked = Event(), Event(), Event(), Event()
    first_thread, second_thread = [], []
    before, observed = _observe_wait(fresh_chain, second_thread, "personal_agent_publication", hit)

    def after(_conn, _cursor, statement, _params, _ctx, _many):
        if (first_thread and get_ident() == first_thread[0]
                and "personal_agent_publication" in statement.lower()
                and "FOR UPDATE" in statement.upper() and not locked.is_set()):
            locked.set()
            assert release.wait(10)

    if winner == "run":
        def send_after_revoke(_config, _payload):
            sends.append(1)
            assert revoked.wait(10)
            return {"choices": [{"finish_reason": "stop", "message": {"content": "迟到"}}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 7}}
        monkeypatch.setattr(execution, "_send_once", send_after_revoke)

    def run():
        try:
            return execution.execute_publication(factory, owner_id=caller_id, publication_id=pub_id,
                                                 request_key="call", input_text="你好")
        except NotFoundError:
            return "not-available"

    def revoke():
        with factory() as db:
            publication.revoke(db, actor_id=admin_id, token_version=token_version,
                               publication_id=pub_id, reason="remove")
        revoked.set()
        return "revoked"

    first_fn, second_fn = (revoke, run) if winner == "revoke" else (run, revoke)
    def first():
        first_thread.append(get_ident())
        return first_fn()
    def second():
        second_thread.append(get_ident())
        return second_fn()
    event.listen(fresh_chain, "before_cursor_execute", before)
    event.listen(fresh_chain, "after_cursor_execute", after)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(first)
            assert locked.wait(10)
            try:
                second_future = pool.submit(second)
                assert hit.wait(10)
                _wait_for_lock(fresh_chain, observed)
            finally:
                release.set()
            first_result = first_future.result(timeout=20)
            second_result = second_future.result(timeout=20)
    finally:
        event.remove(fresh_chain, "before_cursor_execute", before)
        event.remove(fresh_chain, "after_cursor_execute", after)
    with factory() as db:
        reservations = db.query(BillingReservation).filter_by(user_id=caller_id).all()
        if winner == "revoke":
            assert first_result == "revoked" and second_result == "not-available" and not sends and not reservations
        else:
            assert second_result == "revoked" and first_result["status"] == "result_unavailable"
            assert first_result["output"] is None and len(sends) == len(reservations) == 1
            assert reservations[0].status == "settled"
