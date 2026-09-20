"""真实 ASGI 中间件链必须启动生命周期，服务端不可静默跳过启动异常。"""

from contextlib import asynccontextmanager
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_application_middleware_chain_delivers_lifespan_startup_and_shutdown(monkeypatch):
    from app.main import app

    transitions = []

    @asynccontextmanager
    async def tracked_lifespan(actual_app):
        assert actual_app is app
        transitions.append("startup")
        yield
        transitions.append("shutdown")

    # 用无外部副作用的生命周期回调，真实遍历生产应用全部中间件。
    monkeypatch.setattr(app.router, "lifespan_context", tracked_lifespan)
    incoming = iter(({"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}))
    outgoing = []

    async def receive():
        return next(incoming)

    async def send(message):
        outgoing.append(message)

    await app({"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}, receive, send)

    assert transitions == ["startup", "shutdown"]
    assert [message["type"] for message in outgoing] == ["lifespan.startup.complete", "lifespan.shutdown.complete"]


@pytest.mark.asyncio
async def test_uvicorn_required_lifespan_exits_when_middleware_rejects_protocol():
    from uvicorn import Config
    from uvicorn.lifespan.on import LifespanOn

    async def broken_middleware(_scope, _receive, _send):
        raise KeyError("headers")

    lifecycle = LifespanOn(Config(broken_middleware, lifespan="on", log_config=None))
    await lifecycle.startup()

    assert lifecycle.should_exit is True


def test_production_uvicorn_requires_lifespan_protocol():
    dockerfile = Path(__file__).resolve().parents[3] / "Dockerfile"
    command = next(line for line in dockerfile.read_text().splitlines() if line.startswith("CMD "))
    assert '"--lifespan", "on"' in command
