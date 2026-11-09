"""Account-scoped, paginated recovery does not disclose published prompts."""

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.v1.personal_agent_publications import router
from app.core.database import Base, get_db
from app.core.exceptions import AppError
from app.core.security import create_access_token
from app.models.billing_ledger import BillingReservation
from app.models.personal_agent import PersonalAgent, PersonalAgentPublication, PersonalAgentRun, PersonalAgentVersion
from app.models.user import User


def test_history_is_owner_scoped_paginated_and_survives_revocation(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'history.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as db:
        db.add_all(
            [User(id=i, username=f"user{i}", password="x", role="user", status=1, token_version=0) for i in (1, 2, 3)]
        )
        db.add(
            PersonalAgent(id=1, owner_id=1, create_key="create", create_hash="a", current_version=1, status="active")
        )
        db.add(
            PersonalAgentVersion(
                id=1, agent_id=1, version_number=1, title="名称", prompt="秘密提示", request_key="v", request_hash="h"
            )
        )
        db.add(
            PersonalAgentPublication(
                id=1,
                owner_id=1,
                agent_id=1,
                version_id=1,
                version_number=1,
                title="公开名称",
                prompt="秘密提示",
                content_hash="hash",
                request_key="p",
                status="revoked",
            )
        )
        for i, owner, public in [(1, 2, True), (2, 2, True), (3, 3, True), (4, 1, False)]:
            db.add(
                BillingReservation(
                    request_key=f"res{i}",
                    model_name="model",
                    requested_milli=100,
                    actual_milli=42 if i != 2 else None,
                    status="settled" if i != 2 else "unknown",
                )
            )
            db.add(
                PersonalAgentRun(
                    id=i,
                    owner_id=owner,
                    agent_id=1,
                    version_id=1,
                    version_number=1,
                    version_hash="hash",
                    publication_id=1 if public else None,
                    request_key=f"key{i}",
                    request_hash="hash",
                    reservation_key=f"res{i}",
                    status="awaiting_reconciliation" if i in (1, 2) else "succeeded",
                    result_text="秘密结果",
                )
            )
    app = FastAPI()
    app.include_router(router, prefix="/api")

    @app.exception_handler(AppError)
    async def error(_, exc):
        return JSONResponse(status_code=exc.http_status, content={"message": exc.message})

    def database():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = database
    def headers(uid):
        return {"Authorization": "Bearer " + create_access_token(uid, "user", 0)}
    with TestClient(app) as client:
        url = "/api/me/agent-runs"
        one = client.get(url, params={"source": "public", "limit": 1}, headers=headers(2))
        assert one.status_code == 200
        assert one.json()["data"][0]["id"] == 2
        assert one.json()["data"][0]["actual_milli"] is None
        two = client.get(url, params={"source": "public", "limit": 1, "before_id": 2}, headers=headers(2))
        row = two.json()["data"][0]
        assert row["id"] == 1 and row["actual_milli"] == 42 and row["title"] == "公开名称"
        assert row["status"] == "result_unavailable"
        assert row["publication_id"] == 1 and row["agent_id"] is None
        assert not any(secret in two.text for secret in ["秘密提示", "秘密结果", "request_key", "owner_id"])
        assert client.get(url, params={"source": "private"}, headers=headers(2)).json()["data"] == []
        assert [r["id"] for r in client.get(url, params={"source": "private"}, headers=headers(1)).json()["data"]] == [
            4
        ]
        assert client.get(url, params={"source": "public", "limit": 101}, headers=headers(2)).status_code == 422
        assert client.get(url, params={"source": "private", "before_id": 0}, headers=headers(2)).status_code == 422
        too_large = client.get(url, params={"source": "private", "before_id": 9223372036854775808}, headers=headers(2))
        assert too_large.status_code == 422
        assert client.get(url, params={"source": "unknown"}, headers=headers(2)).status_code == 422
        assert client.get(url, params={"source": "public"}, headers=headers(3)).json()["data"][0]["id"] == 3
        assert client.get(url, params={"source": "public"}).status_code == 401
    engine.dispose()


def test_history_cursor_excludes_new_rows_and_remains_owner_and_source_scoped(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'cursor.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as db:
        db.add_all(
            [User(id=i, username=f"cursor{i}", password="x", role="user", status=1, token_version=0) for i in (1, 2)]
        )
        db.add(PersonalAgent(id=1, owner_id=1, create_key="c", create_hash="h", current_version=1, status="active"))
        db.add(
            PersonalAgentVersion(
                id=1, agent_id=1, version_number=1, title="助手", prompt="秘密", request_key="v", request_hash="h"
            )
        )
        for i in range(1, 23):
            db.add(
                PersonalAgentRun(
                    id=i,
                    owner_id=1,
                    agent_id=1,
                    version_id=1,
                    version_number=1,
                    version_hash="h",
                    request_key=f"k{i}",
                    request_hash="h",
                    reservation_key=f"r{i}",
                    status="running",
                )
            )
        db.add(
            PersonalAgentRun(
                id=24,
                owner_id=2,
                agent_id=1,
                version_id=1,
                version_number=1,
                version_hash="h",
                request_key="other",
                request_hash="h",
                reservation_key="other",
                status="running",
            )
        )
    app = FastAPI()
    app.include_router(router, prefix="/api")

    def database():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = database
    headers = {"Authorization": "Bearer " + create_access_token(1, "user", 0)}
    with TestClient(app) as client:
        first = client.get("/api/me/agent-runs", params={"source": "private", "limit": 20}, headers=headers).json()[
            "data"
        ]
        with factory.begin() as db:
            db.add(
                PersonalAgentRun(
                    id=23,
                    owner_id=1,
                    agent_id=1,
                    version_id=1,
                    version_number=1,
                    version_hash="h",
                    request_key="new",
                    request_hash="h",
                    reservation_key="new",
                    status="running",
                )
            )
        second = client.get(
            "/api/me/agent-runs",
            params={"source": "private", "limit": 20, "before_id": first[-1]["id"]},
            headers=headers,
        ).json()["data"]
        assert [row["id"] for row in first] == list(range(22, 2, -1))
        assert [row["id"] for row in second] == [2, 1]
        assert not ({row["id"] for row in first} & {row["id"] for row in second})
        assert (
            client.get("/api/me/agent-runs", params={"source": "public", "before_id": 24}, headers=headers).json()[
                "data"
            ]
            == []
        )
        assert all(
            row["id"] != 24
            for row in client.get(
                "/api/me/agent-runs", params={"source": "private", "before_id": 25}, headers=headers
            ).json()["data"]
        )
    engine.dispose()
