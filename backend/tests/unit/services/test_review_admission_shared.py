"""REST 与 Agent 工具必须共享按账号隔离的审查启动额度。"""
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agents.orchestrator import Orchestrator
from app.agents.review_orchestrator_agent import ReviewOrchestratorAgent
from app.core import rate_limit
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.core.error_handlers import register_handlers
from app.core.exceptions import ServiceUnavailableError
from app.core.security import create_access_token, decode_token
from app.models.user import User
from app.services import review_admission_service
from app.api.v1 import review as review_api


def _tool_orchestrator(db, user):
    orchestrator = object.__new__(Orchestrator)
    orchestrator._db = db
    orchestrator._user = user
    orchestrator.review_orch = ReviewOrchestratorAgent()
    orchestrator.review_orch.inject(db, user=user)
    orchestrator._require_domain_tool_permission = lambda _tool: None
    orchestrator._disabled_result = lambda _agent: None
    return orchestrator


def test_review_rest_and_agent_tool_share_one_account_window(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'review-admission.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    test_limiter = rate_limit.build_limiter("memory://")
    monkeypatch.setattr(rate_limit, "limiter", test_limiter)
    user_id = 77301
    user = User(id=user_id, username="shared-review-quota", password="x", role="reviewer", status=1)
    route = next(item for item in review_api.router.routes if item.path == "/start")
    permission_dependency = route.dependant.dependencies[0].call
    app = FastAPI()
    register_handlers(app)
    app.include_router(review_api.router, prefix="/api/review")
    app.dependency_overrides[get_db] = lambda: db

    def current_user(request: Request):
        token = request.headers.get("authorization", "").partition(" ")[2]
        payload = decode_token(token)
        return User(id=int(payload["sub"]), username="shared-review-quota", password="x",
                    role="reviewer", status=1)

    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[permission_dependency] = lambda: None
    payload = {"project_id": 987654, "file_ids": [1], "review_type": "quick"}
    token = create_access_token(user_id, "reviewer", 0)
    headers = {"Authorization": f"Bearer {token}"}

    with TestClient(app, raise_server_exceptions=False) as client:
        rest_results = [client.post("/api/review/start", json=payload, headers=headers) for _ in range(2)]
        agent = _tool_orchestrator(db, user)
        tool_results = [agent.start_review(987654, [1], "quick") for _ in range(3)]
        sixth_rest = client.post("/api/review/start", json=payload, headers=headers)
        sixth_tool = agent.start_review(987654, [1], "quick")
        # A new login token for the same account retains the same service bucket.
        relogin = create_access_token(user_id, "reviewer", 1)
        relogin_response = client.post(
            "/api/review/start", json=payload, headers={"Authorization": f"Bearer {relogin}"},
        )

    # First five reach service validation and fail only because the isolated project is absent.
    assert [response.status_code for response in rest_results] == [404, 404]
    assert [result.success for result in tool_results] == [False, False, False]
    assert all("项目不存在" in result.error for result in tool_results)
    assert sixth_rest.status_code == 429
    assert sixth_rest.json()["code"] == 42900
    assert int(sixth_rest.headers["Retry-After"]) > 0
    assert sixth_tool.success is False
    assert "请求过于频繁" in sixth_tool.error
    assert relogin_response.status_code == 429

    # Another account has an independent quota.
    other = User(id=user_id + 1, username="other-review-quota", password="x", role="reviewer", status=1)
    other_tool = _tool_orchestrator(db, other).start_review(987654, [1], "quick")
    assert other_tool.success is False
    assert "项目不存在" in other_tool.error

    app.dependency_overrides.clear()
    db.close()
    engine.dispose()


def test_review_admission_fails_closed_when_shared_storage_is_unavailable(monkeypatch):
    class UnavailableBackend:
        def hit(self, *_args, **_kwargs):
            raise OSError("isolated shared-store outage")

    test_limiter = SimpleNamespace(limiter=UnavailableBackend())
    monkeypatch.setattr(rate_limit, "limiter", test_limiter)

    try:
        review_admission_service.admit_review_start(77302)
    except ServiceUnavailableError as exc:
        assert "任务未创建" in exc.message
    else:  # pragma: no cover - protects fail-closed quota behavior
        raise AssertionError("shared quota outage must not admit a review")


def test_production_review_admission_requires_shared_storage(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "app_env", "prod")
    monkeypatch.setattr(settings, "redis_url", "")

    try:
        review_admission_service.admit_review_start(77303)
    except ServiceUnavailableError as exc:
        assert "共享限流存储" in exc.message
    else:  # pragma: no cover - production must not silently use per-process memory
        raise AssertionError("production without Redis must not admit a review")
