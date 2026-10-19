"""圆桌会话进度、重放与输入接受的回归测试。"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents.discussion_bus import DiscussionBus, DiscussionSession
from app.agents.events import DiscussionTurn
from app.core.exceptions import NotFoundError
from app.models.review_task import ReviewTask
from app.models.roundtable import RoundtableSession, RoundtableTurn


def _turn(index: int) -> DiscussionTurn:
    return DiscussionTurn(
        turn_id=index,
        agent_code="general",
        agent_name="通用质量代理",
        role="agent",
        content=f"第 {index} 条发言",
    )


def test_concluded_session_rejects_user_input_without_ghost_turn() -> None:
    bus = DiscussionBus()
    bus.create_session("disc_closed", 0, "main.py", owner_user_id=7)
    accepted: list[str] = []
    bus.set_controller("disc_closed", lambda action, payload: accepted.append(payload["content"]))
    bus.close_session("disc_closed")

    assert bus.accept_user_input("disc_closed", "结束后的消息") is False
    assert accepted == []
    assert bus.get_session("disc_closed").turns == []


def test_partial_report_progress_survives_restart_without_opening_followup() -> None:
    """有效部分报告须可重开且准确标识覆盖缺口，不能误开五分钟追问。"""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    RoundtableSession.__table__.create(engine)
    RoundtableTurn.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    before = DiscussionBus(persist=True, session_factory=factory)
    session = before.create_session("disc_partial", 185, "agent_service.py", owner_user_id=7)
    session.report_task_id = 185
    before.publish_control(session.session_id, "done", {"status": "failed", "partial": True, "task_id": 185})
    before.close_session(session.session_id)

    after = DiscussionBus(persist=True, session_factory=factory)
    restored = after.get_session(session.session_id, owner_user_id=7)
    assert restored is not None
    assert restored.progress["phase"] == "partial"
    assert after.followup_until(restored) == 0
    assert after.accept_user_input(session.session_id, "部分报告后追问") is False
    frames = [json.loads(frame) for frame in after.recovery_frames(session.session_id, 7)]
    assert frames[0]["payload"]["phase"] == "partial"
    assert frames[1]["payload"]["status"] == "partial"
    engine.dispose()


def test_old_failed_session_uses_same_owner_report_coverage_for_partial_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧会话未写 partial 标志时，REST 与重连仍根据报告账本正确展示。"""
    import app.core.database as database
    from app.api.v1.discussion import get_discussion, list_discussions

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    ReviewTask.__table__.create(engine)
    RoundtableSession.__table__.create(engine)
    RoundtableTurn.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", factory)
    with factory.begin() as db:
        report = ReviewTask(
            user_id=7, project_id=14, review_type="discuss", status="failed",
            coverage={"stage": "partial", "successful_turns": 9, "expected_turns": 10},
        )
        db.add(report)
        db.flush()
        report_id = report.id

    before = DiscussionBus(persist=True)
    session = before.create_session("disc_old_partial", 0, "agent_service.py", owner_user_id=7)
    session.report_task_id = report_id
    before.publish_control(session.session_id, "done", {"status": "failed", "task_id": report_id})
    before.close_session(session.session_id)
    assert session.progress["phase"] == "failed"

    restored_bus = DiscussionBus(persist=True)
    monkeypatch.setattr(DiscussionBus, "instance", classmethod(lambda cls: restored_bus))
    restored = restored_bus.get_session(session.session_id, owner_user_id=7)
    assert restored is not None and restored.progress["phase"] == "partial"
    assert restored_bus.followup_until(restored) == 0
    with factory() as db:
        owner = SimpleNamespace(id=7)
        other = SimpleNamespace(id=8)
        listing = list_discussions(limit=20, offset=0, db=db, user=owner).data
        assert listing["items"][0]["progress"]["phase"] == "partial"
        detail = get_discussion(session.session_id, limit=100, before_seq=None, db=db, user=owner).data
        assert detail["progress"]["phase"] == "partial"
        assert detail["followup_until"] == 0
        assert list_discussions(limit=20, offset=0, db=db, user=other).data["items"] == []
        with pytest.raises(NotFoundError):
            get_discussion(session.session_id, limit=100, before_seq=None, db=db, user=other)
        assert db.get(RoundtableSession, session.session_id).progress["phase"] == "failed"

    # 即使报告 ID 指向其它账号，也不能借其覆盖字段改写本账号圆桌的展示。
    with factory.begin() as db:
        db.get(ReviewTask, report_id).user_id = 8
    other_bus = DiscussionBus(persist=True)
    assert other_bus.get_session(session.session_id, owner_user_id=7).progress["phase"] == "failed"
    with factory() as db:
        listing = list_discussions(limit=20, offset=0, db=db, user=owner).data
        assert listing["items"][0]["progress"]["phase"] == "failed"
    engine.dispose()


