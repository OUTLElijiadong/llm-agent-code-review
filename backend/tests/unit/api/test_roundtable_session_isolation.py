"""圆桌会话跨账号隔离：B 账号不能列出、读取或停止 A 账号的圆桌。"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Query, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1 import discussion as discussion_api
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.core.error_handlers import register_handlers
from app.models.roundtable import RoundtableSession, RoundtableTurn
from app.models.user import User


class _StubBus:
    def get_session(self, session_id, owner_user_id=None):
        return SimpleNamespace(status="concluded", report_task_id=0, task_id=0)


@pytest.fixture
def clients(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    alice = User(username="roundtable-a", password="x", role="user", status=1)
    bob = User(username="roundtable-b", password="x", role="user", status=1)
    db.add_all([alice, bob])
    db.flush()
    db.add(RoundtableSession(
        session_id="rt-alice-1", owner_user_id=alice.id, file_name="secret_alice.py", status="concluded",
        agents=["code_reviewer"], progress={"phase": "completed"}, last_turn_seq=1,
    ))
    db.add(RoundtableTurn(session_id="rt-alice-1", seq=1, owner_user_id=alice.id,
                          turn={"speaker": "code_reviewer", "content": "A 的私有发言"}))
    db.commit()
    monkeypatch.setattr(discussion_api, "_bus_for_db", lambda _db: _StubBus())
    monkeypatch.setattr(discussion_api, "purge_stale_pending", lambda: None)
    app = FastAPI()
    register_handlers(app)
    app.include_router(discussion_api.router, prefix="/api")
    current = {"user": alice}
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: current["user"]
    with TestClient(app) as client:
        yield client, alice, bob, current


def test_owner_reads_own_roundtable(clients):
    client, _alice, _bob, _current = clients
    listed = client.get("/api/discuss/sessions")
    assert listed.status_code == 200
    assert [item["session_id"] for item in listed.json()["data"]["items"]] == ["rt-alice-1"]
    detail = client.get("/api/discuss/sessions/rt-alice-1")
    assert detail.status_code == 200
    assert detail.json()["data"]["turns"][0]["content"] == "A 的私有发言"


def test_other_account_cannot_list_read_or_stop_roundtable(clients):
    client, _alice, bob, current = clients
    current["user"] = bob
    listed = client.get("/api/discuss/sessions")
    assert listed.status_code == 200 and listed.json()["data"]["items"] == []
    for response in (
        client.get("/api/discuss/sessions/rt-alice-1"),
        client.post("/api/discuss/sessions/rt-alice-1/stop"),
    ):
        assert response.status_code == 404
        assert "secret_alice" not in response.text and "私有发言" not in response.text


def test_list_roundtables_preserves_priority_order_and_offset_pagination(clients):
    client, alice, _bob, _current = clients
    db = client.app.dependency_overrides[get_db]()
    db.query(RoundtableSession).delete()
    now = datetime.now(timezone.utc)
    sessions = [
        ("active-b", "active", 3),
        ("paused-c", "paused", 2),
        ("active-a", "active", 2),
        ("failed-z", "failed", 2),
        ("concluded-x", "concluded", 4),
        ("partial-y", "partial", 4),
    ]
    db.add_all([
        RoundtableSession(
            session_id=session_id,
            owner_user_id=alice.id,
            file_name=f"{session_id}.py",
            status=status,
            agents=[],
            progress={},
            created_at=now - timedelta(minutes=age),
            updated_at=now - timedelta(minutes=age),
        )
        for session_id, status, age in sessions
    ])
    db.commit()

    first = client.get("/api/discuss/sessions", params={"limit": 2, "offset": 0})
    assert first.status_code == 200
    assert [item["session_id"] for item in first.json()["data"]["items"]] == ["paused-c", "active-a"]
    assert first.json()["data"]["next_offset"] == 2

    second = client.get("/api/discuss/sessions", params={"limit": 2, "offset": 2})
    assert second.status_code == 200
    assert [item["session_id"] for item in second.json()["data"]["items"]] == ["active-b", "failed-z"]
    assert second.json()["data"]["next_offset"] == 4

    third = client.get("/api/discuss/sessions", params={"limit": 2, "offset": 4})
    assert third.status_code == 200
    assert [item["session_id"] for item in third.json()["data"]["items"]] == ["partial-y", "concluded-x"]
    assert third.json()["data"]["next_offset"] is None


def test_list_roundtables_orders_only_session_ids_before_loading_json(clients, monkeypatch):
    client, _alice, _bob, _current = clients
    compiled_queries = []
    original_all = Query.all

    def capture_all(query):
        compiled_queries.append(
            str(query.statement.compile(dialect=mysql.dialect())).upper()
        )
        return original_all(query)

    monkeypatch.setattr(Query, "all", capture_all)
    response = client.get("/api/discuss/sessions", params={"limit": 2, "offset": 0})

    assert response.status_code == 200
    ordered_page = next(
        query for query in compiled_queries
        if "FROM ROUND_TABLE_SESSION" in query or "FROM ROUNDTABLE_SESSION" in query
        if "ORDER BY CASE" in query
    )
    select_clause = ordered_page.split(" FROM ", 1)[0]
    assert "SELECT ROUNDTABLE_SESSION.SESSION_ID" in select_clause
    assert "ROUND_TABLE_SESSION.AGENTS" not in select_clause
    assert "ROUNDTABLE_SESSION.AGENTS" not in select_clause
    assert "ROUND_TABLE_SESSION.PROGRESS" not in select_clause
    assert "ROUNDTABLE_SESSION.PROGRESS" not in select_clause
    assert "UPDATED_AT DESC" in ordered_page
    assert "SESSION_ID DESC" in ordered_page
