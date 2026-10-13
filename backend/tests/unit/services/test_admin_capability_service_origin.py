"""站内能力 API 重入不得丢失当前操作者来源或接受模型伪造来源。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from pydantic import BaseModel

from app.core.rate_limit import client_ip
from app.core.security import decode_token
from app.core.site_request_context import SiteRequestContextMiddleware, bind_request_origin, trusted_request_origin
from app.services.admin_capability_registry import CRITICAL, AdminCapabilitySpec
from app.services.admin_capability_service import AdminCapabilityError, execute_api

POLICY_PATH = "/api/admin/security-center/automatic-blocking"
POLICY_SPEC = AdminCapabilitySpec(
    "security_center.automatic_blocking.update", "/admin/security-center", "调整自动封禁策略",
    "PUT", POLICY_PATH, CRITICAL, "super_admin",
)
READ_SPEC = AdminCapabilitySpec("origin.read", "/admin/security-center", "读取来源", "GET", POLICY_PATH)
ACTOR = SimpleNamespace(id=19, role="super_admin", token_version=3)


class PolicyBody(BaseModel):
    enabled: bool


@pytest.fixture
def api_app(monkeypatch):
    import app.main as main

    app = FastAPI()
    app.add_middleware(SiteRequestContextMiddleware)
    calls = []

    def result(request):
        calls.append(request.client.host)
        token = request.headers["authorization"].split(" ", 1)[1]
        claims = decode_token(token)
        return {"code": 0, "data": {
            "protected_ip": client_ip(request), "context_origin": trusted_request_origin(),
            "actor_id": claims["sub"], "actor_role": claims["role"],
            "has_forwarded_header": "x-real-ip" in request.headers or "x-forwarded-for" in request.headers,
        }}

    @app.put(POLICY_PATH)
    async def update(body: PolicyBody, request: Request):
        await asyncio.sleep(0)
        return result(request)

    @app.get(POLICY_PATH)
    async def read(request: Request):
        return result(request)

    @app.post("/invoke")
    async def invoke():
        return await execute_api(ACTOR, POLICY_SPEC, {"enabled": True}, request_id="origin-regression")

    monkeypatch.setattr(main, "app", app)
    return app, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["203.0.113.12", "2001:db8::12"])
async def test_http_task_preserves_trusted_origin_through_api_reentry(api_app, origin):
    app, calls = api_app
    transport = httpx.ASGITransport(app=app, client=(origin, 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/invoke", headers={"x-forwarded-for": "198.51.100.99"})
    assert response.status_code == 200
    assert response.json()["data"] == {
        "protected_ip": origin, "context_origin": origin, "actor_id": "19", "actor_role": "super_admin",
        "has_forwarded_header": False,
    }
    assert calls == [origin]
    assert trusted_request_origin() is None


@pytest.mark.asyncio
async def test_background_worker_can_rebind_server_origin(api_app):
    _, calls = api_app
    with bind_request_origin("198.51.100.15"):
        result = await execute_api(ACTOR, POLICY_SPEC, {"enabled": True}, request_id="worker-origin")
    assert result["data"]["protected_ip"] == "198.51.100.15"
    assert calls == ["198.51.100.15"]
    assert trusted_request_origin() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [None, "invalid", "0.0.0.0"])
async def test_missing_origin_blocks_policy_update_before_endpoint(api_app, origin):
    _, calls = api_app
    with bind_request_origin(origin):
        with pytest.raises(AdminCapabilityError, match="缺少可信管理员请求来源") as error:
            await execute_api(ACTOR, POLICY_SPEC, {"enabled": True}, request_id="missing-origin")
    assert error.value.status_code == 409
    assert calls == []


@pytest.mark.asyncio
async def test_read_capability_without_origin_retains_ordinary_internal_fallback(api_app):
    result = await execute_api(ACTOR, READ_SPEC, {}, request_id="ordinary-read")
    assert result["data"]["protected_ip"] == "127.0.0.1"
    assert trusted_request_origin() is None


@pytest.mark.asyncio
async def test_model_parameter_cannot_supply_trusted_origin(api_app):
    _, calls = api_app
    with pytest.raises(AdminCapabilityError, match="不接受参数"):
        await execute_api(
            ACTOR, POLICY_SPEC, {"enabled": True, "site_request_origin": "198.51.100.99"},
            request_id="forged-origin",
        )
    assert calls == []


@pytest.mark.asyncio
async def test_parallel_internal_requests_keep_actor_origins_separate(api_app):
    async def call(origin):
        with bind_request_origin(origin):
            result = await execute_api(ACTOR, POLICY_SPEC, {"enabled": True}, request_id="parallel-origin")
            assert result["data"]["protected_ip"] == origin
            assert trusted_request_origin() == origin

    await asyncio.gather(call("203.0.113.31"), call("203.0.113.32"))
    assert sorted(api_app[1]) == ["203.0.113.31", "203.0.113.32"]
    assert trusted_request_origin() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("peer", "headers", "expected"),
    [("203.0.113.40", {"x-real-ip": "198.51.100.99"}, "203.0.113.40"),
     ("10.1.2.3", {"x-real-ip": "198.51.100.40"}, "198.51.100.40")],
)
async def test_real_security_router_protects_server_resolved_origin(monkeypatch, peer, headers, expected):
    import app.main as main
    from app.api.v1 import admin_security_center
    from app.core.config import settings
    from app.core.database import get_db
    from app.core.dependencies import require_super_admin

    app = FastAPI()
    app.add_middleware(SiteRequestContextMiddleware)
    app.include_router(admin_security_center.router, prefix="/api/admin/security-center")
    app.dependency_overrides[get_db] = lambda: None
    app.dependency_overrides[require_super_admin] = lambda: ACTOR
    monkeypatch.setattr(main, "app", app)
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", ["10.0.0.0/8"])
    protected = []

    def configure(db, actor, policy, *, protected_ip):
        protected.append(protected_ip)
        assert actor is ACTOR
        return {"protected_ip": protected_ip}

    monkeypatch.setattr(admin_security_center.security_response_service, "configure", configure)

    @app.post("/invoke")
    async def invoke():
        return await execute_api(ACTOR, POLICY_SPEC, {"enabled": True}, request_id="real-router-origin")

    transport = httpx.ASGITransport(app=app, client=(peer, 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/invoke", headers=headers)
    assert response.status_code == 200
    assert response.json()["data"] == {"protected_ip": expected}
    assert protected == [expected]
    assert trusted_request_origin() is None