@pytest.mark.asyncio
async def test_rest_stop_cancels_persisted_roundtable_and_report_for_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REST 停止须持久取消圆桌与报告，且不能被其他账号调用。"""
    import app.core.database as database
    from app.api.v1.discussion import stop_discussion

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    ReviewTask.__table__.create(engine)
    RoundtableSession.__table__.create(engine)
    RoundtableTurn.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", factory)
    bus = DiscussionBus(persist=True, session_factory=factory)
    monkeypatch.setattr(DiscussionBus, "instance", classmethod(lambda cls: bus))
    with factory.begin() as db:
        report = ReviewTask(
            user_id=7, project_id=14, review_type="discuss", status="running",
            coverage={"stage": "running"},
        )
        db.add(report)
        db.flush()
        report_id = report.id
    session = bus.create_session("disc_rest_stop", 0, "main.py", owner_user_id=7)
    assert bus.link_report_task(session.session_id, report_id) is True

    with factory() as db:
        result = await stop_discussion(
            "disc_rest_stop", db=db, user=SimpleNamespace(id=7),
        )
        assert result.data["status"] == "cancelled"
        assert result.data["terminal"] is True
        assert result.data["stop_requested"] is True
        assert result.data["provider_request_may_finish"] is True
        with pytest.raises(NotFoundError):
            await stop_discussion(
                "disc_rest_stop", db=db, user=SimpleNamespace(id=8),
            )

    with factory() as db:
        assert db.get(RoundtableSession, "disc_rest_stop").status == "cancelled"
        cancelled_report = db.get(ReviewTask, report_id)
        assert cancelled_report.status == "cancelled"
        assert cancelled_report.coverage["stage"] == "cancelled"
        assert cancelled_report.coverage["score_available"] is False
    engine.dispose()


@pytest.mark.asyncio
async def test_rest_stop_does_not_cancel_roundtable_after_report_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """报告先提交成功时，迟到的停止不能把圆桌状态改成 cancelled。"""
    import app.core.database as database
    from app.api.v1.discussion import stop_discussion

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    ReviewTask.__table__.create(engine)
    RoundtableSession.__table__.create(engine)
    RoundtableTurn.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", factory)
    bus = DiscussionBus(persist=True, session_factory=factory)
    monkeypatch.setattr(DiscussionBus, "instance", classmethod(lambda cls: bus))
    with factory.begin() as db:
        report = ReviewTask(
            user_id=7, project_id=14, review_type="discuss", status="success",
            coverage={"stage": "complete"},
        )
        db.add(report)
        db.flush()
        report_id = report.id
    session = bus.create_session("disc_late_stop", 0, "main.py", owner_user_id=7)
    assert bus.link_report_task(session.session_id, report_id) is True

    with factory() as db:
        result = await stop_discussion(
            "disc_late_stop", db=db, user=SimpleNamespace(id=7),
        )

    assert result.data["status"] == "active"
    assert result.data["stop_requested"] is False
    assert session.status == "active"
    assert session.terminal_status == ""
    with factory() as db:
        assert db.get(RoundtableSession, "disc_late_stop").status == "active"
        assert db.get(ReviewTask, report_id).status == "success"
    engine.dispose()


@pytest.mark.asyncio
async def test_rest_stop_cancels_registered_async_roundtable_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REST 停止须取消本进程中的 Agent 编排任务，终态不能被重启。"""
    import app.core.database as database
    from app.api.v1.discussion import stop_discussion

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    RoundtableSession.__table__.create(engine)
    RoundtableTurn.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", factory)
    bus = DiscussionBus(persist=True, session_factory=factory)
    monkeypatch.setattr(DiscussionBus, "instance", classmethod(lambda cls: bus))
    bus.create_session("disc_task_stop", 0, "main.py", owner_user_id=7)
    queue = await bus.subscribe("disc_task_stop")
    blocker = asyncio.Event()
    task = bus.start_discussion_task("disc_task_stop", blocker.wait())

    with factory() as db:
        result = await stop_discussion(
            "disc_task_stop", db=db, user=SimpleNamespace(id=7),
        )

    assert result.data["status"] == "cancelled"
    assert result.data["terminal"] is True
    assert task.cancelled()
    frames = [json.loads(queue.get_nowait()) for _ in range(queue.qsize())]
    actions = {frame.get("action") for frame in frames if frame.get("type") == "control"}
    assert {"cancelled", "done"}.issubset(actions)
    assert any(frame.get("type") == "session_end" for frame in frames)
    with pytest.raises(RuntimeError, match="终态圆桌不能由后台事件重新启动"):
        bus.start_discussion_task("disc_task_stop", blocker.wait())
    engine.dispose()


