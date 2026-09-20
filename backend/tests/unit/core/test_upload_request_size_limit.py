from __future__ import annotations

import pytest
from starlette.responses import JSONResponse

from app.core.request_size_limit import UploadRequestSizeLimitMiddleware


async def _invoke(
    *,
    path: str,
    body: bytes,
    content_length: str | None = None,
    content_type: str | None = None,
):
    called = False

    async def downstream(scope, receive, send):
        nonlocal called
        called = True
        received = bytearray()
        while True:
            message = await receive()
            received.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        await JSONResponse({"received": len(received)})(scope, receive, send)

    headers = []
    if content_length is not None:
        headers.append((b"content-length", content_length.encode("ascii")))
    if content_type is not None:
        headers.append((b"content-type", content_type.encode("ascii")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 443),
        "state": {"request_id": "upload-limit-test"},
    }
    messages = [{"type": "http.request", "body": body, "more_body": False}]
    sent = []

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    middleware = UploadRequestSizeLimitMiddleware(
        downstream,
        max_source_bytes=4,
        max_folder_bytes=8,
        max_avatar_bytes=2,
        max_json_bytes=4,
    )
    await middleware(scope, receive, send)
    return called, sent


@pytest.mark.asyncio
async def test_upload_limit_rejects_content_length_before_downstream_parsing():
    called, sent = await _invoke(
        path="/api/code-files/upload",
        body=b"12345",
        content_length="5",
    )

    assert called is False
    assert sent[0]["status"] == 413
    assert b'"code":41300' in sent[1]["body"]


@pytest.mark.asyncio
async def test_upload_limit_counts_chunked_body_without_content_length():
    called, sent = await _invoke(
        path="/api/projects/7/audit-source-archive",
        body=b"12345",
    )

    assert called is True
    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_upload_limit_does_not_apply_to_non_upload_api():
    called, sent = await _invoke(
        path="/api/review/tasks",
        body=b"12345",
        content_length="5",
    )

    assert called is True
    assert sent[0]["status"] == 200


@pytest.mark.asyncio
async def test_folder_and_avatar_use_independent_request_limits():
    folder_called, folder_sent = await _invoke(
        path="/api/code-files/upload-folder",
        body=b"123456",
        content_length="6",
    )
    avatar_called, avatar_sent = await _invoke(
        path="/api/me/avatar/image",
        body=b"123",
        content_length="3",
    )

    assert folder_called is True
    assert folder_sent[0]["status"] == 200
    assert avatar_called is False
    assert avatar_sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_json_limit_rejects_declared_oversize_before_downstream():
    called, sent = await _invoke(
        path="/api/auth/login",
        body=b"12345",
        content_length="5",
        content_type="application/json",
    )

    assert called is False
    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_json_limit_counts_chunked_body_without_content_length():
    called, sent = await _invoke(
        path="/api/auth/login",
        body=b"12345",
        content_type="application/json; charset=utf-8",
    )

    assert called is True
    assert sent[0]["status"] == 413


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_type", ["lifespan", "websocket"])
async def test_non_http_scope_without_headers_reaches_downstream_unchanged(scope_type):
    scope = {"type": scope_type, "asgi": {"version": "3.0"}, "state": {}}
    reached = []

    async def receive():
        raise AssertionError("请求体限制不能消费非 HTTP 消息")

    async def send(_message):
        raise AssertionError("请求体限制不能为非 HTTP 连接写 HTTP 响应")

    async def downstream(actual_scope, actual_receive, actual_send):
        reached.append(actual_scope)
        assert actual_scope is scope
        assert actual_receive is receive
        assert actual_send is send

    middleware = UploadRequestSizeLimitMiddleware(
        downstream, max_source_bytes=4, max_folder_bytes=8, max_avatar_bytes=2,
    )
    await middleware(scope, receive, send)

    assert reached == [scope]
