"""审查启动限流按已签名会话隔离，并在启动任务前阻断超额请求。"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi.errors import RateLimitExceeded

from app.api.v1 import review as review_api
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.error_handlers import register_handlers
from app.core.rate_limit import limiter
from app.core.security import create_access_token
from app.main import _rate_limit_handler
from app.models.user import User


@pytest.fixture
def review_start_client(db, monkeypatch):
    user = User(id=73001, username="rate-limit-reviewer", password="x", role="reviewer", status=1)
    db.add(user)
    db.commit()

    def override_db():
        yield db

    start_route = next(route for route in review_api.router.routes if route.path == "/start")
    permission_dependency = start_route.dependant.dependencies[0].call
    test_app = FastAPI()
    test_app.state.limiter = limiter
    test_app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)
    register_handlers(test_app)
    test_app.include_router(review_api.router, prefix="/api/review")
    test_app.dependency_overrides[get_db] = override_db
    test_app.dependency_overrides[get_current_user] = lambda: user
    test_app.dependency_overrides[permission_dependency] = lambda: None
    starts = []

    def fake_start(_db, *, user, payload):
        starts.append((user.id, payload.project_id))
        return SimpleNamespace(id=len(starts), status="running")

    monkeypatch.setattr(review_api.review_service, "start", fake_start)
    limiter.reset()
    try:
        with TestClient(test_app) as client:
            yield client, user, starts
    finally:
        limiter.reset()
        test_app.dependency_overrides.clear()


def test_review_start_limits_valid_session_before_queuing_expensive_work(review_start_client):
    client, user, starts = review_start_client
    token = create_access_token(user.id, user.role, user.token_version)
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"project_id": 1, "file_ids": [10], "review_type": "quick"}

    responses = [client.post("/api/review/start", json=payload, headers=headers) for _ in range(6)]

    assert [response.status_code for response in responses] == [200, 200, 200, 200, 200, 429]
    assert len(starts) == 5
    assert responses[-1].json()["message"] == "请求过于频繁,请稍后再试"
    assert responses[-1].json()["detail"] == "审查发起过于频繁，请稍后重试"
    assert int(responses[-1].headers["Retry-After"]) > 0


def test_review_start_buckets_are_separate_for_distinct_authenticated_accounts(review_start_client):
    client, user, starts = review_start_client
    payload = {"project_id": 1, "file_ids": [10], "review_type": "quick"}
    first = create_access_token(user.id, user.role, user.token_version)
    first_headers = {"Authorization": f"Bearer {first}"}

    for _ in range(5):
        assert client.post("/api/review/start", json=payload, headers=first_headers).status_code == 200

    relogin_token = create_access_token(user.id, user.role, user.token_version + 1)
    relogin_headers = {"Authorization": f"Bearer {relogin_token}"}
    relogin_response = client.post("/api/review/start", json=payload, headers=relogin_headers)
    assert relogin_response.status_code == 429

    second_token = create_access_token(user.id + 1, "reviewer", 0)
    second_headers = {"Authorization": f"Bearer {second_token}"}
    response = client.post("/api/review/start", json=payload, headers=second_headers)

    assert response.status_code == 200
    assert len(starts) == 6
