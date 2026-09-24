"""Focused tests for the stateless DeepSeek Responses compatibility layer."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, AsyncIterator, Callable, Dict, List

import httpx
import pytest

from app.services.deepseek_responses_runtime import ContextBudgetError, estimate_tokens
from app.services.deepseek_responses_service import (
    BufferedGatewayResponse,
    DeepSeekResponsesService,
    MemoryTranscriptStore,
    RedisTranscriptStore,
    ResilientTranscriptStore,
    ResponseNotFoundError,
    ResponsesGatewayError,
    ResponseTranscript,
    StreamingGatewayResponse,
    TranscriptStoreUnavailableError,
)


def _client_factory(handler: Callable[[httpx.Request], httpx.Response]) -> Callable[[], httpx.AsyncClient]:
    transport = httpx.MockTransport(handler)
    return lambda: httpx.AsyncClient(transport=transport)


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _response(response_id: str, output: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "status": "completed",
        "store": False,
        "output": output,
    }


class ChunkStream(httpx.AsyncByteStream):
    """Finite byte stream used by MockTransport without network access."""

    def __init__(self, chunks: List[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_non_stream_replays_complete_transcript_and_isolates_bearer_credentials() -> None:
    requests: List[Dict[str, Any]] = []
    call = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": "read_file",
        "arguments": '{"path":"README.md"}',
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.deepseek.com/v1/responses"
        assert request.headers["authorization"] == "Bearer tenant-a"
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(200, json=_response("resp_1", [call]), headers={"x-request-id": "req-1"})
        message = {
            "type": "message",
            "id": "msg_2",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "done", "annotations": []}],
        }
        return httpx.Response(200, json=_response("resp_2", [message]))

    storage = MemoryTranscriptStore()
    service = DeepSeekResponsesService(storage=storage, client_factory=_client_factory(handler))

    first = await service.create(
        {"model": "deepseek-v4-flash", "input": "Read README", "tools": [{"type": "function"}]},
        "Bearer tenant-a",
    )
    assert isinstance(first, BufferedGatewayResponse)
    assert first.status_code == 200
    assert first.headers["x-request-id"] == "req-1"

    tool_output = {"type": "function_call_output", "call_id": "call_1", "output": "file contents"}
    second = await service.create(
        {
            "model": "deepseek-v4-flash",
            "previous_response_id": "resp_1",
            "input": [tool_output],
            "tools": [{"type": "function"}],
        },
        "Bearer tenant-a",
    )
    assert isinstance(second, BufferedGatewayResponse)
    assert "previous_response_id" not in requests[1]
    assert requests[1]["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Read README"}],
        },
        call,
        tool_output,
    ]

    assert (await service.retrieve("resp_2", "Bearer tenant-a"))["status"] == "completed"
    with pytest.raises(ResponseNotFoundError):
        await service.retrieve("resp_2", "Bearer tenant-b")

    items = await service.list_input_items("resp_2", "Bearer tenant-a", order="asc", limit=2)
    assert [item["type"] for item in items["data"]] == ["message", "function_call"]
    assert items["first_id"].startswith("msg_")
    assert items["last_id"] == "fc_1"
    assert items["has_more"] is True

    next_page = await service.list_input_items(
        "resp_2",
        "Bearer tenant-a",
        order="asc",
        after=items["last_id"],
    )
    assert [item["type"] for item in next_page["data"]] == ["function_call_output"]

    assert await service.delete("resp_2", "Bearer tenant-a") == {
        "id": "resp_2",
        "object": "response",
        "deleted": True,
    }
    with pytest.raises(ResponseNotFoundError):
        await service.retrieve("resp_2", "Bearer tenant-a")


@pytest.mark.asyncio
async def test_previous_response_compacts_with_verified_sources_and_keeps_full_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.services.deepseek_responses_service.settings.deepseek_context_window_tokens", 6_000)
    monkeypatch.setattr("app.services.deepseek_responses_service.settings.deepseek_max_output_tokens", 512)
    history = [
        {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "审查全部证据，保留认证约束"},
        ]},
    ]
    for index in range(6):
        history.extend([
            {"type": "function_call", "call_id": f"call_{index}", "name": "read_file",
             "arguments": f'{{"file":"{index}.py"}}'},
            {"type": "function_call_output", "call_id": f"call_{index}",
             "output": f"证据{index}:" + "important finding " * 450},
        ])
    storage = MemoryTranscriptStore()
    await storage.save(_fingerprint("key"), ResponseTranscript(
        response_id="resp_before", response=_response("resp_before", []), input_items=history,
        transcript=history, created_at=1.0,
    ))
    requests: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if payload.get("instructions", "").startswith("你是上下文压缩器"):
            source = payload["input"][0]["content"]
            source_ids = re.findall(r'"source_id":"([^"]+)"', source)
            assert source_ids
            return httpx.Response(200, json=_response("resp_compact", [{
                "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": json.dumps({
                    "covered_source_ids": source_ids,
                    "summary": "已核查来源中的用户认证约束和 read_file 工具证据；继续审查。",
                }, ensure_ascii=False)}],
            }]))
        return httpx.Response(200, json=_response("resp_after", []))

    service = DeepSeekResponsesService(storage=storage, client_factory=_client_factory(handler))
    latest = {"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": "继续，并核对最后一个文件"},
    ]}
    await service.create({"model": "m", "previous_response_id": "resp_before", "input": [latest],
                          "max_output_tokens": 512}, "Bearer key")

    assert len(requests) >= 2
    final = requests[-1]
    assert "previous_response_id" not in final
    assert final["input"][0]["role"] == "user"
    assert "来源 sha256=" in final["input"][0]["content"][0]["text"]
    assert final["input"][-1] == latest
    assert estimate_tokens(final["input"]) + 512 < 6_000
    stored = await storage.load(_fingerprint("key"), "resp_after")
    assert stored is not None
    assert stored.transcript == history + [latest]


@pytest.mark.parametrize("bad_kind", ["missing_coverage", "length"])
@pytest.mark.asyncio
async def test_previous_response_compaction_rejects_unverified_summary_before_main_request(
    monkeypatch: pytest.MonkeyPatch,
    bad_kind: str,
) -> None:
    monkeypatch.setattr("app.services.deepseek_responses_service.settings.deepseek_context_window_tokens", 6_000)
    monkeypatch.setattr("app.services.deepseek_responses_service.settings.deepseek_max_output_tokens", 512)
    history = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "最初目标"}]},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "证据 " * 15_000}]},
    ]
    storage = MemoryTranscriptStore()
    await storage.save(_fingerprint("key"), ResponseTranscript(
        response_id="resp_before", response=_response("resp_before", []), input_items=history,
        transcript=history, created_at=1.0,
    ))
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        source_ids = re.findall(r'"source_id":"([^"]+)"', payload["input"][0]["content"])
        bad_response = _response("resp_bad", [{
            "type": "message", "role": "assistant", "content": [{"type": "output_text", "text":
                json.dumps({
                    "covered_source_ids": source_ids if bad_kind == "length" else [],
                    "summary": "来源摘要未能正常完成",
                }, ensure_ascii=False)}],
        }])
        if bad_kind == "length":
            bad_response["output"][0]["finish_reason"] = "length"
        return httpx.Response(200, json=bad_response)

    service = DeepSeekResponsesService(storage=storage, client_factory=_client_factory(handler))
    with pytest.raises(ResponsesGatewayError) as exc:
        await service.create({"model": "m", "previous_response_id": "resp_before", "input": "继续",
                              "max_output_tokens": 512}, "Bearer key")
    assert exc.value.code == "context_compaction_failed"
    assert len(calls) == (3 if bad_kind == "length" else 1)
    assert await storage.load(_fingerprint("key"), "resp_bad") is None


@pytest.mark.asyncio
async def test_single_oversized_latest_user_input_fails_locally_without_upstream_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.services.deepseek_responses_service.settings.deepseek_context_window_tokens", 6_000)
    monkeypatch.setattr("app.services.deepseek_responses_service.settings.deepseek_max_output_tokens", 512)
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=_response("unexpected", []))

    service = DeepSeekResponsesService(
        storage=MemoryTranscriptStore(), client_factory=_client_factory(handler),
    )
    with pytest.raises(ResponsesGatewayError) as exc:
        await service.create({"model": "m", "input": "不能丢失的最新请求" * 3_000,
                              "max_output_tokens": 512}, "Bearer key")
    assert exc.value.status_code == 413
    assert exc.value.param == "input"
    assert requests == []


@pytest.mark.parametrize(
    ("upstream_status", "expected_status", "expected_code", "expected_type"),
    [(401, 401, "invalid_api_key", "authentication_error"),
     (403, 403, "permission_denied", "permission_error"),
     (429, 429, "upstream_rate_limited", "rate_limit_error"),
     (503, 503, "upstream_unavailable", "server_error"),
     (500, 502, "upstream_unavailable", "server_error")],
)
@pytest.mark.asyncio
async def test_compaction_upstream_status_is_not_misreported_as_input_capacity(
    monkeypatch: pytest.MonkeyPatch, upstream_status: int, expected_status: int,
    expected_code: str, expected_type: str,
) -> None:
    monkeypatch.setattr("app.services.deepseek_responses_service.settings.deepseek_context_window_tokens", 6_000)
    monkeypatch.setattr("app.services.deepseek_responses_service.settings.deepseek_max_output_tokens", 512)
    storage = MemoryTranscriptStore()
    history = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "目标"}]},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "证据" * 4_000}]},
    ]
    await storage.save(_fingerprint("key"), ResponseTranscript(
        response_id="resp_prior", response=_response("resp_prior", []), input_items=history,
        transcript=history, created_at=1.0,
    ))
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(upstream_status, json={"error": {"message": "upstream"}})

    service = DeepSeekResponsesService(storage=storage, client_factory=_client_factory(handler))
    with pytest.raises(ResponsesGatewayError) as exc:
        await service.create({"model": "m", "previous_response_id": "resp_prior",
                              "input": "继续", "max_output_tokens": 512}, "Bearer key")
    assert (exc.value.status_code, exc.value.code, exc.value.error_type) == (
        expected_status, expected_code, expected_type,
    )
    assert len(requests) == 1 and requests[0]["store"] is False


@pytest.mark.asyncio
async def test_compaction_timeout_is_504_not_context_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.deepseek_responses_service.settings.deepseek_context_window_tokens", 6_000)
    monkeypatch.setattr("app.services.deepseek_responses_service.settings.deepseek_max_output_tokens", 512)
    storage = MemoryTranscriptStore()
    history = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "目标"}]},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "证据" * 4_000}]},
    ]
    await storage.save(_fingerprint("key"), ResponseTranscript(
        response_id="resp_prior", response=_response("resp_prior", []), input_items=history,
        transcript=history, created_at=1.0,
    ))

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    service = DeepSeekResponsesService(storage=storage, client_factory=_client_factory(handler))
    with pytest.raises(ResponsesGatewayError) as exc:
        await service.create({"model": "m", "previous_response_id": "resp_prior",
                              "input": "继续", "max_output_tokens": 512}, "Bearer key")
    assert (exc.value.status_code, exc.value.code) == (504, "upstream_timeout")


@pytest.mark.asyncio
async def test_second_layer_rejects_missing_original_source_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.deepseek_responses_service.settings.deepseek_context_window_tokens", 20_000)
    monkeypatch.setattr("app.services.deepseek_responses_service.settings.deepseek_max_output_tokens", 512)
    second_layer_seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        items = json.loads(payload["input"][0]["content"])
        original_ids = [source_id for item in items for source_id in item["covered_source_ids"]]
        if items[0]["source_id"].startswith("第1层压缩块"):
            second_layer_seen.append(True)
            original_ids = original_ids[:-1]
        return httpx.Response(200, json=_response("resp_compact", [{
            "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": json.dumps({
                "covered_source_ids": original_ids,
                "summary": "已确认历史来源约束。" * 100,
            }, ensure_ascii=False)}],
        }]))

    service = DeepSeekResponsesService(
        storage=MemoryTranscriptStore(), client_factory=_client_factory(handler),
    )
    records = [{"type": "message", "role": "assistant", "content": [{
        "type": "output_text", "text": "证据" * 6_000,
    }]}]
    with pytest.raises(ContextBudgetError, match="来源摘要未完整覆盖"):
        await service._semantic_compact_input(
            records, [0], source_digest="source-digest", summary_budget=900,
            model="m", authorization="Bearer key", calls=[0],
        )
    assert second_layer_seen


@pytest.mark.asyncio
async def test_compactor_retries_output_length_and_accounts_for_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.services.deepseek_responses_service.settings.deepseek_context_window_tokens", 20_000)
    budgets: List[int] = []
    usage_logs: List[Any] = []
    monkeypatch.setattr(
        "app.services.deepseek_responses_service.logger.info",
        lambda message, *values: usage_logs.append((message, values)),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        budgets.append(payload["max_output_tokens"])
        if len(budgets) == 1:
            return httpx.Response(200, json={
                **_response("resp_compact", []), "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "usage": {"input_tokens": 100, "output_tokens": 512},
            })
        return httpx.Response(200, json={
            **_response("resp_compact", [{
                "type": "message", "role": "assistant", "content": [{
                    "type": "output_text", "text": json.dumps({
                        "covered_source_ids": ["来源#0:片段1/1"], "summary": "已核验来源。",
                    }, ensure_ascii=False),
                }],
            }]),
            "usage": {"input_tokens": 100, "output_tokens": 40},
        })

    service = DeepSeekResponsesService(
        storage=MemoryTranscriptStore(), client_factory=_client_factory(handler),
    )
    calls = [0]
    summary = await service._call_compactor(
        model="m", authorization="Bearer key",
        source=[{"source_id": "来源#0:片段1/1", "covered_source_ids": ["来源#0:片段1/1"],
                 "content": "完整事实"}],
        expected_ids=["来源#0:片段1/1"], source_digest="verified-digest",
        output_budget=512, calls=calls,
    )
    assert summary == "已核验来源。"
    assert budgets == [512, 1024]
    assert calls == [2]
    assert usage_logs[-1][1] == ("verified-digest", 2, 200, 552, "completed")


@pytest.mark.asyncio
async def test_store_false_is_forwarded_and_never_persisted() -> None:
    observed: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed.update(json.loads(request.content))
        return httpx.Response(200, json=_response("resp_ephemeral", []))

    service = DeepSeekResponsesService(
        storage=MemoryTranscriptStore(),
        client_factory=_client_factory(handler),
    )
    result = await service.create(
        {"model": "deepseek-v4-flash", "input": "hello", "store": False},
        "Bearer key",
    )

    assert isinstance(result, BufferedGatewayResponse)
    assert observed["store"] is False
    with pytest.raises(ResponseNotFoundError):
        await service.retrieve("resp_ephemeral", "Bearer key")


@pytest.mark.asyncio
async def test_upstream_json_error_status_body_and_headers_are_not_disguised() -> None:
    raw_error = b'{"error":{"message":"rate limited","type":"rate_limit_error"}}'

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            content=raw_error,
            headers={"content-type": "application/json", "retry-after": "7", "x-request-id": "req-rate"},
        )

    service = DeepSeekResponsesService(
        storage=MemoryTranscriptStore(),
        client_factory=_client_factory(handler),
    )
    result = await service.create({"model": "m", "input": "x"}, "Bearer key")

    assert isinstance(result, BufferedGatewayResponse)
    assert result.status_code == 429
    assert result.content == raw_error
    assert result.headers["content-type"] == "application/json"
    assert result.headers["retry-after"] == "7"


@pytest.mark.asyncio
async def test_stream_relays_exact_chunks_and_persists_terminal_response() -> None:
    final_response = _response(
        "resp_stream",
        [
            {
                "type": "message",
                "id": "msg_stream",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "hello", "annotations": []}],
            }
        ],
    )
    created = b'event: response.created\ndata: {"type":"response.created"}\n\n'
    completed = (
        b"event: response.completed\r\n"
        + b"data: "
        + json.dumps({"type": "response.completed", "response": final_response}, separators=(",", ":")).encode()
        + b"\r\n\r\n"
    )
    chunks = [created[:11], created[11:] + completed[:17], completed[17:53], completed[53:]]
    stream = ChunkStream(chunks)

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "x-request-id": "req-stream"},
            stream=stream,
        )

    service = DeepSeekResponsesService(
        storage=MemoryTranscriptStore(),
        client_factory=_client_factory(handler),
    )
    result = await service.create(
        {"model": "deepseek-v4-flash", "input": "hello", "stream": True},
        "Bearer stream-key",
    )

    assert isinstance(result, StreamingGatewayResponse)
    assert result.status_code == 200
    received = [chunk async for chunk in result.body]
    assert received == chunks
    assert stream.closed is True
    assert (await service.retrieve("resp_stream", "Bearer stream-key"))["output"] == final_response["output"]


@pytest.mark.asyncio
async def test_stream_error_body_and_status_are_relayed_without_storage() -> None:
    chunks = [b'{"error":', b'{"message":"bad request"}}']

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            headers={"content-type": "application/json"},
            stream=ChunkStream(chunks),
        )

    service = DeepSeekResponsesService(
        storage=MemoryTranscriptStore(),
        client_factory=_client_factory(handler),
    )
    result = await service.create({"model": "m", "input": "x", "stream": True}, "Bearer key")

    assert isinstance(result, StreamingGatewayResponse)
    assert result.status_code == 400
    assert b"".join([chunk async for chunk in result.body]) == b"".join(chunks)


@pytest.mark.asyncio
async def test_timeout_becomes_explicit_gateway_error_only_when_no_upstream_response_exists() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    service = DeepSeekResponsesService(
        storage=MemoryTranscriptStore(),
        client_factory=_client_factory(handler),
    )

    with pytest.raises(ResponsesGatewayError) as exc:
        await service.create({"model": "m", "input": "x"}, "Bearer key")
    assert exc.value.status_code == 504
    assert exc.value.code == "upstream_timeout"


class FailingStore:
    async def save(self, credential_fingerprint: str, record: ResponseTranscript) -> None:
        raise ConnectionError("redis down")

    async def load(self, credential_fingerprint: str, response_id: str) -> ResponseTranscript:
        raise ConnectionError("redis down")

    async def delete(self, credential_fingerprint: str, response_id: str) -> bool:
        raise ConnectionError("redis down")


class RecoverableStore:
    def __init__(self) -> None:
        self.available = True
        self.records: Dict[tuple, ResponseTranscript] = {}

    def _require_available(self) -> None:
        if not self.available:
            raise ConnectionError("redis down")

    async def save(self, credential_fingerprint: str, record: ResponseTranscript) -> None:
        self._require_available()
        self.records[(credential_fingerprint, record.response_id)] = record

    async def load(self, credential_fingerprint: str, response_id: str) -> Any:
        self._require_available()
        return self.records.get((credential_fingerprint, response_id))

    async def delete(self, credential_fingerprint: str, response_id: str) -> bool:
        self._require_available()
        return self.records.pop((credential_fingerprint, response_id), None) is not None


@pytest.mark.asyncio
async def test_redis_failure_falls_back_for_reads_but_refuses_inconsistent_delete() -> None:
    store = ResilientTranscriptStore(FailingStore(), MemoryTranscriptStore())
    record = ResponseTranscript(
        response_id="resp_fallback",
        response=_response("resp_fallback", []),
        input_items=[],
        transcript=[],
        created_at=1.0,
    )

    await store.save("tenant", record)
    assert (await store.load("tenant", "resp_fallback")).response_id == "resp_fallback"
    with pytest.raises(TranscriptStoreUnavailableError):
        await store.delete("tenant", "resp_fallback")
    assert (await store.load("tenant", "resp_fallback")).response_id == "resp_fallback"


@pytest.mark.asyncio
async def test_fallback_only_record_repairs_redis_after_recovery() -> None:
    primary = RecoverableStore()
    primary.available = False
    store = ResilientTranscriptStore(primary, MemoryTranscriptStore())
    record = ResponseTranscript(
        response_id="resp_during_outage",
        response=_response("resp_during_outage", []),
        input_items=[],
        transcript=[],
        created_at=1.0,
    )

    await store.save("tenant", record)
    primary.available = True

    loaded = await store.load("tenant", "resp_during_outage")
    assert loaded is not None
    assert loaded.response_id == "resp_during_outage"
    assert primary.records[("tenant", "resp_during_outage")].response_id == "resp_during_outage"


@pytest.mark.asyncio
async def test_delete_during_redis_outage_fails_then_succeeds_after_recovery() -> None:
    primary = RecoverableStore()
    store = ResilientTranscriptStore(primary, MemoryTranscriptStore())
    record = ResponseTranscript(
        response_id="resp_delete_during_outage",
        response=_response("resp_delete_during_outage", []),
        input_items=[],
        transcript=[],
        created_at=1.0,
    )
    await store.save("tenant", record)
    assert ("tenant", "resp_delete_during_outage") in primary.records

    primary.available = False
    with pytest.raises(TranscriptStoreUnavailableError):
        await store.delete("tenant", "resp_delete_during_outage")
    primary.available = True

    assert (await store.load("tenant", "resp_delete_during_outage")).response_id == "resp_delete_during_outage"
    assert await store.delete("tenant", "resp_delete_during_outage") is True
    assert await store.load("tenant", "resp_delete_during_outage") is None
    assert ("tenant", "resp_delete_during_outage") not in primary.records


@pytest.mark.asyncio
async def test_service_maps_authoritative_delete_outage_to_responses_503() -> None:
    service = DeepSeekResponsesService(storage=ResilientTranscriptStore(FailingStore(), MemoryTranscriptStore()))
    record = ResponseTranscript(
        response_id="resp_delete_503",
        response=_response("resp_delete_503", []),
        input_items=[],
        transcript=[],
        created_at=1.0,
    )
    await service._storage.save(_fingerprint("key"), record)

    with pytest.raises(ResponsesGatewayError) as exc:
        await service.delete("resp_delete_503", "Bearer key")
    assert exc.value.status_code == 503
    assert exc.value.code == "storage_unavailable"


@pytest.mark.asyncio
async def test_authoritative_redis_miss_purges_stale_memory_mirror() -> None:
    primary = RecoverableStore()
    store = ResilientTranscriptStore(primary, MemoryTranscriptStore())
    record = ResponseTranscript(
        response_id="resp_deleted_elsewhere",
        response=_response("resp_deleted_elsewhere", []),
        input_items=[],
        transcript=[],
        created_at=1.0,
    )
    await store.save("tenant", record)

    assert await primary.delete("tenant", "resp_deleted_elsewhere") is True
    assert await store.load("tenant", "resp_deleted_elsewhere") is None

    primary.available = False
    assert await store.load("tenant", "resp_deleted_elsewhere") is None


class FakeRedis:
    def __init__(self) -> None:
        self.values: Dict[str, str] = {}
        self.set_calls: List[Dict[str, Any]] = []

    async def set(self, key: str, value: str, *, ex: int) -> None:
        self.values[key] = value
        self.set_calls.append({"key": key, "value": value, "ex": ex})

    async def get(self, key: str) -> Any:
        return self.values.get(key)

    async def delete(self, key: str) -> int:
        return int(self.values.pop(key, None) is not None)


@pytest.mark.asyncio
async def test_redis_keys_are_credential_scoped_and_never_contain_response_ids() -> None:
    redis = FakeRedis()
    store = RedisTranscriptStore("redis://unused", ttl_seconds=91, client=redis)
    record = ResponseTranscript(
        response_id="resp_secret_name",
        response=_response("resp_secret_name", []),
        input_items=[],
        transcript=[],
        created_at=1.0,
    )

    await store.save("fingerprint-a", record)
    key = redis.set_calls[0]["key"]
    assert "fingerprint-a" in key
    assert "resp_secret_name" not in key
    assert redis.set_calls[0]["ex"] == 91
    assert (await store.load("fingerprint-a", "resp_secret_name")).response_id == "resp_secret_name"
    assert await store.load("fingerprint-b", "resp_secret_name") is None


def test_memory_fallback_is_thread_safe_across_independent_event_loops() -> None:
    store = MemoryTranscriptStore(max_entries=64)

    def write(index: int) -> None:
        record = ResponseTranscript(
            response_id=f"resp_{index}",
            response=_response(f"resp_{index}", []),
            input_items=[],
            transcript=[],
            created_at=float(index),
        )
        asyncio.run(store.save("tenant", record))

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write, range(32)))

    async def verify() -> None:
        records = await asyncio.gather(*(store.load("tenant", f"resp_{index}") for index in range(32)))
        assert {record.response_id for record in records if record is not None} == {
            f"resp_{index}" for index in range(32)
        }

    asyncio.run(verify())


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://api.deepseek.com", "https://api.deepseek.com/v1/responses"),
        ("https://api.deepseek.com/v1", "https://api.deepseek.com/v1/responses"),
        ("https://api.deepseek.com/v1/responses/", "https://api.deepseek.com/v1/responses"),
    ],
)
def test_responses_url_normalisation(base_url: str, expected: str) -> None:
    service = DeepSeekResponsesService(base_url=base_url, storage=MemoryTranscriptStore())
    assert service.responses_url == expected


@pytest.mark.parametrize("authorization", [None, "", "Basic abc", "Bearer", "Bearer key with spaces"])
@pytest.mark.asyncio
async def test_invalid_bearer_credentials_are_rejected_before_http(
    authorization: Any,
) -> None:
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=_response("resp", []))

    service = DeepSeekResponsesService(
        storage=MemoryTranscriptStore(),
        client_factory=_client_factory(handler),
    )
    with pytest.raises(ResponsesGatewayError) as exc:
        await service.create({"model": "m", "input": "x"}, authorization)
    assert exc.value.status_code == 401
    assert called is False


@pytest.mark.asyncio
async def test_invalid_cursor_and_page_options_return_protocol_errors() -> None:
    storage = MemoryTranscriptStore()
    record = ResponseTranscript(
        response_id="resp_page",
        response=_response("resp_page", []),
        input_items=[{"id": "msg_1", "type": "message"}],
        transcript=[],
        created_at=1.0,
    )
    await storage.save(_fingerprint("key"), record)
    service = DeepSeekResponsesService(storage=storage)

    for kwargs in ({"after": "missing"}, {"limit": 0}, {"limit": 101}, {"order": "sideways"}):
        with pytest.raises(ResponsesGatewayError) as exc:
            await service.list_input_items("resp_page", "Bearer key", **kwargs)
        assert exc.value.status_code == 400
