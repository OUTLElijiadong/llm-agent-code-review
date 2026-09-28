"""HTTP contract tests for the independently mounted Responses router."""

from __future__ import annotations

import json
from typing import AsyncIterator
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from app.api import responses as module
from app.services.deepseek_responses_service import (
    BufferedGatewayResponse,
    DeepSeekResponsesService,
    MemoryTranscriptStore,
    StreamingGatewayResponse,
)


async def _client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
def app() -> FastAPI:
    application = FastAPI()
    application.include_router(module.router)
    return application


@pytest.mark.asyncio
async def test_public_responses_scope_is_bearer_key_not_product_login_cookie(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        upstream.append({"authorization": request.headers["authorization"], "payload": payload})
        if request.headers["authorization"] == "Bearer stale-prism-jwt":
            return httpx.Response(401, json={"error": {"message": "invalid upstream key"}})
        return httpx.Response(200, json={
            "id": f"resp_{len(upstream)}", "object": "response", "status": "completed",
            "output": [{"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "ok"},
            ]}],
        })

    transport = httpx.MockTransport(handler)
    service = DeepSeekResponsesService(
        storage=MemoryTranscriptStore(),
        client_factory=lambda: httpx.AsyncClient(transport=transport),
    )
    monkeypatch.setattr(module, "_service", service)
    key_a = {"Authorization": "Bearer upstream-a"}
    key_b = {"Authorization": "Bearer upstream-b"}

    async for client in _client(app):
        # 产品登录 Cookie 不能代替上游密钥。
        cookie_only = await client.post(
            "/v1/responses", headers={"Cookie": "access_token=prism-user-a"},
            json={"model": "m", "input": "private"},
        )
        assert cookie_only.status_code == 401
        assert cookie_only.json()["error"]["code"] == "invalid_api_key"

        # 模拟旧登录令牌的字符串不会被解析成产品账号；它原样转交上游。
        stale_token = await client.post(
            "/v1/responses", headers={"Authorization": "Bearer stale-prism-jwt"},
            json={"model": "m", "input": "private"},
        )
        assert stale_token.status_code == 401
        assert stale_token.json()["error"]["message"] == "invalid upstream key"
        upstream.clear()

        created = await client.post(
            "/v1/responses", headers={**key_a, "Cookie": "access_token=prism-user-a"},
            json={"model": "m", "input": "first"},
        )
        assert created.status_code == 200
        assert created.json()["id"] == "resp_1"

        # 换产品 Cookie 但复用同一上游密钥，公开 API 仍是同一身份。
        same_key = await client.get(
            "/v1/responses/resp_1", headers={**key_a, "Cookie": "access_token=prism-user-b"},
        )
        assert same_key.status_code == 200
        assert same_key.json()["id"] == "resp_1"

        independent = await client.post(
            "/v1/responses", headers={**key_a, "Cookie": "access_token=prism-user-b"},
            json={"model": "m", "input": "independent session"},
        )
        assert independent.status_code == 200
        assert independent.json()["id"] == "resp_2"
        assert (await client.get("/v1/responses/resp_2", headers=key_a)).status_code == 200

        # 不同密钥的查询、历史回放、列表与删除都不能触及 A 的记录。
        assert (await client.get("/v1/responses/resp_1", headers=key_b)).status_code == 404
        assert (await client.get("/v1/responses/resp_1/input_items", headers=key_b)).status_code == 404
        assert (await client.delete("/v1/responses/resp_1", headers=key_b)).status_code == 404
        forbidden_replay = await client.post(
            "/v1/responses", headers=key_b,
            json={"model": "m", "previous_response_id": "resp_1", "input": "continue"},
        )
        assert forbidden_replay.status_code == 404
        assert len(upstream) == 2  # 拒绝跨密钥历史，未发送上游请求。

        continued = await client.post(
            "/v1/responses", headers=key_a,
            json={"model": "m", "previous_response_id": "resp_1", "input": "continue"},
        )
        assert continued.status_code == 200
        assert len(upstream) == 3
        assert upstream[2]["authorization"] == "Bearer upstream-a"
        assert "previous_response_id" not in upstream[2]["payload"]
        assert len(upstream[2]["payload"]["input"]) == 3
        assert (await client.get("/v1/responses/resp_1/input_items", headers=key_a)).status_code == 200
        assert (await client.delete("/v1/responses/resp_1", headers=key_a)).status_code == 200
        assert (await client.get("/v1/responses/resp_1", headers=key_a)).status_code == 404


@pytest.mark.asyncio
async def test_router_exposes_exact_top_level_responses_paths(app: FastAPI) -> None:
    # FastAPI 0.139 会延迟展开 include_router；OpenAPI 是实际对外路由的稳定表示。
    paths = app.openapi()["paths"]
    operations = {
        (path, method.upper())
        for path, definitions in paths.items()
        for method in definitions
    }
    assert ("/v1/responses", "POST") in operations
    assert ("/v1/responses/{response_id}", "GET") in operations
    assert ("/v1/responses/{response_id}", "DELETE") in operations
    assert ("/v1/responses/{response_id}/input_items", "GET") in operations
    assert ("/v1/responses/{response_id}/cancel", "POST") not in operations


