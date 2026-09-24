"""圆桌会话进度、重放与输入接受的回归测试。"""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents.discussion_bus import DiscussionBus
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


@pytest.mark.parametrize("stage", ["summarizing", "extracting", "reporting"])
def test_finalizing_session_saves_user_input_for_followup(stage: str) -> None:
    bus = DiscussionBus()
    session = bus.create_session("disc_finalizing", 0, "main.py", owner_user_id=7)
    accepted: list[str] = []
    bus.set_controller("disc_finalizing", lambda action, payload: accepted.append(payload["content"]))
    bus.publish_control("disc_finalizing", "progress", {"phase": stage})

    assert bus.accept_user_input("disc_finalizing", "收尾阶段迟到的问题") is True
    assert accepted == []
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
