"""可观测性与健康端点回归测试。"""
from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.core.observability import RequestContextMiddleware, resolve_request_id


def _build_request_id_app() -> FastAPI:
    """构造仅用于验证请求 ID 中间件的轻量 FastAPI 应用。

    Returns:
        FastAPI: 注册请求上下文中间件和测试路由的应用。
    """
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/echo")
    def echo(request: Request) -> dict[str, str]:
        """回显服务端最终采用的请求 ID。

        Args:
            request: 当前 HTTP 请求。

        Returns:
            dict[str, str]: 包含服务端请求 ID 的响应体。
        """
        return {"request_id": request.state.request_id}

    return app


def test_resolve_request_id_accepts_safe_value() -> None:
    """合法请求 ID 应原样保留。"""
    request_id = "trace-20260710-abcdef"
    assert resolve_request_id(request_id) == request_id


def test_resolve_request_id_replaces_unsafe_value() -> None:
    """过短或含控制字符的请求 ID 应由服务端替换。"""
    resolved = resolve_request_id("bad\nvalue")
    assert resolved != "bad\nvalue"
    assert len(resolved) == 32
    assert resolved.isalnum()


def test_request_context_sets_state_and_response_header() -> None:
    """中间件应把同一请求 ID 写入 request.state 与响应头。"""
    client = TestClient(_build_request_id_app())
    response = client.get("/echo", headers={"X-Request-Id": "trace-client-123456"})
    assert response.status_code == 200
    assert response.json()["request_id"] == "trace-client-123456"
    assert response.headers["X-Request-Id"] == "trace-client-123456"


def test_request_context_generates_request_id_when_missing() -> None:
    """缺少请求 ID 时应生成稳定回显的服务端 ID。"""
    client = TestClient(_build_request_id_app())
    response = client.get("/echo")
    assert response.status_code == 200
    generated = response.headers["X-Request-Id"]
    assert response.json()["request_id"] == generated
    assert len(generated) == 32


def test_unhandled_error_uses_validated_request_id() -> None:
    """错误响应体与响应头必须使用中间件校验后的同一请求 ID。"""
    from app.core.error_handlers import register_handlers

    app = FastAPI()
    register_handlers(app)
    app.add_middleware(RequestContextMiddleware)

    @app.get("/boom")
    def boom() -> None:
        """触发未处理异常以验证统一错误处理器。"""
        raise RuntimeError("boom")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/boom", headers={"X-Request-Id": "bad\nvalue"})
    assert response.status_code == 500
    assert response.json()["request_id"] == response.headers["X-Request-Id"]
    assert response.json()["request_id"] != "bad\nvalue"


def test_healthz_exposes_version_and_release(monkeypatch) -> None:
    """存活端点应返回当前语义版本和不可变 release 标识。"""
    from app import main

    monkeypatch.setattr(main.settings, "app_release", "a" * 40)
    monkeypatch.setattr(main.settings, "app_version", "3.6.0")
    assert main.healthz() == {
        "status": "ok",
        "version": "3.6.0",
        "release": "a" * 40,
    }


def test_readyz_returns_503_when_database_is_unavailable(monkeypatch) -> None:
    """数据库不可连接时就绪端点必须返回 503。"""
    from app import main

    monkeypatch.setattr(main, "database_is_ready", lambda: False)
    response = main.readyz()
    assert response.status_code == 503
    assert response.headers["Cache-Control"] == "no-store"
    assert json.loads(response.body) == {
        "status": "not_ready",
        "version": main.settings.app_version,
        "release": main.settings.app_release,
    }


def test_readyz_returns_version_release_and_ready_status(monkeypatch) -> None:
    """数据库可用时公网契约只返回状态、版本和 release。"""
    from app import main

    monkeypatch.setattr(main, "database_is_ready", lambda: True)
    monkeypatch.setattr(main.settings, "app_release", "b" * 40)
    monkeypatch.setattr(main.settings, "app_version", "3.6.0")
    response = main.readyz()

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert json.loads(response.body) == {
        "status": "ready",
        "version": "3.6.0",
        "release": "b" * 40,
    }


def test_metrics_endpoint_uses_prometheus_content_type() -> None:
    """指标端点应输出 Prometheus exposition format。"""
    from app import main

    response = main.metrics()
    assert response.status_code == 200
    assert response.media_type.startswith("text/plain")
    assert b"prism_http_requests_total" in response.body
