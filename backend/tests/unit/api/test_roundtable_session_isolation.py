"""圆桌会话跨账号隔离：B 账号不能列出、读取或停止 A 账号的圆桌。"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
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
