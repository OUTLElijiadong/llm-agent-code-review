"""独立圆桌恢复必须重建可信团队 scope；仅隔离 SQLite 和本地 HTTP。"""
import asyncio
import json
from contextvars import copy_context
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agents.discussion_bus import DiscussionBus
from app.ai import discussion_orchestrator as orchestrator
from app.ai.multi_agent import GENERAL_AGENT
from app.api.v1 import ws_discussion as ws
from app.core.database import Base
from app.models.agent_team import AgentTeam, AgentTeamMember, AgentTeamTask
from app.models.ai_call_log import AiCallLog
from app.models.review_task import ReviewTask
from app.models.roundtable import RoundtableSession
from app.services import agent_scheduler_runtime as runtime


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'team-roundtable.sqlite'}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    bus = DiscussionBus(persist=True, session_factory=maker)
    monkeypatch.setattr(DiscussionBus, "_instance", bus)
    monkeypatch.setattr(ws, "SessionLocal", maker)
    monkeypatch.setattr(ws, "_pending", {})
    monkeypatch.setattr(ws, "_session_owners", {})
    monkeypatch.setattr(ws, "_owner_registered_at", {})
    monkeypatch.setattr(ws, "purge_stale_pending", lambda: None)
    monkeypatch.setattr(ws, "_schedule_next_pending_expiry_locked", lambda: None)
    monkeypatch.setattr(ws, "_is_discussion_execution_active", lambda *_: True)
    monkeypatch.setattr(ws, "_is_parent_response_run_cancelled", lambda *_: False)
    monkeypatch.setattr(runtime, "_CANCELLATION_POLL_SECONDS", 0.01)
    with maker.begin() as db:
        db.add(AgentTeam(
            id=21, user_id=7, surface="user", session_key="scope", title="scope", objective="roundtable",
            status="running", trace_id="test-only",
        ))
        db.add(AgentTeamMember(
            id=22, team_id=21, member_key="review", display_name="review", address="agent:review_orchestrator",
            kind="builtin", status="running",
        ))
        db.add(AgentTeamTask(
            id=23, team_id=21, member_id=22, task_key="review", title="review", instructions="roundtable",
            status="running", lease_token="original", lease_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            input_json=json.dumps({
                "operation": "start_roundtable", "project_id": 9, "file_id": 11,
                "_roundtable": {"session_id": "team-disc"},
            }),
        ))
    yield bus, maker
    engine.dispose()


def prepare(recovery, *, origin=True, report=False):
    bus, maker = recovery
    session = bus.create_session("team-disc", 0, "main.py", owner_user_id=7, project_id=9, file_id=11)
    session.status = "pending"
    bus._update_session(session)
    kwargs = {"usage_origin": {"agent_team_id": 21, "agent_team_task_id": 23}} if origin else {}
    ws.register_pending(
        session_id=session.session_id, profiles=(GENERAL_AGENT,), code="original immutable code", language="python",
        file_name="main.py", user_id=7, project_id=9, file_id=11, max_rounds=2, session_token_version=3, **kwargs,
    )
    pending = ws.take_pending(session.session_id)
    if report:
        with maker.begin() as db:
            db.add(ReviewTask(
                id=81, user_id=7, project_id=9, review_type="discuss", status="failed",
                coverage={"stage": "interrupted"}, agent_team_id=21, agent_team_task_id=23,
            ))
        bus.link_report_task(session.session_id, 81)
    return session, pending


async def recover_and_wait(bus):
    await ws.recover_persisted_discussions()
    task = bus.get_discussion_task("team-disc")
    if task is not None:
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)


def successful_runner(bus, observed):
    class Runner:
        async def start_discussion(self, **kwargs):
            runtime.check_agent_execution()
            observed.append(runtime.has_agent_execution_scope())
            bus.publish_control("team-disc", "done", {"status": "success", "task_id": 0})
            bus.close_session("team-disc")
    return Runner


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["snapshot", "report", "reverse", "ordinary"])
async def test_recovery_scope_requires_real_persistent_binding(recovery, monkeypatch, source):
    bus, maker = recovery
    prepare(recovery, origin=source == "snapshot", report=source == "report")
    if source == "ordinary":
        with maker.begin() as db:
            db.query(AgentTeamTask).delete()
    observed = []
    monkeypatch.setattr(orchestrator, "DiscussionOrchestrator", successful_runner(bus, observed))
    await recover_and_wait(bus)
    assert observed == [source != "ordinary"]