@pytest.mark.asyncio
async def test_cancelled_roundtable_ignores_late_events_and_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """停止提交取消终态后，迟到的成功/进度/发言不得复活会话。"""
    import app.core.database as database

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    RoundtableSession.__table__.create(engine)
    RoundtableTurn.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", factory)
    bus = DiscussionBus(persist=True, session_factory=factory)
    session = bus.create_session("disc_late_after_stop", 0, "main.py", owner_user_id=7)
    bus.publish_control(session.session_id, "started", {})
    bus.publish_control(session.session_id, "progress", {"phase": "speaking", "stage": "agent_thinking"})

    bus.request_stop(session.session_id, reason="用户停止")
    bus.publish_control(session.session_id, "cancelled", {"status": "cancelled", "error": "用户停止"})
    queue = await bus.subscribe(session.session_id)
    while not queue.empty():
        queue.get_nowait()
    queued_before_late_events = queue.qsize()

    bus.publish_control(session.session_id, "done", {"status": "success", "task_id": 999})
    bus.publish_control(session.session_id, "progress", {"phase": "speaking", "stage": "agent_thinking"})
    bus.publish_control(session.session_id, "resumed", {})
    bus.publish_turn(session.session_id, _turn(1))

    assert session.status == "cancelled"
    assert session.terminal_status == "cancelled"
    assert session.progress["phase"] == "cancelled"
    assert session.report_task_id == 0
    assert session.turns == []
    assert queue.qsize() == queued_before_late_events
    with factory() as db:
        persisted = db.get(RoundtableSession, session.session_id)
        assert persisted.status == "cancelled"
        assert persisted.progress["phase"] == "cancelled"
        assert persisted.report_task_id == 0
        assert db.query(RoundtableTurn).filter_by(session_id=session.session_id).count() == 0
    engine.dispose()


def test_repeated_stop_does_not_replace_successful_terminal_state() -> None:
    """取消操作只能结束活动会话，不能把已成功完成的会话改成 cancelled。"""
    bus = DiscussionBus()
    session = bus.create_session("disc_success_then_stop", 0, "main.py", owner_user_id=7)
    bus.publish_control(session.session_id, "done", {"status": "success", "task_id": 42})
    bus.close_session(session.session_id)

    assert bus.request_stop(session.session_id, reason="迟到的停止请求") is False

    assert session.status == "concluded"
    assert session.terminal_status == "success"
    assert session.progress["phase"] == "completed"
    assert session.report_task_id == 42


