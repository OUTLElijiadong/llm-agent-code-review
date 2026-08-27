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
