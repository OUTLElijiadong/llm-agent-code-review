"""Private personal agents: identity, immutable versions and idempotency."""

import pytest
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.personal_agents import router
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import AppError
from app.models.personal_agent import PersonalAgentVersion
from app.models.user import User


@pytest.fixture
def personal_client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)()
    db.add_all(
        [
            User(id=1, username="member", password="x", role="user", status=1),
            User(id=2, username="other", password="x", role="user", status=1),
            User(id=3, username="admin", password="x", role="admin", status=1),
        ]
    )
    db.commit()
    app = FastAPI()
    app.include_router(router, prefix="/api")

    @app.exception_handler(AppError)
    async def error(_, exc):
        return JSONResponse(status_code=exc.http_status, content={"message": exc.message})

    def user(x_test_user: int = Header()):
        return db.get(User, x_test_user)

    app.dependency_overrides[get_current_user] = user
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        yield client, db
    db.close()
    engine.dispose()


def test_owner_isolation_versions_and_disabled_agent(personal_client):
    client, db = personal_client
    h = {"x-test-user": "1"}
    payload = {"title": "测试助手", "prompt": "请仔细分析这段内容并给出建议。", "request_key": "create-1"}
    first = client.post("/api/me/agents", headers=h, json=payload)
    assert first.status_code == 200
    row = first.json()["data"]
    assert row["current_version"] == 1 and row["status"] == "active"
    assert row["execution_available"] is False and row["publication_available"] is True
    agent_id = row["id"]
    assert client.post("/api/me/agents", headers=h, json=payload).json()["data"]["id"] == agent_id
    assert client.post("/api/me/agents", headers=h, json={**payload, "title": "不同"}).status_code == 409
    for other in ("2", "3"):
        assert client.get(f"/api/me/agents/{agent_id}", headers={"x-test-user": other}).status_code == 404
        assert client.get("/api/me/agents", headers={"x-test-user": other}).json()["data"] == []
        assert client.get(f"/api/me/agents/{agent_id}/versions", headers={"x-test-user": other}).status_code == 404
        assert client.put(
            f"/api/me/agents/{agent_id}", headers={"x-test-user": other},
            json={**payload, "expected_version": 1},
        ).status_code == 404
        assert (
            client.post(
                f"/api/me/agents/{agent_id}/disable", headers={"x-test-user": other}, json={"expected_version": 1}
            ).status_code
            == 404
        )
    edit = {"title": "第二版", "prompt": "请继续分析但保持输出精简。", "request_key": "edit-1", "expected_version": 1}
    assert client.put(f"/api/me/agents/{agent_id}", headers=h, json=edit).json()["data"]["current_version"] == 2
    assert client.put(f"/api/me/agents/{agent_id}", headers=h, json=edit).status_code == 200
    assert client.put(f"/api/me/agents/{agent_id}", headers=h, json={**edit, "title": "错"}).status_code == 409
    assert (
        client.put(f"/api/me/agents/{agent_id}", headers=h, json={**edit, "request_key": "edit-2"}).status_code == 409
    )
    versions = client.get(f"/api/me/agents/{agent_id}/versions", headers=h).json()["data"]
    assert [(x["version_number"], x["title"]) for x in versions] == [(2, "第二版"), (1, "测试助手")]
    assert db.query(PersonalAgentVersion).count() == 2
    assert client.post(f"/api/me/agents/{agent_id}/disable", headers=h, json={"expected_version": 1}).status_code == 409
    assert (
        client.post(f"/api/me/agents/{agent_id}/disable", headers=h, json={"expected_version": 2}).json()["data"][
            "status"
        ]
        == "disabled"
    )
    assert (
        client.put(
            f"/api/me/agents/{agent_id}", headers=h, json={**edit, "request_key": "edit-3", "expected_version": 2}
        ).status_code
        == 409
    )


def test_reject_client_controlled_execution_fields(personal_client):
    client, _db = personal_client
    response = client.post(
        "/api/me/agents",
        headers={"x-test-user": "1"},
        json={
            "title": "私有助手",
            "prompt": "请认真分析输入内容。",
            "request_key": "create-2",
            "tools": ["shell"],
            "model": "custom-model",
        },
    )
    assert response.status_code == 422


@pytest.mark.parametrize("field,value", [("title", "   "), ("prompt", "\u0000"), ("title", "助手\n名称")])
def test_invalid_plain_text_returns_client_error(personal_client, field, value):
    client, _db = personal_client
    body = {"title": "助手", "prompt": "有效指令", "request_key": "invalid-test", field: value}
    response = client.post("/api/me/agents", headers={"x-test-user": "1"}, json=body)
    assert response.status_code == 400
