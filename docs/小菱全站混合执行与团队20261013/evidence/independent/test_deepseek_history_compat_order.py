"""独立请求顺序样本：本地MockTransport，不调用外部provider或生产。"""
import copy
import hashlib
import json

import httpx
import pytest

from app.services.deepseek_provider_compat import prepare_deepseek_responses_payload
from app.services.deepseek_responses_runtime import _response_from_events
from app.services.deepseek_responses_service import (
    DeepSeekResponsesService,
    MemoryTranscriptStore,
    ResponseTranscript,
)


TOOLS = [{"type": "function", "name": "read", "parameters": {}}]


def _reasoning(text="真实合成协议状态"):
    return {"type": "reasoning", "content": [{"type": "reasoning_text", "text": text}]}


def _call(ident):
    return {"type": "function_call", "call_id": ident, "name": "read", "arguments": "{}"}


def _output(ident):
    return {"type": "function_call_output", "call_id": ident, "output": "ok"}


def _response(ident, items):
    return {"id": ident, "object": "response", "status": "completed", "output": items}


@pytest.mark.parametrize("choice", [None, "auto", "required", "none", {"type": "function", "name": "read"}])
@pytest.mark.parametrize("scenario,history,missing", [
    ("new_user", [{"role": "user", "content": "读取"}], False),
    ("parallel", [_reasoning(), _call("a"), _call("b"), _output("b"), _output("a")], False),
    ("multiple_turns", [_reasoning(), _call("a"), _output("a"), _reasoning(), _call("b"), _output("b")], False),
    ("later_missing", [_reasoning(), _call("a"), _output("a"), _call("b"), _output("b")], True),
    ("no_tool_assistant", [_reasoning(), {"role": "assistant", "content": "回答"},
                           {"role": "user", "content": "下一轮"}], False),
    ("old_no_tool_assistant", [{"role": "assistant", "content": "回答"},
                               {"role": "user", "content": "下一轮"}], True),
    ("system_boundary", [_reasoning(), {"role": "system", "content": "监督纠错"}, _call("b"), _output("b")], True),
    ("encrypted_not_reasoning", [{"type": "reasoning", "encrypted_content": "not-original"}, _call("a")], True),
])
def test_history_and_tools_stay_exact_under_request_only_compat(choice, scenario, history, missing):
    payload = {"model": "deepseek-flash", "input": history, "tools": TOOLS,
               "reasoning": {"effort": "high", "summary": "auto"}}
    if choice is not None:
        payload["tool_choice"] = choice
    original = copy.deepcopy(payload)
    outgoing = prepare_deepseek_responses_payload(payload, "https://api.deepseek.com/v1/responses")
    constrained = choice is not None and choice != "auto"
    assert outgoing["reasoning"]["effort"] == ("none" if constrained or missing else "high"), scenario
    assert outgoing["input"] == history and outgoing["tools"] == TOOLS
    assert payload == original
    if constrained:
        assert outgoing["tool_choice"] == choice
    for host in ("https://other-provider.example/v1/responses", "https://api.deepseek.com.other.example/responses"):
        assert prepare_deepseek_responses_payload(payload, host) == original


