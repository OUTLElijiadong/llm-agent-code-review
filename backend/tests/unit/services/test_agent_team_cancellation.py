"""团队租约取消：独立事务观察、async 请求清理及真实用量保留。"""

import asyncio
import inspect
from datetime import datetime, timedelta, timezone
from threading import Event, Thread
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base
from app.models.agent_team import AgentTeam, AgentTeamEvent, AgentTeamMember, AgentTeamTask
from app.models.ai_call_log import AiCallLog
from app.models.user import User
from app.services import agent_mesh_dispatcher, agent_team_dispatcher, agent_team_service
from app.services import agent_scheduler_runtime as runtime
from app.services.ai_usage_context import UsageAccountingError, current_attribution, record_usage_attempt


@pytest.fixture
def team_execution(tmp_path, monkeypatch):
    # 文件库和独立连接模拟另一 worker 提交取消，避免共享内存连接掩盖事务隔离问题。
    engine = create_engine(f"sqlite:///{tmp_path / 'team-cancellation.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        owner = User(username="cancel-owner", password="x", role="user", status=1)
        db.add(owner)
        db.flush()
        team = AgentTeam(
            user_id=owner.id, surface="user", session_key="cancel-session", title="取消调用",
            objective="观察运行中取消", status="running", trace_id="cancel-trace",
        )
        db.add(team)
        db.flush()
        member = AgentTeamMember(
            team_id=team.id, member_key="reader", display_name="读取成员",
            address="agent:project_analyzer", kind="builtin", status="running",
        )
        db.add(member)
        db.flush()
        task = AgentTeamTask(
            team_id=team.id, member_id=member.id, task_key="read", title="读取输入", instructions="分析输入",
            input_json='{}', status="running", attempt_count=1, max_attempts=3, lease_token="original-lease",
            lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        db.add(task)
        db.commit()
        team_id, task_id, owner_id = team.id, task.id, owner.id
        claimed = {
            "task_id": task.id, "member_id": member.id, "address": member.address,
            "lease_token": task.lease_token, "attempt_count": 1, "input": {},
        }
    monkeypatch.setattr(agent_team_dispatcher, "SessionLocal", factory)
    monkeypatch.setattr(agent_team_dispatcher, "_owner_access_error", lambda *_a, **_k: "")
    monkeypatch.setattr(agent_team_service, "_cleanup_team_runtime_resources", lambda *_a, **_k: None)
    yield factory, team_id, task_id, owner_id, claimed
    engine.dispose()


def _revoke(team_execution, reason):
    factory, team_id, task_id, owner_id, _claimed = team_execution
    with factory() as external_db:
        task = external_db.get(AgentTeamTask, task_id)
        if reason == "team_cancel":
            agent_team_service.cancel_team(
                external_db, external_db.get(User, owner_id), team_id, deliver_terminal_notification=False,
            )
            return
        if reason == "task_cancel":
            task.status, task.lease_token = "cancelled", None
        elif reason == "lease_replace":
            task.lease_token = "new-worker-lease"
        elif reason == "lease_expire":
            task.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        elif reason == "deadline":
            external_db.get(AgentTeam, team_id).deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        elif reason == "member_change":
            external_db.get(AgentTeamMember, task.member_id).address = "agent:language_detector"
        else:
            raise AssertionError(reason)
        external_db.commit()


_REVOKE_REASONS = ("team_cancel", "task_cancel", "lease_replace", "lease_expire", "deadline", "member_change")


@pytest.mark.parametrize("reason", _REVOKE_REASONS)
def test_inactive_lease_drops_late_result_before_supervisor_review(team_execution, monkeypatch, reason):
    factory, team_id, task_id, _owner_id, claimed = team_execution

    def handle(*_args, **_kwargs):
        _revoke(team_execution, reason)
        return None, {"status": "completed", "summary": "late model output", "evidence": [{"files": 1}]}

    monkeypatch.setattr(agent_mesh_dispatcher, "_handle", handle)
    assert agent_team_dispatcher._execute_claimed(team_id, claimed) == {"success": False}
    with factory() as db:
        task = db.get(AgentTeamTask, task_id)
        assert task.result_json == "{}"
        assert task.next_attempt_at is None
        assert db.query(AgentTeamEvent).filter_by(team_id=team_id, event_type="supervisor.after").count() == 0
        assert db.query(AgentTeamEvent).filter_by(team_id=team_id, event_type="task.retry_queued").count() == 0
        if reason == "team_cancel":
            assert db.get(AgentTeam, team_id).status == "cancelled"
            assert task.status == "cancelled"
            assert agent_team_service.claim_next_task(db, team_id) is None
            assert agent_team_service.recover_expired_leases(db) == 0
        elif reason == "lease_replace":
            assert task.status == "running"
            assert task.lease_token == "new-worker-lease"


@pytest.mark.parametrize("reason", _REVOKE_REASONS)
@pytest.mark.parametrize("usage", [None, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                                    {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}])
def test_lease_scope_cancels_inflight_http_and_preserves_usage(team_execution, monkeypatch, reason, usage):
    continued = []
    factory, team_id, task_id, owner_id, claimed = team_execution
    started, stream_closed, finished = Event(), Event(), Event()
    outcome, errors, business_sessions, guard_sessions = [], [], [], []
    control = {}

    def new_guard_session(*args, **kwargs):
        session = Session(*args, **kwargs)
        guard_sessions.append(session)
        return session

    class WaitingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            yield b'data: first chunk\n\n'
            await asyncio.Event().wait()

        async def aclose(self):
            stream_closed.set()

    async def request():
        control.update(loop=asyncio.get_running_loop(), task=asyncio.current_task())
        assert current_attribution(owner_id)["agent_team_task_id"] == task_id
        try:
            transport = httpx.MockTransport(lambda _request: httpx.Response(200, stream=WaitingStream()))
            async with httpx.AsyncClient(transport=transport) as client:
                async with client.stream("POST", "https://model.test/chat/completions") as response:
                    async for _chunk in response.aiter_bytes():
                        pass
        finally:
            record_usage_attempt(
                model_name="test-model", agent_label="team-member", usage=usage, status="failed", error="cancelled",
            )

    def handle(db, *_args, **_kwargs):
        business_sessions.append(db)
        runtime.run_agent_coroutine(request())
        continued.append(True)
        return None, {"status": "completed"}

    def run():
        try:
            outcome.append(agent_team_dispatcher._execute_claimed(team_id, claimed))
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    monkeypatch.setattr(agent_team_dispatcher, "Session", new_guard_session)
    monkeypatch.setattr(agent_mesh_dispatcher, "_handle", handle)
    worker = Thread(target=run, daemon=True)
    worker.start()
    try:
        assert started.wait(2), errors
        _revoke(team_execution, reason)
        assert finished.wait(2), "持久化取消后 HTTP stream/async task 仍在运行"
        assert stream_closed.is_set()
        assert errors == []
        assert outcome == [{"success": False}]
        assert len(guard_sessions) >= 3
        assert len({id(session) for session in guard_sessions}) == len(guard_sessions)
        assert all(session is not business_sessions[0] for session in guard_sessions)
        with factory() as db:
            rows = db.query(AiCallLog).all()
            assert len(rows) == 1
            assert (rows[0].prompt_tokens, rows[0].completion_tokens, rows[0].total_tokens) == (
                tuple(usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens"))
                if usage is not None else (None, None, None)
            )
            assert rows[0].agent_team_id == team_id
            assert rows[0].agent_team_task_id == task_id
            assert db.get(AgentTeamTask, task_id).result_json == "{}"
            if reason == "team_cancel":
                assert db.get(AgentTeamTask, task_id).status == "cancelled"
                assert agent_team_service.recover_expired_leases(db) == 0
                assert agent_team_service.claim_next_task(db, team_id) is None
    finally:
        # 失败时主动唤醒并取消测试请求，避免无限等待的 daemon 污染后续测试。
        if worker.is_alive() and control:
            control["loop"].call_soon_threadsafe(control["task"].cancel)
        worker.join(timeout=2)
        assert not worker.is_alive()
    assert continued == [], "取消后调用方继续执行了正文处理"


@pytest.mark.parametrize("reason", ("team_cancel", "member_change"))
def test_revocation_between_result_and_supervisor_is_fenced(team_execution, monkeypatch, reason):
    factory, team_id, _task_id, _owner_id, claimed = team_execution
    require_lease = agent_team_service.require_active_task_lease

    def revoke_before_lock(*args, **kwargs):
        if kwargs.get("lock"):
            _revoke(team_execution, reason)
        return require_lease(*args, **kwargs)

    monkeypatch.setattr(agent_team_service, "require_active_task_lease", revoke_before_lock)
    monkeypatch.setattr(agent_mesh_dispatcher, "_handle", Mock(return_value=(None, {
        "status": "completed", "summary": "normal result", "evidence": [{"files": 1}],
    })))
    assert agent_team_dispatcher._execute_claimed(team_id, claimed) == {"success": False}
    with factory() as db:
        assert db.query(AgentTeamEvent).filter_by(team_id=team_id, event_type="supervisor.after").count() == 0


@pytest.mark.parametrize("reason", _REVOKE_REASONS[:-1])
def test_business_fence_still_rejects_revoked_lease(team_execution, reason):
    factory, team_id, task_id, owner_id, claimed = team_execution
    effects = []
    with factory() as stale_db:
        agent_team_service.require_active_task_lease(
            stale_db, team_id=team_id, task_id=task_id, owner_user_id=owner_id, lease_token=claimed["lease_token"],
        )
        _revoke(team_execution, reason)
        with pytest.raises(agent_team_service.AgentTeamLeaseError):
            agent_team_service.require_active_task_lease(
                stale_db, team_id=team_id, task_id=task_id, owner_user_id=owner_id,
                lease_token=claimed["lease_token"], lock=True,
            )
            effects.append("business-write")
    assert effects == []


@pytest.mark.asyncio
async def test_normal_completion_and_concurrent_scopes_are_isolated(monkeypatch):
    monkeypatch.setattr(runtime, "_CANCELLATION_POLL_SECONDS", 0.01)
    revoked, started = Event(), asyncio.Event()
    cancelled_cleanup, unaffected = [], []

    def guard():
        if revoked.is_set():
            raise ValueError("lease revoked")

    async def cancelled():
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            cancelled_cleanup.append(True)

    async def execute_cancelled():
        with runtime.agent_execution_scope(guard):
            with pytest.raises(runtime.AgentExecutionCancelled):
                await runtime.await_agent_coroutine(cancelled())

    async def normal():
        await started.wait()
        revoked.set()
        await asyncio.sleep(0.05)
        unaffected.append(True)
        return "completed"

    async def execute_normal():
        with runtime.agent_execution_scope(lambda: None):
            assert await runtime.await_agent_coroutine(normal()) == "completed"

    await asyncio.gather(execute_cancelled(), execute_normal())
    assert cancelled_cleanup == [True]
    assert unaffected == [True]
    # scope 结束后必须恢复，不能把已撤销租约带进同线程下一项任务。
    runtime.check_agent_execution()
    assert await runtime.await_agent_coroutine(asyncio.sleep(0, result="outside")) == "outside"


@pytest.mark.asyncio
async def test_parent_cancellation_awaits_request_finalizer():
    started, cleaned = asyncio.Event(), []

    async def request():
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.append(True)

    with runtime.agent_execution_scope(lambda: None):
        task = asyncio.create_task(runtime.await_agent_coroutine(request()))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
    assert cleaned == [True]


@pytest.mark.asyncio
async def test_suppressed_cancellation_cannot_return_late_result(monkeypatch):
    monkeypatch.setattr(runtime, "_CANCELLATION_POLL_SECONDS", 0.01)
    revoked, cleaned = Event(), []

    def guard():
        if revoked.is_set():
            raise ValueError("lease revoked")

    async def request():
        revoked.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return "late result"
        finally:
            cleaned.append(True)

    with runtime.agent_execution_scope(guard):
        with pytest.raises(runtime.AgentExecutionCancelled):
            await asyncio.wait_for(runtime.await_agent_coroutine(request()), timeout=1)
    assert cleaned == [True]


@pytest.mark.asyncio
async def test_database_guard_failure_closes_unstarted_coroutine():
    def guard():
        raise OSError("database unavailable")

    request = asyncio.sleep(0)
    with runtime.agent_execution_scope(guard):
        with pytest.raises(runtime.AgentExecutionCancelled) as error:
            await runtime.await_agent_coroutine(request)
    assert isinstance(error.value.__cause__, OSError)
    assert inspect.getcoroutinestate(request) == inspect.CORO_CLOSED


@pytest.mark.asyncio
async def test_invalid_lease_cleans_up_already_started_task():
    started, cleaned = asyncio.Event(), []

    async def request():
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            cleaned.append(True)

    def guard():
        raise ValueError("lease revoked")

    task = asyncio.create_task(request())
    await asyncio.wait_for(started.wait(), timeout=1)
    try:
        with runtime.agent_execution_scope(guard):
            with pytest.raises(runtime.AgentExecutionCancelled):
                await runtime.await_agent_coroutine(task)
        assert task.done()
        assert cleaned == [True]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_usage_accounting_failure_in_cancel_finalizer_is_not_swallowed(monkeypatch):
    monkeypatch.setattr(runtime, "_CANCELLATION_POLL_SECONDS", 0.01)
    revoked = Event()

    def guard():
        if revoked.is_set():
            raise ValueError("lease revoked")

    async def request():
        try:
            revoked.set()
            await asyncio.Event().wait()
        finally:
            raise UsageAccountingError("audit commit failed")

    with runtime.agent_execution_scope(guard):
        with pytest.raises(UsageAccountingError, match="audit commit failed"):
            await asyncio.wait_for(runtime.await_agent_coroutine(request()), timeout=1)


@pytest.mark.asyncio
async def test_request_error_propagates_without_retry():
    calls = []

    async def request():
        calls.append(True)
        raise httpx.ReadError("connection lost")

    with runtime.agent_execution_scope(lambda: None):
        with pytest.raises(httpx.ReadError, match="connection lost"):
            await runtime.await_agent_coroutine(request())
    assert calls == [True]


@pytest.mark.asyncio
async def test_sync_bridge_rejects_nested_loop_without_leaking_coroutines():
    request = asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="await_agent_coroutine"):
        runtime.run_agent_coroutine(request)
    assert inspect.getcoroutinestate(request) == inspect.CORO_CLOSED


@pytest.mark.asyncio
async def test_lease_cancellation_before_http_headers_closes_client(monkeypatch):
    monkeypatch.setattr(runtime, "_CANCELLATION_POLL_SECONDS", 0.01)
    started, revoked, cleaned = asyncio.Event(), Event(), []
    clients = []

    def guard():
        if revoked.is_set():
            raise ValueError("lease revoked")

    async def transport(_request):
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            cleaned.append(True)

    async def request():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            clients.append(client)
            await client.post("https://model.test/chat/completions")

    with runtime.agent_execution_scope(guard):
        task = asyncio.create_task(runtime.await_agent_coroutine(request()))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            revoked.set()
            with pytest.raises(runtime.AgentExecutionCancelled):
                await asyncio.wait_for(task, timeout=1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert cleaned == [True]
    assert len(clients) == 1 and clients[0].is_closed


@pytest.mark.asyncio
async def test_request_finishes_before_poll_but_revoked_result_is_rejected():
    revoked, effects = Event(), []

    def guard():
        if revoked.is_set():
            raise ValueError("lease revoked")

    async def request():
        revoked.set()
        return "late result"

    with runtime.agent_execution_scope(guard):
        with pytest.raises(runtime.AgentExecutionCancelled):
            result = await runtime.await_agent_coroutine(request())
            effects.append(result)
    assert effects == []


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ("initial", "parent", "lease"))
async def test_repeated_parent_cancellation_does_not_interrupt_cleanup(monkeypatch, phase):
    monkeypatch.setattr(runtime, "_CANCELLATION_POLL_SECONDS", 0.01)
    started, finalizing, release_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
    revoked, cleaned = Event(), []

    def guard():
        if phase == "initial" or revoked.is_set():
            raise ValueError("lease revoked")

    async def request():
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            finalizing.set()
            await release_cleanup.wait()
            cleaned.append(True)

    if phase == "initial":
        operation = asyncio.create_task(request())
        await asyncio.wait_for(started.wait(), timeout=1)
    else:
        operation = request()
    with runtime.agent_execution_scope(guard):
        wrapper = asyncio.create_task(runtime.await_agent_coroutine(operation))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            if phase == "parent":
                wrapper.cancel()
            elif phase == "lease":
                revoked.set()
            await asyncio.wait_for(finalizing.wait(), timeout=1)
            wrapper.cancel()
            await asyncio.sleep(0)
            wrapper.cancel()
            await asyncio.sleep(0)
            assert not wrapper.done()
            assert cleaned == []
            release_cleanup.set()
            expected = asyncio.CancelledError if phase == "parent" else runtime.AgentExecutionCancelled
            with pytest.raises(expected):
                await asyncio.wait_for(wrapper, timeout=1)
            assert cleaned == [True]
        finally:
            release_cleanup.set()
            wrapper.cancel()
            await asyncio.gather(wrapper, return_exceptions=True)
            if isinstance(operation, asyncio.Task):
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.parametrize("entry", ("call_raw", "chat"))
@pytest.mark.parametrize("phase", ("headers", "body"))
@pytest.mark.parametrize("reason", _REVOKE_REASONS)
def test_real_deepseek_http_is_cancelled_from_persisted_team_state(team_execution, monkeypatch, entry, phase, reason):
    from types import SimpleNamespace

    from app.ai import deepseek_agent

    factory, team_id, task_id, owner_id, claimed = team_execution
    started, stream_closed, finished = Event(), Event(), Event()
    calls, clients, outcome, errors = [], [], [], []
    control = {}
    original_client = httpx.AsyncClient

    class WaitingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            yield b'{"choices":'
            await asyncio.Event().wait()

        async def aclose(self):
            stream_closed.set()

    async def transport(request):
        calls.append(request)
        control.update(loop=asyncio.get_running_loop(), task=asyncio.current_task())
        if phase == "headers":
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stream_closed.set()
        return httpx.Response(200, stream=WaitingStream())

    def client_factory(**kwargs):
        assert kwargs["trust_env"] is False
        client = original_client(**kwargs, transport=httpx.MockTransport(transport))
        clients.append(client)
        return client

    monkeypatch.setattr(deepseek_agent.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(deepseek_agent, "pin_public_http_url", lambda _url: SimpleNamespace(
        request_url="https://93.184.216.34/chat/completions", host_header="model.test",
        ip_address="93.184.216.34", request_extensions={"sni_hostname": b"model.test"},
    ))
    monkeypatch.setattr(deepseek_agent, "_get_http_client", Mock(side_effect=AssertionError("sync HTTP still used")))
    monkeypatch.setattr(runtime, "_CANCELLATION_POLL_SECONDS", 0.01)
    agent = deepseek_agent.DeepSeekAgent(api_key="unit-only", model="unit-model", max_retries=3)

    def handle(db, _user, *_args, **_kwargs):
        if entry == "chat":
            agent.chat(system_prompt="system", user_prompt="user", db=db, user_id=owner_id,
                       agent_label="cancel-test")
        else:
            agent.call_raw("system", "user", agent_label="cancel-test")
        return None, {"status": "completed", "summary": "must not return"}

    monkeypatch.setattr(agent_mesh_dispatcher, "_handle", handle)

    def worker():
        try:
            outcome.append(agent_team_dispatcher._execute_claimed(team_id, claimed))
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = Thread(target=worker, daemon=True)
    thread.start()
    try:
        # 并行回归可能延迟工作线程启动；撤销后的中断期限仍由下面的 2 秒断言约束。
        assert started.wait(15), f"async HTTP never started; outcome={outcome}, errors={errors}"
        _revoke(team_execution, reason)
        assert finished.wait(2), "persisted cancellation did not interrupt real DeepSeek HTTP"
    finally:
        if not finished.is_set() and control:
            control["loop"].call_soon_threadsafe(control["task"].cancel)
        thread.join(timeout=2)
    assert errors == [] and outcome == [{"success": False}]
    assert stream_closed.is_set()
    assert len(calls) == 1 and len(clients) == 1 and clients[0].is_closed
    assert calls[0].headers["host"] == "model.test"
    assert calls[0].extensions["sni_hostname"] == b"model.test"
    with factory() as db:
        logs = db.query(AiCallLog).all()
        assert len(logs) == 1
        row = logs[0]
        assert (row.user_id, row.agent_team_id, row.agent_team_task_id) == (owner_id, team_id, task_id)
        assert row.status == "cancelled"
        assert (row.prompt_tokens, row.completion_tokens, row.total_tokens) == (None, None, None)
        assert row.duration_ms is not None and row.duration_ms >= 0
        task = db.get(AgentTeamTask, task_id)
        assert task.next_attempt_at is None and task.result_json == "{}"
        assert db.query(AgentTeamEvent).filter_by(event_type="supervisor.after").count() == 0
        if reason in ("team_cancel", "task_cancel"):
            assert task.status == "cancelled"
            assert agent_team_service.recover_expired_leases(db) == 0


@pytest.mark.parametrize("entry", ("call_raw", "chat"))
@pytest.mark.parametrize("usage", (None, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                                   {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}))
def test_deepseek_late_response_keeps_received_usage_but_drops_output(team_execution, monkeypatch, entry, usage):
    from types import SimpleNamespace

    from app.ai import deepseek_agent

    continued = []
    factory, team_id, task_id, owner_id, claimed = team_execution
    calls, clients = [], []
    original_client = httpx.AsyncClient

    async def transport(request):
        calls.append(request)
        _revoke(team_execution, "team_cancel")
        return httpx.Response(200, json={
            "model": "reported-model", "usage": usage,
            "choices": [{"finish_reason": "stop", "message": {"content": '{"ok": true}'}}],
        })

    def client_factory(**kwargs):
        client = original_client(**kwargs, transport=httpx.MockTransport(transport))
        clients.append(client)
        return client

    monkeypatch.setattr(deepseek_agent.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(deepseek_agent, "pin_public_http_url", lambda _url: SimpleNamespace(
        request_url="https://model.test/chat/completions", host_header="model.test", request_extensions={},
    ))
    agent = deepseek_agent.DeepSeekAgent(api_key="unit-only", max_retries=3)

    def handle(db, _user, *_args, **_kwargs):
        if entry == "chat":
            agent.chat(system_prompt="system", user_prompt="user", db=db, user_id=owner_id, agent_label="late")
        else:
            agent.call_raw("system", "user", agent_label="late")
        continued.append(True)
        return None, {"status": "completed"}

    monkeypatch.setattr(agent_mesh_dispatcher, "_handle", handle)
    assert agent_team_dispatcher._execute_claimed(team_id, claimed) == {"success": False}
    assert len(calls) == 1 and clients[0].is_closed
    with factory() as db:
        row = db.query(AiCallLog).one()
        expected = (None, None, None) if usage is None else (usage["prompt_tokens"], usage["completion_tokens"],
                                                            usage["total_tokens"])
        assert (row.prompt_tokens, row.completion_tokens, row.total_tokens) == expected
        assert row.status == "cancelled" and row.agent_team_task_id == task_id
        assert db.get(AgentTeamTask, task_id).result_json == "{}"
    assert continued == [], "取消后调用方继续执行了正文处理"


@pytest.mark.parametrize("entry", ("call_raw", "chat"))
def test_deepseek_stop_during_retry_backoff_sends_no_second_request(team_execution, monkeypatch, entry):
    from types import SimpleNamespace

    from app.ai import deepseek_agent

    continued = []
    factory, team_id, _task_id, owner_id, claimed = team_execution
    waiting, finished = Event(), Event()
    calls, outcome = [], []
    original_client, wait_before_retry = httpx.AsyncClient, deepseek_agent.DeepSeekAgent._wait_before_retry

    async def transport(request):
        calls.append(request)
        return httpx.Response(429, json={"usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}})

    monkeypatch.setattr(deepseek_agent.httpx, "AsyncClient", lambda **kwargs: original_client(
        **kwargs, transport=httpx.MockTransport(transport),
    ))
    monkeypatch.setattr(deepseek_agent, "pin_public_http_url", lambda _url: SimpleNamespace(
        request_url="https://model.test/chat/completions", host_header="model.test", request_extensions={},
    ))
    monkeypatch.setattr(runtime, "_CANCELLATION_POLL_SECONDS", 0.01)

    original_sleep = asyncio.sleep

    async def observed_sleep(delay, *args, **kwargs):
        if delay == 30:
            # 回调在协程真正挂起后运行，证明撤销发生在退避 sleep 期间。
            asyncio.get_running_loop().call_soon(waiting.set)
        await original_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", observed_sleep)

    def wait_before_stop(_delay):
        wait_before_retry(30)

    monkeypatch.setattr(deepseek_agent.DeepSeekAgent, "_wait_before_retry", staticmethod(wait_before_stop))
    agent = deepseek_agent.DeepSeekAgent(api_key="unit-only", max_retries=3)

    def handle(db, _user, *_args, **_kwargs):
        if entry == "chat":
            agent.chat(system_prompt="system", user_prompt="user", db=db, user_id=owner_id, agent_label="retry")
        else:
            agent.call_raw("system", "user", agent_label="retry")
        continued.append(True)
        return None, {"status": "completed"}

    monkeypatch.setattr(agent_mesh_dispatcher, "_handle", handle)

    def worker():
        try:
            outcome.append(agent_team_dispatcher._execute_claimed(team_id, claimed))
        finally:
            finished.set()

    thread = Thread(target=worker, daemon=True)
    thread.start()
    assert waiting.wait(2)
    _revoke(team_execution, "team_cancel")
    assert finished.wait(2)
    thread.join(timeout=2)
    assert outcome == [{"success": False}] and len(calls) == 1
    with factory() as db:
        row = db.query(AiCallLog).one()
        assert row.status == "retry"  # 已完成的真实尝试保留，退避取消不伪造另一条调用。
        assert (row.prompt_tokens, row.completion_tokens, row.total_tokens) == (7, 3, 10)
    assert continued == [], "取消后调用方继续执行了正文处理"


@pytest.mark.parametrize("entry", ("call_raw", "chat"))
def test_deepseek_revoked_before_send_does_not_create_http_or_usage(team_execution, monkeypatch, entry):
    from app.ai import deepseek_agent

    continued = []
    factory, team_id, _task_id, owner_id, claimed = team_execution
    client_factory = Mock(side_effect=AssertionError("request must not start"))
    monkeypatch.setattr(deepseek_agent.httpx, "AsyncClient", client_factory)
    agent = deepseek_agent.DeepSeekAgent(api_key="unit-only", max_retries=3)

    def handle(db, _user, *_args, **_kwargs):
        _revoke(team_execution, "team_cancel")
        if entry == "chat":
            agent.chat(system_prompt="system", user_prompt="user", db=db, user_id=owner_id)
        else:
            agent.call_raw("system", "user")
        continued.append(True)
        return None, {"status": "completed"}

    monkeypatch.setattr(agent_mesh_dispatcher, "_handle", handle)
    assert agent_team_dispatcher._execute_claimed(team_id, claimed) == {"success": False}
    client_factory.assert_not_called()
    with factory() as db:
        assert db.query(AiCallLog).count() == 0
    assert continued == [], "取消后调用方继续执行了正文处理"


@pytest.mark.parametrize("entry", ("call_raw", "chat"))
@pytest.mark.parametrize("reason", ("team_cancel", "task_cancel"))
def test_deepseek_cancellation_closes_real_local_tcp_connection(team_execution, monkeypatch, entry, reason):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from types import SimpleNamespace

    from app.ai import deepseek_agent

    continued = []
    factory, team_id, _task_id, owner_id, claimed = team_execution
    started, disconnected, finished = Event(), Event(), Event()
    calls, outcome = [], []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            calls.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "1000000")
            self.end_headers()
            self.wfile.write(b'{"choices":')
            self.wfile.flush()
            started.set()
            self.connection.settimeout(3)
            if self.connection.recv(1) == b"":
                disconnected.set()

    monkeypatch.setattr(runtime, "_CANCELLATION_POLL_SECONDS", 0.01)
    agent = deepseek_agent.DeepSeekAgent(api_key="unit-only", timeout=3, max_retries=3)
    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        server_thread = Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        # 仅测试替换 URL pinning，让真正的 httpx/httpcore 连接本地测试服务。
        monkeypatch.setattr(deepseek_agent, "pin_public_http_url", lambda _url: SimpleNamespace(
            request_url=f"http://127.0.0.1:{server.server_port}/chat/completions",
            host_header="model.test", request_extensions={},
        ))

        def handle(db, _user, *_args, **_kwargs):
            if entry == "chat":
                agent.chat(system_prompt="system", user_prompt="user", db=db, user_id=owner_id)
            else:
                agent.call_raw("system", "user")
            continued.append(True)
            return None, {"status": "completed"}

        monkeypatch.setattr(agent_mesh_dispatcher, "_handle", handle)

        def worker():
            try:
                outcome.append(agent_team_dispatcher._execute_claimed(team_id, claimed))
            finally:
                finished.set()

        thread = Thread(target=worker, daemon=True)
        thread.start()
        try:
            assert started.wait(2)
            _revoke(team_execution, reason)
            assert disconnected.wait(2), "HTTP peer did not observe TCP EOF on cancellation"
            assert finished.wait(2)
            assert outcome == [{"success": False}] and calls == ["/chat/completions"]
            with factory() as db:
                row = db.query(AiCallLog).one()
                assert row.status == "cancelled" and row.total_tokens is None
        finally:
            server.shutdown()
            thread.join(timeout=4)
            server_thread.join(timeout=4)
    assert continued == [], "取消后调用方继续执行了正文处理"


@pytest.mark.parametrize("entry", ("call_raw", "chat"))
@pytest.mark.parametrize("phase", ("stream_close", "client_close"))
def test_deepseek_cancel_during_normal_close_finishes_cleanup(team_execution, monkeypatch, entry, phase):
    from types import SimpleNamespace

    from app.ai import deepseek_agent

    continued = []
    factory, team_id, task_id, owner_id, claimed = team_execution
    closing, release_close, closed, revoked, finished = Event(), Event(), Event(), Event(), Event()
    clients, calls, outcome, errors = [], [], [], []
    original_client, original_check = httpx.AsyncClient, runtime.check_agent_execution
    body = b'{"choices":[{"message":{"content":"{}"}}],"usage":{"total_tokens":10}}'

    async def close():
        closing.set()
        await asyncio.to_thread(release_close.wait)
        closed.set()

    class ClosingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield body

        async def aclose(self):
            if phase == "stream_close":
                await close()

    class ClosingTransport(httpx.MockTransport):
        async def aclose(self):
            if phase == "client_close":
                await close()

    def transport(request):
        calls.append(request)
        return httpx.Response(200, stream=ClosingStream())

    def client_factory(**kwargs):
        client = original_client(**kwargs, transport=ClosingTransport(transport))
        clients.append(client)
        return client

    def check():
        try:
            original_check()
        except runtime.AgentExecutionCancelled:
            revoked.set()
            raise

    monkeypatch.setattr(runtime, "check_agent_execution", check)
    monkeypatch.setattr(runtime, "_CANCELLATION_POLL_SECONDS", 0.001)
    monkeypatch.setattr(deepseek_agent.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(deepseek_agent, "pin_public_http_url", lambda _url: SimpleNamespace(
        request_url="https://model.test/chat/completions", host_header="model.test", request_extensions={},
    ))
    agent = deepseek_agent.DeepSeekAgent(api_key="unit-only", max_retries=3)

    def handle(db, _user, *_args, **_kwargs):
        if entry == "chat":
            agent.chat(system_prompt="system", user_prompt="user", db=db, user_id=owner_id)
        else:
            agent.call_raw("system", "user")
        continued.append(True)
        return None, {"status": "completed"}

    monkeypatch.setattr(agent_mesh_dispatcher, "_handle", handle)

    def worker():
        try:
            outcome.append(agent_team_dispatcher._execute_claimed(team_id, claimed))
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = Thread(target=worker, daemon=True)
    thread.start()
    try:
        assert closing.wait(2)
        _revoke(team_execution, "team_cancel")
        assert revoked.wait(2)
        # 首次取消也不能中断已经开始的正常清理，更不能提前记账/退出。
        assert not finished.wait(0.05)
    finally:
        release_close.set()
        thread.join(timeout=2)
    assert finished.is_set() and closed.is_set()
    assert errors == [] and outcome == [{"success": False}]
    assert len(calls) == 1 and clients[0].is_closed
    with factory() as db:
        row = db.query(AiCallLog).one()
        assert row.status == "cancelled"
        # aread 直到流关闭才缓存完整响应；此前取消时 usage 仍未知。
        assert row.total_tokens == (10 if phase == "client_close" else None)
        assert db.get(AgentTeamTask, task_id).result_json == "{}"
    assert continued == [], "取消后调用方继续执行了正文处理"


@pytest.mark.parametrize("entry", ("call_raw", "chat"))
@pytest.mark.parametrize("reason", ("team_cancel", "task_cancel", "lease_replace"))
def test_deepseek_revoked_during_usage_audit_never_delivers_body(team_execution, monkeypatch, entry, reason):
    from types import SimpleNamespace

    from app.ai import deepseek_agent

    factory, team_id, task_id, owner_id, claimed = team_execution
    calls, continued = [], []
    original_client, original_record = httpx.AsyncClient, deepseek_agent.record_usage_attempt

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        })

    def audit(**kwargs):
        log_id = original_record(**kwargs)
        _revoke(team_execution, reason)
        return log_id

    monkeypatch.setattr(deepseek_agent, "record_usage_attempt", audit)
    monkeypatch.setattr(deepseek_agent.httpx, "AsyncClient", lambda **kwargs: original_client(
        **kwargs, transport=httpx.MockTransport(transport),
    ))
    monkeypatch.setattr(deepseek_agent, "pin_public_http_url", lambda _url: SimpleNamespace(
        request_url="https://model.test/chat/completions", host_header="model.test", request_extensions={},
    ))
    agent = deepseek_agent.DeepSeekAgent(api_key="unit-only", max_retries=3)

    def handle(db, _user, *_args, **_kwargs):
        if entry == "chat":
            agent.chat(system_prompt="system", user_prompt="user", db=db, user_id=owner_id)
        else:
            agent.call_raw("system", "user")
        continued.append(True)
        return None, {"status": "completed", "summary": "late result"}

    monkeypatch.setattr(agent_mesh_dispatcher, "_handle", handle)
    assert agent_team_dispatcher._execute_claimed(team_id, claimed) == {"success": False}
    assert continued == [], "审计期间撤销后模型正文仍交付了调用方"
    assert len(calls) == 1
    with factory() as db:
        row = db.query(AiCallLog).one()
        # 请求及记账均已完成；丢弃交付不能抹去已收到的真实 token 消耗。
        assert row.status == "success"
        assert (row.prompt_tokens, row.completion_tokens, row.total_tokens) == (7, 3, 10)
        assert db.get(AgentTeamTask, task_id).result_json == "{}"
        assert db.query(AgentTeamEvent).filter_by(event_type="supervisor.after").count() == 0


@pytest.mark.parametrize("entry", ("call", "call_json"))
@pytest.mark.parametrize("phase", ("headers", "body"))
@pytest.mark.parametrize("reason", _REVOKE_REASONS)
def test_base_agent_http_is_cancelled_from_persisted_team_state(team_execution, monkeypatch, entry, phase, reason):
    from types import SimpleNamespace

    from app.agents import base

    factory, team_id, task_id, owner_id, claimed = team_execution
    started, closed, finished = Event(), Event(), Event()
    calls, clients, outcome, errors, continued = [], [], [], [], []
    control = {}
    original_client = httpx.AsyncClient

    class WaitingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            yield b'{"choices":'
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    async def transport(request):
        calls.append(request)
        control.update(loop=asyncio.get_running_loop(), task=asyncio.current_task())
        if phase == "headers":
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        return httpx.Response(200, stream=WaitingStream())

    def client_factory(**kwargs):
        assert kwargs["trust_env"] is False
        client = original_client(**kwargs, transport=httpx.MockTransport(transport))
        clients.append(client)
        return client

    monkeypatch.setattr(base.httpx, "AsyncClient", client_factory)
    sync_client = Mock(side_effect=AssertionError("BaseAgent still uses synchronous HTTP"))
    monkeypatch.setattr(base.httpx, "Client", sync_client)
    monkeypatch.setattr(base, "pin_public_http_url", lambda _url: SimpleNamespace(
        request_url="https://93.184.216.34/chat/completions", host_header="model.test",
        request_extensions={"sni_hostname": b"model.test"},
    ))
    monkeypatch.setattr(runtime, "_CANCELLATION_POLL_SECONDS", 0.01)
    agent = base.BaseAgent()
    agent._api_key, agent._max_retries = "unit-only", 3

    def handle(db, user, *_args, **_kwargs):
        agent.bind_usage_source(db, user)
        getattr(agent, entry)("user", base.AgentContext(user_id=owner_id))
        continued.append(True)
        return None, {"status": "completed"}

    monkeypatch.setattr(agent_mesh_dispatcher, "_handle", handle)

    def worker():
        try:
            outcome.append(agent_team_dispatcher._execute_claimed(team_id, claimed))
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = Thread(target=worker, daemon=True)
    thread.start()
    try:
        # 等待启动留调度余量；取消后的 2 秒边界单独核验。
        assert started.wait(15), (outcome, errors, sync_client.call_count)
        _revoke(team_execution, reason)
        assert finished.wait(2), "BaseAgent HTTP did not stop after persisted revocation"
        assert closed.is_set() and all(client.is_closed for client in clients)
        assert outcome == [{"success": False}] and errors == [] and continued == []
        sync_client.assert_not_called()
        assert len(calls) == 1
        assert calls[0].headers["Host"] == "model.test"
        assert calls[0].extensions["sni_hostname"] == b"model.test"
        with factory() as db:
            row = db.query(AiCallLog).one()
            assert row.status == "cancelled"
            assert (row.prompt_tokens, row.completion_tokens, row.total_tokens) == (None, None, None)
            assert row.agent_team_id == team_id and row.agent_team_task_id == task_id
            assert db.get(AgentTeamTask, task_id).result_json == "{}"
            if reason == "team_cancel":
                assert db.get(AgentTeam, team_id).status == "cancelled"
                assert agent_team_service.recover_expired_leases(db) == 0
                assert agent_team_service.claim_next_task(db, team_id) is None
    finally:
        if thread.is_alive() and control:
            control["loop"].call_soon_threadsafe(control["task"].cancel)
        thread.join(timeout=3)
        assert not thread.is_alive()


@pytest.fixture
def base_member(team_execution, monkeypatch, entry):
    from types import SimpleNamespace

    from app.agents import base

    _factory, _team_id, _task_id, owner_id, _claimed = team_execution
    agent = base.BaseAgent(system_prompt="system")
    agent._api_key, agent._timeout, agent._max_retries = "unit-only", 30, 3
    events, continued = [], []
    monkeypatch.setattr(agent, "_emit", lambda type_, *_args, **_kwargs: events.append(type_.value))
    monkeypatch.setattr(runtime, "_CANCELLATION_POLL_SECONDS", 0.01)
    monkeypatch.setattr(base, "pin_public_http_url", lambda _url: SimpleNamespace(
        request_url="https://model.test/chat/completions", host_header="model.test", request_extensions={},
    ))

    def handle(db, user, *_args, **_kwargs):
        agent.bind_usage_source(db, user)
        result = getattr(agent, entry)("user", base.AgentContext(user_id=owner_id))
        continued.append(result)
        return None, {"status": "completed"}

    monkeypatch.setattr(agent_mesh_dispatcher, "_handle", handle)
    return agent, events, continued


@pytest.mark.parametrize("entry", ("call", "call_json"))
@pytest.mark.parametrize("reason", ("team_cancel", "task_cancel"))
def test_base_agent_cancellation_closes_real_tcp(team_execution, base_member, monkeypatch, entry, reason):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from types import SimpleNamespace

    from app.agents import base

    factory, team_id, _task_id, _owner_id, claimed = team_execution
    _agent, events, continued = base_member
    started, disconnected, finished = Event(), Event(), Event()
    calls, outcome, errors = [], [], []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            calls.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "1000000")
            self.end_headers()
            self.wfile.write(b'{"choices":')
            self.wfile.flush()
            started.set()
            self.connection.settimeout(4)
            if self.connection.recv(1) == b"":
                disconnected.set()

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        server_thread = Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        monkeypatch.setattr(base, "pin_public_http_url", lambda _url: SimpleNamespace(
            request_url=f"http://127.0.0.1:{server.server_port}/chat/completions",
            host_header="model.test", request_extensions={},
        ))

        def worker():
            try:
                outcome.append(agent_team_dispatcher._execute_claimed(team_id, claimed))
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()

        thread = Thread(target=worker, daemon=True)
        thread.start()
        try:
            assert started.wait(15), (outcome, errors)
            _revoke(team_execution, reason)
            assert disconnected.wait(2), "server did not observe TCP EOF after stop"
            assert finished.wait(2), errors
            assert outcome == [{"success": False}] and errors == [] and continued == []
            assert "complete" not in events
            assert calls == ["/chat/completions"]
            with factory() as db:
                row = db.query(AiCallLog).one()
                assert row.status == "cancelled" and row.total_tokens is None
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server_thread.join(timeout=5)
            assert not thread.is_alive()


@pytest.mark.parametrize("entry", ("call", "call_json"))
@pytest.mark.parametrize("phase", ("stream_close", "client_close"))
@pytest.mark.parametrize("usage", (None, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                                   {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}))
def test_base_agent_cancel_during_close_retains_usage(team_execution, base_member, monkeypatch, entry, phase, usage):
    import json

    from app.agents import base

    factory, team_id, _task_id, _owner_id, claimed = team_execution
    _agent, events, continued = base_member
    closing, release, closed, revoked, finished = Event(), Event(), Event(), Event(), Event()
    calls, clients, outcome, errors = [], [], [], []
    original_client, original_check = httpx.AsyncClient, runtime.check_agent_execution
    body = json.dumps({"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                       "model": "actual-model", "usage": usage}).encode()

    async def close():
        closing.set()
        await asyncio.to_thread(release.wait)
        closed.set()

    class ClosingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield body

        async def aclose(self):
            if phase == "stream_close":
                await close()

    class ClosingTransport(httpx.MockTransport):
        async def aclose(self):
            if phase == "client_close":
                await close()

    def transport(request):
        calls.append(request)
        return httpx.Response(200, stream=ClosingStream())

    def client_factory(**kwargs):
        client = original_client(**kwargs, transport=ClosingTransport(transport))
        clients.append(client)
        return client

    def check():
        try:
            original_check()
        except runtime.AgentExecutionCancelled:
            revoked.set()
            raise

    monkeypatch.setattr(base.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(runtime, "check_agent_execution", check)

    def worker():
        try:
            outcome.append(agent_team_dispatcher._execute_claimed(team_id, claimed))
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = Thread(target=worker, daemon=True)
    thread.start()
    try:
        assert closing.wait(15), (outcome, errors)
        _revoke(team_execution, "team_cancel")
        assert revoked.wait(2), "watcher did not detect persisted cancellation"
        assert not finished.is_set(), "cancel returned before HTTP cleanup completed"
        release.set()
        assert finished.wait(2), errors
        assert closed.is_set() and all(client.is_closed for client in clients)
        assert outcome == [{"success": False}] and errors == [] and continued == []
        assert "complete" not in events and len(calls) == 1
        with factory() as db:
            row = db.query(AiCallLog).one()
            assert row.status == "cancelled"
            # httpx 在 EOF 关闭结束后才缓存 aread；此前取消时不能读取或推算 usage。
            known = usage if phase == "client_close" else None
            expected = (tuple(known[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens"))
                        if known is not None else (None, None, None))
            assert (row.prompt_tokens, row.completion_tokens, row.total_tokens) == expected
            if phase == "client_close":
                assert row.model_name == "actual-model"
    finally:
        release.set()
        thread.join(timeout=4)
        assert not thread.is_alive()


@pytest.mark.parametrize("entry", ("call", "call_json"))
def test_base_agent_revoked_during_usage_commit_cannot_deliver_success(team_execution, base_member, monkeypatch, entry):
    from app.agents import base
    from app.services import ai_usage_context

    factory, team_id, _task_id, _owner_id, claimed = team_execution
    _agent, events, continued = base_member
    original_client, record = httpx.AsyncClient, ai_usage_context.record_usage_attempt
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        })

    def audit(**kwargs):
        log_id = record(**kwargs)
        _revoke(team_execution, "team_cancel")
        return log_id

    monkeypatch.setattr(base.httpx, "AsyncClient", lambda **kwargs: original_client(
        **kwargs, transport=httpx.MockTransport(transport),
    ))
    monkeypatch.setattr(ai_usage_context, "record_usage_attempt", audit)
    assert agent_team_dispatcher._execute_claimed(team_id, claimed) == {"success": False}
    assert continued == [] and "complete" not in events and len(calls) == 1
    with factory() as db:
        row = db.query(AiCallLog).one()
        assert row.status == "success" and row.total_tokens == 10
        assert db.get(AgentTeam, team_id).status == "cancelled"


@pytest.mark.parametrize("entry", ("call", "call_json"))
def test_base_agent_revoked_before_send_has_no_http_or_usage(team_execution, base_member, monkeypatch, entry):
    from app.agents import base

    factory, team_id, _task_id, _owner_id, claimed = team_execution
    _agent, _events, continued = base_member
    handle = agent_mesh_dispatcher._handle
    client = Mock(side_effect=AssertionError("request must not start"))
    monkeypatch.setattr(base.httpx, "AsyncClient", client)

    def stop_first(*args, **kwargs):
        _revoke(team_execution, "team_cancel")
        return handle(*args, **kwargs)

    monkeypatch.setattr(agent_mesh_dispatcher, "_handle", stop_first)
    assert agent_team_dispatcher._execute_claimed(team_id, claimed) == {"success": False}
    client.assert_not_called()
    assert continued == []
    with factory() as db:
        assert db.query(AiCallLog).count() == 0


@pytest.mark.parametrize("entry", ("call", "call_json"))
def test_base_agent_stop_during_backoff_does_not_retry(team_execution, base_member, monkeypatch, entry):
    from app.agents import base

    factory, team_id, _task_id, _owner_id, claimed = team_execution
    _agent, events, continued = base_member
    waiting, finished = Event(), Event()
    original_client, original_sleep = httpx.AsyncClient, asyncio.sleep
    calls, outcome, errors = [], [], []

    def transport(request):
        calls.append(request)
        return httpx.Response(429, json={"usage": {"total_tokens": 10}})

    async def sleep(delay, *args, **kwargs):
        if delay == 2:
            asyncio.get_running_loop().call_soon(waiting.set)
        await original_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(base.httpx, "AsyncClient", lambda **kwargs: original_client(
        **kwargs, transport=httpx.MockTransport(transport),
    ))
    monkeypatch.setattr(base.asyncio, "sleep", sleep)

    def worker():
        try:
            outcome.append(agent_team_dispatcher._execute_claimed(team_id, claimed))
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = Thread(target=worker, daemon=True)
    thread.start()
    try:
        assert waiting.wait(15), (outcome, errors)
        _revoke(team_execution, "team_cancel")
        assert finished.wait(2), errors
        assert outcome == [{"success": False}] and errors == [] and continued == [] and len(calls) == 1
        assert "complete" not in events
        with factory() as db:
            row = db.query(AiCallLog).one()
            assert row.status == "retry" and row.total_tokens == 10
    finally:
        thread.join(timeout=4)
        assert not thread.is_alive()


@pytest.mark.parametrize("entry", ("call", "call_json"))
def test_base_agent_scoped_success_keeps_payload_usage_and_single_complete(
    team_execution, base_member, monkeypatch, entry,
):
    import json

    from app.agents import base

    factory, team_id, _task_id, owner_id, claimed = team_execution
    agent, events, _continued = base_member
    calls, results = [], []
    original_client = httpx.AsyncClient

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"answer": "ok"}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        })

    monkeypatch.setattr(base.httpx, "AsyncClient", lambda **kwargs: original_client(
        **kwargs, transport=httpx.MockTransport(transport),
    ))

    def handle(db, user, *_args, **_kwargs):
        agent.bind_usage_source(db, user)
        results.append(getattr(agent, entry)("user", base.AgentContext(user_id=owner_id), thinking=False))
        # 外层只验证租约，不让监督模型产生额外请求。
        _revoke(team_execution, "team_cancel")
        return None, {"status": "completed"}

    monkeypatch.setattr(agent_mesh_dispatcher, "_handle", handle)
    assert agent_team_dispatcher._execute_claimed(team_id, claimed) == {"success": False}
    assert len(results) == 1 and results[0].success and len(calls) == 1
    assert results[0].tokens == {"prompt": 7, "completion": 3, "total": 10}
    assert results[0].data == ({"answer": "ok"} if entry == "call_json" else '{"answer": "ok"}')
    assert events.count("complete") == 1
    payload = json.loads(calls[0].content)
    assert payload["thinking"] == {"type": "disabled"}
    assert (payload.get("response_format") == {"type": "json_object"}) == (entry == "call_json")
    with factory() as db:
        row = db.query(AiCallLog).one()
        assert row.status == "success" and row.total_tokens == 10 and results[0].usage_log_ids == [row.id]


@pytest.mark.parametrize("entry", ("call_json",))
@pytest.mark.parametrize("valid_json", (True, False))
def test_base_agent_json_parse_revocation_cannot_deliver(team_execution, base_member, monkeypatch, entry, valid_json):
    from app.agents import base

    factory, team_id, _task_id, _owner_id, claimed = team_execution
    _agent, events, continued = base_member
    original_client, loads = httpx.AsyncClient, base.json_lib.loads
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 10},
        })

    def parse(text, *args, **kwargs):
        if calls and text == "{}":
            _revoke(team_execution, "team_cancel")
            if not valid_json:
                raise base.json_lib.JSONDecodeError("invalid", text, 0)
        return loads(text, *args, **kwargs)

    monkeypatch.setattr(base.httpx, "AsyncClient", lambda **kwargs: original_client(
        **kwargs, transport=httpx.MockTransport(transport),
    ))
    monkeypatch.setattr(base.json_lib, "loads", parse)
    assert agent_team_dispatcher._execute_claimed(team_id, claimed) == {"success": False}
    assert continued == [] and "complete" not in events and "failed" not in events and len(calls) == 1
    with factory() as db:
        row = db.query(AiCallLog).one()
        assert row.status == "success" and row.total_tokens == 10


