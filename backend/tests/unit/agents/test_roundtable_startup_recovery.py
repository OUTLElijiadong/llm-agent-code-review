"""圆桌启动失败、可信持久续接与人工停止保护；仅使用隔离 SQLite。"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agents.discussion_bus import DiscussionBus
from app.ai import discussion_orchestrator as orchestrator
from app.ai.multi_agent import GENERAL_AGENT
from app.api.v1 import ws_discussion as ws
from app.models.review_task import ReviewTask
from app.models.roundtable import RoundtableSession, RoundtableTurn


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    # 总线与线程池各持独立事务；StaticPool 会让另一个 Session 的 rollback 撤销当前写入。
    engine = create_engine(
        f"sqlite:///{tmp_path / 'startup-recovery.sqlite'}", connect_args={"check_same_thread": False},
    )
    from app.core.database import Base

    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    bus = DiscussionBus(persist=True, session_factory=maker)
    monkeypatch.setattr(DiscussionBus, "_instance", bus)
    monkeypatch.setattr(ws, "SessionLocal", maker)
    monkeypatch.setattr(orchestrator, "SessionLocal", maker)
    monkeypatch.setattr(ws, "purge_stale_pending", lambda: None)
    monkeypatch.setattr(ws, "_is_discussion_execution_active", lambda *_: True)
    monkeypatch.setattr(ws, "_is_parent_response_run_cancelled", lambda *_: False)
    monkeypatch.setattr(ws, "_pending", {})
    monkeypatch.setattr(ws, "_session_owners", {})
    monkeypatch.setattr(ws, "_owner_registered_at", {})
    monkeypatch.setattr(ws, "_schedule_next_pending_expiry_locked", lambda: None)
    yield bus, maker
    engine.dispose()


def prepare(bus, sid="disc_recover", owner=7):
    session = bus.create_session(sid, 0, "main.py", owner_user_id=owner, project_id=9, file_id=11)
    session.status = "pending"
    bus._update_session(session)
    ws.register_pending(
        session_id=sid, profiles=(GENERAL_AGENT,), code="original immutable code", language="python",
        file_name="main.py", user_id=owner, project_id=9, file_id=11, max_rounds=2,
        session_token_version=3,
    )
    return session, ws.take_pending(sid)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["authorization", "construction", "execution"])
async def test_startup_exception_closes_session_and_linked_report(runtime, monkeypatch, failure):
    bus, maker = runtime
    session, pending = prepare(bus)
    with maker.begin() as db:
        db.add(ReviewTask(id=51, user_id=7, project_id=9, review_type="discuss", status="running"))
    bus.link_report_task(session.session_id, 51)

    def explode(*_args, **_kwargs):
        raise RuntimeError("startup unavailable")

    class Runner:
        async def start_discussion(self, **kwargs):
            explode()

    monkeypatch.setattr(orchestrator, "DiscussionOrchestrator", Runner)
    if failure == "authorization":
        monkeypatch.setattr(ws, "_is_discussion_execution_active", explode)
    if failure == "construction":
        monkeypatch.setattr(orchestrator, "DiscussionOrchestrator", explode)
    await ws._run_pending_discussion(pending)
    with maker() as db:
        row = db.get(RoundtableSession, session.session_id)
        assert row.status == "failed" and row.closed_at is not None
        assert row.progress["phase"] == "failed"
        assert db.get(ReviewTask, 51).status == "failed"
    assert session.terminal_status == "failed"


def test_unavailable_main_loop_cannot_leave_claimed_pending_running(runtime, monkeypatch):
    bus, maker = runtime
    session, pending = prepare(bus)
    monkeypatch.setattr(ws, "_MAIN_LOOP", None)
    with pytest.raises(RuntimeError, match="主事件循环"):
        ws.launch_pending_discussion_from_thread(pending)
    with maker() as db:
        row = db.get(RoundtableSession, session.session_id)
        assert row.status == "failed" and row.closed_at is not None


@pytest.mark.asyncio
async def test_restart_recovers_original_owner_input_and_same_report_but_never_user_stop(runtime, monkeypatch):
    bus, maker = runtime
    session, _ = prepare(bus)
    with maker.begin() as db:
        db.add(ReviewTask(id=61, user_id=7, project_id=9, review_type="discuss", status="running"))
    bus.link_report_task(session.session_id, 61)
    bus.publish_control(session.session_id, "started", {})
    stopped, _ = prepare(bus, "disc_stopped")
    bus.request_stop(stopped.session_id, reason="用户停止")
    calls = []

    class Runner:
        async def start_discussion(self, **kwargs):
            calls.append(kwargs)
            bus_after.publish_control(kwargs["session_id"], "done", {"status": "success", "task_id": 61})
            bus_after.close_session(kwargs["session_id"])

    monkeypatch.setattr(orchestrator, "DiscussionOrchestrator", Runner)
    bus_after = DiscussionBus(persist=True, session_factory=maker)
    monkeypatch.setattr(DiscussionBus, "_instance", bus_after)
    ws._pending.clear()
    ws._session_owners.clear()
    assert bus_after.get_session(session.session_id, owner_user_id=999) is None
    await ws.recover_persisted_discussions()
    running = bus_after.get_discussion_task(session.session_id)
    if running:
        await running
    assert len(calls) == 1
    assert calls[0]["user_id"] == 7 and calls[0]["code"] == "original immutable code"
    assert calls[0]["profiles"] == (GENERAL_AGENT,)
    assert calls[0]["resume_task_id"] == 61
    with maker() as db:
        assert db.get(RoundtableSession, stopped.session_id).status == "cancelled"
        assert db.get(RoundtableSession, session.session_id).report_task_id == 61
    assert ws.launch_pending_discussion(ws.PendingDiscussion(stopped.session_id, user_id=7)) is None


@pytest.mark.asyncio
async def test_private_recovery_payload_is_absent_from_public_frames_and_rest_progress(runtime):
    from app.agents.discussion_bus import visible_roundtable_progress

    bus, maker = runtime
    session, _ = prepare(bus)
    assert session.progress.get("_recovery"), "启动上下文必须先落库再入内存 pending"
    queue = await bus.subscribe(session.session_id)
    frames = []
    while not queue.empty():
        frames.append(json.loads(queue.get_nowait()))
    frames += [json.loads(frame) for frame in bus.recovery_frames(session.session_id, 7)]
    bus.publish_control(session.session_id, "progress", {"phase": "preparing"})
    while not queue.empty():
        frames.append(json.loads(queue.get_nowait()))
    with maker() as db:
        public = visible_roundtable_progress(db, db.get(RoundtableSession, session.session_id))
    assert "_recovery" not in public
    assert "original immutable code" not in json.dumps([frames, public])


def test_stopped_report_cannot_dispatch_model_even_inside_worker(runtime, monkeypatch):
    _, maker = runtime
    with maker.begin() as db:
        db.add(ReviewTask(id=71, user_id=7, project_id=9, review_type="discuss", status="cancelled"))
    monkeypatch.setattr(orchestrator, "_require_discussion_actor", lambda *_: None)
    calls = []
    agent = SimpleNamespace(call_raw=lambda **kwargs: calls.append(kwargs))
    with pytest.raises(RuntimeError, match="终止|运行|取消"):
        orchestrator._call_raw_for_task(agent, 71, 7, system_prompt="", user_prompt="")
    assert calls == []


@pytest.mark.asyncio
async def test_service_shutdown_preserves_recoverable_report_instead_of_user_cancel(runtime, monkeypatch):
    bus, maker = runtime
    session, pending = prepare(bus)
    with maker.begin() as db:
        db.add(ReviewTask(id=81, user_id=7, project_id=9, review_type="discuss", status="running"))
    bus.link_report_task(session.session_id, 81)
    entered = asyncio.Event()

    class Runner:
        async def start_discussion(self, **kwargs):
            entered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(orchestrator, "DiscussionOrchestrator", Runner)
    running = asyncio.create_task(ws._run_pending_discussion(pending))
    await asyncio.wait_for(entered.wait(), 2)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    with maker() as db:
        row = db.get(RoundtableSession, session.session_id)
        assert row.progress["phase"] == "interrupted"
        assert row.progress["terminal_status"] == "interrupted"
        assert row.closed_at is not None
        assert db.get(ReviewTask, 81).coverage["stage"] == "interrupted"
        assert db.get(ReviewTask, 81).status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [False, True])
async def test_runner_return_without_report_completion_cannot_leave_fake_running(runtime, monkeypatch, terminal):
    bus, maker = runtime
    session, pending = prepare(bus)
    with maker.begin() as db:
        db.add(ReviewTask(id=82, user_id=7, project_id=9, review_type="discuss", status="running"))
    bus.link_report_task(session.session_id, 82)

    class Runner:
        async def start_discussion(self, **kwargs):
            if terminal:
                bus.publish_control(session.session_id, "done", {"status": "failed", "task_id": 82})
                bus.close_session(session.session_id)

    monkeypatch.setattr(orchestrator, "DiscussionOrchestrator", Runner)
    await ws._run_pending_discussion(pending)
    with maker() as db:
        assert db.get(ReviewTask, 82).status == "failed"
        assert db.get(RoundtableSession, session.session_id).status == "failed"


@pytest.mark.asyncio
async def test_recovery_auth_rejection_cancels_linked_report_before_any_model(runtime, monkeypatch):
    bus, maker = runtime
    session, pending = prepare(bus)
    with maker.begin() as db:
        db.add(ReviewTask(id=83, user_id=7, project_id=9, review_type="discuss", status="running"))
    bus.link_report_task(session.session_id, 83)
    monkeypatch.setattr(ws, "_is_discussion_execution_active", lambda *_: False)
    await ws._run_pending_discussion(pending)
    with maker() as db:
        assert db.get(ReviewTask, 83).status == "cancelled"
        assert db.get(RoundtableSession, session.session_id).status == "cancelled"


@pytest.mark.asyncio
async def test_restart_reconciles_committed_success_without_restarting(runtime, monkeypatch):
    bus, maker = runtime
    session, _ = prepare(bus)
    with maker.begin() as db:
        db.add(ReviewTask(id=84, user_id=7, project_id=9, review_type="discuss", status="success"))
    bus.link_report_task(session.session_id, 84)
    starts = []
    monkeypatch.setattr(ws, "launch_pending_discussion", starts.append)
    await ws.recover_persisted_discussions()
    assert starts == []
    with maker() as db:
        row = db.get(RoundtableSession, session.session_id)
        assert row.status == "concluded" and row.closed_at is not None
        assert row.progress["terminal_status"] == "success"
    assert bus.get_session(session.session_id).terminal_status == "success"


@pytest.mark.asyncio
async def test_real_orchestrator_resumes_checkpoint_and_commits_same_report(runtime, monkeypatch):
    """真实调度/持久化/报告收尾，仅模型传输和外部环境使用替身。"""
    from datetime import datetime, timezone
    from threading import Event

    from app.models.code_file import CodeFile
    from app.models.code_version import CodeVersion
    from app.models.review_task_file import ReviewTaskFile

    bus, maker = runtime
    session, pending = prepare(bus)
    code = pending.kwargs["code"]
    with maker.begin() as db:
        db.add(CodeFile(id=11, project_id=9, file_name="main.py", language="python", content=code))
        db.add(CodeVersion(file_id=11, version_no=1, content=code, create_time=datetime.now(timezone.utc)))
    monkeypatch.setattr(orchestrator, "_require_discussion_actor", lambda *_: None)
    monkeypatch.setattr(orchestrator, "build_discussion_environment", lambda **_: None)
    monkeypatch.setattr(orchestrator, "_extract_issues", lambda *_, **__: [])
    entered = asyncio.Event()
    release = Event()
    loop = asyncio.get_running_loop()
    calls = []
    shutting_down = True

    def call_raw(**kwargs):
        calls.append(kwargs)
        if shutting_down and len(calls) == 2:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "测试应及时释放模拟的在途请求"
        text = "主持人完成共识" if kwargs.get("agent_label") == "general" and not kwargs.get("json_mode") else (
            '{"action":"speak","stance":"propose","reply_to":null,"content":"专家已核对原始输入"}'
        )
        return text, {"model_name": "fake-model"}

    agent = SimpleNamespace(model="fake-model", call_raw=call_raw)
    monkeypatch.setattr(orchestrator, "_build_discussion_agents", lambda *_: (agent, {"code_reviewer": agent}))
    running = asyncio.create_task(ws._run_pending_discussion(pending))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        with maker() as db:
            row = db.get(RoundtableSession, session.session_id)
            original_task_id = row.report_task_id
            checkpoint = row.progress["_checkpoint"]
            assert len(checkpoint["remaining_plan"]) == 1
            assert len(checkpoint["turns"]) == 1
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    finally:
        release.set()
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
    # 模拟原线程已退出，再从空总线恢复；当前代码文件已变更也不得替换冻结输入。
    shutting_down = False
    with maker.begin() as db:
        db.get(CodeFile, 11).content = "changed after scan"
    bus_after = DiscussionBus(persist=True, session_factory=maker)
    monkeypatch.setattr(DiscussionBus, "_instance", bus_after)
    await ws.recover_persisted_discussions()
    resumed = bus_after.get_discussion_task(session.session_id)
    assert resumed is not None
    await asyncio.wait_for(resumed, 8)
    with maker() as db:
        row = db.get(RoundtableSession, session.session_id)
        task = db.get(ReviewTask, original_task_id)
        assert row.status == "concluded" and row.report_task_id == original_task_id
        assert task.status == "success" and task.coverage["stage"] == "complete"
        assert task.coverage["model_calls_used"] == 4
        assert db.query(ReviewTask).count() == 1
        assert db.query(ReviewTaskFile).filter_by(task_id=original_task_id).count() == 1
        turns = [item.turn for item in db.query(RoundtableTurn).order_by(RoundtableTurn.seq).all()]
        expert_turns = [turn for turn in turns if turn["agent_code"] == "general"]
        assert [turn["round_index"] for turn in expert_turns] == [1, 2]
        assert len({turn["seq"] for turn in turns}) == len(turns)
    assert len(calls) == 4
    assert all(code in call["user_prompt"] for call in calls)
    assert all("changed after scan" not in call["user_prompt"] for call in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["preflight_commit", "registration", "after_registration"])
async def test_preflight_and_registration_failure_close_pending(runtime, monkeypatch, failure_stage):
    from sqlalchemy import event

    from app.ai import multi_agent
    from app.api.v1 import discussion as api
    from app.models.code_file import CodeFile
    from app.models.project import Project
    from app.services import rbac_service

    bus, maker = runtime
    with maker.begin() as db:
        db.add(Project(id=9, user_id=7, project_name="preflight", status="active", language="python"))
        db.add(CodeFile(id=11, project_id=9, file_name="main.py", language="python", content="print(1)"))
    monkeypatch.setattr(api, "require_project_execution", lambda *_: None)
    monkeypatch.setattr(rbac_service, "check_permission", lambda *_: True)
    monkeypatch.setattr(multi_agent, "get_discussion_agent_profiles", lambda: (GENERAL_AGENT,))
    monkeypatch.setattr(api.rule_service, "get_enabled_rules", lambda *_, **__: [])
    monkeypatch.setattr(api, "_gen_session_id", lambda: "disc_preflight_failure")
    register = api.register_pending

    def fail_registration(**kwargs):
        if failure_stage == "after_registration":
            register(**kwargs)
        raise RuntimeError("injected startup failure")

    with maker() as db:
        if failure_stage == "preflight_commit":
            def fail_commit(_db):
                raise RuntimeError("injected startup failure")

            event.listen(db, "before_commit", fail_commit, once=True)
        else:
            monkeypatch.setattr(api, "register_pending", fail_registration)
        with pytest.raises(RuntimeError, match="injected startup failure"):
            api.start_discussion(
                project_id=9, file_id=11, review_type="full", db=db,
                user=SimpleNamespace(id=7, token_version=3),
            )
    with maker() as db:
        row = db.get(RoundtableSession, "disc_preflight_failure")
        assert row.status == "failed" and row.closed_at is not None
        assert row.progress["terminal_status"] == "failed"
        assert db.query(ReviewTask).count() == 0
    assert "disc_preflight_failure" not in ws._pending
    launches = []
    monkeypatch.setattr(ws, "launch_pending_discussion", launches.append)
    await ws.recover_persisted_discussions()
    assert launches == []
    assert bus.get_discussion_task("disc_preflight_failure") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("issue_count,outcome", [(0, "resume"), (1, "resume"), (0, "budget"),
                                                (0, "cancelled"), (0, "revoked")])
async def test_successful_extraction_is_reused_after_shutdown(runtime, monkeypatch, issue_count, outcome):
    """在成功抽取与问题归一化之间停机，恢复不能再次派发抽取请求。"""
    from datetime import datetime, timezone
    from hashlib import sha256
    from threading import Event

    from app.models.code_file import CodeFile
    from app.models.code_version import CodeVersion
    from app.models.review_issue import ReviewIssue
    from app.models.review_task_file import ReviewTaskFile

    bus, maker = runtime
    session, pending = prepare(bus)
    code = pending.kwargs["code"]
    with maker.begin() as db:
        db.add(CodeFile(id=11, project_id=9, file_name="main.py", language="python", content=code))
        db.add(CodeVersion(file_id=11, version_no=1, content=code, create_time=datetime.now(timezone.utc)))
    monkeypatch.setattr(orchestrator, "_require_discussion_actor", lambda *_: None)
    monkeypatch.setattr(orchestrator, "build_discussion_environment", lambda **_: None)
    monkeypatch.setattr(orchestrator.DeepSeekAgent, "log_deferred", lambda *_, **__: None)
    entered = asyncio.Event()
    release = Event()
    finished = Event()
    loop = asyncio.get_running_loop()
    calls = []
    normalize = orchestrator._normalize_discussion_issues
    finalize = orchestrator._finalize_review
    first_normalization = True

    def normalize_after_extraction(*args, **kwargs):
        nonlocal first_normalization
        if first_normalization:
            first_normalization = False
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "应及时释放报告归一化线程"
        return normalize(*args, **kwargs)

    def track_finalizer(**kwargs):
        try:
            return finalize(**kwargs)
        finally:
            finished.set()

    def call_raw(**kwargs):
        kind = (
            "extract" if "本批目标来源" in kwargs.get("user_prompt", "")
            else "speaker" if kwargs.get("json_mode") else "summary"
        )
        calls.append((kind, kwargs))
        issues = [{
            "issue_type": "其他", "severity": "低", "line_number": 1,
            "description": "冻结代码的可复核问题", "evidence": code,
        }] if issue_count else []
        raw = (
            json.dumps({"issues": issues}, ensure_ascii=False) if kind == "extract"
            else '{"action":"speak","stance":"propose","reply_to":null,"content":"专家已核对原始输入"}'
            if kind == "speaker" else "主持人完成共识"
        )
        return raw, {"model_name": "fake-model"}

    agent = SimpleNamespace(model="fake-model", call_raw=call_raw)
    monkeypatch.setattr(orchestrator, "_build_discussion_agents", lambda *_: (agent, {"code_reviewer": agent}))
    monkeypatch.setattr(orchestrator, "_normalize_discussion_issues", normalize_after_extraction)
    monkeypatch.setattr(orchestrator, "_finalize_review", track_finalizer)
    running = asyncio.create_task(ws._run_pending_discussion(pending))
    try:
        await asyncio.wait_for(entered.wait(), 8)
        with maker() as db:
            row = db.get(RoundtableSession, session.session_id)
            original_task_id = row.report_task_id
            checkpoint = row.progress["_checkpoint"]
            assert checkpoint["summary_done"] is True
            assert checkpoint["coverage"]["model_calls_used"] == 4
            assert checkpoint["extraction"]["binding"] == {
                "task_id": original_task_id, "user_id": 7, "project_id": 9, "file_id": 11,
                "content_sha256": sha256(code.encode("utf-8")).hexdigest(),
            }
            assert len(checkpoint["extraction"]["issues"]) == issue_count
            assert checkpoint["extraction"]["batches"] == {"completed": 1, "total": 1}
        assert [kind for kind, _ in calls] == ["speaker", "speaker", "summary", "extract"]
        if outcome == "cancelled":
            from app.api.v1 import discussion as api

            with maker() as db:
                result = await api.stop_discussion(session.session_id, db=db, user=SimpleNamespace(id=7))
                assert result.data["status"] == "cancelled" and result.data["stop_requested"]
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    finally:
        release.set()
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        assert await asyncio.to_thread(finished.wait, 5), "旧 finalizer 必须退出再恢复"
    with maker.begin() as db:
        db.get(CodeFile, 11).content = "changed after scan"
        task = db.get(ReviewTask, original_task_id)
        if outcome == "cancelled":
            assert task.status == "cancelled" and task.coverage["stage"] == "cancelled"
        else:
            assert task.status == "failed" and task.coverage["stage"] == "interrupted"
        if outcome == "budget":
            task.coverage = {**task.coverage, "model_calls_used": orchestrator._ROUNDTABLE_MAX_MODEL_CALLS}
    if outcome == "revoked":
        def deny_actor(*_):
            raise RuntimeError("execution permission revoked")

        monkeypatch.setattr(orchestrator, "_require_discussion_actor", deny_actor)
    bus_after = DiscussionBus(persist=True, session_factory=maker)
    monkeypatch.setattr(DiscussionBus, "_instance", bus_after)
    await ws.recover_persisted_discussions()
    resumed = bus_after.get_discussion_task(session.session_id)
    if outcome == "cancelled":
        assert resumed is None
    else:
        assert resumed is not None
        await asyncio.wait_for(resumed, 8)
    assert [kind for kind, _ in calls] == ["speaker", "speaker", "summary", "extract"]
    if outcome in {"cancelled", "revoked"}:
        with maker() as db:
            row = db.get(RoundtableSession, session.session_id)
            task = db.get(ReviewTask, original_task_id)
            assert row.status == ("cancelled" if outcome == "cancelled" else "failed")
            assert row.closed_at is not None and row.report_task_id == original_task_id
            assert task.status == row.status
            assert task.coverage["model_calls_used"] == 4
            assert db.query(ReviewIssue).filter_by(task_id=original_task_id).count() == 0
            assert db.query(ReviewTask).count() == 1
        return
    with maker() as db:
        row = db.get(RoundtableSession, session.session_id)
        task = db.get(ReviewTask, original_task_id)
        assert row.status == "concluded" and row.report_task_id == original_task_id
        assert task.status == "success" and task.coverage["stage"] == "complete"
        expected_calls = orchestrator._ROUNDTABLE_MAX_MODEL_CALLS if outcome == "budget" else 4
        assert task.coverage["model_calls_used"] == expected_calls
        assert db.query(ReviewTask).count() == 1
        assert db.query(ReviewTaskFile).filter_by(task_id=original_task_id).one().content_sha256 == (
            sha256(code.encode("utf-8")).hexdigest()
        )
        assert db.query(ReviewIssue).filter_by(task_id=original_task_id).count() == issue_count
    assert all(code in call["user_prompt"] for _, call in calls)
    assert all("changed after scan" not in call["user_prompt"] for _, call in calls)


@pytest.mark.parametrize("field", ["task_id", "user_id", "project_id", "file_id", "content_sha256"])
def test_extraction_checkpoint_binding_mismatch_fails_closed(runtime, monkeypatch, field):
    from hashlib import sha256

    from app.models.review_issue import ReviewIssue

    _, maker = runtime
    with maker.begin() as db:
        db.add(ReviewTask(id=91, user_id=7, project_id=9, review_type="discuss", status="running"))
    monkeypatch.setattr(orchestrator, "_require_discussion_actor", lambda *_: None)
    calls = []
    monkeypatch.setattr(orchestrator, "_extract_issues", lambda *_, **__: calls.append("extract") or [])
    binding = {"task_id": 91, "user_id": 7, "project_id": 9, "file_id": 11,
               "content_sha256": sha256(b"original immutable code").hexdigest()}
    binding[field] = "wrong hash" if field == "content_sha256" else 999
    orchestrator._finalize_review(
        task_id=91, user_id=7, file_id=11, file_name="main.py", all_turns=[],
        code="original immutable code", language="python", deferred_logs=[],
        agent=SimpleNamespace(model="fake-model"), stopped=False,
        coverage={"valid_speeches": 1},
        cached_extraction={"binding": binding, "issues": [], "batches": {"completed": 1, "total": 1}},
    )
    assert calls == []
    with maker() as db:
        task = db.get(ReviewTask, 91)
        assert task.status == "failed" and task.coverage["score_available"] is False
        assert "抽取检查点与原报告或冻结输入不匹配" in task.error_message
        assert db.query(ReviewIssue).filter_by(task_id=91).count() == 0
