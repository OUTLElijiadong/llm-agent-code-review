"""登录 API 对原子失败限流协议的回归测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app.api.v1 import auth as auth_api
from app.core.exceptions import AuthError, TooManyRequestsError
from app.core.rate_limit import LoginFailureLimiter
from app.models.user import User
from app.schemas.auth import LoginIn


def _request(peer: str = "203.0.113.20") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/login",
            "headers": [],
            "client": (peer, 443),
            "server": ("testserver", 443),
            "scheme": "https",
            "query_string": b"",
        }
    )


class _RecordingLimiter:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.finished: list[tuple[str, str, bool]] = []
        self.released: list[tuple[str, str]] = []

    def begin_attempt(self, ip: str):
        return SimpleNamespace(
            allowed=self.allowed,
            remaining=4 if self.allowed else 0,
            retry_after=0 if self.allowed else 37,
            reservation_id="reservation-1" if self.allowed else None,
        )

    def finish_attempt(self, ip: str, reservation_id: str, *, success: bool):
        self.finished.append((ip, reservation_id, success))

    def release_attempt(self, ip: str, reservation_id: str):
        self.released.append((ip, reservation_id))


def test_login_rejects_before_authentication_when_atomic_reservation_is_denied(monkeypatch) -> None:
    limiter = _RecordingLimiter(allowed=False)
    monkeypatch.setattr(auth_api, "login_failure_limiter", limiter)
    monkeypatch.setattr(
        auth_api.auth_service,
        "login",
        lambda *_args, **_kwargs: pytest.fail("认证服务不应被调用"),
    )

    with pytest.raises(TooManyRequestsError) as exc:
        auth_api.login(LoginIn(username="user", password="secret"), _request(), object())

    assert exc.value.retry_after == 37


def test_login_authentication_failure_atomically_settles_reservation(monkeypatch) -> None:
    limiter = _RecordingLimiter()
    monkeypatch.setattr(auth_api, "login_failure_limiter", limiter)
    monkeypatch.setattr(
        auth_api.auth_service,
        "login",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AuthError("用户名或密码错误")),
    )
    monkeypatch.setattr(auth_api.audit_service, "log", lambda *_args, **_kwargs: None)

    with pytest.raises(AuthError):
        auth_api.login(LoginIn(username="user", password="bad-secret"), _request(), object())

    assert limiter.finished == [("203.0.113.20", "reservation-1", False)]
    assert limiter.released == []


def test_login_success_atomically_settles_and_clears_failures(monkeypatch) -> None:
    limiter = _RecordingLimiter()
    user = User(id=9, username="user", password="hashed", role="user", status=1)
    monkeypatch.setattr(auth_api, "login_failure_limiter", limiter)
    monkeypatch.setattr(auth_api.auth_service, "login", lambda *_args, **_kwargs: ("token", user))
    monkeypatch.setattr(auth_api.audit_service, "log", lambda *_args, **_kwargs: None)

    response = auth_api.login(LoginIn(username="user", password="secret"), _request(), object())

    assert response.data.access_token == "token"
    assert limiter.finished == [("203.0.113.20", "reservation-1", True)]
    assert limiter.released == []


def test_login_internal_error_releases_without_counting_failure(monkeypatch) -> None:
    limiter = _RecordingLimiter()
    monkeypatch.setattr(auth_api, "login_failure_limiter", limiter)
    monkeypatch.setattr(
        auth_api.auth_service,
        "login",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )
    monkeypatch.setattr(auth_api.audit_service, "log", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="database unavailable"):
        auth_api.login(LoginIn(username="user", password="secret"), _request(), object())

    assert limiter.finished == []
    assert limiter.released == [("203.0.113.20", "reservation-1")]


def test_login_route_allows_five_failures_then_rejects_sixth(monkeypatch) -> None:
    limiter = LoginFailureLimiter(redis_url="", limit=5, window_seconds=60)
    monkeypatch.setattr(auth_api, "login_failure_limiter", limiter)
    monkeypatch.setattr(
        auth_api.auth_service,
        "login",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AuthError("用户名或密码错误")),
    )
    monkeypatch.setattr(auth_api.audit_service, "log", lambda *_args, **_kwargs: None)

    for _attempt in range(5):
        with pytest.raises(AuthError):
            auth_api.login(LoginIn(username="user", password="bad-secret"), _request(), object())

    with pytest.raises(TooManyRequestsError):
        auth_api.login(LoginIn(username="user", password="bad-secret"), _request(), object())


def test_real_login_http_cooldown_counts_down_and_correct_password_cannot_bypass(monkeypatch, tmp_path):
    """实际路由、密码校验和数据库；只控制限流时钟，不冲击生产登录。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core import rate_limit
    from app.core.database import Base, get_db
    from app.core.error_handlers import register_handlers
    from app.core.security import hash_password

    now = [1000.0]
    monkeypatch.setattr(rate_limit, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(
        auth_api, "login_failure_limiter", LoginFailureLimiter(redis_url="", limit=5, window_seconds=60),
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'login.sqlite'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add(User(username="cooldown-user", password=hash_password("correct-password"), role="user", status=1))
        db.commit()
    app = FastAPI()
    app.include_router(auth_api.router, prefix="/api/auth")
    register_handlers(app)

    def database():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = database
    original_login = auth_api.auth_service.login
    authentication_calls = []

    def login(*args, **kwargs):
        authentication_calls.append(True)
        return original_login(*args, **kwargs)

    monkeypatch.setattr(auth_api.auth_service, "login", login)
    with TestClient(app) as client:
        for index in range(5):
            now[0] = 1000.0 + index * 2
            failure = client.post("/api/auth/login", json={"username": "cooldown-user", "password": "wrong-password"})
            assert failure.status_code == 401 and failure.json()["message"] == "用户名或密码错误"
        now[0] = 1010.0
        sixth = client.post("/api/auth/login", json={"username": "cooldown-user", "password": "wrong-password"})
        assert sixth.status_code == 429
        assert sixth.headers["Retry-After"] == "50"
        assert sixth.json()["retry_after_seconds"] == 50
        assert sixth.json()["retryable"] is True
        assert "50" in sixth.json()["message"]
        now[0] = 1030.0
        correct_but_blocked = client.post(
            "/api/auth/login", json={"username": "cooldown-user", "password": "correct-password"},
        )
        assert correct_but_blocked.status_code == 429
        assert correct_but_blocked.headers["Retry-After"] == "30"
        assert correct_but_blocked.json()["retry_after_seconds"] == 30
        assert len(authentication_calls) == 5
        now[0] = 1060.01
        restored = client.post("/api/auth/login", json={"username": "cooldown-user", "password": "correct-password"})
        assert restored.status_code == 200 and restored.json()["data"]["access_token"]
        assert len(authentication_calls) == 6
        assert "Retry-After" not in restored.headers
    engine.dispose()