@pytest.mark.parametrize("member_path", ("temporary", "builtin"))
def test_actual_temporary_and_builtin_member_reach_cancellable_http(team_execution, monkeypatch, member_path):
    from types import SimpleNamespace

    from app.agents import base
    from app.agents.language_agent import LanguageDetectorAgent
    from app.core.permission_codes import PermissionCode
    from app.models.rbac import Permission, Role, RolePermission, UserRole
    from app.services import agent_model_service, temporary_agent_runtime

    factory, team_id, task_id, owner_id, claimed = team_execution
    started, closed, finished = Event(), Event(), Event()
    calls, outcome, errors, continued = [], [], [], []
    control = {}
    original_client = httpx.AsyncClient
    address = "temporary:question" if member_path == "temporary" else "agent:language_detector"
    with factory() as db:
        member = db.get(AgentTeamMember, claimed["member_id"])
        member.address, member.kind = address, member_path
        role = Role(code="user", name="取消测试", status="active")
        permission = Permission(code=PermissionCode.AGENT_CHAT, name="Agent对话", module="agent")
        db.add_all([role, permission])
        db.flush()
        db.add_all([UserRole(user_id=owner_id, role_id=role.id),
                    RolePermission(role_id=role.id, permission_id=permission.id)])
        db.commit()
    claimed["address"] = address
    claimed["input"] = {"operation": "answer_question", "text": "解释任务取消"}

    class WaitingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            control.update(loop=asyncio.get_running_loop(), task=asyncio.current_task())
            started.set()
            yield b'{"choices":'
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    def transport(request):
        calls.append(request)
        return httpx.Response(200, stream=WaitingStream())

    monkeypatch.setattr(base.httpx, "AsyncClient", lambda **kwargs: original_client(
        **kwargs, transport=httpx.MockTransport(transport),
    ))
    monkeypatch.setattr(base, "pin_public_http_url", lambda _url: SimpleNamespace(
        request_url="https://model.test/chat/completions", host_header="model.test", request_extensions={},
    ))
    monkeypatch.setattr(runtime, "_CANCELLATION_POLL_SECONDS", 0.01)
    monkeypatch.setattr(agent_model_service, "configure_subagent", lambda _db, agent, **_kwargs: agent)

    def handle(db, user, _address, message, **_kwargs):
        if member_path == "temporary":
            temporary_agent_runtime.run_temporary_agent(
                db, user, message, {"purpose": "普通问答", "instructions": "解释取消"}, "临时问答成员",
            )
        else:
            agent = LanguageDetectorAgent()
            agent.bind_usage_source(db, user)
            agent.execute("测试项目")
        continued.append(True)
        return None, {"status": "completed"}

    monkeypatch.setattr(agent_mesh_dispatcher, "_handle", handle)

    def worker():
        try:
            outcome.append(agent_team_dispatcher._execute_claimed(team_id, claimed))
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = Thread(target=worker, daemon=True)
    thread.start()
    try:
        assert started.wait(15), (outcome, errors)
        _revoke(team_execution, "team_cancel")
        assert finished.wait(2), errors
        assert closed.is_set() and len(calls) == 1
        assert outcome == [{"success": False}] and errors == [] and continued == []
        with factory() as db:
            row = db.query(AiCallLog).one()
            assert row.status == "cancelled" and row.total_tokens is None and row.agent_team_task_id == task_id
            assert row.agent_label.startswith("temporary_") if member_path == "temporary" else (
                row.agent_label == "language_detector"
            )
    finally:
        if thread.is_alive() and control:
            control["loop"].call_soon_threadsafe(control["task"].cancel)
        thread.join(timeout=3)
        assert not thread.is_alive()
