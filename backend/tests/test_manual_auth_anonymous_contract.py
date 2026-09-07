"""8个手动鉴权路由缺凭据先401，禁止触及DB、事件订阅、worker、上游和存储。"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import responses
from app.api.v1 import agents
from app.core.database import get_db
from app.main import app
from app.services import sandbox_service

PATH = Path(__file__).resolve().parents[1] / "scripts/verify_manual_auth_https.py"
SPEC = importlib.util.spec_from_file_location("manual_auth_cases", PATH)
manual = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manual)


@pytest.mark.parametrize("method,path,_proof", manual.CASES, ids=[f"{item[0]} {item[1]}" for item in manual.CASES])
def test_anonymous_manual_auth_is_rejected_before_any_external_or_business_operation(monkeypatch, method, path, _proof):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append("forbidden")
        raise AssertionError("缺凭据请求进入业务操作")

    class ForbiddenDb:
        def __getattr__(self, name):
            return forbidden()

    def fixture_db():
        yield ForbiddenDb()

    monkeypatch.setattr(agents.AgentEventBus, "instance", forbidden)
    monkeypatch.setattr(sandbox_service, "_proxy_worker_preview", forbidden)
    gateway = responses.get_responses_service()
    monkeypatch.setattr(gateway, "_prepare_upstream_payload", forbidden)
    monkeypatch.setattr(gateway, "_request_buffered", forbidden)
    monkeypatch.setattr(gateway, "_open_stream", forbidden)
    monkeypatch.setattr(gateway, "_storage", SimpleNamespace(load=forbidden, save=forbidden, delete=forbidden))
    previous = dict(app.dependency_overrides)
    app.dependency_overrides.clear()
    app.dependency_overrides[get_db] = fixture_db
    client = TestClient(app)
    try:
        response = client.request(method, path, json={} if method == "POST" else None)
        assert response.status_code == 401
        assert calls == []
    finally:
        client.close()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def test_manual_auth_plan_only_accepts_exact_reviewed_paths():
    plan = manual.build_plan(PATH.parents[1])
    manual.validate_plan(plan, PATH.parents[1])
    plan["cases"][0][1] = "/api/auth/register"
    with pytest.raises(ValueError, match="精确"):
        manual.validate_plan(plan, PATH.parents[1])
