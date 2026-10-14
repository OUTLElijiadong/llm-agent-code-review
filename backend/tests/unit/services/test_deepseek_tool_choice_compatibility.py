"""受约束的 DeepSeek Responses 请求只局部关闭思考，不放宽工具选择。"""

import copy
import json

import httpx
import pytest

from app.services import agent_responses_service as native
from app.services.deepseek_responses_runtime import (
    DeepSeekResponsesRuntime,
    InMemoryCheckpointStore,
    ToolExecutionResult,
    _tool_choice_for_round,
)
from app.services.deepseek_responses_service import DeepSeekResponsesService, MemoryTranscriptStore
from app.utils.api_resolver import ApiConfig

CHOICES = ["none", "required", {"type": "function", "name": "read_forum"}]


@pytest.mark.parametrize("model", ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-flash", "gpt-example"])
def test_completion_guard_required_constraint_is_not_weakened_for_thinking_models(model):
    assert _tool_choice_for_round(model, 1) == "required"


def test_latest_deepseek_flash_alias_uses_default_autonomous_selection():
    assert _tool_choice_for_round("deepseek-flash", 0) is None


class EventStream(httpx.AsyncByteStream):
    def __init__(self, content):
        self.content = content

    async def __aiter__(self):
        yield self.content


async def send(monkeypatch, kind, payload, handler, base_url="https://api.deepseek.com/v1"):
    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def factory(*args, **kwargs):
        return original_client(*args, transport=transport, **kwargs)

    if kind == "native":
        monkeypatch.setattr(native.httpx, "AsyncClient", factory)
        client = native.NativeResponsesTransport(
            ApiConfig(api_key="unit-test-key", base_url=base_url, model=payload["model"]),
        )
        return [event async for event in await client.create_response(payload)]
    gateway = DeepSeekResponsesService(
        base_url=base_url, storage=MemoryTranscriptStore(), client_factory=factory,
    )
    response = await gateway.create(payload, "Bearer unit-test-key")
    assert response.status_code == 200
    if hasattr(response, "body"):
        return b"".join([chunk async for chunk in response.body])
    return response.content


def provider_response(payload):
    # 生产上游实际报错；工具未执行，不能用取消约束的自动重试掩盖。
    if "tool_choice" in payload and payload.get("reasoning", {}).get("effort") != "none":
        return httpx.Response(400, json={"error": {"message": "Thinking mode does not support this tool_choice"}})
    event = {"type": "response.completed", "response": {"id": "compat-response", "status": "completed", "output": []}}
    return httpx.Response(200, stream=EventStream(f"event: response.completed\ndata: {json.dumps(event)}\n\n".encode()),
                          headers={"Content-Type": "text/event-stream"})


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["native", "gateway"])
@pytest.mark.parametrize("model", ["deepseek-v4-flash", "deepseek-flash"])
@pytest.mark.parametrize("reasoning", [None, {"effort": "high", "summary": "auto"}])
@pytest.mark.parametrize("choice", CHOICES)
async def test_constrained_request_disables_thinking_locally_and_preserves_tool_choice(
    monkeypatch, kind, model, reasoning, choice,
):
    payload = {"model": model, "input": "读取论坛", "stream": True, "store": False,
               "tool_choice": choice, "tools": [{"type": "function", "name": "read_forum"}]}
    if reasoning is not None:
        payload["reasoning"] = reasoning
    original = copy.deepcopy(payload)
    seen = []

    def handler(request):
        outgoing = json.loads(request.content)
        seen.append(outgoing)
        return provider_response(outgoing)

    await send(monkeypatch, kind, payload, handler)
    assert len(seen) == 1, "兼容调整必须在发送前完成，不因400重复模型调用"
    assert seen[0]["tool_choice"] == choice
    assert seen[0]["reasoning"]["effort"] == "none"
    if reasoning:
        assert seen[0]["reasoning"]["summary"] == reasoning["summary"]
    assert seen[0]["tools"] == payload["tools"]
    assert payload == original, "不得改变调用方的模型配置或后续轮次请求"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["native", "gateway"])
@pytest.mark.parametrize("model", ["deepseek-v4-flash", "deepseek-flash"])
async def test_autonomous_request_omits_redundant_auto_and_preserves_thinking(monkeypatch, kind, model):
    payload = {"model": model, "input": "规划任务", "stream": True, "store": False,
               "reasoning": {"effort": "high"}, "tool_choice": "auto",
               "tools": [{"type": "function", "name": "read_forum"}]}
    seen = []

    def handler(request):
        outgoing = json.loads(request.content)
        seen.append(outgoing)
        return provider_response(outgoing)

    await send(monkeypatch, kind, payload, handler)
    assert "tool_choice" not in seen[0]
    assert seen[0]["reasoning"] == {"effort": "high"}
    assert payload["tool_choice"] == "auto"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["native", "gateway"])
@pytest.mark.parametrize("choice", CHOICES)
async def test_other_provider_constraints_and_reasoning_are_unchanged(monkeypatch, kind, choice):
    payload = {"model": "deepseek-flash", "input": "读取论坛", "stream": True, "store": False,
               "reasoning": {"effort": "high"}, "tool_choice": choice,
               "tools": [{"type": "function", "name": "read_forum"}]}
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return provider_response({"reasoning": {"effort": "none"}})

    await send(monkeypatch, kind, payload, handler, base_url="https://other-provider.example/v1")
    assert seen[0] == payload


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["deepseek-v4-flash", "deepseek-flash"])
async def test_runtime_guard_and_native_transport_keep_required_then_restore_autonomous_thinking(monkeypatch, model):
    """模拟上游限制的三轮调用，不用放宽监督来让HTTP请求变绿。"""
    seen = []

    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        if "tool_choice" in payload and payload.get("reasoning", {}).get("effort") != "none":
            return provider_response(payload)
        if len(seen) == 2:
            output = [{"type": "function_call", "call_id": "forum-call", "name": "read_forum", "arguments": "{}"}]
        else:
            output = [{"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "已查询论坛"},
            ]}]
        event = {"type": "response.completed", "response": {
            "id": f"compat-response-{len(seen)}", "status": "completed", "output": output,
        }}
        return httpx.Response(200, stream=EventStream(f"data: {json.dumps(event)}\n\n".encode()),
                              headers={"Content-Type": "text/event-stream"})

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(native.httpx, "AsyncClient", lambda **kwargs: original_client(transport=transport, **kwargs))

    class Executor:
        def __init__(self):
            self.calls = []

        async def execute(self, call, *, approved=False):
            self.calls.append(call)
            return ToolExecutionResult.success({"titles": ["版本更新"], "read_only": True})

    executor = Executor()

    def guard(checkpoint, _text):
        has_output = any(item.get("type") == "function_call_output" for item in checkpoint.transcript)
        return None if has_output else "缺少读取证据"

    runtime = DeepSeekResponsesRuntime(
        transport=native.NativeResponsesTransport(ApiConfig(
            api_key="unit-test-key", base_url="https://api.deepseek.com/v1", model=model,
        )), tool_executor=executor, checkpoint_store=InMemoryCheckpointStore(), model=model,
        completion_guard=guard,
    )
    result = await runtime.start("只读论坛", tools=[{"type": "function", "name": "read_forum"}])
    assert result.status == "completed", result.error
    assert len(executor.calls) == 1 and executor.calls[0].name == "read_forum"
    assert [payload.get("tool_choice") for payload in seen] == [None, "required", None]
    assert seen[1]["reasoning"] == {"effort": "none"}
    assert "reasoning" not in seen[0] and "reasoning" not in seen[2]
    assert all(payload["model"] == model for payload in seen)