def test_stale_worker_stop_cannot_cancel_already_successful_session() -> None:
    """旧 worker 的 active 快照不能覆盖另一 worker 已提交的成功终态。"""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    RoundtableSession.__table__.create(engine)
    RoundtableTurn.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    completing_worker = DiscussionBus(persist=True, session_factory=factory)
    session = completing_worker.create_session(
        "disc_stale_stop_success", 42, "main.py", owner_user_id=7,
    )
    completing_worker.publish_control(
        session.session_id, "done", {"status": "success", "task_id": 42},
    )
    completing_worker.close_session(session.session_id)

    stale_worker = DiscussionBus(persist=True, session_factory=factory)
    stale_worker._sessions[session.session_id] = DiscussionSession(
        session_id=session.session_id,
        task_id=42,
        report_task_id=42,
        file_name="main.py",
        owner_user_id=7,
        status="active",
        progress={"phase": "speaking"},
    )

    assert stale_worker.request_stop(session.session_id, reason="迟到的停止请求") is False
    refreshed = stale_worker.get_session(session.session_id)
    assert refreshed is not None
    assert refreshed.status == "concluded"
    assert refreshed.terminal_status == "success"
    with factory() as db:
        row = db.get(RoundtableSession, session.session_id)
        assert row.status == "concluded"
        assert row.progress["terminal_status"] == "success"
    engine.dispose()


def test_cross_worker_stop_wins_over_late_report_link() -> None:
    """停止已落库后，另一 worker 绑定迟到报告不得把圆桌恢复为 active。"""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    RoundtableSession.__table__.create(engine)
    RoundtableTurn.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    stopping_worker = DiscussionBus(persist=True, session_factory=factory)
    session = stopping_worker.create_session(
        "disc_cross_worker_link", 0, "main.py", owner_user_id=7,
    )
    stale_worker = DiscussionBus(persist=True, session_factory=factory)
    stale_worker._sessions[session.session_id] = DiscussionSession(
        session_id=session.session_id,
        task_id=session.task_id,
        file_name=session.file_name,
        owner_user_id=session.owner_user_id,
        status="active",
        progress=dict(session.progress),
    )

    stopping_worker.request_stop(session.session_id, reason="用户停止")
    assert stale_worker.link_report_task(session.session_id, 9876) is False

    stale = stale_worker.get_session(session.session_id)
    assert stale is not None
    assert stale.status == "cancelled"
    assert stale.terminal_status == "cancelled"
    assert stale.report_task_id == 9876
    with factory() as db:
        row = db.get(RoundtableSession, session.session_id)
        assert row.status == "cancelled"
        assert row.report_task_id == 9876
        assert row.task_id == 9876
    engine.dispose()


def test_persisted_success_terminal_preserves_report_on_late_link() -> None:
    """成功终态已有报告时，迟到绑定不能把会话改指向新报告。"""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    ReviewTask.__table__.create(engine)
    RoundtableSession.__table__.create(engine)
    RoundtableTurn.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as db:
        original = ReviewTask(
            user_id=7, project_id=14, review_type="discuss", status="success",
            coverage={"stage": "complete"},
        )
        late = ReviewTask(
            user_id=7, project_id=14, review_type="discuss", status="running",
            coverage={"stage": "running"},
        )
        db.add_all([original, late])
        db.flush()
        original_id, late_id = original.id, late.id

    completing_worker = DiscussionBus(persist=True, session_factory=factory)
    session = completing_worker.create_session(
        "disc_late_link_after_success", 0, "main.py", owner_user_id=7,
    )
    assert completing_worker.link_report_task(session.session_id, original_id) is True
    completing_worker.publish_control(
        session.session_id, "done", {"status": "success", "task_id": original_id},
    )
    completing_worker.close_session(session.session_id)

    stale_worker = DiscussionBus(persist=True, session_factory=factory)
    stale_worker._sessions[session.session_id] = DiscussionSession(
        session_id=session.session_id,
        task_id=original_id,
        report_task_id=original_id,
        file_name="main.py",
        owner_user_id=7,
        status="active",
        progress={"phase": "speaking"},
    )
    assert stale_worker.link_report_task(session.session_id, late_id) is False

    refreshed = stale_worker.get_session(session.session_id)
    assert refreshed is not None
    assert refreshed.status == "concluded"
    assert refreshed.terminal_status == "success"
    assert refreshed.task_id == original_id
    assert refreshed.report_task_id == original_id
    with factory() as db:
        row = db.get(RoundtableSession, session.session_id)
        assert row.status == "concluded"
        assert row.task_id == original_id
        assert row.report_task_id == original_id
        assert db.get(ReviewTask, original_id).status == "success"
        assert db.get(ReviewTask, late_id).status == "running"
    engine.dispose()


