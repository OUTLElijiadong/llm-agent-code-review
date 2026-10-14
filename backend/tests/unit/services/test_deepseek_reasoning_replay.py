"""Responses真实事件形状：内部推理完整回放，不变成用户可见output_text。"""
from __future__ import annotations

import copy

import pytest

from app.services.deepseek_responses_runtime import (
    COMPLETED,
    FAILED,
    DeepSeekResponsesRuntime,
    IncompleteResponseStreamError,
    InMemoryCheckpointStore,
    _collect_response,
    _response_from_events,
)
from tests.unit.services.test_deepseek_responses_runtime import RecordingExecutor, ScriptedTransport


def _reasoning(text=""):
    return {"type": "reasoning", "id": "reasoning-1", "content": [
        {"type": "reasoning_text", "text": text},
    ]}


def _message(text="可见回答"):
    return {"type": "message", "id": "message-1", "role": "assistant",
            "content": [{"type": "output_text", "text": text}]}


def _events(source="delta", terminal_output=True):
    events = [{"type": "response.output_item.added", "output_index": 0,
               "item": {"type": "reasoning", "id": "reasoning-1"}}]
    locator = {"output_index": 0, "item_id": "reasoning-1", "content_index": 0}
    if source == "delta":
        events.extend({"type": "response.reasoning_text.delta", **locator, "delta": value}
                      for value in ("内部", "推理"))
    elif source == "done":
        events.extend([
            {"type": "response.reasoning_text.delta", **locator, "delta": "半截"},
            {"type": "response.reasoning_text.done", **locator, "text": "内部推理"},
        ])
    elif source == "content_part":
        events.extend([
            {"type": "response.content_part.added", **locator,
             "part": {"type": "reasoning_text", "text": ""}},
            {"type": "response.content_part.done", **locator,
             "part": {"type": "reasoning_text", "text": "内部推理"}},
        ])
    events.append({"type": "response.output_item.done", "output_index": 0,
                   "item": _reasoning("内部推理") if source == "output_item" else {
                       "type": "reasoning", "id": "reasoning-1", "content": [],
                   }})
    events.append({"type": "response.output_text.delta", "output_index": 1, "delta": "可见回答"})
    response = {"id": "response-1", "status": "completed", "usage": {"output_tokens": 14}}
    if terminal_output:
        response["output"] = [_reasoning("内部推理") if source == "terminal" else
                              {"type": "reasoning", "id": "reasoning-1", "content": []}, _message()]
    events.append({"type": "response.completed", "response": response})
    return events


@pytest.mark.parametrize("source,terminal_output", [
    (source, terminal_output)
    for source in ("delta", "done", "content_part", "output_item", "terminal")
    for terminal_output in (True, False)
    if source != "terminal" or terminal_output
])
def test_reasoning_content_is_reconstructed_from_each_real_sse_shape(source, terminal_output):
    events = _events(source, terminal_output)
    before = copy.deepcopy(events)
    response = _response_from_events(events)
    assert response["output"][0]["content"] == [{"type": "reasoning_text", "text": "内部推理"}]
    assert response["output"][1]["type"] == "message"
    assert response["output"][1]["content"] == _message()["content"]
    assert response["usage"] == {"output_tokens": 14}
    assert events == before


def test_terminal_full_reasoning_content_is_authoritative_not_duplicated():
    events = _events("done")
    events[-1]["response"]["output"][0] = _reasoning("终帧完整原文")
    assert _response_from_events(events)["output"][0] == _reasoning("终帧完整原文")


def test_multiple_reasoning_parts_use_content_index_and_done_not_duplicate_deltas():
    events = _events("done")
    events.insert(-2, {"type": "response.reasoning_text.delta", "output_index": 0,
                      "content_index": 1, "item_id": "reasoning-1", "delta": "第二部分"})
    response = _response_from_events(events)
    assert response["output"][0]["content"] == [
        {"type": "reasoning_text", "text": "内部推理"},
        {"type": "reasoning_text", "text": "第二部分"},
    ]


def test_item_id_only_fragments_restore_missing_terminal_reasoning_without_duplicate_items():
    events = _events("delta")
    for event in events:
        if event.get("type") == "response.reasoning_text.delta":
            event.pop("output_index")
    events[-1]["response"]["output"] = [_message()]
    response = _response_from_events(events)
    assert [item["type"] for item in response["output"]] == ["reasoning", "message"]
    assert response["output"][0] == _reasoning("内部推理")


def test_done_full_text_prevents_repeated_late_delta_and_long_reasoning_is_not_truncated():
    text = "完整内部原文🙂" * 1000
    events = _events("done")
    done = next(event for event in events if event["type"] == "response.reasoning_text.done")
    done["text"] = text
    events.insert(-2, {"type": "response.reasoning_text.delta", "output_index": 0,
                      "content_index": 0, "item_id": "reasoning-1", "delta": "不应重复"})
    assert _response_from_events(events)["output"][0] == _reasoning(text)


@pytest.mark.asyncio
async def test_completed_no_tool_turn_reasoning_is_replayed_in_later_tool_turn():
    transport = ScriptedTransport([_events("done"), _events("delta"), _events("content_part")])
    executor, store = RecordingExecutor(), InMemoryCheckpointStore()
    runtime = DeepSeekResponsesRuntime(transport=transport, tool_executor=executor, checkpoint_store=store)
    first = await runtime.start("先只读观察页面", run_id="reasoning-before-tool")
    assert first.status == COMPLETED and first.output_text == "可见回答"
    prior = await store.load("reasoning-before-tool")
    second_events = transport.responses[0]
    call = {"type": "function_call", "id": "fc-1", "call_id": "call-1", "name": "read_forum",
            "arguments": "{}"}
    second_events[-1]["response"]["output"][1] = call
    second = await runtime.start([*prior.transcript, {"role": "user", "content": "再读最新三帖"}],
                                 tools=[{"type": "function", "name": "read_forum", "parameters": {}}],
                                 run_id="reasoning-tool-followup")
    assert second.status == COMPLETED and second.output_text == "可见回答"
    assert len(executor.calls) == 1
    reasoning = [item for item in transport.payloads[-1]["input"] if item.get("type") == "reasoning"]
    assert len(reasoning) == 2
    assert all(item["content"] == [{"type": "reasoning_text", "text": "内部推理"}] for item in reasoning)
    assert [item["content"] for item in transport.payloads[-1]["input"] if item.get("role") == "user"] == [
        "先只读观察页面", "再读最新三帖",
    ]
    assert any(item.get("type") == "function_call_output" for item in transport.payloads[-1]["input"])
    assert "内部推理" not in second.output_text


@pytest.mark.asyncio
async def test_incomplete_stream_preserves_reasoning_in_error_only_never_executes_partial_call():
    async def stream():
        for event in _events("done")[:-1]:
            yield event
        yield {"type": "response.output_item.done", "output_index": 1,
               "item": {"type": "function_call", "call_id": "partial", "name": "write", "arguments": "{}"}}
    executor = RecordingExecutor()
    runtime = DeepSeekResponsesRuntime(transport=ScriptedTransport([stream]), tool_executor=executor,
                                       checkpoint_store=InMemoryCheckpointStore())
    response = await runtime.start("不得执行半截调用", run_id="reasoning-stream-partial")
    assert response.status == FAILED and executor.calls == []
    with pytest.raises(IncompleteResponseStreamError) as error:
        await _collect_response(stream())
    assert error.value.observed_response["output"][0]["content"] == [
        {"type": "reasoning_text", "text": "内部推理"},
    ]
