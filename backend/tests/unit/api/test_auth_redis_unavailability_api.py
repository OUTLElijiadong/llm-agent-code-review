"""Real HTTP/database failure boundaries; upstream Redis is an isolated fixture."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.v1 import auth as auth_api
from app.core.database import Base, get_db
from app.core.error_handlers import register_handlers
from app.core.rate_limit import LoginFailureLimiter
from app.core.security import create_access_token, hash_password
from app.models.audit_log import AuditLog
from app.models.user import User

SPEC = importlib.util.spec_from_file_location(
    "http_login_redis_fixture", Path(__file__).parents[1] / "core/test_login_rate_limit.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.fixture
def local_auth(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'redis-outage.sqlite'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add(
            User(
                id=901,
                username="isolated-outage-user",
                password=hash_password("isolated-correct-password"),
                role="user",
                status=1,
                token_version=0,
            )
        )
        db.commit()
    redis = MODULE._AtomicRedis()
    monkeypatch.setattr(
        auth_api,
        "login_failure_limiter",
        LoginFailureLimiter(redis_url="redis://local-test.invalid", limit=5, window_seconds=60, redis_client=redis),
    )
    app = FastAPI()
    register_handlers(app)
    app.include_router(auth_api.router, prefix="/api/auth")

    def database():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = database
    with TestClient(app) as client:
        yield client, factory, redis
    engine.dispose()


def test_redis_outage_rejects_login_before_auth_but_existing_session_get_still_works(local_auth):
    client, factory, redis = local_auth
    existing_token = create_access_token(901, "user", 0)
    redis.fail = True
    response = client.post(
        "/api/auth/login", json={"username": "isolated-outage-user", "password": "isolated-correct-password"}
    )
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert response.json()["retry_after_seconds"] == 5
    assert "access_token" not in response.text
    with factory() as db:
        assert db.get(User, 901).token_version == 0
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer " + existing_token}).status_code == 200


def test_settlement_failure_returns_no_jwt_and_does_not_undo_committed_session_version(local_auth, monkeypatch):
    client, factory, redis = local_auth
    original = auth_api.auth_service.login
    generated = []

    def success_then_outage(*args, **kwargs):
        result = original(*args, **kwargs)
        generated.append(result[0])
        redis.fail = True
        return result

    monkeypatch.setattr(auth_api.auth_service, "login", success_then_outage)
    response = client.post(
        "/api/auth/login", json={"username": "isolated-outage-user", "password": "isolated-correct-password"}
    )
    assert response.status_code == 503 and response.headers["Retry-After"] == "5"
    assert response.json()["retry_after_seconds"] == 5
    assert "access_token" not in response.text and generated[0] not in response.text
    with factory() as db:
        assert db.get(User, 901).token_version == 1
        audit = db.query(AuditLog).filter_by(action="login").one()
        assert audit.status == "failed"
        assert "会话版本已递增" in audit.detail and "令牌未返回" in audit.detail
        assert generated[0] not in audit.detail


@pytest.mark.parametrize("failure", ["credentials", "infrastructure"])
def test_auth_failure_and_reservation_release_outage_also_fail_closed(local_auth, monkeypatch, failure):
    from app.core.exceptions import AuthError

    client, factory, redis = local_auth

    def fail_auth(*_args, **_kwargs):
        redis.fail = True
        if failure == "credentials":
            raise AuthError("密码错误")
        raise RuntimeError("isolated auth database fault")

    monkeypatch.setattr(auth_api.auth_service, "login", fail_auth)
    response = client.post(
        "/api/auth/login", json={"username": "isolated-outage-user", "password": "isolated-correct-password"}
    )
    assert response.status_code == 503 and response.headers["Retry-After"] == "5"
    assert "access_token" not in response.text and "isolated auth database fault" not in response.text
    with factory() as db:
        assert db.get(User, 901).token_version == 0
    bucket = next(iter(redis.values.values()))
    assert bucket["pending"] == 1 and bucket["failures"] == 0
