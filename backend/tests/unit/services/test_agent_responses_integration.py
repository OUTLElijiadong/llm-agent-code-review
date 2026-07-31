"""平台 Responses 持久化、审批与 SSE 适配测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Mapping

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.orm import sessionmaker

from app.api.v1 import agent_responses as api_module
from app.models.admin_chat import AdminChatMessage, OpsExecution
from app.models.agent_governance import ApprovalItem
from app.models.agent_response_run import AgentResponseRun, AgentToolExecution
from app.services import agent_responses_service as service_module
from app.services.agent_responses_service import DatabaseCheckpointStore, PrismToolExecutor
from app.services.deepseek_responses_runtime import (
    InvalidRunStateError,
    RunCheckpoint,
    RuntimeResult,
    ToolCall,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    AgentResponseRun.__table__.create(engine)
    AgentToolExecution.__table__.create(engine)
    ApprovalItem.__table__.create(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


@pytest.mark.asyncio
async def test_database_checkpoint_store_is_owner_and_session_isolated(db) -> None:
    owner = DatabaseCheckpointStore(db, user_id=7, surface="user", session_key="session-01")
    other = DatabaseCheckpointStore(db, user_id=8, surface="user", session_key="session-01")
    checkpoint = RunCheckpoint(
        run_id="run_owner",
        model="deepseek-v4-flash",
        transcript=[{"role": "user", "content": "hello"}],
        tools=[],
    )

    await owner.save(checkpoint)
    assert (await owner.load("run_owner")).transcript[-1]["content"] == "hello"
    assert await other.load("run_owner") is None

    with pytest.raises(InvalidRunStateError):
        await other.save(checkpoint)


@pytest.mark.asyncio
async def test_database_checkpoint_store_blocks_new_run_while_session_is_pending(db) -> None:
    store = DatabaseCheckpointStore(db, user_id=7, surface="admin", session_key="session-single-active")
    first = RunCheckpoint(
        run_id="run_first_pending",
        model="deepseek-v4-flash",
        transcript=[{"role": "user", "content": "处理审批"}],
        tools=[],
        status="waiting_approval",
    )
    second = RunCheckpoint(
        run_id="run_second_pending",
        model="deepseek-v4-flash",
        transcript=[{"role": "user", "content": "开始新任务"}],
        tools=[],
    )

    assert await store.create(first) is True
    assert await store.create(second) is False
    assert db.query(AgentResponseRun).filter_by(run_id="run_second_pending").first() is None


@pytest.mark.asyncio
async def test_database_checkpoint_store_preserves_payload_larger_than_mysql_text(db) -> None:
    store = DatabaseCheckpointStore(db, user_id=7, surface="admin", session_key="session-large")
    large_tool_schema = {
        "type": "function",
        "name": "large_admin_tool",
        "description": "x" * 70_000,
        "parameters": {"type": "object", "properties": {}},
    }
    checkpoint = RunCheckpoint(
        run_id="run_large_checkpoint",
        model="deepseek-v4-flash",
        transcript=[{"role": "user", "content": "查询平台状态"}],
        tools=[large_tool_schema],
    )

    await store.save(checkpoint)
    loaded = await store.load(checkpoint.run_id)

    assert loaded is not None
    assert loaded.tools[0]["description"] == large_tool_schema["description"]


def test_responses_payload_columns_compile_for_each_database_dialect() -> None:
    columns = (
        AgentResponseRun.__table__.c.checkpoint_json,
        AgentToolExecution.__table__.c.arguments_json,
        AgentToolExecution.__table__.c.result_json,
        ApprovalItem.__table__.c.request_json,
        AdminChatMessage.__table__.c.content,
        AdminChatMessage.__table__.c.payload_json,
        OpsExecution.__table__.c.params_json,
        OpsExecution.__table__.c.result_json,
    )

    assert [column.type.compile(dialect=mysql.dialect()) for column in columns] == ["LONGTEXT"] * 8
    assert [column.type.compile(dialect=sqlite.dialect()) for column in columns] == ["TEXT"] * 8


def test_session_recovery_returns_only_visible_transcript_and_pending_action(db) -> None:
    checkpoint = {
        "model": "deepseek-v4-flash",
        "status": "waiting_approval",
        "rounds": 2,
        "transcript": [
            {"role": "user", "content": "删除测试用户"},
            {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "内部推理"}]},
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "已找到两个候选用户"}],
            },
            {"type": "function_call", "name": "admin_delete_users", "arguments": "{}"},
        ],
        "pending": {
            "kind": "approval",
            "call": {
                "call_id": "call_batch",
                "name": "admin_delete_users",
                "arguments": {
                    "user_ids": [101, 102],
                    "api_key": "must-not-leak",
                },
            },
            "remaining_calls": [],
            "operation": "批量删除 2 个用户",
            "impact": "账号将被软删除",
            "danger": True,
            "approval_id": 19,
            "preview": {
                "count": 2,
                "targets": [{"id": 101}, {"id": 102}],
                "authorization": "Bearer must-not-leak",
            },
        },
    }
    db.add(
        AgentResponseRun(
            run_id="run_recovery",
            user_id=7,
            surface="admin",
            session_key="session-recovery",
            status="waiting_approval",
            checkpoint_json=json.dumps(checkpoint, ensure_ascii=False),
            version=3,
        )
    )
    db.commit()

    response = api_module.get_agent_response_session(
        surface="admin",
        session_id="session-recovery",
        db=db,
        user=SimpleNamespace(id=7, role="admin"),
    )

    assert response.data["messages"] == [
        {"role": "user", "content": "删除测试用户"},
        {"role": "assistant", "content": "已找到两个候选用户"},
    ]
    assert response.data["pending"] == {
        "type": "response.approval.required",
        "run_id": "run_recovery",
        "call_id": "call_batch",
        "tool_name": "admin_delete_users",
        "arguments": {"user_ids": [101, 102], "api_key": "[REDACTED]"},
        "operation": "批量删除 2 个用户",
        "impact": "账号将被软删除",
        "danger": True,
        "approval_id": 19,
        "preview": {
            "count": 2,
            "targets": [{"id": 101}, {"id": 102}],
            "authorization": "[REDACTED]",
        },
    }


def test_session_recovery_redacts_reasoning_and_secrets_in_textual_payloads(db) -> None:
    checkpoint = {
        "status": "waiting_approval",
        "error": "Authorization: Bearer checkpoint-secret-marker",
        "transcript": [
            {"role": "user", "content": "执行安全检查"},
            {
                "type": "reasoning",
                "role": "assistant",
                "content": "role-reasoning-secret-marker",
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "已完成安全检查"}],
            },
        ],
        "pending": {
            "kind": "approval",
            "call": {
                "call_id": "call_text_secret",
                "name": "mcp_sensitive_tool",
                "arguments": {
                    "payload": '{"api_key":"nested-api-secret-marker","query":"visible"}',
                    "reasoning": "argument-reasoning-secret-marker",
                },
            },
            "remaining_calls": [],
            "operation": "执行工具",
            "impact": "authorization=impact-secret-marker",
            "danger": False,
            "preview": {
                "message": "Authorization: Bearer preview-secret-marker",
            },
        },
    }
    db.add(
        AgentResponseRun(
            run_id="run_recovery_text_secret",
            user_id=7,
            surface="admin",
            session_key="session-recovery-text-secret",
            status="waiting_approval",
            checkpoint_json=json.dumps(checkpoint, ensure_ascii=False),
            version=1,
        )
    )
    db.commit()

    response = api_module.get_agent_response_session(
        surface="admin",
        session_id="session-recovery-text-secret",
        db=db,
        user=SimpleNamespace(id=7, role="admin"),
    )
    serialized = json.dumps(response.data, ensure_ascii=False)

    assert response.data["messages"] == [
        {"role": "user", "content": "执行安全检查"},
        {"role": "assistant", "content": "已完成安全检查"},
    ]
    assert '"query": "visible"' in response.data["pending"]["arguments"]["payload"]
    for marker in (
        "checkpoint-secret-marker",
        "role-reasoning-secret-marker",
        "nested-api-secret-marker",
        "argument-reasoning-secret-marker",
        "impact-secret-marker",
        "preview-secret-marker",
    ):
        assert marker not in serialized


def test_public_terminal_response_drops_non_message_output_items() -> None:
    event = api_module._public_stream_event(
        {
            "type": "response.completed",
            "response": {
                "id": "run_public_terminal",
                "status": "completed",
                "output_text": "用户可见结果",
                "output": [
                    {
                        "type": "reasoning",
                        "content": "terminal-direct-reasoning-marker",
                    },
                    {
                        "type": "function_call",
                        "arguments": '{"api_key":"terminal-direct-argument-marker"}',
                    },
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "用户可见结果"}],
                    },
                ],
                "error": "Authorization: Bearer terminal-direct-error-marker",
            },
        }
    )
    serialized = json.dumps(event, ensure_ascii=False)

    assert "用户可见结果" in serialized
    assert "terminal-direct-reasoning-marker" not in serialized
    assert "terminal-direct-argument-marker" not in serialized
    assert "terminal-direct-error-marker" not in serialized


def test_session_recovery_is_user_and_surface_isolated(db) -> None:
    db.add(
        AgentResponseRun(
            run_id="run_other_user",
            user_id=8,
            surface="admin",
            session_key="session-isolated",
            status="completed",
            checkpoint_json='{"transcript":[{"role":"user","content":"secret"}]}',
        )
    )
    db.commit()

    response = api_module.get_agent_response_session(
        surface="admin",
        session_id="session-isolated",
        db=db,
        user=SimpleNamespace(id=7, role="admin"),
    )

    assert response.data == {
        "surface": "admin",
        "session_id": "session-isolated",
        "run": None,
        "messages": [],
        "pending": None,
    }


def test_session_recovery_restores_stale_approval_transition(db) -> None:
    checkpoint = {
        "model": "deepseek-v4-flash",
        "status": "approving",
        "transcript": [{"role": "user", "content": "删除测试用户"}],
        "pending": {
            "kind": "approval",
            "call": {
                "call_id": "call_stale_approval",
                "name": "admin_delete_user",
                "arguments": {"user_id": 101},
            },
            "operation": "删除测试用户",
            "impact": "账号将被软删除",
            "danger": True,
        },
    }
    db.add(
        AgentResponseRun(
            run_id="run_stale_approval",
            user_id=7,
            surface="admin",
            session_key="session-stale-approval",
            status="approving",
            checkpoint_json=json.dumps(checkpoint, ensure_ascii=False),
            update_time=datetime.now(timezone.utc) - timedelta(hours=1),
        )
    )
    db.commit()

    response = api_module.get_agent_response_session(
        surface="admin",
        session_id="session-stale-approval",
        db=db,
        user=SimpleNamespace(id=7, role="admin"),
    )

    assert response.data["run"]["status"] == "waiting_approval"
    assert response.data["pending"]["call_id"] == "call_stale_approval"
    row = db.query(AgentResponseRun).filter_by(run_id="run_stale_approval").one()
    assert row.status == "waiting_approval"


def test_session_recovery_prefers_older_pending_run_over_newer_completed_run(db) -> None:
    pending = {
        "status": "waiting_input",
        "transcript": [{"role": "user", "content": "查找相近的 Agent"}],
        "pending": {
            "kind": "input",
            "call": {
                "call_id": "call_clarify",
                "name": "ask_user",
                "arguments": {
                    "question": "你指的是哪个 Agent？",
                    "options": ["安全审查", "代码审查"],
                    "allow_free_text": True,
                },
            },
        },
    }
    db.add(
        AgentResponseRun(
            run_id="run_older_pending",
            user_id=7,
            surface="user",
            session_key="session-pending-priority",
            status="waiting_input",
            checkpoint_json=json.dumps(pending, ensure_ascii=False),
        )
    )
    db.flush()
    db.add(
        AgentResponseRun(
            run_id="run_newer_completed",
            user_id=7,
            surface="user",
            session_key="session-pending-priority",
            status="completed",
            checkpoint_json='{"status":"completed","transcript":[]}',
        )
    )
    db.commit()

    response = api_module.get_agent_response_session(
        surface="user",
        session_id="session-pending-priority",
        db=db,
        user=SimpleNamespace(id=7, role="user"),
    )

    assert response.data["run"]["run_id"] == "run_older_pending"
    assert response.data["pending"]["type"] == "response.input.required"
    assert response.data["pending"]["options"] == ["安全审查", "代码审查"]


def test_session_recovery_fails_stale_running_work_instead_of_hanging(db) -> None:
    db.add(
        AgentResponseRun(
            run_id="run_stale_running",
            user_id=7,
            surface="user",
            session_key="session-stale-running",
            status="running",
            checkpoint_json=json.dumps(
                {
                    "status": "running",
                    "transcript": [{"role": "user", "content": "执行任务"}],
                },
                ensure_ascii=False,
            ),
            update_time=datetime.now(timezone.utc) - timedelta(hours=1),
        )
    )
    db.commit()

    response = api_module.get_agent_response_session(
        surface="user",
        session_id="session-stale-running",
        db=db,
        user=SimpleNamespace(id=7, role="user"),
    )

    assert response.data["run"]["status"] == "failed"
    assert "安全终止" in response.data["run"]["error"]
    assert response.data["pending"] is None


class EmptyMcp:
    async def discover(self) -> list[dict[str, Any]]:
        return []

    def has_tool(self, _: str) -> bool:
        return False


@pytest.mark.asyncio
async def test_write_tool_requires_click_approval_then_executes_exact_call(db, monkeypatch) -> None:
    class FakeOrchestrator:
        def __init__(self) -> None:
            self.calls: list[tuple[str, Mapping[str, Any]]] = []

        def invoke_tool(self, name: str, arguments: Mapping[str, Any], _ctx: Any) -> Any:
            self.calls.append((name, arguments))
            return SimpleNamespace(success=True, data={"deleted": arguments["project_id"]}, error="")

    orchestrator = FakeOrchestrator()
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_args, **_kwargs: orchestrator)
    user = SimpleNamespace(id=7, role="user")
    executor = PrismToolExecutor(
        db,
        user,
        surface="user",
        run_id="run_approval",
        mcp_provider=EmptyMcp(),
    )
    call = ToolCall(
        call_id="call_delete",
        name="delete_project",
        arguments={"project_id": 9},
        raw_arguments='{"project_id":9}',
    )

    paused = await executor.execute(call)
    assert paused.status == "approval_required"
    assert paused.danger is True
    assert orchestrator.calls == []

    completed = await executor.execute(call, approved=True)
    assert completed.output == {"deleted": 9}
    assert orchestrator.calls == [("delete_project", {"project_id": 9})]
    approval = db.get(ApprovalItem, paused.approval_id)
    assert approval.status == "approved"
    assert approval.decided_by == 7

    repeated = await executor.execute(call, approved=True)
    assert repeated.output == {"deleted": 9}
    assert orchestrator.calls == [("delete_project", {"project_id": 9})]
    execution = db.query(AgentToolExecution).one()
    assert execution.status == "success"


@pytest.mark.asyncio
async def test_uncertain_tool_execution_is_never_retried(db, monkeypatch) -> None:
    class FakeOrchestrator:
        def __init__(self) -> None:
            self.calls = 0

        def invoke_tool(self, *_args, **_kwargs) -> Any:
            self.calls += 1
            return SimpleNamespace(success=True, data={"done": True}, error="")

    orchestrator = FakeOrchestrator()
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_args, **_kwargs: orchestrator)
    user = SimpleNamespace(id=7, role="user")
    executor = PrismToolExecutor(
        db,
        user,
        surface="user",
        run_id="run_uncertain",
        mcp_provider=EmptyMcp(),
    )
    call = ToolCall("call_once", "list_projects", {}, "{}")
    db.add(
        AgentToolExecution(
            request_id=service_module._request_id("run_uncertain", "call_once"),
            run_id="run_uncertain",
            call_id="call_once",
            user_id=7,
            tool_name="list_projects",
            status="executing",
            arguments_json="{}",
        )
    )
    db.commit()

    result = await executor.execute(call)

    assert result.status == "error"
    assert "不会自动重试" in result.error
    assert orchestrator.calls == 0


async def _collect_stream(response: Any) -> str:
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    return "".join(chunks)


@pytest.mark.asyncio
async def test_api_stream_filters_empty_deltas_and_emits_one_final_event(monkeypatch) -> None:
    class FakeService:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def start(self, _messages, *, run_id: str, event_sink) -> RuntimeResult:
            await event_sink({"type": "response.output_text.delta", "delta": ""})
            await event_sink({"type": "response.output_text.delta", "delta": "第一行\n第二行"})
            await event_sink({"type": "response.completed", "response": {"id": "upstream"}})
            return RuntimeResult(
                run_id=run_id,
                status="completed",
                output_text="第一行\n第二行",
                response={"output": []},
                rounds=1,
            )

    monkeypatch.setattr(api_module, "AgentResponsesService", FakeService)
    request = api_module.AgentResponsesRequest(
        surface="user",
        session_id="session-01",
        messages=[{"role": "user", "content": "执行"}],
    )
    response = await api_module.stream_agent_response(
        request,
        db=object(),
        user=SimpleNamespace(id=7, role="user"),
    )
    body = await _collect_stream(response)

    assert '"delta": ""' not in body
    assert "第一行\\n第二行" in body
    assert body.count("event: response.completed") == 1
    assert "[DONE]" not in body


@pytest.mark.asyncio
async def test_api_stream_never_exposes_reasoning_or_raw_function_arguments(monkeypatch) -> None:
    class FakeService:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def start(self, _messages, *, run_id: str, event_sink) -> RuntimeResult:
            await event_sink({"type": "response.reasoning_text.delta", "delta": "reasoning-marker"})
            await event_sink({
                "type": "response.function_call_arguments.delta",
                "delta": '{"api_key":"argument-secret-marker"}',
            })
            await event_sink({
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "name": "tool",
                    "arguments": '{"authorization":"output-secret-marker"}',
                },
            })
            await event_sink({
                "type": "response.tool.started",
                "call_id": "call-sensitive",
                "tool_name": "mcp_sensitive_tool",
                "arguments": {
                    "payload": '{"api_key":"nested-stream-secret-marker","query":"visible"}',
                    "reasoning": "tool-reasoning-secret-marker",
                },
            })
            await event_sink({
                "type": "response.tool.failed",
                "call_id": "call-sensitive",
                "tool_name": "mcp_sensitive_tool",
                "error": "Authorization: Bearer stream-error-secret-marker",
            })
            await event_sink({"type": "response.output_text.delta", "delta": "用户可见结果"})
            return RuntimeResult(
                run_id=run_id,
                status="completed",
                output_text="用户可见结果",
                response={
                    "output": [
                        {"type": "reasoning", "content": "terminal-reasoning-marker"},
                        {
                            "type": "function_call",
                            "name": "tool",
                            "arguments": '{"api_key":"terminal-secret-marker"}',
                        },
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "用户可见结果"}],
                        },
                    ],
                },
                rounds=1,
            )

    monkeypatch.setattr(api_module, "AgentResponsesService", FakeService)
    request = api_module.AgentResponsesRequest(
        surface="user",
        session_id="session-redaction",
        messages=[{"role": "user", "content": "执行"}],
    )
    response = await api_module.stream_agent_response(
        request,
        db=object(),
        user=SimpleNamespace(id=7, role="user"),
    )
    body = await _collect_stream(response)

    assert "用户可见结果" in body
    for marker in (
        "reasoning-marker",
        "argument-secret-marker",
        "output-secret-marker",
        "terminal-reasoning-marker",
        "terminal-secret-marker",
        "nested-stream-secret-marker",
        "tool-reasoning-secret-marker",
        "stream-error-secret-marker",
    ):
        assert marker not in body


@pytest.mark.asyncio
async def test_api_stream_translates_runtime_approval_event_for_frontend(monkeypatch) -> None:
    class FakeService:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def start(self, _messages, *, run_id: str, event_sink) -> RuntimeResult:
            return RuntimeResult(
                run_id=run_id,
                status="waiting_approval",
                events=(
                    {
                        "type": "response.approval.required",
                        "run_id": run_id,
                        "tool_call_id": "call_1",
                        "name": "delete_project",
                        "arguments": {"project_id": 3},
                        "operation": "delete_project",
                        "impact": "写操作",
                        "danger": True,
                    },
                ),
            )

    monkeypatch.setattr(api_module, "AgentResponsesService", FakeService)
    request = api_module.AgentResponsesRequest(
        surface="user",
        session_id="session-02",
        messages=[{"role": "user", "content": "删除项目"}],
    )
    response = await api_module.stream_agent_response(
        request,
        db=object(),
        user=SimpleNamespace(id=7, role="user"),
    )
    body = await _collect_stream(response)
    frames = [frame for frame in body.split("\n\n") if "response.approval.required" in frame]

    assert len(frames) == 1
    payload = json.loads(next(line[6:] for line in frames[0].splitlines() if line.startswith("data: ")))
    assert payload["call_id"] == "call_1"
    assert payload["tool_name"] == "delete_project"
    assert "event: response.completed" not in body


@pytest.mark.asyncio
async def test_client_disconnect_does_not_cancel_started_agent_run(monkeypatch) -> None:
    started = asyncio.Event()
    finished = asyncio.Event()
    cancelled = False

    class FakeService:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def start(self, _messages, *, run_id: str, event_sink) -> RuntimeResult:
            nonlocal cancelled
            started.set()
            try:
                await asyncio.sleep(0.03)
                return RuntimeResult(run_id=run_id, status="completed", rounds=1)
            except asyncio.CancelledError:
                cancelled = True
                raise
            finally:
                finished.set()

    monkeypatch.setattr(api_module, "AgentResponsesService", FakeService)
    request = api_module.AgentResponsesRequest(
        surface="user",
        session_id="session-disconnect",
        messages=[{"role": "user", "content": "执行完整任务"}],
    )
    response = await api_module.stream_agent_response(
        request,
        db=object(),
        user=SimpleNamespace(id=7, role="user"),
    )
    iterator = response.body_iterator.__aiter__()
    first = await iterator.__anext__()
    assert "response.created" in first

    pending = asyncio.create_task(iterator.__anext__())
    await started.wait()
    pending.cancel()
    with pytest.raises((asyncio.CancelledError, StopAsyncIteration)):
        await pending

    await asyncio.wait_for(finished.wait(), timeout=0.2)
    assert cancelled is False


@pytest.mark.asyncio
async def test_client_disconnect_discards_burst_events_without_blocking_worker(monkeypatch) -> None:
    started = asyncio.Event()
    finished = asyncio.Event()

    class FakeService:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def start(self, _messages, *, run_id: str, event_sink) -> RuntimeResult:
            started.set()
            try:
                await asyncio.sleep(0.03)
                for index in range(500):
                    await event_sink(
                        {
                            "type": "response.tool.started",
                            "run_id": run_id,
                            "tool_call_id": f"call_{index}",
                            "tool_name": "list_projects",
                            "status": "running",
                        }
                    )
                return RuntimeResult(run_id=run_id, status="completed", rounds=1)
            finally:
                finished.set()

    monkeypatch.setattr(api_module, "AgentResponsesService", FakeService)
    request = api_module.AgentResponsesRequest(
        surface="user",
        session_id="session-disconnect-burst",
        messages=[{"role": "user", "content": "执行大量工具事件"}],
    )
    response = await api_module.stream_agent_response(
        request,
        db=object(),
        user=SimpleNamespace(id=7, role="user"),
    )
    iterator = response.body_iterator.__aiter__()
    first = await iterator.__anext__()
    assert "response.created" in first

    pending = asyncio.create_task(iterator.__anext__())
    await started.wait()
    pending.cancel()
    with pytest.raises((asyncio.CancelledError, StopAsyncIteration)):
        await pending

    await asyncio.wait_for(finished.wait(), timeout=0.5)