def test_in_memory_success_terminal_preserves_report_on_late_link() -> None:
    bus = DiscussionBus()
    session = bus.create_session(
        "disc_local_late_link", 42, "main.py", owner_user_id=7,
    )
    session.report_task_id = 42
    bus.publish_control(session.session_id, "done", {"status": "success", "task_id": 42})
    bus.close_session(session.session_id)

    assert bus.link_report_task(session.session_id, 9876) is False
    assert session.status == "concluded"
    assert session.terminal_status == "success"
    assert session.task_id == 42
    assert session.report_task_id == 42


def test_cross_worker_stop_blocks_stale_progress_and_turns() -> None:
    """旧 worker 的进度和发言不能覆盖数据库中的停止终态。"""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    RoundtableSession.__table__.create(engine)
    RoundtableTurn.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    stopping_worker = DiscussionBus(persist=True, session_factory=factory)
    session = stopping_worker.create_session(
        "disc_cross_worker_events", 0, "main.py", owner_user_id=7,
    )
    stale_worker = DiscussionBus(persist=True, session_factory=factory)
    stale_worker._sessions[session.session_id] = DiscussionSession(
        session_id=session.session_id,
        task_id=session.task_id,
        file_name=session.file_name,
        owner_user_id=session.owner_user_id,
        status="active",
        progress=dict(session.progress),
    )

    stopping_worker.request_stop(session.session_id, reason="用户停止")
    queue = asyncio.Queue()
    stale_worker._queues[session.session_id] = [queue]
    stale_worker.publish_turn(session.session_id, _turn(1))
    assert stale_worker.get_session(session.session_id).status == "cancelled"
    stale_worker.publish_control(session.session_id, "progress", {
        "phase": "speaking", "stage": "agent_thinking",
    })

    stale = stale_worker.get_session(session.session_id)
    assert stale is not None and stale.status == "cancelled"
    assert queue.empty()
    with factory() as db:
        row = db.get(RoundtableSession, session.session_id)
        assert row.status == "cancelled"
        assert row.progress["phase"] == "cancelled"
        assert db.query(RoundtableTurn).filter_by(session_id=session.session_id).count() == 0
    engine.dispose()


def test_persisted_cancel_close_saves_closed_at_without_followup_window() -> None:
    """取消终态关闭时间应持久化，但不能因此开放成功追问窗口。"""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    RoundtableSession.__table__.create(engine)
    RoundtableTurn.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    bus = DiscussionBus(persist=True, session_factory=factory)
    session = bus.create_session("disc_persisted_cancel", 0, "main.py", owner_user_id=7)

    bus.request_stop(session.session_id, reason="用户停止")
    bus.close_session(session.session_id)

    with factory() as db:
        row = db.get(RoundtableSession, session.session_id)
        assert row.status == "cancelled"
        assert row.closed_at is not None
        assert "followup_start_seq" not in row.progress
    assert bus.followup_until(session) == 0
    engine.dispose()


def test_persisted_success_close_keeps_followup_window_after_terminal_commit() -> None:
    """正常成功终态提交后，关闭阶段仍能写入持久追问边界。"""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    RoundtableSession.__table__.create(engine)
    RoundtableTurn.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    bus = DiscussionBus(persist=True, session_factory=factory)
    session = bus.create_session("disc_persisted_success", 42, "main.py", owner_user_id=7)
    session.report_task_id = 42
    bus.publish_control(session.session_id, "done", {"status": "success", "task_id": 42})
    bus.close_session(session.session_id)

    with factory() as db:
        row = db.get(RoundtableSession, session.session_id)
        assert row.status == "concluded"
        assert row.progress["followup_start_seq"] == 0
        assert row.closed_at is not None
    assert bus.followup_until(session) > 0
    engine.dispose()