@pytest.mark.asyncio
async def test_gateway_restored_history_is_checked_after_previous_response_replay():
    storage = MemoryTranscriptStore()
    history = [{"role": "user", "content": "旧请求"}, {"role": "assistant", "content": "非思考回答"}]
    token = "independent-mock-only"
    await storage.save(hashlib.sha256(token.encode()).hexdigest(), ResponseTranscript(
        response_id="previous-no-reasoning", response=_response("previous-no-reasoning", history[1:]),
        input_items=history[:1], transcript=history, created_at=1.0,
    ))
    seen = []
    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        if payload.get("reasoning", {}).get("effort") != "none":
            return httpx.Response(400, json={"error": {"message": "mock: reasoning_text must be passed back"}})
        return httpx.Response(200, json=_response("continued", []))
    service = DeepSeekResponsesService(base_url="https://api.deepseek.com/v1", storage=storage,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    latest = {"role": "user", "content": "继续旧请求"}
    response = await service.create({"model": "deepseek-flash", "input": [latest], "tools": TOOLS,
        "reasoning": {"effort": "high"}, "previous_response_id": "previous-no-reasoning"}, f"Bearer {token}")
    assert seen[0]["input"] == [*history, latest]
    assert response.status_code == 200, (response.status_code, seen[0].get("reasoning"))
    assert seen[0]["reasoning"]["effort"] == "none"


@pytest.mark.asyncio
@pytest.mark.parametrize("final", ["tool_pair", "no_tool_answer"])
async def test_gateway_checks_final_real_compaction_projection(monkeypatch, final):
    from app.services.deepseek_responses_service import settings
    monkeypatch.setattr(settings, "deepseek_context_window_tokens", 6000)
    monkeypatch.setattr(settings, "deepseek_compaction_threshold_tokens", 500)
    monkeypatch.setattr(settings, "deepseek_compaction_keep_recent_tokens", 350)
    history = [{"role": "user", "content": "任务"}, _reasoning("x" * 900)]
    history += [_call("a"), _output("a")] if final == "tool_pair" else [
        {"type": "message", "role": "assistant", "content": "可见回答"},
        {"role": "user", "content": "继续"},
    ]
    seen = []
    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        assert not any(item.get("type") == "reasoning" for item in payload["input"])
        if payload.get("reasoning", {}).get("effort") != "none":
            return httpx.Response(400, json={"error": {"message": "mock: reasoning_text must be passed back"}})
        return httpx.Response(200, json=_response("projected", []))
    service = DeepSeekResponsesService(base_url="https://api.deepseek.com/v1", storage=MemoryTranscriptStore(),
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    async def semantic_stub(*args, **kwargs):
        return "来源摘要"
    monkeypatch.setattr(service, "_semantic_compact_input", semantic_stub)
    # 只替代模型摘要；仍执行真实_project_input与compact_transcript，定位适配顺序。
    response = await service.create({"model": "deepseek-flash", "input": history, "tools": TOOLS,
        "reasoning": {"effort": "high"}, "max_output_tokens": 500}, "Bearer independent-mock-only")
    assert response.status_code == 200, (response.status_code, seen[0].get("reasoning"))
    assert seen[0]["reasoning"]["effort"] == "none"


def test_collector_recovers_real_reasoning_so_valid_history_keeps_thinking():
    response = _response_from_events([
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"id": "reason-1", "type": "reasoning"}},
        {"type": "response.reasoning_text.delta", "output_index": 0, "item_id": "reason-1",
         "content_index": 0, "delta": "合成原文"},
        {"type": "response.completed", "response": _response("complete", [
            {"id": "reason-1", "type": "reasoning", "content": []}, _call("a"),
        ])},
    ])
    history = [*response["output"], _output("a")]
    original = copy.deepcopy(history)
    outgoing = prepare_deepseek_responses_payload({"model": "deepseek-flash", "input": history,
        "tools": TOOLS, "reasoning": {"effort": "high"}}, "https://api.deepseek.com/v1/responses")
    assert outgoing["reasoning"] == {"effort": "high"}
    assert outgoing["input"] == original


class _EventBytes(httpx.AsyncByteStream):
    def __init__(self, response):
        self.data = f"data: {json.dumps({'type': 'response.completed', 'response': response})}\n\n".encode()

    async def __aiter__(self):
        yield self.data


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("choice", [None, "auto", "required", "none", {"type": "function", "name": "read"}])
@pytest.mark.parametrize("scenario", ["restored_missing", "restored_complete", "compacted_tool", "compacted_answer"])
async def test_fixed_gateway_order_extra_protocol_matrix(monkeypatch, stream, choice, scenario):
    from app.services.deepseek_responses_service import settings
    storage, seen = MemoryTranscriptStore(), []
    token = "independent-mock-only"
    fingerprint = hashlib.sha256(token.encode()).hexdigest()
    history = [{"role": "user", "content": "原始目标"}]
    if scenario != "restored_missing":
        history.append(_reasoning("x" * 900))
    history += [_call("a"), _output("a")] if scenario == "compacted_tool" else [
        {"type": "message", "role": "assistant", "content": "旧回答"},
    ]
    if scenario.startswith("compacted"):
        monkeypatch.setattr(settings, "deepseek_context_window_tokens", 6000)
        monkeypatch.setattr(settings, "deepseek_compaction_threshold_tokens", 500)
        monkeypatch.setattr(settings, "deepseek_compaction_keep_recent_tokens", 350)
    await storage.save(fingerprint, ResponseTranscript(
        response_id="before", response=_response("before", []), input_items=history,
        transcript=history, created_at=1.0,
    ))
    old = copy.deepcopy(await storage.load(fingerprint, "before"))
    constrained = choice is not None and choice != "auto"
    expected_effort = "none" if constrained or scenario != "restored_complete" else "high"
    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        assert payload["reasoning"]["effort"] == expected_effort
        if constrained:
            assert payload["tool_choice"] == choice
        assert payload["tools"] == TOOLS and "previous_response_id" not in payload
        response = _response("after", [])
        return (httpx.Response(200, stream=_EventBytes(response), headers={"Content-Type": "text/event-stream"})
                if stream else httpx.Response(200, json=response))
    service = DeepSeekResponsesService(base_url="https://api.deepseek.com/v1", storage=storage,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    async def semantic_stub(*args, **kwargs):
        return "来源摘要"
    monkeypatch.setattr(service, "_semantic_compact_input", semantic_stub)
    latest = {"role": "user", "content": "继续"}
    payload = {"model": "deepseek-flash", "input": [latest], "previous_response_id": "before",
               "tools": TOOLS, "reasoning": {"effort": "high"}, "stream": stream, "max_output_tokens": 500}
    if choice is not None:
        payload["tool_choice"] = choice
    original = copy.deepcopy(payload)
    response = await service.create(payload, f"Bearer {token}")
    if stream:
        async for _ in response.body:
            pass
    assert response.status_code == 200 and len(seen) == 1
    assert payload == original and await storage.load(fingerprint, "before") == old
    assert (await storage.load(fingerprint, "after")).transcript == [*history, latest]
