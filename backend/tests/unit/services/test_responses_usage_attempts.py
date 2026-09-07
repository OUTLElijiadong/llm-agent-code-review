"""Responses 在失败、取消、模型回退与审计故障时的实际用量记录。"""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app.services.agent_responses_service import NativeResponsesTransport
from app.services.deepseek_responses_runtime import DeepSeekResponsesRuntime, InMemoryCheckpointStore, ObservedResponseError
from app.utils.api_resolver import ApiConfig


def _runtime(transport, on_round):
    return DeepSeekResponsesRuntime(transport=transport, tool_executor=SimpleNamespace(), checkpoint_store=InMemoryCheckpointStore(),
                                    model="requested-model", fallback_model="fallback-model", on_round=on_round)


def _final(**fields):
    return {"id": "response-final", "status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "完成"}]}], **fields}


@pytest.mark.asyncio
async def test_native_non_200_usage_reaches_failed_round(monkeypatch):
    from app.services import agent_responses_service as service

    requests, logs = [], []
    original = httpx.AsyncClient
    def handler(request):
        requests.append(request)
        return httpx.Response(400, json={"error": "invalid prompt", "model": "actual-model", "usage": {"input_tokens": 0, "output_tokens": 5, "total_tokens": 5}})
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    transport = NativeResponsesTransport(ApiConfig(api_key="unit", base_url="https://unit.example/v1", model="unit"))
    result = await _runtime(transport, logs.append).start("unit")
    assert result.status == "failed" and len(requests) == len(logs) == 1
    assert logs[0]["model"] == "actual-model" and logs[0]["usage"]["total_tokens"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["disconnect", "cancel", "no_terminal"])
async def test_observed_usage_survives_stream_failure_and_cancellation(ending):
    logs = []
    async def stream():
        yield {"type": "response.created", "response": {"model": "actual-model", "usage": {"total_tokens": 8}}}
        if ending == "disconnect":
            raise httpx.ReadError("stream disconnected")
        if ending == "cancel":
            raise asyncio.CancelledError()
    runtime = _runtime(lambda _payload: stream(), logs.append)
    if ending == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await runtime.start("unit")
    else:
        result = await runtime.start("unit")
        assert result.status == "failed"
    assert len(logs) == 1 and logs[0]["status"] == "failed"
    assert logs[0]["usage"]["total_tokens"] == 8 and logs[0]["model"] == "actual-model"


@pytest.mark.asyncio
async def test_terminal_without_output_keeps_usage_and_delta_text():
    logs = []
    async def stream():
        yield {"type": "response.output_text.delta", "delta": "完成"}
        yield {"type": "response.completed", "response": {"id": "final", "status": "completed", "model": "actual-model", "usage": {"total_tokens": 7}}}
    result = await _runtime(lambda _payload: stream(), logs.append).start("unit")
    assert result.status == "completed" and result.output_text == "完成"
    assert logs[0]["usage"]["total_tokens"] == 7


@pytest.mark.asyncio
async def test_failed_provider_and_successful_model_fallback_have_separate_round_logs():
    payloads, logs = [], []
    async def transport(payload):
        payloads.append(dict(payload))
        if len(payloads) == 1:
            raise ObservedResponseError("model not found", {"model": "requested-model", "usage": {"total_tokens": 2}})
        return _final(model="fallback-model", usage={"total_tokens": 5})
    result = await _runtime(transport, logs.append).start("unit")
    assert result.status == "completed"
    assert [log["usage"]["total_tokens"] for log in logs] == [2, 5]
    assert [log["model"] for log in logs] == ["requested-model", "fallback-model"]
    assert [log["status"] for log in logs] == ["failed", "completed"]


@pytest.mark.asyncio
async def test_audit_failure_cannot_be_mistaken_for_model_unavailability():
    payloads = []
    async def transport(payload):
        payloads.append(dict(payload))
        return _final(model="requested-model", usage={"total_tokens": 3})
    def fail_accounting(_response):
        raise RuntimeError("deepseek-v4-pro audit INSERT failed")
    result = await _runtime(transport, fail_accounting).start("unit")
    assert result.status == "failed" and len(payloads) == 1
    assert "用量记账失败" in result.error


@pytest.mark.asyncio
async def test_native_sink_cancel_after_terminal_read_keeps_usage(monkeypatch):
    import json
    from app.services import agent_responses_service as service

    logs = []
    original = httpx.AsyncClient
    terminal = {"type": "response.completed", "response": _final(usage={"total_tokens": 11})}
    def handler(_request):
        return httpx.Response(200, text=f"data: {json.dumps(terminal)}\n\n", headers={"content-type": "text/event-stream"})
    async def cancel_sink(_event):
        raise asyncio.CancelledError()
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    transport = NativeResponsesTransport(ApiConfig(api_key="unit", base_url="https://unit.example/v1", model="unit"), cancel_sink)
    with pytest.raises(asyncio.CancelledError):
        await _runtime(transport, logs.append).start("unit")
    assert len(logs) == 1 and logs[0]["usage"]["total_tokens"] == 11
