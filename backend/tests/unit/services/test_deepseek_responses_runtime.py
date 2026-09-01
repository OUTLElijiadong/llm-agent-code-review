"""DeepSeek Responses 无状态 Agent 工具循环内核测试。"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Dict, List, Mapping, Sequence

import pytest

from app.services.deepseek_responses_runtime import (
    COMPLETED,
    FAILED,
    MAX_ROUNDS_EXCEEDED,
    WAITING_APPROVAL,
    WAITING_INPUT,
    DeepSeekResponsesRuntime,
    InMemoryCheckpointStore,
    InvalidRunStateError,
    RunCheckpoint,
    ToolCall,
    ToolExecutionResult,
    estimate_tokens,
)


def _function_response(*calls: tuple[str, str, Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "id": "resp_tools",
        "object": "response",
        "status": "completed",
        "output": [
            {
                "type": "function_call",
                "id": f"item_{call_id}",
                "call_id": call_id,
                "name": name,
                "arguments": json.dumps(arguments, ensure_ascii=False),
            }
            for call_id, name, arguments in calls
        ],
    }


def _message_response(text: str) -> Dict[str, Any]:
    return {
        "id": "resp_final",
        "object": "response",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
    }


class ScriptedTransport:
    def __init__(self, responses: Sequence[Any]) -> None:
        self.responses = list(responses)
        self.payloads: List[Mapping[str, Any]] = []

    async def create_response(self, payload: Mapping[str, Any]) -> Any:
        self.payloads.append(payload)
        response = self.responses.pop(0)
        return response() if callable(response) else response


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: List[tuple[ToolCall, bool]] = []

    async def execute(self, call: ToolCall, *, approved: bool = False) -> ToolExecutionResult:
        self.calls.append((call, approved))
        return ToolExecutionResult.success({"tool": call.name, "args": call.arguments})


class CrossRuntimeCancelExecutor(RecordingExecutor):
    """在第一个工具完成后让另一个 Runtime 请求取消，验证持久状态优先。"""

    def __init__(self, store: InMemoryCheckpointStore) -> None:
        super().__init__()
        self.store = store
        self.cancel_runtime: DeepSeekResponsesRuntime | None = None

    async def execute(self, call: ToolCall, *, approved: bool = False) -> ToolExecutionResult:
        self.calls.append((call, approved))
        if len(self.calls) == 1:
            assert self.cancel_runtime is not None
            await self.cancel_runtime.cancel("run-cross-runtime-cancel", reason="人工停止")
        return ToolExecutionResult.success({"tool": call.name})


def _runtime(
    transport: ScriptedTransport,
    executor: Any,
    *,
    max_rounds: int = 12,
    **runtime_options: Any,
) -> DeepSeekResponsesRuntime:
    return DeepSeekResponsesRuntime(
        transport=transport,
        tool_executor=executor,
        checkpoint_store=InMemoryCheckpointStore(),
        max_rounds=max_rounds,
        **runtime_options,
    )


@pytest.mark.asyncio
async def test_replays_two_tool_rounds_without_previous_response_id() -> None:
    transport = ScriptedTransport(
        [
            _function_response(("call_1", "lookup", {"key": "a"})),
            _function_response(("call_2", "calculate", {"value": 2})),
            _message_response("任务完成"),
        ]
    )
    executor = RecordingExecutor()

    result = await _runtime(transport, executor).start(
        "处理任务",
        tools=[{"type": "function", "name": "lookup", "parameters": {"type": "object"}}],
        run_id="run_two_rounds",
    )

    assert result.status == "completed"
    assert result.output_text == "任务完成"
    assert result.rounds == 3
    assert [call.name for call, _ in executor.calls] == ["lookup", "calculate"]
    assert all("tool_choice" not in payload for payload in transport.payloads)
    assert all("previous_response_id" not in payload for payload in transport.payloads)
    second_input = transport.payloads[1]["input"]
    assert second_input[-2]["call_id"] == "call_1"
    assert second_input[-1]["type"] == "function_call_output"
    third_input = transport.payloads[2]["input"]
    assert [item.get("call_id") for item in third_input if item.get("type") == "function_call_output"] == [
        "call_1",
        "call_2",
    ]
    assert all(payload["max_output_tokens"] == 32768 for payload in transport.payloads)


@pytest.mark.asyncio
async def test_compacts_model_projection_but_preserves_full_audit_transcript() -> None:
    transcript: List[Dict[str, Any]] = [{"role": "user", "content": "审查项目 42，保留所有证据"}]
    transcript.extend(
        {"role": "assistant" if index % 2 else "user", "content": f"历史-{index}-" + "x" * 180}
        for index in range(48)
    )
    transcript.extend(
        [
            {
                "type": "function_call",
                "call_id": "call_recent",
                "name": "lookup",
                "arguments": '{"project_id":42}',
            },
            {
                "type": "function_call_output",
                "call_id": "call_recent",
                "output": '{"status":"success","project_id":42}',
            },
        ]
    )
    store = InMemoryCheckpointStore()
    transport = ScriptedTransport([_message_response("投影执行完成")])
    runtime = DeepSeekResponsesRuntime(
        transport=transport,
        tool_executor=RecordingExecutor(),
        checkpoint_store=store,
        context_window_tokens=4_000,
        max_output_tokens=400,
        compaction_threshold_tokens=900,
        keep_recent_tokens=500,
    )

    result = await runtime.start(transcript, run_id="run_compaction")

    assert result.status == "completed"
    payload = transport.payloads[0]
    assert payload["max_output_tokens"] == 400
    assert payload["input"][0]["role"] == "system"
    assert "平台上下文压缩" in payload["input"][0]["content"][0]["text"]
    recent_items = [item for item in payload["input"] if item.get("call_id") == "call_recent"]
    assert [item["type"] for item in recent_items] == ["function_call", "function_call_output"]
    checkpoint = await runtime.get_checkpoint("run_compaction")
    assert checkpoint.transcript[: len(transcript)] == transcript
    assert checkpoint.context_metadata["compacted"] is True
    assert checkpoint.context_metadata["omitted_items"] > 0
    assert len(checkpoint.context_metadata["summary_sha256"]) == 64
    assert estimate_tokens(payload["input"]) <= checkpoint.context_metadata["transcript_budget_tokens"]


@pytest.mark.asyncio
async def test_fails_closed_when_required_context_cannot_fit_budget() -> None:
    transport = ScriptedTransport([_message_response("不应调用")])
    runtime = _runtime(
        transport,
        RecordingExecutor(),
        context_window_tokens=1_000,
        max_output_tokens=200,
        compaction_threshold_tokens=500,
        keep_recent_tokens=300,
    )

    result = await runtime.start("x" * 20_000, run_id="run_context_overflow")

    assert result.status == FAILED
    assert "上下文超出安全预算" in result.error
    assert transport.payloads == []


def test_checkpoint_round_trip_preserves_context_metadata() -> None:
    checkpoint = RunCheckpoint(
        run_id="run_metadata",
        model="deepseek-v4-flash",
        transcript=[{"role": "user", "content": "hello"}],
        tools=[],
        context_metadata={"compacted": True, "compaction_count": 2},
    )

    restored = RunCheckpoint.from_dict(checkpoint.to_dict())

    assert restored.context_metadata == {"compacted": True, "compaction_count": 2}


@pytest.mark.asyncio
async def test_executes_multiple_function_calls_before_next_model_turn() -> None:
    transport = ScriptedTransport(
        [
            _function_response(
                ("call_a", "first", {"n": 1}),
                ("call_b", "second", {"n": 2}),
            ),
            _message_response("两个工具均完成"),
        ]
    )
    executor = RecordingExecutor()

    result = await _runtime(transport, executor).start("并行意图", run_id="run_multi")

    assert result.output_text == "两个工具均完成"
    assert [call.call_id for call, _ in executor.calls] == ["call_a", "call_b"]
    outputs = [
        item for item in transport.payloads[1]["input"] if item.get("type") == "function_call_output"
    ]
    assert [item["call_id"] for item in outputs] == ["call_a", "call_b"]


@pytest.mark.asyncio
async def test_approval_executes_exact_call_and_resumes_without_new_user_message() -> None:
    class ApprovalExecutor(RecordingExecutor):
        async def execute(self, call: ToolCall, *, approved: bool = False) -> ToolExecutionResult:
            self.calls.append((call, approved))
            if not approved:
                return ToolExecutionResult.approval_required(
                    operation="删除项目",
                    impact="项目 9 将被删除",
                    danger=True,
                    approval_id=88,
                )
            return ToolExecutionResult.success({"deleted": call.arguments["project_id"]})

    transport = ScriptedTransport(
        [
            _function_response(("call_delete", "delete_project", {"project_id": 9})),
            _message_response("项目已删除"),
        ]
    )
    executor = ApprovalExecutor()
    runtime = _runtime(transport, executor)

    paused = await runtime.start("删除项目", run_id="run_approval")

    assert paused.status == WAITING_APPROVAL
    assert paused.pending == {
        "kind": "approval",
        "tool_call_id": "call_delete",
        "name": "delete_project",
        "arguments": {"project_id": 9},
        "operation": "删除项目",
        "impact": "项目 9 将被删除",
        "danger": True,
        "approval_id": 88,
    }
    assert paused.events[-1]["type"] == "response.approval.required"

    completed = await runtime.approve(
        "run_approval",
        "call_delete",
        confirmation="",
    )

    assert completed.status == "completed"
    assert completed.output_text == "项目已删除"
    assert [(call.call_id, approved) for call, approved in executor.calls] == [
        ("call_delete", False),
        ("call_delete", True),
    ]
    resumed_input = transport.payloads[1]["input"]
    assert sum(1 for item in resumed_input if item.get("role") == "user") == 1
    assert resumed_input[-1]["call_id"] == "call_delete"


@pytest.mark.asyncio
async def test_approval_resume_preserves_remaining_calls_from_same_response() -> None:
    class MiddleApprovalExecutor(RecordingExecutor):
        async def execute(self, call: ToolCall, *, approved: bool = False) -> ToolExecutionResult:
            self.calls.append((call, approved))
            if call.name == "second" and not approved:
                return ToolExecutionResult.approval_required(
                    operation="第二步写操作",
                    impact="改变测试资源",
                    danger=True,
                )
            return ToolExecutionResult.success(call.name)

    transport = ScriptedTransport(
        [
            _function_response(
                ("call_1", "first", {}),
                ("call_2", "second", {}),
                ("call_3", "third", {}),
            ),
            _message_response("顺序完成"),
        ]
    )
    executor = MiddleApprovalExecutor()
    runtime = _runtime(transport, executor)

    paused = await runtime.start("依次执行", run_id="run_middle_approval")
    completed = await runtime.approve(
        "run_middle_approval",
        "call_2",
    )

    assert paused.status == WAITING_APPROVAL
    assert completed.output_text == "顺序完成"
    assert [(call.call_id, approved) for call, approved in executor.calls] == [
        ("call_1", False),
        ("call_2", False),
        ("call_2", True),
        ("call_3", False),
    ]
    outputs = [
        item for item in transport.payloads[1]["input"] if item.get("type") == "function_call_output"
    ]
    assert [item["call_id"] for item in outputs] == ["call_1", "call_2", "call_3"]


@pytest.mark.asyncio
async def test_rejection_is_tool_output_and_model_continues() -> None:
    class ApprovalExecutor(RecordingExecutor):
        def __init__(self) -> None:
            super().__init__()
            self.rejections: List[tuple[str, str]] = []

        async def execute(self, call: ToolCall, *, approved: bool = False) -> ToolExecutionResult:
            self.calls.append((call, approved))
            return ToolExecutionResult.approval_required(
                operation="写入配置",
                impact="改变运行配置",
                danger=True,
            )

        async def reject(self, call: ToolCall, *, reason: str) -> None:
            self.rejections.append((call.call_id, reason))

    transport = ScriptedTransport(
        [
            _function_response(("call_write", "write_config", {"enabled": True})),
            _message_response("已取消写入，配置保持不变"),
        ]
    )
    executor = ApprovalExecutor()
    runtime = _runtime(transport, executor)

    await runtime.start("修改配置", run_id="run_reject")
    completed = await runtime.reject("run_reject", "call_write", reason="本次不执行")

    assert completed.output_text == "已取消写入，配置保持不变"
    assert len(executor.calls) == 1
    assert executor.rejections == [("call_write", "本次不执行")]
    resumed_input = transport.payloads[1]["input"]
    assert sum(1 for item in resumed_input if item.get("role") == "user") == 1
    rejection = json.loads(resumed_input[-1]["output"])
    assert resumed_input[-1]["call_id"] == "call_write"
    assert rejection == {"status": "rejected", "error": "本次不执行"}


@pytest.mark.asyncio
async def test_model_generated_question_resumes_as_tool_output() -> None:
    transport = ScriptedTransport(
        [
            _function_response(
                ("call_question", "ask_user", {"question": "目标项目 ID 是多少？", "context": "决定审查范围"})
            ),
            _message_response("已开始审查项目 42"),
        ]
    )
    executor = RecordingExecutor()
    runtime = _runtime(transport, executor)

    paused = await runtime.start("开始审查", run_id="run_question")

    assert paused.status == WAITING_INPUT
    assert paused.pending["arguments"]["question"] == "目标项目 ID 是多少？"
    assert executor.calls == []

    completed = await runtime.answer("run_question", "42", "call_question")

    assert completed.output_text == "已开始审查项目 42"
    resumed_input = transport.payloads[1]["input"]
    assert sum(1 for item in resumed_input if item.get("role") == "user") == 1
    answer_item = resumed_input[-1]
    assert answer_item["type"] == "function_call_output"
    assert json.loads(answer_item["output"])["answer"] == "42"


@pytest.mark.asyncio
async def test_model_generated_options_are_exposed_and_restrict_answer() -> None:
    options = [
        {"label": "按用户 ID", "value": "user_ids", "description": "把 26-69 解释为数据库用户 ID"},
        {"label": "按列表序号", "value": "list_positions", "description": "先按当前列表顺序解析目标"},
    ]
    transport = ScriptedTransport(
        [
            _function_response(
                (
                    "call_options",
                    "ask_user",
                    {
                        "question": "你说的 26-69 是用户 ID 还是列表序号？",
                        "context": "两种解释对应不同账号",
                        "options": options,
                        "allow_free_text": False,
                    },
                )
            ),
            _message_response("已按用户 ID 继续"),
        ]
    )
    runtime = _runtime(transport, RecordingExecutor())

    paused = await runtime.start("删除序号26-69的用户", run_id="run_dynamic_options")

    assert paused.events[-1]["options"] == options
    assert paused.events[-1]["allow_free_text"] is False
    ask_schema = next(tool for tool in transport.payloads[0]["tools"] if tool["name"] == "ask_user")
    assert set(ask_schema["parameters"]["required"]) == {
        "question",
        "context",
        "options",
        "allow_free_text",
    }
    with pytest.raises(InvalidRunStateError, match="仅接受"):
        await runtime.answer("run_dynamic_options", "随便执行", "call_options")

    completed = await runtime.answer("run_dynamic_options", "user_ids", "call_options")
    assert completed.status == "completed"
    assert completed.output_text == "已按用户 ID 继续"


@pytest.mark.asyncio
async def test_empty_stream_deltas_do_not_create_blank_final_output() -> None:
    async def streamed_events() -> AsyncIterator[Mapping[str, Any]]:
        yield {"type": "response.output_text.delta", "delta": ""}
        yield {"type": "response.output_text.delta", "delta": "执行"}
        yield {"type": "response.output_text.delta", "delta": ""}
        yield {"type": "response.output_text.delta", "delta": "完成"}
        yield {"type": "response.completed"}

    transport = ScriptedTransport([streamed_events])

    result = await _runtime(transport, RecordingExecutor()).start("执行", run_id="run_stream")

    assert result.status == "completed"
    assert result.output_text == "执行完成"
    assert result.output_text.splitlines() == ["执行完成"]


@pytest.mark.asyncio
async def test_completion_guard_discards_unverified_text_and_forces_tool_retry() -> None:
    transport = ScriptedTransport(
        [
            _message_response("已删除模板，deleted_count=1"),
            _function_response(("call_delete", "delete_template", {"id": 4})),
            _message_response("模板删除成功"),
        ]
    )
    executor = RecordingExecutor()

    def guard(checkpoint: RunCheckpoint, output_text: str) -> str | None:
        has_success = any(item.get("type") == "function_call_output" for item in checkpoint.transcript)
        if "删除" in output_text and not has_success:
            return "缺少真实写工具证据"
        return None

    result = await _runtime(transport, executor, completion_guard=guard, model="deepseek-v4-flash").start(
        "删除模板 4",
        tools=[{"type": "function", "name": "delete_template", "parameters": {"type": "object"}}],
        run_id="run_completion_guard_retry",
    )

    assert result.status == "completed"
    assert result.output_text == "模板删除成功"
    assert [payload.get("tool_choice") for payload in transport.payloads] == [
        None,
        None,
        None,
    ]
    projected_text = json.dumps(transport.payloads[-1]["input"], ensure_ascii=False)
    assert "已删除模板，deleted_count=1" not in projected_text
    assert "runtime_completion_guard" in projected_text
    assert [call.name for call, _approved in executor.calls] == ["delete_template"]


@pytest.mark.asyncio
async def test_completion_guard_retry_uses_auto_for_deepseek_thinking_models() -> None:
    transport = ScriptedTransport(
        [
            _message_response("已删除模板，deleted_count=1"),
            _function_response(("call_delete", "delete_template", {"id": 4})),
            _message_response("模板删除成功"),
        ]
    )
    executor = RecordingExecutor()

    def guard(checkpoint: RunCheckpoint, output_text: str) -> str | None:
        has_success = any(item.get("type") == "function_call_output" for item in checkpoint.transcript)
        if "删除" in output_text and not has_success:
            return "缺少真实写工具证据"
        return None

    result = await _runtime(
        transport,
        executor,
        completion_guard=guard,
        model="deepseek-v4-flash",
    ).start(
        "删除模板 4",
        tools=[{"type": "function", "name": "delete_template", "parameters": {"type": "object"}}],
        run_id="run_completion_guard_retry_v4",
    )

    assert result.status == "completed"
    assert [payload.get("tool_choice") for payload in transport.payloads] == [
        None,
        None,
        None,
    ]
    projected_text = json.dumps(transport.payloads[1]["input"], ensure_ascii=False)
    assert "runtime_completion_guard" in projected_text
    assert [call.name for call, _approved in executor.calls] == ["delete_template"]


@pytest.mark.asyncio
async def test_completion_guard_drops_same_round_text_when_tool_call_exists() -> None:
    mixed_response = _function_response(("call_delete", "delete_template", {"id": 4}))
    mixed_response["output"].insert(
        0,
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "已删除模板"}],
        },
    )
    transport = ScriptedTransport([mixed_response, _message_response("模板删除成功")])

    result = await _runtime(
        transport,
        RecordingExecutor(),
        completion_guard=lambda _checkpoint, _text: None,
    ).start("delete", run_id="run_completion_guard_mixed")

    assert result.status == "completed"
    final_input = json.dumps(transport.payloads[-1]["input"], ensure_ascii=False)
    assert "已删除模板" not in final_input
    assert "call_delete" in final_input


@pytest.mark.asyncio
async def test_completion_guard_fails_after_bounded_retries_without_persisting_claim() -> None:
    transport = ScriptedTransport(
        [
            _message_response("删除成功"),
            _message_response("删除成功"),
            _message_response("删除成功"),
        ]
    )

    result = await _runtime(
        transport,
        RecordingExecutor(),
        completion_guard=lambda _checkpoint, _text: "没有执行证据",
        model="deepseek-v4-flash",
    ).start("delete", run_id="run_completion_guard_fail")

    assert result.status == FAILED
    assert result.output_text == ""
    assert "执行证据校验失败" in result.error
    assert [payload.get("tool_choice") for payload in transport.payloads] == [
        None,
        None,
        None,
    ]


@pytest.mark.asyncio
async def test_stops_at_maximum_model_rounds() -> None:
    transport = ScriptedTransport(
        [
            _function_response(("call_1", "again", {})),
            _function_response(("call_2", "again", {})),
        ]
    )

    result = await _runtime(transport, RecordingExecutor(), max_rounds=2).start(
        "循环",
        run_id="run_limit",
    )

    assert result.status == MAX_ROUNDS_EXCEEDED
    assert result.rounds == 2
    assert "最大轮数 2" in result.error
    assert len(transport.payloads) == 2


@pytest.mark.asyncio
async def test_long_sandbox_polling_completes_with_bounded_extended_budget() -> None:
    responses = [
        _function_response(
            (
                f"call_poll_{index}",
                "user_execute_capability",
                {
                    "capability": "sandboxes.get",
                    "params": {"public_id": "sbx_long_task"},
                },
            )
        )
        for index in range(21)
    ]
    responses.append(_message_response("沙箱已完成，报告可查看"))
    transport = ScriptedTransport(responses)
    executor = RecordingExecutor()

    result = await _runtime(transport, executor, max_rounds=64).start(
        "等待完整沙箱验证终态",
        run_id="run_long_sandbox_polling",
    )

    assert result.status == COMPLETED
    assert result.rounds == 22
    assert result.output_text == "沙箱已完成，报告可查看"
    assert len(executor.calls) == 21
    final_input = transport.payloads[-1]["input"]
    assert sum(item.get("type") == "function_call" for item in final_input) == 21
    assert sum(item.get("type") == "function_call_output" for item in final_input) == 21


@pytest.mark.asyncio
async def test_tool_exception_is_returned_to_model_and_loop_continues() -> None:
    class BrokenExecutor:
        async def execute(self, call: ToolCall, *, approved: bool = False) -> ToolExecutionResult:
            raise RuntimeError("下游不可用")

    transport = ScriptedTransport(
        [
            _function_response(("call_broken", "broken_tool", {"x": 1})),
            _message_response("工具失败，已给出替代方案"),
        ]
    )

    result = await _runtime(transport, BrokenExecutor()).start("处理失败", run_id="run_error")

    assert result.status == "completed"
    assert result.output_text == "工具失败，已给出替代方案"
    tool_output = transport.payloads[1]["input"][-1]
    assert tool_output["type"] == "function_call_output"
    parsed = json.loads(tool_output["output"])
    assert parsed["status"] == "error"
    assert "下游不可用" in parsed["error"]


@pytest.mark.asyncio
async def test_failed_model_response_never_executes_contained_function_call() -> None:
    failed_response = _function_response(("call_unsafe", "side_effect", {"value": 1}))
    failed_response["status"] = "failed"
    failed_response["error"] = {"message": "上游失败"}
    transport = ScriptedTransport([failed_response])
    executor = RecordingExecutor()

    result = await _runtime(transport, executor).start("执行", run_id="run_failed_response")

    assert result.status == FAILED
    assert executor.calls == []
    assert len(transport.payloads) == 1
    assert "上游失败" in result.error


@pytest.mark.asyncio
async def test_retry_recovers_completed_tool_call_interrupted_before_output_persisted() -> None:
    call_response = _function_response(
        ("call_interrupted", "save_knowledge_note", {"title": "登录数据查询方法"})
    )
    store = InMemoryCheckpointStore()
    checkpoint = RunCheckpoint(
        run_id="run_interrupted_tool",
        model="deepseek-v4-flash",
        transcript=[
            {"role": "user", "content": "记录查询方法"},
            *call_response["output"],
        ],
        tools=[],
        status=FAILED,
        rounds=12,
        last_response=call_response,
        error=(
            "Responses transport 调用失败: Responses 上游 HTTP 400: "
            "No tool output found for tool call call_interrupted."
        ),
    )
    await store.create(checkpoint)
    transport = ScriptedTransport([_message_response("知识已记录")])
    executor = RecordingExecutor()
    runtime = DeepSeekResponsesRuntime(
        transport=transport,
        tool_executor=executor,
        checkpoint_store=store,
    )

    result = await runtime.retry(checkpoint.run_id)

    assert result.status == "completed"
    assert result.output_text == "知识已记录"
    assert [call.call_id for call, _ in executor.calls] == ["call_interrupted"]
    replay = transport.payloads[0]["input"]
    assert [
        item.get("type") for item in replay if item.get("call_id") == "call_interrupted"
    ] == ["function_call", "function_call_output"]


@pytest.mark.asyncio
async def test_retry_does_not_recover_unpaired_call_from_failed_response() -> None:
    failed_response = _function_response(("call_unsafe_retry", "side_effect", {"value": 1}))
    failed_response["status"] = "failed"
    failed_response["error"] = {"message": "上游失败"}
    store = InMemoryCheckpointStore()
    checkpoint = RunCheckpoint(
        run_id="run_failed_unpaired",
        model="deepseek-v4-flash",
        transcript=[{"role": "user", "content": "执行"}, *failed_response["output"]],
        tools=[],
        status=FAILED,
        rounds=1,
        last_response=failed_response,
        error="上游失败",
    )
    await store.create(checkpoint)
    transport = ScriptedTransport([_message_response("已安全重试")])
    executor = RecordingExecutor()
    runtime = DeepSeekResponsesRuntime(
        transport=transport,
        tool_executor=executor,
        checkpoint_store=store,
    )

    result = await runtime.retry(checkpoint.run_id)

    assert result.status == "completed"
    assert executor.calls == []
    assert not any(
        item.get("call_id") == "call_unsafe_retry"
        for item in transport.payloads[0]["input"]
    )


@pytest.mark.asyncio
async def test_shared_store_allows_only_one_concurrent_retry_owner() -> None:
    class SlowTransport(ScriptedTransport):
        async def create_response(self, payload: Mapping[str, Any]) -> Any:
            await asyncio.sleep(0.02)
            return await super().create_response(payload)

    store = InMemoryCheckpointStore()
    checkpoint = RunCheckpoint(
        run_id="run_concurrent_retry",
        model="deepseek-v4-flash",
        transcript=[{"role": "user", "content": "重试一次"}],
        tools=[],
        status=FAILED,
        rounds=1,
        error="上游中断",
    )
    await store.create(checkpoint)
    transport_a = SlowTransport([_message_response("唯一恢复者完成")])
    transport_b = SlowTransport([_message_response("不应执行")])
    runtime_a = DeepSeekResponsesRuntime(
        transport=transport_a,
        tool_executor=RecordingExecutor(),
        checkpoint_store=store,
    )
    runtime_b = DeepSeekResponsesRuntime(
        transport=transport_b,
        tool_executor=RecordingExecutor(),
        checkpoint_store=store,
    )

    outcomes = await asyncio.gather(
        runtime_a.retry(checkpoint.run_id),
        runtime_b.retry(checkpoint.run_id),
        return_exceptions=True,
    )

    completed = [item for item in outcomes if not isinstance(item, BaseException)]
    rejected = [item for item in outcomes if isinstance(item, InvalidRunStateError)]
    assert len(completed) == 1
    assert completed[0].output_text == "唯一恢复者完成"
    assert len(rejected) == 1
    assert len(transport_a.payloads) + len(transport_b.payloads) == 1


@pytest.mark.asyncio
async def test_retry_recovers_dangerous_interrupted_call_without_bypassing_approval() -> None:
    class ApprovalExecutor:
        def __init__(self) -> None:
            self.calls: list[tuple[str, bool]] = []

        async def execute(self, call: ToolCall, *, approved: bool = False) -> ToolExecutionResult:
            self.calls.append((call.call_id, approved))
            if not approved:
                return ToolExecutionResult.approval_required(
                    operation="删除测试数据",
                    impact="测试数据将被删除",
                    danger=True,
                )
            return ToolExecutionResult.success({"deleted": True})

    call_response = _function_response(("call_dangerous", "delete_test_data", {"id": 7}))
    store = InMemoryCheckpointStore()
    await store.create(RunCheckpoint(
        run_id="run_interrupted_dangerous",
        model="deepseek-v4-flash",
        transcript=[{"role": "user", "content": "删除测试数据"}, *call_response["output"]],
        tools=[],
        status=FAILED,
        rounds=1,
        last_response=call_response,
        error="工具输出尚未持久化",
    ))
    executor = ApprovalExecutor()
    runtime = DeepSeekResponsesRuntime(
        transport=ScriptedTransport([]),
        tool_executor=executor,
        checkpoint_store=store,
    )

    result = await runtime.retry("run_interrupted_dangerous")

    assert result.status == WAITING_APPROVAL
    assert result.pending is not None
    assert result.pending["tool_call_id"] == "call_dangerous"
    assert executor.calls == [("call_dangerous", False)]


@pytest.mark.asyncio
async def test_retry_recovers_only_missing_calls_from_completed_multi_call_response() -> None:
    call_response = _function_response(
        ("call_done", "read_status", {"id": 1}),
        ("call_missing", "read_status", {"id": 2}),
    )
    store = InMemoryCheckpointStore()
    await store.create(RunCheckpoint(
        run_id="run_partial_multi_call",
        model="deepseek-v4-flash",
        transcript=[
            {"role": "user", "content": "读取两个状态"},
            *call_response["output"],
            {
                "type": "function_call_output",
                "call_id": "call_done",
                "output": json.dumps({"status": "success", "output": {"id": 1}}),
            },
        ],
        tools=[],
        status=FAILED,
        rounds=1,
        last_response=call_response,
        error="第二个调用输出尚未持久化",
    ))
    executor = RecordingExecutor()
    runtime = DeepSeekResponsesRuntime(
        transport=ScriptedTransport([_message_response("两个状态均已读取")]),
        tool_executor=executor,
        checkpoint_store=store,
    )

    result = await runtime.retry("run_partial_multi_call")

    assert result.status == COMPLETED
    assert [call.call_id for call, _ in executor.calls] == ["call_missing"]


@pytest.mark.asyncio
async def test_truncated_stream_never_executes_partial_function_call() -> None:
    async def truncated_events() -> AsyncIterator[Mapping[str, Any]]:
        yield {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "type": "function_call",
                "call_id": "call_partial",
                "name": "side_effect",
                "arguments": "{}",
            },
        }

    transport = ScriptedTransport([truncated_events])
    executor = RecordingExecutor()

    result = await _runtime(transport, executor).start("执行", run_id="run_truncated")

    assert result.status == FAILED
    assert executor.calls == []
    assert "终止帧" in result.error


@pytest.mark.asyncio
async def test_shared_store_allows_only_one_concurrent_approval_execution() -> None:
    class SlowApprovalExecutor(RecordingExecutor):
        async def execute(self, call: ToolCall, *, approved: bool = False) -> ToolExecutionResult:
            self.calls.append((call, approved))
            if not approved:
                return ToolExecutionResult.approval_required(
                    operation="写操作",
                    impact="改变资源",
                    danger=True,
                )
            await asyncio.sleep(0.02)
            return ToolExecutionResult.success({"done": True})

    store = InMemoryCheckpointStore()
    transport = ScriptedTransport(
        [
            _function_response(("call_once", "write_once", {})),
            _message_response("只执行一次"),
        ]
    )
    executor = SlowApprovalExecutor()
    runtime_a = DeepSeekResponsesRuntime(
        transport=transport,
        tool_executor=executor,
        checkpoint_store=store,
    )
    runtime_b = DeepSeekResponsesRuntime(
        transport=transport,
        tool_executor=executor,
        checkpoint_store=store,
    )
    await runtime_a.start("执行一次", run_id="run_concurrent_approval")

    outcomes = await asyncio.gather(
        runtime_a.approve("run_concurrent_approval", "call_once"),
        runtime_b.approve("run_concurrent_approval", "call_once"),
        return_exceptions=True,
    )

    completed = [item for item in outcomes if not isinstance(item, BaseException)]
    rejected = [item for item in outcomes if isinstance(item, InvalidRunStateError)]
    assert len(completed) == 1
    assert completed[0].output_text == "只执行一次"
    assert len(rejected) == 1
    assert sum(1 for _, approved in executor.calls if approved) == 1


@pytest.mark.asyncio
async def test_concurrent_approve_and_reject_persists_only_winning_decision() -> None:
    class RacingExecutor(RecordingExecutor):
        def __init__(self) -> None:
            super().__init__()
            self.rejections: List[str] = []

        async def execute(self, call: ToolCall, *, approved: bool = False) -> ToolExecutionResult:
            self.calls.append((call, approved))
            if not approved:
                return ToolExecutionResult.approval_required(
                    operation="写操作",
                    impact="改变资源",
                    danger=True,
                )
            await asyncio.sleep(0.02)
            return ToolExecutionResult.success({"done": True})

        async def reject(self, call: ToolCall, *, reason: str) -> None:
            self.rejections.append(call.call_id)

    store = InMemoryCheckpointStore()
    transport = ScriptedTransport(
        [
            _function_response(("call_race", "write_once", {})),
            _message_response("决定已处理"),
        ]
    )
    executor = RacingExecutor()
    runtime_a = DeepSeekResponsesRuntime(
        transport=transport,
        tool_executor=executor,
        checkpoint_store=store,
    )
    runtime_b = DeepSeekResponsesRuntime(
        transport=transport,
        tool_executor=executor,
        checkpoint_store=store,
    )
    await runtime_a.start("执行一次", run_id="run_approve_reject_race")

    outcomes = await asyncio.gather(
        runtime_a.approve("run_approve_reject_race", "call_race"),
        runtime_b.reject("run_approve_reject_race", "call_race"),
        return_exceptions=True,
    )

    assert sum(not isinstance(item, BaseException) for item in outcomes) == 1
    assert sum(isinstance(item, InvalidRunStateError) for item in outcomes) == 1
    approved_count = sum(1 for _, approved in executor.calls if approved)
    assert (approved_count, len(executor.rejections)) in {(1, 0), (0, 1)}


class BlockingTransport:
    """create_response 会阻塞直到取消信号到来，用于验证运行中取消。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.calls = 0

    async def create_response(self, payload: Mapping[str, Any]) -> Any:
        self.calls += 1
        self.started.set()
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_cancel_marks_running_checkpoint_cancelled_and_stops_loop() -> None:
    transport = BlockingTransport()
    runtime = DeepSeekResponsesRuntime(
        transport=transport,
        tool_executor=RecordingExecutor(),
        checkpoint_store=InMemoryCheckpointStore(),
        model="deepseek-v4-pro",
        max_rounds=12,
        stream=False,
    )
    task = asyncio.create_task(runtime.start(
        [{"role": "user", "content": "开始"}],
        instructions="",
        tools=[],
        run_id="run-cancel-1",
    ))
    await asyncio.wait_for(transport.started.wait(), timeout=5)

    result = await runtime.cancel("run-cancel-1")
    assert result.status == "cancelled"

    checkpoint = await runtime.get_checkpoint("run-cancel-1")
    assert checkpoint.status == "cancelled"
    assert checkpoint.pending is None

    # 取消后 drive 会在下一轮边界收敛;阻塞 transport 的 task 不会挂起测试。
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_cancel_is_idempotent_after_completion() -> None:
    runtime = DeepSeekResponsesRuntime(
        transport=ScriptedTransport([_message_response("完成")]),
        tool_executor=RecordingExecutor(),
        checkpoint_store=InMemoryCheckpointStore(),
        model="deepseek-v4-pro",
        max_rounds=12,
        stream=False,
    )
    result = await runtime.start([{"role": "user", "content": "开始"}], run_id="run-cancel-2")
    assert result.status == COMPLETED

    cancelled = await runtime.cancel("run-cancel-2")
    assert cancelled.status == COMPLETED
    checkpoint = await runtime.get_checkpoint("run-cancel-2")
    assert checkpoint.status == COMPLETED


