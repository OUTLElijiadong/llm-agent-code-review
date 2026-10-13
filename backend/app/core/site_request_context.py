"""站内工具重入和后台任务共享的可信请求来源，仅由服务器绑定。"""

from __future__ import annotations

import ipaddress
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.rate_limit import client_ip

_REQUEST_ORIGIN: ContextVar[str | None] = ContextVar("site_request_origin", default=None)


def _validated_origin(value: str | None) -> str | None:
    if not isinstance(value, str) or "%" in value:
        return None
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if address.is_unspecified or address.is_multicast:
        return None
    return str(address)


def trusted_request_origin() -> str | None:
    """读取经过可信代理识别的来源，不读取模型参数或新的客户端头。"""
    return _REQUEST_ORIGIN.get()


@contextmanager
def bind_request_origin(value: str | None) -> Iterator[None]:
    """绑定 HTTP 来源或服务器私有事件中的来源，并在异常/结束时恢复。"""
    token = _REQUEST_ORIGIN.set(_validated_origin(value))
    try:
        yield
    finally:
        _REQUEST_ORIGIN.reset(token)


class SiteRequestContextMiddleware:
    """纯 ASGI 包裹整个 HTTP/SSE 生命周期，沿用现有可信代理规则。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # client_ip 的无 peer 兜底不是可信来源；敏感后台操作必须保留缺失态。
        origin = client_ip(Request(scope)) if scope.get("client") else None
        with bind_request_origin(origin):
            await self.app(scope, receive, send)
