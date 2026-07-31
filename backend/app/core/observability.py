"""HTTP 请求上下文、Prometheus 指标与数据库就绪探测。"""
from __future__ import annotations

import re
import time
import uuid
from typing import Optional

from loguru import logger
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from sqlalchemy import text
from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")

HTTP_REQUESTS_TOTAL = Counter(
    "prism_http_requests_total",
    "Prism HTTP 请求总数。",
    ("method", "path", "status"),
)
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "prism_http_request_duration_seconds",
    "Prism HTTP 请求耗时（秒）。",
    ("method", "path"),
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
HTTP_REQUESTS_IN_PROGRESS = Gauge(
    "prism_http_requests_in_progress",
    "Prism 当前处理中 HTTP 请求数。",
    ("method",),
)


def resolve_request_id(candidate: Optional[str]) -> str:
    """校验客户端请求 ID，不合法或缺失时生成服务端 ID。

    Args:
        candidate: 客户端 ``X-Request-Id`` 候选值。

    Returns:
        str: 可安全用于响应头和日志字段的请求 ID。
    """
    if candidate and _REQUEST_ID_PATTERN.fullmatch(candidate):
        return candidate
    return uuid.uuid4().hex


def get_request_id(request: Request) -> str:
    """读取中间件写入的请求 ID，并为未经过中间件的请求兜底生成。

    Args:
        request: 当前 Starlette/FastAPI 请求。

    Returns:
        str: 已校验的服务端请求 ID。
    """
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        return request_id
    request_id = resolve_request_id(request.headers.get("X-Request-Id"))
    request.state.request_id = request_id
    return request_id


def _route_label(scope: Scope) -> str:
    """从 ASGI scope 提取低基数路由模板，避免指标标签包含动态 ID。

    Args:
        scope: 当前 ASGI 请求上下文。

    Returns:
        str: 路由模板；未匹配路由时返回 ``__unmatched__``。
    """
    route = scope.get("route")
    route_path = getattr(route, "path", None)
    return str(route_path or "__unmatched__")


class RequestContextMiddleware:
    """为每个 HTTP 请求提供 request id、结构化日志上下文和基础指标。"""

    def __init__(self, app: ASGIApp) -> None:
        """初始化 ASGI 中间件。

        Args:
            app: 下游 ASGI 应用。

        Returns:
            None: 构造函数不返回值。
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """处理一次 ASGI 调用并注入请求上下文。

        Args:
            scope: ASGI 连接上下文。
            receive: ASGI 消息接收函数。
            send: ASGI 消息发送函数。

        Returns:
            None: 响应由下游应用通过 ``send`` 发送。
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", "UNKNOWN"))
        request_id = resolve_request_id(Headers(scope=scope).get("X-Request-Id"))
        scope.setdefault("state", {})["request_id"] = request_id
        started_at = time.perf_counter()
        status_code = 500
        HTTP_REQUESTS_IN_PROGRESS.labels(method=method).inc()

        async def send_with_request_id(message: Message) -> None:
            """给响应起始消息附加请求 ID 并记录状态码。

            Args:
                message: 下游应用产生的 ASGI 消息。

            Returns:
                None: 消息会继续转发给原始 ``send``。
            """
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                MutableHeaders(scope=message)["X-Request-Id"] = request_id
            await send(message)

        with logger.contextualize(request_id=request_id):
            try:
                await self.app(scope, receive, send_with_request_id)
            finally:
                elapsed = max(time.perf_counter() - started_at, 0.0)
                path = _route_label(scope)
                HTTP_REQUESTS_TOTAL.labels(
                    method=method,
                    path=path,
                    status=str(status_code),
                ).inc()
                HTTP_REQUEST_DURATION_SECONDS.labels(
                    method=method,
                    path=path,
                ).observe(elapsed)
                HTTP_REQUESTS_IN_PROGRESS.labels(method=method).dec()
                logger.bind(
                    method=method,
                    path=path,
                    status=status_code,
                    duration_ms=round(elapsed * 1000, 2),
                ).info("HTTP request completed")


def database_is_ready() -> bool:
    """执行最小只读 SQL，确认应用数据库当前可连接。

    Returns:
        bool: ``SELECT 1`` 成功时为 True，否则为 False。
    """
    from app.core.database import engine

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.warning("数据库就绪探测失败: {}", type(exc).__name__)
        return False


def render_metrics() -> tuple[bytes, str]:
    """渲染 Prometheus 文本格式指标。

    Returns:
        tuple[bytes, str]: 指标字节与 Prometheus Content-Type。
    """
    return generate_latest(), CONTENT_TYPE_LATEST
