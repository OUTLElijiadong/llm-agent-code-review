"""会话状态读取只投影最新状态，避免把历史检查点读入轮询请求。"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.models.agent_response_run import AgentResponseRun
from app.services import agent_mesh_service


@pytest.fixture()
def state_db():
    engine = create_engine("sqlite:///:memory:")
    AgentResponseRun.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_run(db, *, run_id, owner=7, surface="user", session="session-a", status="completed", checkpoint="{}",
              created=None):
    db.add(AgentResponseRun(
        run_id=run_id, user_id=owner, surface=surface, session_key=session,
        status=status, checkpoint_json=checkpoint, create_time=created or datetime(2026, 10, 14),
    ))


def _capture_selects(db):
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(db.get_bind(), "before_cursor_execute", capture)
    return statements, capture


def test_latest_states_select_latest_id_for_exact_owner_surface_session_pairs(state_db):
    db = state_db
    _seed_run(db, run_id="old-user", status="running")
    _seed_run(db, run_id="old-admin", surface="admin", session="session-b", status="failed")
    _seed_run(db, run_id="latest-user", status="completed", created=datetime(2026, 10, 13))
    _seed_run(db, run_id="latest-admin", surface="admin", session="session-b", status="waiting_input")
    # 这些较大的 ID 必须排除：不能把 surface/session 各自 IN 拼成笛卡尔配对。
    _seed_run(db, run_id="cross-pair-a", surface="admin", status="failed")
    _seed_run(db, run_id="cross-pair-b", session="session-b", status="failed")
    _seed_run(db, run_id="other-owner", owner=8, status="failed")
    db.commit()

    states = agent_mesh_service._latest_run_states(db, user_id=7, conversation_refs=[
        ("user", "session-a"), ("admin", "session-b"), ("user", "session-a"), ("user", "missing"),
    ])

    assert list(states) == [("admin", "session-b"), ("user", "session-a")]
    assert states == {
        ("user", "session-a"): ("latest-user", "completed"),
        ("admin", "session-b"): ("latest-admin", "waiting_input"),
    }


def test_latest_states_do_not_select_or_materialize_large_history_checkpoints(state_db):
    db = state_db
    checkpoint = '{"synthetic_payload":"' + "x" * (1024 * 1024) + '"}'
    for index in range(36):
        _seed_run(db, run_id=f"synthetic-{index}", checkpoint=checkpoint,
                  created=datetime(2026, 10, 14) + timedelta(seconds=index))
    db.commit()
    db.expunge_all()
    statements, capture = _capture_selects(db)
    loaded = []

    def observe_load(row, _context):
        loaded.append(row.id)

    event.listen(AgentResponseRun, "load", observe_load)
    try:
        states = agent_mesh_service._latest_run_states(db, user_id=7, conversation_refs=[("user", "session-a")])
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", capture)
        event.remove(AgentResponseRun, "load", observe_load)

    assert states == {("user", "session-a"): ("synthetic-35", "completed")}
    assert len(statements) == 1
    assert "checkpoint_json" not in statements[0].lower()
    assert "max(agent_response_run.id)" in statements[0].lower()
    assert loaded == []


def test_authoritative_state_projects_only_latest_run_id_and_status(state_db):
    db = state_db
    checkpoint = '{"synthetic_payload":"' + "x" * (2 * 1024 * 1024) + '"}'
    _seed_run(db, run_id="older", status="running", checkpoint=checkpoint)
    _seed_run(db, run_id="newer", status="completed", checkpoint=checkpoint, created=datetime(2026, 10, 13))
    _seed_run(db, run_id="other", owner=8, status="failed", checkpoint=checkpoint)
    _seed_run(db, run_id="admin", surface="admin", status="failed", checkpoint=checkpoint)
    db.commit()
    db.expunge_all()
    statements, capture = _capture_selects(db)
    try:
        state = agent_mesh_service._authoritative_run_state(db, user_id=7, surface="user", session_key="session-a")
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", capture)

    assert state == ("newer", "completed")
    assert len(statements) == 1
    assert "checkpoint_json" not in statements[0].lower()
    assert "agent_response_run.run_id" in statements[0].lower()
    assert "agent_response_run.status" in statements[0].lower()


def test_empty_or_unknown_conversations_have_no_state(state_db):
    db = state_db
    statements, capture = _capture_selects(db)
    try:
        assert agent_mesh_service._latest_run_states(db, user_id=7, conversation_refs=[]) == {}
        assert statements == []
        assert agent_mesh_service._authoritative_run_state(
            db, user_id=7, surface="user", session_key="missing",
        ) == ("", "")
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", capture)
