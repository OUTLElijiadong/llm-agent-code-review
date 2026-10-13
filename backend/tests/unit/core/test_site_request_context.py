"""可信来源在真实 ASGI 请求、SSE、异常与并发之间的隔离回归。"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from starlette.responses import StreamingResponse

from app.core.config import settings
from app.core.site_request_context import (
    SiteRequestContextMiddleware,
    bind_request_origin,
    trusted_request_origin,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("203.0.113.5", "203.0.113.5"), (" ::1 ", "::1"), ("2001:db8::1", "2001:db8::1"),
     (None, None), ("localhost", None), ("203.0.113.5, 127.0.0.1", None), ("0.0.0.0", None),
     ("::", None), ("224.0.0.1", None), ("fe80::1%eth0", None)],
)
def test_bind_validates_ip_literals_and_restores_previous_context(value, expected):
    assert trusted_request_origin() is None
    with bind_request_origin("192.0.2.1"):
        with bind_request_origin(value):
            assert trusted_request_origin() == expected
        assert trusted_request_origin() == "192.0.2.1"
    assert trusted_request_origin() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("peer", "headers", "expected"),
    [
        ("203.0.113.7", {"x-real-ip": "198.51.100.9", "x-forwarded-for": "198.51.100.10"}, "203.0.113.7"),
        ("10.1.2.3", {"x-real-ip": "198.51.100.9", "x-forwarded-for": "198.51.100.10"}, "198.51.100.9"),
        ("10.1.2.3", {"x-forwarded-for": "198.51.100.10"}, "10.1.2.3"),
        ("10.1.2.3", {"x-real-ip": "invalid", "x-prism-site-origin": "198.51.100.10"}, "10.1.2.3"),
        ("testclient", {"x-real-ip": "198.51.100.9"}, None),
        (None, {"x-real-ip": "198.51.100.9"}, None),
    ],
)
async def test_middleware_reuses_trusted_proxy_rules(monkeypatch, peer, headers, expected):
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", ["10.0.0.0/8"])
    app = FastAPI()
    app.add_middleware(SiteRequestContextMiddleware)

    @app.get("/origin")
    async def read_origin():
        return {"origin": trusted_request_origin()}

    transport = httpx.ASGITransport(app=app, client=(peer, 1234) if peer else None)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        result = await client.get("/origin", headers=headers)
    assert result.json() == {"origin": expected}
    assert trusted_request_origin() is None


@pytest.mark.asyncio
async def test_parallel_requests_keep_separate_origins():
    app = FastAPI()
    app.add_middleware(SiteRequestContextMiddleware)
    arrived = 0
    both_arrived = asyncio.Event()

    @app.get("/origin")
    async def read_origin():
        nonlocal arrived
        before = trusted_request_origin()
        arrived += 1
        if arrived == 2:
            both_arrived.set()
        await asyncio.wait_for(both_arrived.wait(), 2)
        return {"before": before, "after": trusted_request_origin()}

    async def call(origin):
        transport = httpx.ASGITransport(app=app, client=(origin, 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            result = await client.get("/origin")
            assert result.json() == {"before": origin, "after": origin}
        assert trusted_request_origin() is None

    await asyncio.gather(call("203.0.113.11"), call("203.0.113.12"))
    assert trusted_request_origin() is None


@pytest.mark.asyncio
async def test_sse_generator_retains_origin_until_response_finishes():
    app = FastAPI()
    app.add_middleware(SiteRequestContextMiddleware)

    @app.get("/stream")
    async def stream():
        async def events():
            yield f"data: {trusted_request_origin()}\n\n"
            await asyncio.sleep(0)
            yield f"data: {trusted_request_origin()}\n\n"
        return StreamingResponse(events(), media_type="text/event-stream")

    transport = httpx.ASGITransport(app=app, client=("203.0.113.15", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/stream")
    assert response.text == "data: 203.0.113.15\n\ndata: 203.0.113.15\n\n"
    assert trusted_request_origin() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_request_error_or_cancellation_restores_outer_binding(error):
    async def application(scope, receive, send):
        assert trusted_request_origin() == "203.0.113.20"
        raise error()

    middleware = SiteRequestContextMiddleware(application)
    with bind_request_origin("192.0.2.1"):
        with pytest.raises(error):
            await middleware({"type": "http", "client": ("203.0.113.20", 1), "headers": []}, None, None)
        assert trusted_request_origin() == "192.0.2.1"
    assert trusted_request_origin() is None


@pytest.mark.asyncio
async def test_non_http_scope_is_forwarded_without_parsing_request():
    scopes = []

    async def application(scope, receive, send):
        scopes.append(scope)

    scope = {"type": "lifespan"}
    await SiteRequestContextMiddleware(application)(scope, None, None)
    assert scopes == [scope]
    assert trusted_request_origin() is None


def test_main_registers_pure_asgi_context_middleware():
    from app.main import app

    assert SiteRequestContextMiddleware in {middleware.cls for middleware in app.user_middleware}