def revoke(maker, reason):
    with maker.begin() as db:
        team, task, member = db.get(AgentTeam, 21), db.get(AgentTeamTask, 23), db.get(AgentTeamMember, 22)
        raw = json.loads(task.input_json)
        if reason == "team_cancel":
            team.status = "cancelled"
        elif reason == "task_cancel":
            task.status = "cancelled"
        elif reason == "lease_replace":
            task.lease_token = "replacement"
        elif reason == "expiry":
            task.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        elif reason == "deadline":
            team.deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        elif reason == "member":
            member.address = "agent:other"
        elif reason == "owner":
            team.user_id = 99
        elif reason in {"project_id", "file_id", "operation", "session_id"}:
            if reason == "session_id":
                raw["_roundtable"]["session_id"] = "different"
            else:
                raw[reason] = "invalid"
            task.input_json = json.dumps(raw)
        elif reason == "missing_lease":
            task.lease_token = None
        elif reason == "session_cancel":
            row = db.get(RoundtableSession, "team-disc")
            row.status = "cancelled"
            row.progress = {**row.progress, "phase": "cancelled", "terminal_status": "cancelled"}
        else:
            raise AssertionError(reason)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [
    "team_cancel", "task_cancel", "expiry", "deadline", "member", "owner",
    "project_id", "file_id", "operation", "session_id", "missing_lease", "session_cancel",
])
async def test_revoked_team_never_restarts_or_promotes_interrupted_report(recovery, monkeypatch, reason):
    bus, maker = recovery
    prepare(recovery, report=True)
    revoke(maker, reason)
    observed = []
    monkeypatch.setattr(orchestrator, "DiscussionOrchestrator", successful_runner(bus, observed))
    await recover_and_wait(bus)
    assert observed == []
    with maker() as db:
        assert db.get(RoundtableSession, "team-disc").status == "cancelled"
        assert db.get(ReviewTask, 81).status != "running"
    await recover_and_wait(bus)
    assert observed == [], "人工取消不能被下一轮恢复重启"


@pytest.mark.asyncio
@pytest.mark.parametrize("claim", [
    {"agent_team_id": 21}, {"agent_team_task_id": 23},
    {"agent_team_id": 21, "agent_team_task_id": 999},
    {"agent_team_id": 999, "agent_team_task_id": 23},
    {"agent_team_id": True, "agent_team_task_id": 23},
    "malformed-origin",
])
async def test_untrusted_team_claim_cannot_fall_back_to_ordinary(recovery, monkeypatch, claim):
    bus, maker = recovery
    prepare(recovery)
    with maker.begin() as db:
        row = db.get(RoundtableSession, "team-disc")
        progress = json.loads(json.dumps(row.progress))
        progress["_recovery"]["kwargs"]["usage_origin"] = claim
        row.progress = progress
    observed = []
    monkeypatch.setattr(orchestrator, "DiscussionOrchestrator", successful_runner(bus, observed))
    await recover_and_wait(bus)
    assert observed == []


@pytest.mark.asyncio
async def test_claim_to_launch_never_adopts_replacement_lease(recovery, monkeypatch):
    bus, maker = recovery
    prepare(recovery)
    observed = []
    launch = ws.launch_pending_discussion

    def replace_then_launch(pending):
        revoke(maker, "lease_replace")
        return launch(pending)

    monkeypatch.setattr(ws, "launch_pending_discussion", replace_then_launch)
    monkeypatch.setattr(orchestrator, "DiscussionOrchestrator", successful_runner(bus, observed))
    await recover_and_wait(bus)
    assert observed == []
    assert bus.get_session("team-disc").terminal_status == "cancelled"


@pytest.mark.asyncio
async def test_initial_team_launch_preserves_inherited_guard_before_reverse_mapping_commit(recovery, monkeypatch):
    bus, maker = recovery
    _, pending = prepare(recovery)
    with maker.begin() as db:
        task = db.get(AgentTeamTask, 23)
        raw = json.loads(task.input_json)
        raw.pop("_roundtable")
        task.input_json = json.dumps(raw)
    observed = []
    monkeypatch.setattr(orchestrator, "DiscussionOrchestrator", successful_runner(bus, observed))
    with runtime.agent_execution_scope(lambda: None):
        await ws._run_pending_discussion(pending)
    assert observed == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["team_cancel", "task_cancel", "lease_replace", "session_cancel"])
