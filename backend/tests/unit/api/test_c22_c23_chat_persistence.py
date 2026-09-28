"""C22/C23: isolated HTTP evidence for owned chat history and pre-run failures."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1 import agent_mesh, agent_responses
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.core.error_handlers import register_handlers
from app.models.agent_mesh import AgentMeshConversation
from app.models.agent_response_run import AgentResponseRun
from app.models.user import User


@pytest.fixture
def db():
    # TestClient routes run in worker threads; one shared connection preserves
    # the isolated in-memory schema across the test and ASGI request threads.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def browser(db, monkeypatch):
    users = {}
    for role in ("user", "reviewer", "admin"):
        row = User(username=f"c22-{role}", password="x", role=role, status=1)
        db.add(row)
        users[role] = row
    db.commit()
    current = {"user": users["user"]}
    application = FastAPI()
    register_handlers(application)
    application.include_router(agent_mesh.router, prefix="/api/agent-mesh")
    application.include_router(agent_responses.router, prefix="/api/agent-responses")
    application.dependency_overrides[get_db] = lambda: db
    application.dependency_overrides[get_current_user] = lambda: current["user"]
    monkeypatch.setattr("app.core.rbac_dependency.check_permission", lambda *_args: True)
    monkeypatch.setattr(
        "app.services.rbac_service.is_admin_user",
        lambda _db, user_id: int(user_id) == int(users["admin"].id),
    )
    return TestClient(application), users, current


def _heartbeat(client: TestClient, surface: str, session_id: str, title: str) -> None:
    response = client.post("/api/agent-mesh/conversations/heartbeat", json={
        "surface": surface, "session_id": session_id, "title": title,
    })
    assert response.status_code == 200, response.text


def _save_run(db, user: User, surface: str, session_id: str, messages: list[dict]) -> None:
    db.add(AgentResponseRun(
        run_id=f"c22-{user.id}-{surface}-{session_id}",
        user_id=user.id,
        surface=surface,
        session_key=session_id,
        status="completed",
        checkpoint_json=json.dumps({"transcript": messages}, ensure_ascii=False),
    ))
    db.commit()


def _data(response):
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_three_roles_own_history_and_paginate_in_server_order(db, browser):
    client, users, current = browser
    session_id = "same-session-c22"
    for role in ("user", "reviewer", "admin"):
        current["user"] = users[role]
        surface = "admin" if role == "admin" else "user"
        _heartbeat(client, surface, session_id, f"{role} 独立会话")
        messages = [
            {"role": "user" if index % 2 == 0 else "assistant", "content": f"{role}-{index:03d}"}
            for index in range(127)
        ]
        _save_run(db, users[role], surface, session_id, messages)

    for role in ("user", "reviewer", "admin"):
        current["user"] = users[role]
        surface = "admin" if role == "admin" else "user"
        first = _data(client.get("/api/agent-responses/session", params={
            "surface": surface, "session_id": session_id,
        }))
        assert first["run"]["run_id"] == f"c22-{users[role].id}-{surface}-{session_id}"
        assert first["history_page"] == {
            "has_more": True, "oldest_message_index": 27, "total": 127,
        }
        assert len(first["messages"]) == 100
        older = _data(client.get("/api/agent-responses/session/messages", params={
            "surface": surface, "session_id": session_id,
            "before_message": 27, "limit": 50,
        }))
        assert older["has_more"] is False
        assert older["oldest_message_index"] == 0
        assert [item["content"] for item in older["messages"] + first["messages"]] == [
            f"{role}-{index:03d}" for index in range(127)
        ]
        assert _data(client.get("/api/agent-mesh/conversations", params={
            "surface": surface, "status": "active",
        }))["total"] == 1

    current["user"] = users["reviewer"]
    assert client.get("/api/agent-responses/session", params={
        "surface": "admin", "session_id": session_id,
    }).status_code == 403
    assert client.get("/api/agent-mesh/conversations", params={
        "surface": "admin", "status": "active",
    }).status_code == 403


def test_archived_chat_stays_owned_and_restores_with_history(db, browser):
    client, users, current = browser
    session_id = "owner-archive-c22"
    _heartbeat(client, "user", session_id, "本人真实会话")
    _save_run(db, users["user"], "user", session_id, [
        {"role": "user", "content": "首问"},
        {"role": "assistant", "content": "答复"},
    ])
    request = {"surface": "user", "session_id": session_id}
    assert _data(client.post("/api/agent-mesh/conversations/archive", json=request))["status"] == "archived"
    assert _data(client.get("/api/agent-mesh/conversations", params={"status": "active"}))["total"] == 0
    assert _data(client.get("/api/agent-mesh/conversations", params={"status": "archived"}))["total"] == 1
    assert [row["content"] for row in _data(client.get(
        "/api/agent-responses/session", params=request,
    ))["messages"]] == ["首问", "答复"]

    current["user"] = users["reviewer"]
    assert _data(client.get("/api/agent-mesh/conversations", params={"status": "archived"}))["total"] == 0
    assert _data(client.get("/api/agent-responses/session", params=request))["messages"] == []
    assert client.post("/api/agent-mesh/conversations/restore", json=request).status_code == 404

    current["user"] = users["user"]
    assert _data(client.post("/api/agent-mesh/conversations/restore", json=request))["lifecycle_status"] == "active"
    assert _data(client.get("/api/agent-mesh/conversations", params={"status": "active"}))["total"] == 1
    assert [row["content"] for row in _data(client.get(
        "/api/agent-responses/session", params=request,
    ))["messages"]] == ["首问", "答复"]


@pytest.mark.parametrize("is_prod", [False, True])
@pytest.mark.parametrize("invalid_fields", [
    {"action": "answer", "answer": "继续", "messages": []},
    {"action": "start", "messages": []},
    {"action": "answer", "run_id": "invalid id", "answer": "继续", "messages": []},
])
def test_validation_failure_before_run_id_has_request_id_but_no_server_transcript(
    db, browser, monkeypatch, is_prod, invalid_fields,
):
    monkeypatch.setattr("app.core.error_handlers._is_prod", lambda: is_prod)
    client, users, _current = browser
    session_id = "pre-run-error-c23"
    _heartbeat(client, "user", session_id, "提交前失败")
    response = client.post("/api/agent-responses/stream", json={
        "surface": "user", "session_id": session_id, **invalid_fields,
    })
    assert response.status_code == 400, response.text
    assert response.json()["code"] == 40002
    assert response.json().get("request_id")
    assert response.headers["X-Request-Id"] == response.json()["request_id"]
    assert response.json().get("message")
    assert ("detail" in response.json()) is not is_prod
    session = _data(client.get("/api/agent-responses/session", params={
        "surface": "user", "session_id": session_id,
    }))
    assert session["run"] is None
    assert session["messages"] == []
    assert session["history_page"]["total"] == 0
    assert db.query(AgentResponseRun).filter_by(user_id=users["user"].id, session_key=session_id).count() == 0
    assert db.query(AgentMeshConversation).filter_by(user_id=users["user"].id, session_key=session_id).count() == 1