@pytest.mark.parametrize("stage", ["summarizing", "extracting", "reporting"])
def test_finalizing_session_saves_user_input_for_followup(stage: str) -> None:
    bus = DiscussionBus()
    session = bus.create_session("disc_finalizing", 0, "main.py", owner_user_id=7)
    accepted: list[str] = []
    bus.set_controller("disc_finalizing", lambda action, payload: accepted.append(payload["content"]))
    bus.publish_control("disc_finalizing", "progress", {"phase": stage})

    assert bus.accept_user_input("disc_finalizing", "收尾阶段迟到的问题") is True
    assert accepted == (["收尾阶段迟到的问题"] if stage == "summarizing" else [])
    assert [turn.content for turn in session.turns] == ["收尾阶段迟到的问题"]
    assert session.progress["finalizing_input_start_seq"] == 0
    session.report_task_id = 42
    bus.publish_control(session.session_id, "done", {"status": "success", "task_id": 42})
    bus.close_session(session.session_id)
    assert session.progress["followup_start_seq"] == 0
    assert bus.followup_until(session) > 0


def test_user_input_persist_failure_never_reaches_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """用户发言落库失败时不得被编排器消费，也不得伪造已发送消息。"""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    RoundtableSession.__table__.create(engine)
    RoundtableTurn.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    bus = DiscussionBus(persist=True, session_factory=factory)
    bus.create_session("disc_write_fail", 0, "main.py", owner_user_id=7)
    received: list[str] = []
    bus.set_controller("disc_write_fail", lambda _action, payload: received.append(payload["content"]))

    def fail_write(_session, _turn) -> None:
        raise RuntimeError("数据库不可写")

    monkeypatch.setattr(bus, "_persist_turn", fail_write)
    with pytest.raises(RuntimeError, match="数据库不可写"):
        bus.accept_user_input("disc_write_fail", "请检查鉴权")

    assert received == []
    assert bus.get_session("disc_write_fail").turns == []
    assert bus.get_session("disc_write_fail").last_turn_seq == 0
    with factory() as db:
        assert db.query(RoundtableTurn).count() == 0
        assert db.get(RoundtableSession, "disc_write_fail").last_turn_seq == 0
    engine.dispose()


@pytest.mark.asyncio
async def test_long_discussion_replay_is_bounded_and_ordered() -> None:
    bus = DiscussionBus()
    bus.create_session("disc_long", 0, "main.py", owner_user_id=7)
    for index in range(250):
        bus.publish_turn("disc_long", _turn(index))

    queue = await bus.subscribe("disc_long")
    replay = []
    while not queue.empty():
        replay.append(json.loads(queue.get_nowait()))

    turn_ids = [message["turn"]["turn_id"] for message in replay if message["type"] == "discuss"]
    assert turn_ids == list(range(150, 250))


def test_speaker_control_updates_progress_snapshot() -> None:
    bus = DiscussionBus()
    session = bus.create_session("disc_progress", 0, "main.py", owner_user_id=7, max_rounds=2)
    bus.publish_control("disc_progress", "round_start", {"round": 1, "total_rounds": 2})
    bus.publish_control("disc_progress", "speaker", {
        "speaker_code": "security", "speaker_index": 2, "total_speakers": 5,
    })

    assert session.progress["phase"] == "speaking"
    assert session.progress["round"] == 1
    assert session.progress["completed"] == 1
    assert session.progress["total"] == 13  # 十次发言 + 主持、抽取、报告
    assert session.progress["speaker_code"] == "security"
    assert session.progress["seq"] >= 2