@pytest.mark.asyncio
async def test_create_relays_authorization_status_raw_json_and_upstream_headers(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = AsyncMock()
    raw = b'{"error":{"message":"upstream rejected"}}'
    service.create.return_value = BufferedGatewayResponse(
        status_code=422,
        headers={
            "content-type": "application/json",
            "content-length": "999",
            "date": "Thu, 30 Jul 2026 00:00:00 GMT",
            "retry-after": "3",
            "server": "upstream-server",
            "x-request-id": "deepseek-request",
        },
        content=raw,
    )
    monkeypatch.setattr(module, "_service", service)

    async for client in _client(app):
        response = await client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer exact-key"},
            json={"model": "deepseek-v4-flash", "input": "hello"},
        )

    assert response.status_code == 422
    assert response.content == raw
    assert response.headers["content-type"] == "application/json"
    assert response.headers["retry-after"] == "3"
    assert response.headers["x-request-id"] == "deepseek-request"
    assert response.headers["content-length"] == str(len(raw))
    assert "date" not in response.headers
    assert "server" not in response.headers
    service.create.assert_awaited_once_with(
        {"model": "deepseek-v4-flash", "input": "hello"},
        "Bearer exact-key",
    )


@pytest.mark.asyncio
async def test_create_stream_relays_exact_sse_without_blank_payload_events(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = [
        b'event: response.output_text.delta\ndata: {"delta":"a"}\n\n',
        b'event: response.completed\ndata: {"type":"response.completed"}\n\n',
    ]

    async def body() -> AsyncIterator[bytes]:
        for chunk in chunks:
            yield chunk

    service = AsyncMock()
    service.create.return_value = StreamingGatewayResponse(
        status_code=200,
        headers={"content-type": "text/event-stream", "x-request-id": "stream-request"},
        body=body(),
    )
    monkeypatch.setattr(module, "_service", service)

    async for client in _client(app):
        response = await client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer stream-key"},
            json={"model": "deepseek-v4-flash", "input": "hello", "stream": True},
        )

    assert response.status_code == 200
    assert response.content == b"".join(chunks)
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    assert b"data: \n\n" not in response.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b"not-json", "invalid_json"),
        (b"[]", "invalid_request"),
    ],
)
async def test_create_rejects_invalid_json_with_responses_error_shape(
    app: FastAPI,
    body: bytes,
    code: str,
) -> None:
    async for client in _client(app):
        response = await client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer key", "Content-Type": "application/json"},
            content=body,
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == code
    assert "code" not in {key for key in response.json() if key != "error"}


@pytest.mark.asyncio
async def test_retrieve_delete_and_input_items_delegate_with_bearer_scope(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = AsyncMock()
    service.retrieve.return_value = {"id": "resp_1", "object": "response", "output": []}
    service.delete.return_value = {"id": "resp_1", "object": "response", "deleted": True}
    service.list_input_items.return_value = {
        "object": "list",
        "data": [{"id": "msg_1", "type": "message"}],
        "first_id": "msg_1",
        "last_id": "msg_1",
        "has_more": False,
    }
    monkeypatch.setattr(module, "_service", service)
    headers = {"Authorization": "Bearer tenant-key"}

    async for client in _client(app):
        retrieved = await client.get("/v1/responses/resp_1", headers=headers)
        items = await client.get(
            "/v1/responses/resp_1/input_items",
            headers=headers,
            params=[("after", "msg_0"), ("include", "reasoning.encrypted_content"), ("limit", "7"), ("order", "asc")],
        )
        deleted = await client.delete("/v1/responses/resp_1", headers=headers)

    assert retrieved.json()["id"] == "resp_1"
    assert items.json()["data"][0]["id"] == "msg_1"
    assert deleted.json()["deleted"] is True
    service.retrieve.assert_awaited_once_with("resp_1", "Bearer tenant-key")
    service.list_input_items.assert_awaited_once_with(
        "resp_1",
        "Bearer tenant-key",
        after="msg_0",
        limit=7,
        order="asc",
    )
    service.delete.assert_awaited_once_with("resp_1", "Bearer tenant-key")


@pytest.mark.asyncio
async def test_local_service_errors_keep_openai_error_envelope(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.deepseek_responses_service import ResponseNotFoundError

    service = AsyncMock()
    service.retrieve.side_effect = ResponseNotFoundError("resp_missing")
    monkeypatch.setattr(module, "_service", service)

    async for client in _client(app):
        response = await client.get(
            "/v1/responses/resp_missing",
            headers={"Authorization": "Bearer key"},
        )

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "message": "Response 'resp_missing' not found.",
            "type": "invalid_request_error",
            "param": "response_id",
            "code": "response_not_found",
        }
    }


@pytest.mark.asyncio
async def test_invalid_input_items_limit_uses_responses_error_envelope(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = AsyncMock()
    monkeypatch.setattr(module, "_service", service)

    async for client in _client(app):
        response = await client.get(
            "/v1/responses/resp_1/input_items?limit=not-an-integer",
            headers={"Authorization": "Bearer key"},
        )

    assert response.status_code == 400
    assert response.json()["error"] == {
        "message": "Invalid limit; expected an integer from 1 to 100.",
        "type": "invalid_request_error",
        "param": "limit",
        "code": "invalid_value",
    }
    service.list_input_items.assert_not_awaited()
