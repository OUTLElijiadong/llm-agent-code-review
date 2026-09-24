"""重启后圆桌与普通审查的恢复分流。"""

from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents.discussion_bus import DiscussionBus
from app.core import database
from app.main import _reconcile_orphan_reviews
from app.models.review_task import ReviewTask
from app.models.roundtable import RoundtableSession, RoundtableTurn


def test_restart_interrupts_roundtable_atomically_without_ordinary_redispatch(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    ReviewTask.__table__.create(engine)
    RoundtableSession.__table__.create(engine)
    RoundtableTurn.__table__.create(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", maker)
    with maker.begin() as db:
        db.add_all([
            ReviewTask(
                id=401, user_id=7, project_id=9, review_type="discuss",
                status="running", execution_token="roundtable-lease",
            ),
            ReviewTask(
                id=402, user_id=7, project_id=9, review_type="standard",
                status="running", execution_token="ordinary-lease",
            ),
            RoundtableSession(
                session_id="disc_restart", owner_user_id=7, task_id=401,
                file_name="main.py", status="active", max_rounds=2,
                agents=[], progress={"phase": "speaking", "seq": 3},
                created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
            ),
            RoundtableTurn(
                session_id="disc_restart", seq=1, owner_user_id=7,
                turn={"turn_id": 1, "agent_code": "general", "agent_name": "质量代理",
                      "role": "agent", "content": "已提交的审查证据", "seq": 1},
                created_at=datetime.now(timezone.utc),
            ),
        ])

    assert _reconcile_orphan_reviews() == [(402, 7, "ordinary-lease")]
    with maker() as db:
        discussion = db.get(ReviewTask, 401)
        ordinary = db.get(ReviewTask, 402)
        session = db.get(RoundtableSession, "disc_restart")
        assert discussion.status == "failed"
        assert discussion.coverage["stage"] == "interrupted"
        assert ordinary.status == "running"
        assert session.status == "concluded"
        assert session.progress["phase"] == "interrupted"
        assert db.get(RoundtableTurn, ("disc_restart", 1)).turn["content"] == "已提交的审查证据"

    # 启动流程已落终态后，用户读取不会再次改变报告任务或丢失记录。
    bus = DiscussionBus(persist=True, session_factory=maker)
    restored = bus.get_session("disc_restart", owner_user_id=7)
    assert restored is not None and restored.progress["phase"] == "interrupted"
    assert [turn.content for turn in restored.turns] == ["已提交的审查证据"]
    engine.dispose()