def test_persisted_sessions_restore_order_and_enforce_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """刷新/新 Bus 可恢复最近发言，REST 只能读取本账号并能翻页。"""
    import app.core.database as database
    from app.api.v1.discussion import get_discussion, list_discussions

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    RoundtableSession.__table__.create(engine)
    RoundtableTurn.__table__.create(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", session_factory)
    first = DiscussionBus(persist=True)
    first.create_session(
        "disc_persisted", 0, "main.py", owner_user_id=7,
        max_rounds=2, agents=[{"code": "general", "name": "通用质量代理"}],
    )
    for index in range(150):
        first.publish_turn("disc_persisted", _turn(index))
    first.close_session("disc_persisted")

    restored = DiscussionBus(persist=True)
    monkeypatch.setattr(DiscussionBus, "instance", classmethod(lambda cls: restored))
    session = restored.get_session("disc_persisted", owner_user_id=7)
    assert session is not None
    assert session.last_turn_seq == 150
    assert [turn.seq for turn in session.turns] == list(range(51, 151))
    recent_page = restored.get_turns_page("disc_persisted", 7, limit=100)
    assert [turn.seq for turn in recent_page["turns"]] == list(range(51, 151))
    assert recent_page["total"] == 150
    assert recent_page["has_more"] is True
    oldest_page = restored.get_turns_page(
        "disc_persisted", 7, limit=100,
        before_seq=recent_page["next_before_seq"],
    )
    assert [turn.seq for turn in oldest_page["turns"]] == list(range(1, 51))
    with pytest.raises(PermissionError):
        restored.get_turns_page("disc_persisted", 8)

    with session_factory() as db:
        owner = SimpleNamespace(id=7)
        other = SimpleNamespace(id=8)
        listing = list_discussions(limit=20, offset=0, db=db, user=owner).data
        assert [item["session_id"] for item in listing["items"]] == ["disc_persisted"]
        assert list_discussions(limit=20, offset=0, db=db, user=other).data["items"] == []
        latest = get_discussion("disc_persisted", limit=100, before_seq=None, db=db, user=owner).data
        assert [turn["seq"] for turn in latest["turns"]] == list(range(51, 151))
        assert latest["has_earlier"] is True
        older = get_discussion(
            "disc_persisted", limit=100, before_seq=latest["next_before_seq"],
            db=db, user=owner,
        ).data
        assert [turn["seq"] for turn in older["turns"]] == list(range(1, 51))
        with pytest.raises(NotFoundError):
            get_discussion("disc_persisted", limit=100, before_seq=None, db=db, user=other)
    engine.dispose()


def test_active_session_after_process_restart_is_marked_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外账号无法通过缓存/恢复/REST 改写状态，所属账号恢复时才标中断。"""
    import app.core.database as database
    from app.api.v1.discussion import get_discussion

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    RoundtableSession.__table__.create(engine)
    RoundtableTurn.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", factory)
    original = DiscussionBus(persist=True)
    original.create_session("disc_interrupted", 0, "main.py", owner_user_id=7)
    original.publish_control("disc_interrupted", "round_start", {"round": 1, "total_rounds": 2})
    assert original.get_session("disc_interrupted", owner_user_id=8) is None

    restored = DiscussionBus(persist=True, session_factory=factory)
    assert restored.get_session("disc_interrupted") is None
    assert restored.get_session("disc_interrupted", owner_user_id=8) is None
    with pytest.raises(PermissionError):
        restored.get_turns_page("disc_interrupted", 8)
    monkeypatch.setattr(DiscussionBus, "instance", classmethod(lambda cls: restored))
    with factory() as db:
        with pytest.raises(NotFoundError):
            get_discussion(
                "disc_interrupted", limit=100, before_seq=None,
                db=db, user=SimpleNamespace(id=8),
            )
        assert db.get(RoundtableSession, "disc_interrupted").status == "active"

    session = restored.get_session("disc_interrupted", owner_user_id=7)

    assert session is not None
    assert session.status == "concluded"
    assert session.progress["phase"] == "interrupted"
    with factory() as db:
        row = db.get(RoundtableSession, "disc_interrupted")
        assert row.status == "concluded"
        assert row.progress["phase"] == "interrupted"
    engine.dispose()