@pytest.mark.asyncio
async def test_cancel_persists_reason_in_checkpoint_and_output() -> None:
    transport = BlockingTransport()
    runtime = DeepSeekResponsesRuntime(
        transport=transport,
        tool_executor=RecordingExecutor(),
        checkpoint_store=InMemoryCheckpointStore(),
        model="deepseek-v4-pro",
        max_rounds=12,
        stream=False,
    )
    task = asyncio.create_task(runtime.start(
        [{"role": "user", "content": "开始"}],
        instructions="",
        tools=[],
        run_id="run-cancel-reason",
    ))
    await asyncio.wait_for(transport.started.wait(), timeout=5)

    result = await runtime.cancel("run-cancel-reason", reason="需求变更，暂停分析")
    assert result.status == "cancelled"

    checkpoint = await runtime.get_checkpoint("run-cancel-reason")
    assert checkpoint.status == "cancelled"
    assert checkpoint.cancel_reason == "需求变更，暂停分析"
    assert "需求变更，暂停分析" in checkpoint.output_text

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_cancel_without_reason_keeps_default_message() -> None:
    transport = BlockingTransport()
    runtime = DeepSeekResponsesRuntime(
        transport=transport,
        tool_executor=RecordingExecutor(),
        checkpoint_store=InMemoryCheckpointStore(),
        model="deepseek-v4-pro",
        max_rounds=12,
        stream=False,
    )
    task = asyncio.create_task(runtime.start(
        [{"role": "user", "content": "开始"}],
        instructions="",
        tools=[],
        run_id="run-cancel-plain",
    ))
    await asyncio.wait_for(transport.started.wait(), timeout=5)

    result = await runtime.cancel("run-cancel-plain")
    assert result.status == "cancelled"

    checkpoint = await runtime.get_checkpoint("run-cancel-plain")
    assert checkpoint.cancel_reason == ""
    assert checkpoint.output_text == "用户取消了本次运行。已停止任务，未执行剩余操作；如需继续可重新发起。"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