@pytest.mark.parametrize("entry", ["call", "call_json"])
async def test_recovered_base_http_closes_real_tcp_without_retry_or_fake_zero(recovery, monkeypatch, reason, entry):
    from app.agents import base
    from app.services.ai_usage_context import usage_context

    bus, maker = recovery
    prepare(recovery)
    started, disconnected = Event(), Event()
    requests, delivered, workers = [], [], []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            requests.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "1000000")
            self.end_headers()
            self.wfile.write(b'{"choices":')
            self.wfile.flush()
            started.set()
            self.connection.settimeout(5)
            if self.connection.recv(1) == b"":
                disconnected.set()

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        server_thread = Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        monkeypatch.setattr(base, "pin_public_http_url", lambda _: SimpleNamespace(
            request_url=f"http://127.0.0.1:{server.server_port}/chat/completions",
            host_header="unit.test", request_extensions={},
        ))
        agent = base.BaseAgent()
        agent._api_key, agent._max_retries = "unit-only", 3
        with maker() as db:
            agent.bind_usage_source(db, SimpleNamespace(id=7))

        class Runner:
            async def start_discussion(self, **kwargs):
                assert runtime.has_agent_execution_scope()

                def call_member():
                    with usage_context(7, kwargs["usage_origin"]):
                        return getattr(agent, entry)("review original code")

                future = asyncio.get_running_loop().run_in_executor(None, copy_context().run, call_member)
                workers.append(future)
                delivered.append(await asyncio.shield(future))
                bus.publish_control("team-disc", "done", {"status": "success"})

        monkeypatch.setattr(orchestrator, "DiscussionOrchestrator", Runner)
        try:
            await ws.recover_persisted_discussions()
            running = bus.get_discussion_task("team-disc")
            assert await asyncio.to_thread(started.wait, 10), "恢复成员未进入真实HTTP"
            revoke(maker, reason)
            assert await asyncio.to_thread(disconnected.wait, 2), "停止后本地HTTP peer未收到TCP EOF"
            await asyncio.wait_for(asyncio.gather(running, return_exceptions=True), 3)
            await asyncio.wait_for(asyncio.gather(*workers, return_exceptions=True), 3)
            assert delivered == [] and requests == ["/chat/completions"]
            with maker() as db:
                log = db.query(AiCallLog).one()
                assert log.status == "cancelled" and log.total_tokens is None
                assert log.agent_team_id == 21 and log.agent_team_task_id == 23
                assert db.get(RoundtableSession, "team-disc").status == "cancelled"
            await recover_and_wait(bus)
            assert len(requests) == 1, "人工停止后的恢复发出了第二次HTTP"
        finally:
            task = bus.get_discussion_task("team-disc")
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await asyncio.gather(*workers, return_exceptions=True)
            await asyncio.to_thread(server.shutdown)
            server_thread.join(timeout=5)


@pytest.mark.asyncio
async def test_conflicting_report_binding_never_recovers(recovery, monkeypatch):
    bus, maker = recovery
    prepare(recovery, report=True)
    with maker.begin() as db:
        db.get(ReviewTask, 81).agent_team_task_id = 999
    observed = []
    monkeypatch.setattr(orchestrator, "DiscussionOrchestrator", successful_runner(bus, observed))
    await recover_and_wait(bus)
    assert observed == []
    with maker() as db:
        assert db.get(RoundtableSession, "team-disc").status == "cancelled"
        assert db.get(ReviewTask, 81).status == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["team_cancel", "lease_replace"])
async def test_recovery_cancel_tombstone_rejects_late_delivery_and_report(recovery, monkeypatch, reason):
    bus, maker = recovery
    prepare(recovery, report=True)
    entered, rejected = asyncio.Event(), []
    publish = ws._publish_cancelled_terminal

    def observe_cancel_before_report_lock(*args, **kwargs):
        assert bus.get_session("team-disc").cancel_event.is_set()
        return publish(*args, **kwargs)

    monkeypatch.setattr(ws, "_publish_cancelled_terminal", observe_cancel_before_report_lock)

    class Runner:
        async def start_discussion(self, **kwargs):
            assert runtime.has_agent_execution_scope()
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                with maker() as db:
                    assert db.get(ReviewTask, 81).status == "cancelled"
                    assert db.get(RoundtableSession, "team-disc").status == "cancelled"
                # 一个不配合取消的迟到回调仍不能把 cancelled 改成 success。
                bus.publish_control("team-disc", "done", {"status": "success", "task_id": 81})
                rejected.append(bus.get_session("team-disc").terminal_status)

    monkeypatch.setattr(orchestrator, "DiscussionOrchestrator", Runner)
    await ws.recover_persisted_discussions()
    running = bus.get_discussion_task("team-disc")
    await asyncio.wait_for(entered.wait(), 2)
    revoke(maker, reason)
    await asyncio.wait_for(running, 3)
    assert rejected == ["cancelled"]
    await recover_and_wait(bus)
    with maker() as db:
        assert db.get(ReviewTask, 81).status == "cancelled"
        assert db.get(RoundtableSession, "team-disc").status == "cancelled"


@pytest.mark.asyncio
async def test_service_shutdown_remains_recoverable_with_team_scope(recovery, monkeypatch):
    bus, maker = recovery
    prepare(recovery)
    entered = asyncio.Event()

    class Runner:
        async def start_discussion(self, **kwargs):
            entered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(orchestrator, "DiscussionOrchestrator", Runner)
    await ws.recover_persisted_discussions()
    running = bus.get_discussion_task("team-disc")
    await asyncio.wait_for(entered.wait(), 2)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    with maker() as db:
        assert db.get(RoundtableSession, "team-disc").progress["phase"] == "interrupted"
