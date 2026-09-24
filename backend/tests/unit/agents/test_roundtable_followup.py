"""圆桌正式结束后的五分钟追问、账号隔离与后台有序恢复。"""
import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents.discussion_bus import DiscussionBus
from app.agents.events import DiscussionTurn
from app.ai.deepseek_agent import DeepSeekOutputTruncatedError
from app.models.roundtable import RoundtableSession, RoundtableTurn
from app.services import roundtable_followup_service as followup


def _completed(bus: DiscussionBus, session_id: str = "disc_followup"):
    session = bus.create_session(session_id, 42, "auth.py", owner_user_id=7)
    bus.publish_turn(session_id, DiscussionTurn(
        turn_id=1, agent_code="security", agent_name="安全", role="agent", content="检查鉴权",
    ))
    session.report_task_id = 42
    bus.publish_control(session_id, "done", {"status": "success", "task_id": 42})
    bus.close_session(session_id)
    return session


def test_five_minute_boundary_and_replayed_deadline(monkeypatch: pytest.MonkeyPatch):
    clock = [1_000.0]
    monkeypatch.setattr("app.agents.discussion_bus.time.time", lambda: clock[0])
    bus = DiscussionBus()
    session = _completed(bus)
    assert bus.followup_until(session) == 1_300.0
    assert bus.accept_user_input(session.session_id, "报告完成后第一条追问") is True
    clock[0] = 1_299.999
    assert bus.accept_user_input(session.session_id, "期限前追问") is True
    clock[0] = 1_300.0
    assert bus.accept_user_input(session.session_id, "期限处追问") is False
    assert [turn.content for turn in session.turns if turn.role == "user"] == [
        "报告完成后第一条追问", "期限前追问",
    ]
    frames = [json.loads(frame) for frame in bus.recovery_frames(session.session_id, 7)]
    assert frames[-2]["payload"]["followup_until"] == 1_300.0
    assert frames[-1]["followup_until"] == 1_300.0


def test_restart_preserves_fixed_deadline_and_owner_scope(monkeypatch: pytest.MonkeyPatch):
    clock = [2_000.0]
    monkeypatch.setattr("app.agents.discussion_bus.time.time", lambda: clock[0])
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    RoundtableSession.__table__.create(engine)
    RoundtableTurn.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    before = DiscussionBus(persist=True, session_factory=factory)
    _completed(before, "disc_restart_followup")
    clock[0] = 2_150.0
    after = DiscussionBus(persist=True, session_factory=factory)
    assert after.get_session("disc_restart_followup", owner_user_id=8) is None
    session = after.get_session("disc_restart_followup", owner_user_id=7)
    assert session is not None
    assert after.followup_until(session) == 2_300.0
    from app.api.v1.discussion import get_discussion

    with factory() as db:
        detail = get_discussion(
            session.session_id, limit=100, before_seq=None, db=db,
            user=SimpleNamespace(id=7),
        ).data
        assert detail["followup_until"] == 2_300.0
    assert after.accept_user_input(session.session_id, "刷新后继续追问")
    assert [turn.seq for turn in after.get_turns_page(session.session_id, 7)["turns"]] == [1, 2]
    clock[0] = 2_300.0
    assert after.accept_user_input(session.session_id, "到期后不得写入") is False
    with factory() as db:
        assert db.query(RoundtableTurn).filter_by(session_id=session.session_id).count() == 2
    engine.dispose()


@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted"])
def test_unsuccessful_roundtable_has_no_followup(status: str):
    bus = DiscussionBus()
    session = bus.create_session("disc_no_followup", 0, "main.py", owner_user_id=7)
    session.report_task_id = 42
    bus.publish_control(session.session_id, "done", {"status": status, "task_id": 42})
    bus.close_session(session.session_id)
    assert bus.followup_until(session) == 0
    assert bus.accept_user_input(session.session_id, "失败任务追问") is False