class GatedApprovalExecutor:
    """先阻塞在工具执行中,释放后才返回审批请求,用于复现取消竞态。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: List[tuple[ToolCall, bool]] = []

    async def execute(self, call: ToolCall, *, approved: bool = False) -> ToolExecutionResult:
        self.calls.append((call, approved))
        self.started.set()
        await self.release.wait()
        return ToolExecutionResult.approval_required(
            operation="update_project",
            impact="修改项目描述",
            danger=True,
            approval_id=1,
        )


@pytest.mark.asyncio
async def test_cancel_wins_over_approval_pause_write_back() -> None:
    transport = ScriptedTransport(
        [_function_response(("call_gate", "update_project", {"project_id": 1}))]
    )
    executor = GatedApprovalExecutor()
    runtime = DeepSeekResponsesRuntime(
        transport=transport,
        tool_executor=executor,
        checkpoint_store=InMemoryCheckpointStore(),
        model="deepseek-v4-pro",
        max_rounds=12,
        stream=False,
    )
    task = asyncio.create_task(runtime.start(
        [{"role": "user", "content": "修改项目"}],
        instructions="",
        tools=[],
        run_id="run-cancel-race",
    ))
    await asyncio.wait_for(executor.started.wait(), timeout=5)

    # 工具执行尚未返回审批时,取消请求先到;释放后驱动循环会尝试落盘 waiting_approval。
    result = await runtime.cancel("run-cancel-race", reason="用户要求立即停止")
    assert result.status == "cancelled"

    # 放行工具执行:驱动循环会尝试把状态回写为 waiting_approval,取消必须最终获胜。
    executor.release.set()
    drive_result = await asyncio.wait_for(task, timeout=5)
    assert drive_result.status == "cancelled"

    checkpoint = await runtime.get_checkpoint("run-cancel-race")
    assert checkpoint.status == "cancelled"
    assert checkpoint.pending is None
    assert checkpoint.cancel_reason == "用户要求立即停止"
    assert "已停止任务" in checkpoint.output_text


@pytest.mark.asyncio
async def test_cross_runtime_cancel_blocks_remaining_tool_calls() -> None:
    """另一个 HTTP 请求取消后，旧驱动不得继续执行同一响应中的后续工具。"""
    store = InMemoryCheckpointStore()
    transport = ScriptedTransport([
        _function_response(
            ("call_first", "read_status", {}),
            ("call_second", "write_side_effect", {}),
        ),
        _message_response("不应到达"),
    ])
    executor = CrossRuntimeCancelExecutor(store)
    driver = DeepSeekResponsesRuntime(
        transport=transport,
        tool_executor=executor,
        checkpoint_store=store,
        stream=False,
    )
    canceller = DeepSeekResponsesRuntime(
        transport=ScriptedTransport([]),
        tool_executor=RecordingExecutor(),
        checkpoint_store=store,
        stream=False,
    )
    executor.cancel_runtime = canceller

    result = await driver.start("执行", run_id="run-cross-runtime-cancel")

    assert result.status == "cancelled"
    assert [call.call_id for call, _ in executor.calls] == ["call_first"]
    checkpoint = await canceller.get_checkpoint("run-cross-runtime-cancel")
    assert checkpoint.status == "cancelled"


def test_checkpoint_round_trips_cancel_reason() -> None:
    checkpoint = RunCheckpoint(run_id="run-1", model="m", transcript=[], tools=[])
    checkpoint.cancel_reason = "用户要求停止"
    restored = RunCheckpoint.from_dict(checkpoint.to_dict())
    assert restored.cancel_reason == "用户要求停止"