@pytest.mark.asyncio
async def test_reopen_concluded_roundtable_replays_terminal_deadline(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.agents.discussion_bus.time.time", lambda: 3_000.0)
    bus = DiscussionBus()
    session = _completed(bus, "disc_reopen_followup")

    queue = await bus.subscribe(session.session_id)
    frames = [json.loads(queue.get_nowait()) for _ in range(queue.qsize())]
    assert frames[-2]["action"] == "done"
    assert frames[-2]["payload"]["followup_until"] == 3_300.0
    assert frames[-1] == {"type": "session_end", "followup_until": 3_300.0}
    assert bus.accept_user_input(session.session_id, "后台重开后的追问")


@pytest.mark.asyncio
async def test_followup_answers_are_serial_and_owner_scoped(monkeypatch: pytest.MonkeyPatch):
    bus = DiscussionBus()
    session = _completed(bus)
    assert bus.get_session(session.session_id, owner_user_id=8) is None
    assert bus.accept_user_input(session.session_id, "第一问")
    assert bus.accept_user_input(session.session_id, "第二问")
    captured: list[tuple[int, list[int]]] = []

    def fake_answer(_session, turns, question):
        captured.append((question.seq, [turn.seq for turn in turns]))
        return f"完整回答 {question.seq}"

    monkeypatch.setattr(followup, "_answer", fake_answer)
    followup.ensure_followup_worker(bus, session.session_id, 7)
    task = followup._workers[session.session_id]
    await asyncio.wait_for(task, timeout=2)
    assert [turn.content for turn in session.turns][-2:] == ["完整回答 2", "完整回答 3"]
    assert captured[0] == (2, [1, 2, 3])
    assert captured[1] == (3, [1, 2, 3, 4])
    assert [turn.seq for turn in session.turns] == list(range(1, 6))
    assert all(turn.round_index == -1 for turn in session.turns[-2:])


@pytest.mark.asyncio
async def test_restart_resumes_accepted_question_without_browser_reconnect(
    monkeypatch: pytest.MonkeyPatch,
):
    from app.core import database

    clock = [2_000.0]
    monkeypatch.setattr("app.agents.discussion_bus.time.time", lambda: clock[0])
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    RoundtableSession.__table__.create(engine)
    RoundtableTurn.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    before = DiscussionBus(persist=True, session_factory=factory)
    session = _completed(before, "disc_recover_unanswered")
    assert before.accept_user_input(session.session_id, "请解释鉴权证据")
    # 模拟后台执行器在确认问题落库后崩溃；禁言期限已过仍须完成已接收的问题。
    clock[0] = 2_400.0
    after = DiscussionBus(persist=True, session_factory=factory)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(followup, "_answer", lambda _session, _turns, _question: "证据在第 1 条。")
    assert await followup.resume_pending_followups(after) == 1
    await asyncio.wait_for(followup._workers[session.session_id], timeout=2)
    restored = after.get_session(session.session_id, owner_user_id=7)
    assert restored is not None
    assert [turn.content for turn in after.get_turns_page(session.session_id, 7)["turns"]][-1] == "证据在第 1 条。"
    assert after.accept_user_input(session.session_id, "过期新问题") is False
    # 已回答的问题再次启动扫描不能再触发额外模型调用。
    assert await followup.resume_pending_followups(after) == 0
    engine.dispose()


@pytest.mark.asyncio
async def test_truncated_model_output_is_visible_failure_not_success(monkeypatch: pytest.MonkeyPatch):
    bus = DiscussionBus()
    session = _completed(bus, "disc_failed_answer")
    assert bus.accept_user_input(session.session_id, "请说明遗漏")

    def truncated(*_args):
        raise RuntimeError("finish_reason=length")

    monkeypatch.setattr(followup, "_answer", truncated)
    followup.ensure_followup_worker(bus, session.session_id, 7)
    await asyncio.wait_for(followup._workers[session.session_id], timeout=2)
    assert "未能得到完整回答" in session.turns[-1].content
    assert "length" not in session.turns[-1].content
    assert session.turns[-1].turn_id == 2


def test_followup_retries_length_with_more_output_budget(monkeypatch: pytest.MonkeyPatch):
    from app.ai import discussion_orchestrator as orchestrator

    bus = DiscussionBus()
    session = _completed(bus, "disc_retry_answer")
    bus.accept_user_input(session.session_id, "证据在哪里？")
    monkeypatch.setattr(orchestrator, "_build_discussion_agents", lambda *_args: (object(), {}))
    monkeypatch.setattr(orchestrator, "_roundtable_history_records", lambda turns: [
        (str(turn.seq), turn.content) for turn in turns
    ])
    monkeypatch.setattr(orchestrator, "_compress_roundtable_history", lambda *_args, **_kwargs: "【来源 1】检查鉴权")
    monkeypatch.setattr(orchestrator, "_roundtable_input_budget_error", lambda *_args, **_kwargs: None)
    budgets = []

    def fake_model(*_args, **kwargs):
        budgets.append(kwargs["max_tokens"])
        if len(budgets) == 1:
            raise DeepSeekOutputTruncatedError("finish_reason=length")
        return "证据位于来源 1。", {}

    monkeypatch.setattr(orchestrator, "_call_raw_for_task", fake_model)
    answer = followup._answer(session, session.turns, session.turns[-1])
    assert answer == "证据位于来源 1。"
    assert len(budgets) == 2
    assert budgets[1] >= budgets[0]
