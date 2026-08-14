"""平台 Responses 持久化、审批与 SSE 适配测试。"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Mapping

import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import create_engine
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.orm import sessionmaker

from app.api.v1 import agent_responses as api_module
from app.models.admin_chat import AdminChatMessage, OpsExecution
from app.models.agent_governance import (
    AgentMemory,
    AgentProfile,
    AgentToolPermission,
    ApprovalItem,
    PolicyDecisionLog,
    PolicyRule,
)
from app.models.agent_response_run import AgentResponseRun, AgentToolExecution
from app.services import agent_responses_service as service_module
from app.services.agent_responses_service import DatabaseCheckpointStore, PrismToolExecutor
from app.services.deepseek_responses_runtime import (
    FAILED,
    DeepSeekResponsesRuntime,
    InvalidRunStateError,
    RunCheckpoint,
    RuntimeResult,
    ToolCall,
)


@pytest.mark.asyncio
async def test_agent_responses_runtime_uses_configured_long_task_round_budget(
    db,
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}

    class CapturingRuntime:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(
        service_module,
        "resolve_api_config",
        lambda *_args, **_kwargs: SimpleNamespace(model="deepseek-v4-flash", source="system"),
    )
    monkeypatch.setattr(service_module, "NativeResponsesTransport", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(service_module, "DeepSeekResponsesRuntime", CapturingRuntime)
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(service_module.settings, "agent_responses_max_rounds", 64)
    service = service_module.AgentResponsesService(
        db,
        SimpleNamespace(id=7, username="tester", role="user"),
        surface="user",
        session_key="session-long-task",
    )

    await service._runtime("run-long-task", None)

    assert captured["max_rounds"] == 64


def test_agent_response_request_accepts_only_one_start_input_source() -> None:
    request = api_module.AgentResponsesRequest(
        surface="user",
        session_id="session-mesh-01",
        mesh_message_id="msg_0123456789abcdef",
    )
    assert request.messages == []

    with pytest.raises(PydanticValidationError):
        api_module.AgentResponsesRequest(
            surface="user",
            session_id="session-mesh-01",
            mesh_message_id="msg_0123456789abcdef",
            messages=[{"role": "user", "content": "不应与结构化消息混用"}],
        )

    team_result = api_module.AgentResponsesRequest(
        surface="user",
        session_id="session-mesh-01",
        mesh_message_id="team-40-task-7-attempt-1-result",
    )
    assert team_result.mesh_message_id == "team-40-task-7-attempt-1-result"


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    AgentResponseRun.__table__.create(engine)
    AgentToolExecution.__table__.create(engine)
    AgentMemory.__table__.create(engine)
    ApprovalItem.__table__.create(engine)
    AgentProfile.__table__.create(engine)
    AgentToolPermission.__table__.create(engine)
    PolicyRule.__table__.create(engine)
    PolicyDecisionLog.__table__.create(engine)
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
async def test_database_checkpoint_store_cancelled_is_terminal_against_write_back(db) -> None:
    drive_store = DatabaseCheckpointStore(db, user_id=7, surface="user", session_key="session-cancel-race")
    cancel_store = DatabaseCheckpointStore(db, user_id=7, surface="user", session_key="session-cancel-race")
    checkpoint = RunCheckpoint(
        run_id="run_store_cancel_race",
        model="deepseek-v4-flash",
        transcript=[{"role": "user", "content": "修改项目"}],
        tools=[],
        status="running",
    )
    assert await drive_store.create(checkpoint) is True

    cancelled = RunCheckpoint.from_dict(checkpoint.to_dict())
    cancelled.status = "cancelled"
    cancelled.cancel_reason = "需求变更"
    await cancel_store.save(cancelled)

    # 原始驱动循环稍后回写等待审批,不能覆盖已取消终态。
    drive_store._db.expire_all()
    overwrite = RunCheckpoint.from_dict(checkpoint.to_dict())
    overwrite.status = "waiting_approval"
    overwrite.pending = None
    await drive_store.save(overwrite)

    row = db.query(AgentResponseRun).filter_by(run_id="run_store_cancel_race").one()
    assert row.status == "cancelled"
    assert json.loads(row.checkpoint_json)["cancel_reason"] == "需求变更"


@pytest.mark.asyncio
async def test_cancelled_is_terminal_across_independent_sessions(tmp_path) -> None:
    """真实跨请求模型:两个独立 Session,驱动循环持旧快照回写不能覆盖取消。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'cancel-terminal.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    AgentResponseRun.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    driver_db = factory()
    cancel_db = factory()
    try:
        driver_store = DatabaseCheckpointStore(driver_db, user_id=7, surface="user", session_key="session-x")
        cancel_store = DatabaseCheckpointStore(cancel_db, user_id=7, surface="user", session_key="session-x")
        checkpoint = RunCheckpoint(
            run_id="run_cross_request_cancel",
            model="deepseek-v4-flash",
            transcript=[{"role": "user", "content": "修改项目"}],
            tools=[],
            status="running",
        )
        assert await driver_store.create(checkpoint) is True

        # 驱动循环先读到旧 running 快照;取消请求随后独立提交 cancelled。
        stale = await driver_store.load(checkpoint.run_id)
        cancelled = RunCheckpoint.from_dict(stale.to_dict())
        cancelled.status = "cancelled"
        cancelled.cancel_reason = "需求变更"
        await cancel_store.save(cancelled)

        # 驱动循环再回写 waiting_approval,条件 UPDATE 不能覆盖取消终态。
        stale.status = "waiting_approval"
        await driver_store.save(stale)

        row = cancel_db.query(AgentResponseRun).filter_by(run_id=checkpoint.run_id).one()
        assert row.status == "cancelled"
        payload = json.loads(row.checkpoint_json)
        assert payload["status"] == "cancelled"
        assert payload["cancel_reason"] == "需求变更"
    finally:
        driver_db.close()
        cancel_db.close()
        engine.dispose()


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


@pytest.mark.asyncio
async def test_database_checkpoint_store_claim_is_compare_and_swap(db) -> None:
    first_store = DatabaseCheckpointStore(db, user_id=7, surface="admin", session_key="session-claim")
    second_store = DatabaseCheckpointStore(db, user_id=7, surface="admin", session_key="session-claim")
    checkpoint = RunCheckpoint(
        run_id="run_claim_once",
        model="deepseek-v4-flash",
        transcript=[{"role": "user", "content": "只允许恢复一次"}],
        tools=[],
        status="failed",
    )
    assert await first_store.create(checkpoint) is True

    claimed = await first_store.claim(
        checkpoint.run_id,
        expected_status="failed",
        claimed_status="running",
    )
    rejected = await second_store.claim(
        checkpoint.run_id,
        expected_status="failed",
        claimed_status="running",
    )

    assert claimed is not None
    assert claimed.status == "running"
    assert rejected is None
    row = db.query(AgentResponseRun).filter_by(run_id=checkpoint.run_id).one()
    assert row.status == "running"
    assert json.loads(row.checkpoint_json)["status"] == "running"


@pytest.mark.asyncio
async def test_database_checkpoint_store_claim_is_atomic_across_sessions(tmp_path) -> None:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'response-retry-claim.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    AgentResponseRun.__table__.create(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    seed = session_factory()
    try:
        await DatabaseCheckpointStore(
            seed,
            user_id=7,
            surface="admin",
            session_key="session-retry-claim",
        ).save(
            RunCheckpoint(
                run_id="run_database_retry_claim",
                model="deepseek-v4-flash",
                transcript=[{"role": "user", "content": "恢复运行"}],
                tools=[],
                status=FAILED,
                error="上游中断",
            )
        )
    finally:
        seed.close()

    barrier = threading.Barrier(2)

    def claim_once() -> Any:
        session = session_factory()
        try:
            barrier.wait(timeout=2)
            return asyncio.run(
                DatabaseCheckpointStore(
                    session,
                    user_id=7,
                    surface="admin",
                    session_key="session-retry-claim",
                ).claim(
                    "run_database_retry_claim",
                    expected_status=FAILED,
                    claimed_status="running",
                )
            )
        finally:
            session.close()

    try:
        outcomes = await asyncio.gather(
            asyncio.to_thread(claim_once),
            asyncio.to_thread(claim_once),
        )
        assert sum(item is not None for item in outcomes) == 1

        check = session_factory()
        try:
            row = check.query(AgentResponseRun).filter_by(run_id="run_database_retry_claim").one()
            assert row.status == "running"
            assert row.version == 2
            assert json.loads(row.checkpoint_json)["status"] == "running"
        finally:
            check.close()
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_retry_reuses_persisted_tool_result_when_checkpoint_output_was_lost(
    db,
    monkeypatch,
) -> None:
    class FinalTransport:
        def __init__(self) -> None:
            self.payloads: list[Mapping[str, Any]] = []

        async def create_response(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
            self.payloads.append(payload)
            return {
                "id": "resp_persisted_result",
                "object": "response",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "已从持久化账本安全恢复"}],
                    }
                ],
            }

    class UnexpectedOrchestrator:
        def invoke_tool(self, *_args, **_kwargs) -> Any:
            raise AssertionError("已落库工具结果不得重复触发业务执行")

    run_id = "run_persisted_tool_recovery"
    call_id = "call_persisted_list"
    arguments = {
        "keyword": "",
        "language": "",
        "status": "active",
        "page": 1,
        "page_size": 20,
    }
    call_response = {
        "id": "resp_interrupted_after_tool",
        "object": "response",
        "status": "completed",
        "output": [
            {
                "type": "function_call",
                "id": "item_persisted_list",
                "call_id": call_id,
                "name": "list_projects",
                "arguments": json.dumps(arguments, ensure_ascii=False),
            }
        ],
    }
    checkpoint = RunCheckpoint(
        run_id=run_id,
        model="deepseek-v4-flash",
        transcript=[{"role": "user", "content": "查询项目"}, *call_response["output"]],
        tools=[],
        status=FAILED,
        rounds=1,
        last_response=call_response,
        error="Worker 在工具账本落库后、检查点输出落库前中断",
    )
    store = DatabaseCheckpointStore(
        db,
        user_id=7,
        surface="admin",
        session_key="session-persisted-tool-recovery",
    )
    await store.save(checkpoint)
    db.add(
        AgentToolExecution(
            request_id=service_module._request_id(run_id, call_id),
            run_id=run_id,
            call_id=call_id,
            user_id=7,
            tool_name="list_projects",
            status="success",
            arguments_json=json.dumps(arguments, ensure_ascii=False),
            result_json=json.dumps(
                {"status": "success", "output": {"items": [], "total": 0}},
                ensure_ascii=False,
            ),
        )
    )
    db.commit()
    monkeypatch.setattr(
        service_module,
        "get_request_orchestrator",
        lambda *_args, **_kwargs: UnexpectedOrchestrator(),
    )
    user = SimpleNamespace(id=7, role="admin", username="manager", token_version=0)
    executor = PrismToolExecutor(
        db,
        user,
        surface="admin",
        run_id=run_id,
        mcp_provider=EmptyMcp(),
    )
    transport = FinalTransport()
    runtime = DeepSeekResponsesRuntime(
        transport=transport,
        tool_executor=executor,
        checkpoint_store=store,
    )

    result = await runtime.retry(run_id)

    assert result.status == "completed"
    assert result.output_text == "已从持久化账本安全恢复"
    recovered_outputs = [
        item
        for item in transport.payloads[0]["input"]
        if item.get("type") == "function_call_output" and item.get("call_id") == call_id
    ]
    assert len(recovered_outputs) == 1
    assert json.loads(recovered_outputs[0]["output"])["output"] == {"items": [], "total": 0}
    assert db.query(AgentToolExecution).filter_by(run_id=run_id, call_id=call_id).count() == 1


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


def test_session_recovery_replays_completed_tools_before_conclusion_by_sequence(db) -> None:
    checkpoint = {
        "model": "deepseek-v4-flash",
        "status": "completed",
        "rounds": 3,
        "transcript": [
            {"role": "user", "content": "先查询项目再给结论"},
            {
                "type": "function_call",
                "call_id": "call_projects",
                "name": "user_execute_capability",
                "arguments": '{"capability":"projects.list","params":{}}',
            },
            {
                "type": "function_call_output",
                "call_id": "call_projects",
                "output": '{"status":"success"}',
            },
            {
                "type": "function_call",
                "call_id": "call_profile",
                "name": "user_execute_capability",
                "arguments": '{"capability":"profile.get","params":{}}',
            },
            {
                "type": "function_call_output",
                "call_id": "call_profile",
                "output": '{"status":"error"}',
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "结论：项目查询完成，画像查询失败。"}],
            },
        ],
    }
    run = AgentResponseRun(
        run_id="run_replay_sequence",
        user_id=7,
        surface="user",
        session_key="session-replay-sequence",
        status="completed",
        checkpoint_json=json.dumps(checkpoint, ensure_ascii=False),
    )
    db.add(run)
    db.flush()
    db.add_all(
        [
            AgentToolExecution(
                request_id="request-projects",
                run_id=run.run_id,
                call_id="call_projects",
                user_id=7,
                tool_name="user_execute_capability",
                status="success",
                arguments_json=json.dumps(
                    {
                        "capability": "projects.list",
                        "params": {},
                        "api_key": "ledger-argument-secret",
                    }
                ),
                result_json=json.dumps(
                    {
                        "status": "success",
                        "output": {
                            "count": 2,
                            "authorization": "Bearer ledger-output-secret",
                        },
                    }
                ),
            ),
            AgentToolExecution(
                request_id="request-profile",
                run_id=run.run_id,
                call_id="call_profile",
                user_id=7,
                tool_name="user_execute_capability",
                status="failed",
                arguments_json=json.dumps({"capability": "profile.get", "params": {}}),
                result_json=json.dumps(
                    {
                        "status": "error",
                        "error": "Authorization: Bearer ledger-error-secret",
                    }
                ),
                error="Authorization: Bearer duplicate-ledger-error-secret",
            ),
        ]
    )
    db.commit()

    response = api_module.get_agent_response_session(
        surface="user",
        session_id="session-replay-sequence",
        db=db,
        user=SimpleNamespace(id=7, role="user"),
    )

    events = response.data["events"]
    assert [event["type"] for event in events] == [
        "response.tool.started",
        "response.tool.completed",
        "response.tool.started",
        "response.tool.failed",
        "response.output_text.delta",
    ]
    assert [event["sequence_number"] for event in events] == [1, 2, 3, 4, 5]
    assert response.data["last_sequence_number"] == 5
    assert events[-1]["delta"].startswith("结论：")
    assert max(
        event["sequence_number"]
        for event in events
        if event["type"].startswith("response.tool.")
    ) < events[-1]["sequence_number"]
    serialized = json.dumps(response.data, ensure_ascii=False)
    for marker in (
        "ledger-argument-secret",
        "ledger-output-secret",
        "ledger-error-secret",
        "duplicate-ledger-error-secret",
    ):
        assert marker not in serialized


def test_session_recovery_preserves_long_visible_table_without_leaking_secrets() -> None:
    rows = [
        f"| {index} | production_acceptance_user_{index:02d} | 生产验收用户昵称 {index} | user | 启用 |"
        for index in range(1, 21)
    ]
    table = "\n".join(
        [
            "共 56 个用户，第一页 20 条：",
            "| ID | 用户名 | 昵称 | 角色 | 状态 |",
            "| --- | --- | --- | --- | --- |",
            *rows,
            "api_key=sk-session-secret-12345678",
        ]
    )

    messages = api_module._public_transcript_messages(
        [
            {"role": "user", "content": "查询用户列表"},
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": table}],
            },
        ]
    )

    restored = messages[-1]["content"]
    assert len(restored) > 1000
    assert restored.count("\n| ") == 22
    assert "production_acceptance_user_20" in restored
    assert "[TRUNCATED" not in restored
    assert "sk-session-secret-12345678" not in restored
    assert "api_key=[REDACTED]" in restored


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
        "mesh_messages": [],
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
async def test_full_project_validation_waits_for_terminal_sandbox(db, monkeypatch) -> None:
    class FakeOrchestrator:
        def __init__(self) -> None:
            self.calls = 0

        def run_full_project_validation(self, **_kwargs):
            self.calls += 1
            return SimpleNamespace(
                success=True,
                data={"public_id": "sbx_wait", "project_id": 17, "status": "queued"},
                error="",
            )

    orchestrator = FakeOrchestrator()
    rollback_calls = 0
    original_rollback = db.rollback
    states = iter(
        [
            {"public_id": "sbx_wait", "project_id": 17, "status": "dispatching"},
            {
                "public_id": "sbx_wait",
                "project_id": 17,
                "status": "succeeded",
                "result": {"passed": True, "summary": "组合验证通过"},
                "artifacts": [{"artifact_type": "review_report", "id": 9}],
            },
        ]
    )

    async def no_sleep(_seconds: float) -> None:
        return None

    def counted_rollback() -> None:
        nonlocal rollback_calls
        rollback_calls += 1
        original_rollback()

    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_args, **_kwargs: orchestrator)
    monkeypatch.setattr(service_module.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(service_module.sandbox_service, "get_environment", lambda *_args: next(states))
    monkeypatch.setattr(db, "rollback", counted_rollback)
    executor = PrismToolExecutor(
        db,
        SimpleNamespace(id=7, role="user", token_version=0),
        surface="user",
        run_id="run_wait_terminal",
        mcp_provider=EmptyMcp(),
    )

    result = await executor.execute(
        ToolCall(
            "call_wait_terminal",
            "run_full_project_validation",
            {"project_id": 17, "language": "python"},
            '{"project_id":17,"language":"python"}',
        )
    )

    assert result.status == "success"
    assert result.output["public_id"] == "sbx_wait"
    assert result.output["status"] == "succeeded"
    assert result.output["terminal"] is True
    assert result.output["result"]["passed"] is True
    assert orchestrator.calls == 1
    assert rollback_calls == 2
    executions = db.query(AgentToolExecution).all()
    assert [(row.tool_name, row.status) for row in executions] == [
        ("run_full_project_validation", "success")
    ]


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


@pytest.mark.asyncio
async def test_invalid_tool_attempt_is_persisted_with_failure_kind_and_strategy(db, monkeypatch) -> None:
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_args, **_kwargs: SimpleNamespace())
    executor = PrismToolExecutor(
        db,
        SimpleNamespace(id=7, role="user", token_version=0),
        surface="user",
        run_id="run_invalid_strategy",
        mcp_provider=EmptyMcp(),
    )

    result = await executor.execute(
        ToolCall(
            "call_invalid_strategy",
            "run_full_project_validation",
            {"language": "python", "worker_code": "missing"},
            '{"language":"python","worker_code":"missing"}',
        )
    )

    assert result.status == "error"
    ledger = db.query(AgentToolExecution).filter_by(call_id="call_invalid_strategy").one()
    persisted = json.loads(ledger.result_json)
    assert persisted["failure_kind"] == "invalid_arguments"
    memory = db.query(AgentMemory).filter_by(memory_type="execution_strategy").one()
    assert memory.failure_kind == "invalid_arguments"
    assert memory.failure_count == 1


@pytest.mark.asyncio
async def test_cached_execution_is_not_reused_for_different_arguments(db, monkeypatch) -> None:
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_args, **_kwargs: SimpleNamespace())
    run_id = "run_cached_argument_mismatch"
    call_id = "call_describe"
    executor = PrismToolExecutor(
        db,
        SimpleNamespace(id=7, role="admin", token_version=0),
        surface="admin",
        run_id=run_id,
        mcp_provider=EmptyMcp(),
    )
    db.add(
        AgentToolExecution(
            request_id=service_module._request_id(run_id, call_id),
            run_id=run_id,
            call_id=call_id,
            user_id=7,
            tool_name="admin_describe_capabilities",
            status="success",
            arguments_json=json.dumps({"page": "/admin/overview"}),
            result_json=json.dumps({"status": "success", "output": {"count": 1}}),
        )
    )
    db.commit()

    result = await executor.execute(
        ToolCall(
            call_id,
            "admin_describe_capabilities",
            {"page": "/admin/users"},
            '{"page":"/admin/users"}',
        )
    )

    assert result.status == "error"
    assert "工具或参数不一致" in result.error


def test_persisted_argument_identity_distinguishes_redacted_and_deep_values() -> None:
    first_secret = "sk-first-secret-value-12345678"
    second_secret = "sk-second-secret-value-87654321"
    first = service_module._persisted_tool_arguments(
        "admin_execute_capability",
        {
            "capability": "llm.config.update",
            "params": {"api_key": first_secret},
        },
    )
    second = service_module._persisted_tool_arguments(
        "admin_execute_capability",
        {
            "capability": "llm.config.update",
            "params": {"api_key": second_secret},
        },
    )

    serialized_first = json.dumps(first, ensure_ascii=False)
    assert first != second
    assert first_secret not in serialized_first
    assert second_secret not in json.dumps(second, ensure_ascii=False)
    assert "[REDACTED]" in serialized_first

    long_first = service_module._persisted_tool_arguments(
        "custom_tool",
        {"items": [*range(21), {"nested": {"a": {"b": {"c": {"value": "first"}}}}}]},
    )
    long_second = service_module._persisted_tool_arguments(
        "custom_tool",
        {"items": [*range(21), {"nested": {"a": {"b": {"c": {"value": "second"}}}}}]},
    )
    assert long_first != long_second


@pytest.mark.asyncio
async def test_admin_capability_tools_are_admin_only_and_discover_exact_contracts(db, monkeypatch) -> None:
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(service_module.rbac_service, "check_permission", lambda *_args, **_kwargs: False)
    admin = SimpleNamespace(id=7, role="admin", token_version=0)
    executor = PrismToolExecutor(
        db,
        admin,
        surface="admin",
        run_id="run_admin_capabilities",
        mcp_provider=EmptyMcp(),
    )

    schemas = await executor.tool_schemas()
    names = {schema["name"] for schema in schemas}
    assert "admin_describe_capabilities" in names
    assert "admin_execute_capability" in names

    result = await executor.execute(
        ToolCall(
            "call_describe",
            "admin_describe_capabilities",
            {"page": "/admin/beta-codes"},
            '{"page":"/admin/beta-codes"}',
        )
    )
    assert result.status == "success"
    assert {row["capability"] for row in result.output["items"]} == {
        "beta_codes.list",
        "beta_codes.generate",
        "beta_codes.revoke",
    }

    monkeypatch.setattr(service_module.rbac_service, "is_admin_user", lambda *_args, **_kwargs: False)
    ordinary = PrismToolExecutor(
        db,
        SimpleNamespace(id=8, role="user"),
        surface="user",
        run_id="run_user_capabilities",
        mcp_provider=EmptyMcp(),
    )
    ordinary_names = {schema["name"] for schema in await ordinary.tool_schemas()}
    assert "admin_describe_capabilities" not in ordinary_names
    denied = await ordinary.execute(
        ToolCall(
            "call_forge",
            "admin_execute_capability",
            {"capability": "users.list", "params": {}},
            '{"capability":"users.list","params":{}}',
        )
    )
    assert denied.status == "error"
    assert "没有管理员工具权限" in denied.error


@pytest.mark.asyncio
async def test_admin_capability_read_executes_and_write_is_approved_once(db, monkeypatch) -> None:
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(
        service_module.tool_gateway,
        "authorize",
        lambda *_args, **_kwargs: SimpleNamespace(decision=service_module.policy_engine.ALLOW, reason="allow"),
    )
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_execute(_user, spec, params, *, request_id):
        calls.append((spec.code, dict(params)))
        return {"capability": spec.code, "request_id": request_id, "data": {"ok": True}}

    monkeypatch.setattr(service_module.admin_capability_service, "execute_api", fake_execute)
    executor = PrismToolExecutor(
        db,
        SimpleNamespace(id=7, role="admin", token_version=0),
        surface="admin",
        run_id="run_admin_execute",
        mcp_provider=EmptyMcp(),
    )

    read_call = ToolCall(
        "call_read",
        "admin_execute_capability",
        {"capability": "overview.security", "params": {}},
        '{"capability":"overview.security","params":{}}',
    )
    read = await executor.execute(read_call)
    assert read.status == "success"
    assert calls == [("overview.security", {})]

    write_params = {"name": "内测模板", "type": "custom", "content": "<h1>{{ title }}</h1>"}
    write_call = ToolCall(
        "call_write",
        "admin_execute_capability",
        {"capability": "report_templates.create", "params": write_params},
        json.dumps({"capability": "report_templates.create", "params": write_params}, ensure_ascii=False),
    )
    paused = await executor.execute(write_call)
    assert paused.status == "approval_required"
    assert paused.danger is False
    assert calls == [("overview.security", {})]

    completed = await executor.execute(write_call, approved=True)
    assert completed.status == "success"
    assert calls == [("overview.security", {}), ("report_templates.create", write_params)]
    repeated = await executor.execute(write_call, approved=True)
    assert repeated.output == completed.output
    assert calls == [("overview.security", {}), ("report_templates.create", write_params)]


@pytest.mark.asyncio
async def test_manager_registered_admin_capability_executes_through_real_gateway(db, monkeypatch) -> None:
    """复现生产故障路径：管理 Agent 真实策略网关应放行 users.list。"""
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_args, **_kwargs: SimpleNamespace())
    profile = AgentProfile(
        code="manager",
        name="管理Agent",
        config_json=json.dumps(
            {
                "governance_boundary": {
                    "allowed_tools": ["governance_reader"],
                    "approval_tools": ["workflow_dispatch"],
                    "blocked_tools": ["shell"],
                }
            },
            ensure_ascii=False,
        ),
    )
    db.add(profile)
    db.commit()
    service_module.agent_governance_service._ensure_manager_admin_capability_contract(db, profile)
    db.commit()

    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_execute(_user, spec, params, *, request_id):
        calls.append((spec.code, dict(params)))
        return {"capability": spec.code, "request_id": request_id, "data": {"users": []}}

    monkeypatch.setattr(service_module.admin_capability_service, "execute_api", fake_execute)
    executor = PrismToolExecutor(
        db,
        SimpleNamespace(id=7, role="admin", token_version=0),
        surface="admin",
        run_id="run_manager_real_gateway",
        mcp_provider=EmptyMcp(),
    )
    result = await executor.execute(
        ToolCall(
            "call_users_list",
            "admin_execute_capability",
            {"capability": "users.list", "params": {}},
            '{"capability":"users.list","params":{}}',
        )
    )

    assert result.status == "success"
    assert calls == [("users.list", {})]
    decision = db.query(PolicyDecisionLog).filter_by(action="admin.users.list").one()
    assert decision.decision == "allow"


def test_admin_completion_guard_requires_current_run_write_evidence() -> None:
    checkpoint = RunCheckpoint(
        run_id="run_guard",
        model="test",
        transcript=[
            {
                "role": "user",
                "content": "请通过 report_templates.delete 删除模板 ID 4",
            }
        ],
        tools=[],
    )

    assert service_module._admin_completion_guard(checkpoint, "已处理") is not None
    assert service_module._admin_completion_guard(
        RunCheckpoint(
            run_id="run_read",
            model="test",
            transcript=[{"role": "user", "content": "查询已发布 Agent"}],
            tools=[],
        ),
        "找到 2 个已发布 Agent",
    ) is None


def test_admin_completion_guard_accepts_successful_real_write_output() -> None:
    arguments = {"capability": "report_templates.delete", "params": {"template_id": 4}}
    checkpoint = RunCheckpoint(
        run_id="run_guard_success",
        model="test",
        transcript=[
            {"role": "user", "content": "删除模板 ID 4"},
            {
                "type": "function_call",
                "call_id": "call_delete",
                "name": "admin_execute_capability",
                "arguments": json.dumps(arguments, ensure_ascii=False),
            },
            {
                "type": "function_call_output",
                "call_id": "call_delete",
                "output": json.dumps({"status": "success", "output": {"deleted_count": 1}}),
            },
        ],
        tools=[],
    )

    assert service_module._admin_completion_guard(checkpoint, "删除操作已成功完成") is None


def test_admin_completion_guard_does_not_promote_non_success_status() -> None:
    arguments = {"capability": "report_templates.delete", "params": {"template_id": 4}}
    checkpoint = RunCheckpoint(
        run_id="run_guard_non_success_status",
        model="test",
        transcript=[
            {"role": "user", "content": "删除模板 ID 4"},
            {
                "type": "function_call",
                "call_id": "call_delete",
                "name": "admin_execute_capability",
                "arguments": json.dumps(arguments, ensure_ascii=False),
            },
            {
                "type": "function_call_output",
                "call_id": "call_delete",
                "output": json.dumps({"status": "completed", "output": {"deleted_count": 1}}),
            },
        ],
        tools=[],
    )

    assert service_module._admin_completion_guard(checkpoint, "模板已删除") is not None


def test_admin_completion_guard_does_not_reuse_success_from_another_capability() -> None:
    calls = [
        (
            "call_create",
            {"capability": "report_templates.create", "params": {"name": "temporary"}},
        ),
    ]
    transcript: list[dict[str, Any]] = [
        {"role": "user", "content": "请通过 report_templates.delete 删除模板 ID 4"},
    ]
    for call_id, arguments in calls:
        transcript.extend(
            [
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": "admin_execute_capability",
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps({"status": "success", "output": {"id": 4}}),
                },
            ]
        )
    checkpoint = RunCheckpoint(
        run_id="run_guard_cross_capability",
        model="test",
        transcript=transcript,
        tools=[],
    )

    error = service_module._admin_completion_guard(
        checkpoint,
        "report_templates.delete 已成功",
    )

    assert error is not None
    assert "report_templates.delete" in error


def test_admin_completion_ledger_requires_same_run_user_call_and_capability(db) -> None:
    arguments = {"capability": "report_templates.delete", "params": {"template_id": 4}}
    checkpoint = RunCheckpoint(
        run_id="run_ledger_exact",
        model="test",
        transcript=[
            {
                "type": "function_call",
                "call_id": "call_delete",
                "name": "admin_execute_capability",
                "arguments": json.dumps(arguments, ensure_ascii=False),
            }
        ],
        tools=[],
    )
    db.add_all(
        [
            AgentToolExecution(
                request_id="wrong-run",
                run_id="another_run",
                call_id="call_delete",
                user_id=7,
                tool_name="admin_execute_capability",
                status="success",
                arguments_json=json.dumps(arguments),
            ),
            AgentToolExecution(
                request_id="wrong-user",
                run_id="run_ledger_exact",
                call_id="call_delete",
                user_id=8,
                tool_name="admin_execute_capability",
                status="success",
                arguments_json=json.dumps(arguments),
            ),
            AgentToolExecution(
                request_id=service_module._request_id("run_ledger_exact", "call_delete"),
                run_id="run_ledger_exact",
                call_id="call_delete",
                user_id=7,
                tool_name="admin_execute_capability",
                status="success",
                arguments_json=json.dumps(arguments),
            ),
        ]
    )
    db.commit()

    evidence = service_module._ledger_admin_write_evidence(
        db,
        user_id=7,
        checkpoint=checkpoint,
    )

    assert evidence == {"call_delete": ("report_templates.delete", "success")}


def test_admin_completion_ledger_rejects_forged_request_id(db) -> None:
    arguments = {"capability": "report_templates.delete", "params": {"template_id": 4}}
    checkpoint = RunCheckpoint(
        run_id="run_forged_ledger",
        model="test",
        transcript=[
            {
                "type": "function_call",
                "call_id": "call_delete",
                "name": "admin_execute_capability",
                "arguments": json.dumps(arguments),
            }
        ],
        tools=[],
    )
    db.add(
        AgentToolExecution(
            request_id="forged-request-id",
            run_id="run_forged_ledger",
            call_id="call_delete",
            user_id=7,
            tool_name="admin_execute_capability",
            status="success",
            arguments_json=json.dumps(arguments),
        )
    )
    db.commit()

    assert service_module._ledger_admin_write_evidence(
        db,
        user_id=7,
        checkpoint=checkpoint,
    ) == {}


def test_admin_completion_ledger_rejects_argument_mismatch(db) -> None:
    expected_arguments = {"capability": "report_templates.delete", "params": {"template_id": 5}}
    checkpoint = RunCheckpoint(
        run_id="run_argument_mismatch",
        model="test",
        transcript=[
            {
                "type": "function_call",
                "call_id": "call_delete",
                "name": "admin_execute_capability",
                "arguments": json.dumps(expected_arguments),
            }
        ],
        tools=[],
    )
    db.add(
        AgentToolExecution(
            request_id=service_module._request_id("run_argument_mismatch", "call_delete"),
            run_id="run_argument_mismatch",
            call_id="call_delete",
            user_id=7,
            tool_name="admin_execute_capability",
            status="success",
            arguments_json=json.dumps(
                {"capability": "report_templates.delete", "params": {"template_id": 4}}
            ),
            result_json=json.dumps({"status": "success", "output": {"deleted_count": 1}}),
        )
    )
    db.commit()

    assert service_module._ledger_admin_write_evidence(
        db,
        user_id=7,
        checkpoint=checkpoint,
    ) == {}


@pytest.mark.asyncio
async def test_admin_completion_validator_requires_ledger_for_success(db) -> None:
    arguments = {"capability": "report_templates.delete", "params": {"template_id": 4}}
    checkpoint = RunCheckpoint(
        run_id="run_transcript_only_success",
        model="test",
        transcript=[
            {"role": "user", "content": "删除模板 ID 4"},
            {
                "type": "function_call",
                "call_id": "call_delete",
                "name": "admin_execute_capability",
                "arguments": json.dumps(arguments, ensure_ascii=False),
            },
            {
                "type": "function_call_output",
                "call_id": "call_delete",
                "output": json.dumps({"status": "success"}),
            },
        ],
        tools=[],
    )
    service = object.__new__(service_module.AgentResponsesService)
    service._db = db
    service._user = SimpleNamespace(id=7)

    assert await service._validate_admin_completion(checkpoint, "模板已成功删除") is not None


def test_admin_completion_guard_does_not_treat_hypothetical_question_as_write() -> None:
    checkpoint = RunCheckpoint(
        run_id="run_hypothetical",
        model="test",
        transcript=[{"role": "user", "content": "如果删除成功后如何恢复？"}],
        tools=[],
    )

    assert service_module._admin_completion_guard(
        checkpoint,
        "删除成功后可以通过备份恢复",
    ) is None


def test_admin_completion_guard_allows_capability_contract_discussion() -> None:
    checkpoint = RunCheckpoint(
        run_id="run_capability_discussion",
        model="test",
        transcript=[
            {
                "role": "user",
                "content": "report_templates.delete 的参数和风险是什么？",
            }
        ],
        tools=[],
    )

    assert service_module._admin_completion_guard(
        checkpoint,
        "report_templates.delete 成功后会移除指定模板",
    ) is None


def test_admin_completion_guard_requires_tool_for_natural_language_write_request() -> None:
    checkpoint = RunCheckpoint(
        run_id="run_natural_write",
        model="test",
        transcript=[{"role": "user", "content": "请帮我删除 ID 4 的报告模板"}],
        tools=[],
    )

    assert service_module._admin_completion_guard(checkpoint, "已处理") is not None


def test_admin_completion_guard_recognizes_polite_mutation_request() -> None:
    checkpoint = RunCheckpoint(
        run_id="run_polite_write",
        model="test",
        transcript=[{"role": "user", "content": "能否帮我删除 ID 4 的报告模板？"}],
        tools=[],
    )

    assert service_module._admin_completion_guard(checkpoint, "模板已删除") is not None


@pytest.mark.parametrize(
    "user_text",
    [
        "请按这些参数删除 ID 4 的报告模板",
        "请更新用户风险等级",
        "麻烦删除 ID 4 的报告模板",
        "请调整用户角色",
        "请编辑报告模板",
        "请更改系统配置",
        "请撤销邀请码",
        "请导入审查规则",
        "请下线这个 Agent",
    ],
)
def test_admin_completion_guard_recognizes_production_mutation_phrasing(user_text: str) -> None:
    checkpoint = RunCheckpoint(
        run_id="run_production_mutation_phrasing",
        model="test",
        transcript=[{"role": "user", "content": user_text}],
        tools=[],
    )

    assert service_module._admin_completion_guard(checkpoint, "操作完成") is not None


def test_admin_completion_guard_recognizes_every_registered_write_capability() -> None:
    write_specs = [
        spec
        for spec in service_module.CAPABILITY_BY_CODE.values()
        if spec.risk != service_module.CAPABILITY_READ
    ]

    assert write_specs
    for spec in write_specs:
        transcript = [{"role": "user", "content": f"请{spec.description}"}]
        assert service_module._requests_admin_mutation(transcript), spec.code


def test_admin_completion_guard_distinguishes_question_from_polite_command() -> None:
    question = RunCheckpoint(
        run_id="run_mutation_question",
        model="test",
        transcript=[{"role": "user", "content": "能否删除 ID 4 的报告模板？"}],
        tools=[],
    )
    command = RunCheckpoint(
        run_id="run_mutation_command",
        model="test",
        transcript=[{"role": "user", "content": "能否帮我删除 ID 4 的报告模板？"}],
        tools=[],
    )

    assert service_module._requests_admin_mutation(question.transcript) is False
    assert service_module._requests_admin_mutation(command.transcript) is True


@pytest.mark.parametrize(
    "user_text",
    [
        "请说明如何删除报告模板",
        "请告诉我如何删除报告模板",
        "请介绍删除报告模板的风险",
    ],
)
def test_admin_completion_guard_treats_explanations_as_discussion(user_text: str) -> None:
    checkpoint = RunCheckpoint(
        run_id="run_mutation_discussion",
        model="test",
        transcript=[{"role": "user", "content": user_text}],
        tools=[],
    )

    assert service_module._requests_admin_mutation(checkpoint.transcript) is False
    assert service_module._admin_completion_guard(checkpoint, "删除操作需要审批") is None


def test_admin_completion_guard_requires_success_for_every_same_capability_call() -> None:
    transcript: list[dict[str, Any]] = [{"role": "user", "content": "删除 ID 4 和 ID 5 的报告模板"}]
    for call_id, template_id, status in (
        ("call_delete_4", 4, "success"),
        ("call_delete_5", 5, "failed"),
    ):
        transcript.extend(
            [
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": "admin_execute_capability",
                    "arguments": json.dumps(
                        {
                            "capability": "report_templates.delete",
                            "params": {"template_id": template_id},
                        }
                    ),
                },
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps({"status": status}),
                },
            ]
        )
    checkpoint = RunCheckpoint(
        run_id="run_multi_delete",
        model="test",
        transcript=transcript,
        tools=[],
    )

    error = service_module._admin_completion_guard(checkpoint, "两个模板均已成功删除")

    assert error is not None
    assert "call_delete_5" in error


def test_admin_completion_guard_rejects_conflicting_duplicate_call_id() -> None:
    checkpoint = RunCheckpoint(
        run_id="run_conflicting_call_id",
        model="test",
        transcript=[
            {"role": "user", "content": "删除 ID 4 的报告模板"},
            {
                "type": "function_call",
                "call_id": "call_delete",
                "name": "admin_execute_capability",
                "arguments": json.dumps(
                    {"capability": "report_templates.delete", "params": {"template_id": 4}}
                ),
            },
            {
                "type": "function_call",
                "call_id": "call_delete",
                "name": "admin_execute_capability",
                "arguments": json.dumps(
                    {"capability": "report_templates.delete", "params": {"template_id": 5}}
                ),
            },
            {
                "type": "function_call_output",
                "call_id": "call_delete",
                "output": json.dumps({"status": "success"}),
            },
        ],
        tools=[],
    )

    assert service_module._admin_completion_guard(checkpoint, "模板已成功删除") is not None


def test_admin_completion_guard_rejects_success_claim_for_failed_call() -> None:
    arguments = {"capability": "report_templates.delete", "params": {"template_id": 4}}
    checkpoint = RunCheckpoint(
        run_id="run_failed_write",
        model="test",
        transcript=[
            {"role": "user", "content": "删除模板 ID 4"},
            {
                "type": "function_call",
                "call_id": "call_delete",
                "name": "admin_execute_capability",
                "arguments": json.dumps(arguments, ensure_ascii=False),
            },
        ],
        tools=[],
    )

    assert (
        service_module._admin_completion_guard(
            checkpoint,
            "模板已删除",
            write_evidence={"call_delete": ("report_templates.delete", "failed")},
        )
        is not None
    )
    assert (
        service_module._admin_completion_guard(
            checkpoint,
            "删除失败，模板未被修改",
            write_evidence={"call_delete": ("report_templates.delete", "failed")},
        )
        is None
    )
    assert (
        service_module._admin_completion_guard(
            checkpoint,
            "操作完成",
            write_evidence={"call_delete": ("report_templates.delete", "failed")},
        )
        is not None
    )


def test_admin_completion_guard_allows_honest_rejection_without_retry() -> None:
    arguments = {"capability": "report_templates.delete", "params": {"template_id": 4}}
    checkpoint = RunCheckpoint(
        run_id="run_rejected_write",
        model="test",
        transcript=[
            {"role": "user", "content": "删除模板 ID 4"},
            {
                "type": "function_call",
                "call_id": "call_delete",
                "name": "admin_execute_capability",
                "arguments": json.dumps(arguments, ensure_ascii=False),
            },
            {
                "type": "function_call_output",
                "call_id": "call_delete",
                "output": json.dumps({"status": "rejected", "error": "用户拒绝执行该操作"}),
            },
        ],
        tools=[],
    )

    assert service_module._admin_completion_guard(checkpoint, "已取消，不会删除模板") is None


@pytest.mark.parametrize(
    "output_text",
    [
        "请求被拒绝",
        "已取消删除操作",
        "操作已取消",
        "不会执行该操作",
    ],
)
def test_admin_completion_guard_recognizes_natural_failure_phrases(output_text: str) -> None:
    arguments = {"capability": "report_templates.delete", "params": {"template_id": 4}}
    checkpoint = RunCheckpoint(
        run_id="run_natural_failure",
        model="test",
        transcript=[
            {"role": "user", "content": "删除模板 ID 4"},
            {
                "type": "function_call",
                "call_id": "call_delete",
                "name": "admin_execute_capability",
                "arguments": json.dumps(arguments, ensure_ascii=False),
            },
        ],
        tools=[],
    )

    assert service_module._claims_mutation_failure(output_text) is True
    assert (
        service_module._admin_completion_guard(
            checkpoint,
            output_text,
            write_evidence={"call_delete": ("report_templates.delete", "failed")},
        )
        is None
    )


def test_admin_completion_guard_accepts_successful_reject_action() -> None:
    arguments = {"capability": "evolution.proposals.reject", "params": {"proposal_id": 12}}
    checkpoint = RunCheckpoint(
        run_id="run_successful_reject_action",
        model="test",
        transcript=[
            {"role": "user", "content": "请拒绝进化提案 12"},
            {
                "type": "function_call",
                "call_id": "call_reject",
                "name": "admin_execute_capability",
                "arguments": json.dumps(arguments, ensure_ascii=False),
            },
            {
                "type": "function_call_output",
                "call_id": "call_reject",
                "output": json.dumps({"status": "success", "output": {"rejected": True}}),
            },
        ],
        tools=[],
    )

    assert service_module._claims_mutation_failure("已拒绝发布申请") is False
    assert service_module._claims_mutation_success("已拒绝发布申请") is True
    assert service_module._admin_completion_guard(checkpoint, "已拒绝发布申请") is None


@pytest.mark.asyncio
async def test_admin_transport_sink_filters_text_and_upstream_tool_lifecycle() -> None:
    events: list[Mapping[str, Any]] = []
    sink = service_module._buffer_admin_text_sink(events.append)

    await sink({"type": "response.output_text.delta", "delta": "未验证文本"})
    await sink({"type": "response.tool.started", "call_id": "call-1"})

    assert events == []


def test_public_failed_tool_event_always_has_error() -> None:
    event = api_module._public_stream_event(
        {"type": "response.tool.failed", "tool_name": "create_agent_team", "error": None}
    )
    assert event is not None
    assert event["error"] == "工具 create_agent_team 执行失败"


@pytest.mark.asyncio
async def test_admin_critical_capability_redacts_secret_from_approval_and_ledger(db, monkeypatch) -> None:
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(
        service_module.tool_gateway,
        "authorize",
        lambda *_args, **_kwargs: SimpleNamespace(decision=service_module.policy_engine.ALLOW, reason="allow"),
    )

    async def fake_execute(_user, spec, _params, *, request_id):
        return {"capability": spec.code, "request_id": request_id, "data": {"updated": True}}

    monkeypatch.setattr(service_module.admin_capability_service, "execute_api", fake_execute)
    executor = PrismToolExecutor(
        db,
        SimpleNamespace(id=7, role="admin", token_version=0),
        surface="admin",
        run_id="run_admin_secret",
        mcp_provider=EmptyMcp(),
    )
    secret = "sk-secret-value-12345678"
    call = ToolCall(
        "call_secret",
        "admin_execute_capability",
        {"capability": "llm.config.update", "params": {"active": True, "api_key": secret}},
        "{}",
    )

    paused = await executor.execute(call)
    assert paused.status == "approval_required"
    assert paused.danger is True
    approval = db.get(ApprovalItem, paused.approval_id)
    assert secret not in approval.request_json
    assert "[REDACTED]" in approval.request_json

    completed = await executor.execute(call, approved=True)
    assert completed.status == "success"
    execution = db.query(AgentToolExecution).filter_by(run_id="run_admin_secret", call_id="call_secret").one()
    assert secret not in execution.arguments_json
    assert "[REDACTED]" in execution.arguments_json


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capability", "params", "api_data", "secret", "safe_marker"),
    [
        (
            "beta_codes.generate",
            {"count": 1, "expiry_days": 7},
            {"codes": ["BETA-ONE-TIME-SECRET"], "items": [{"id": 21}]},
            "BETA-ONE-TIME-SECRET",
            "generated_count",
        ),
        (
            "users.reset_password",
            {"user_id": 9},
            {"default_password": "TEMP-PASSWORD-SECRET"},
            "TEMP-PASSWORD-SECRET",
            "password_reset",
        ),
    ],
)
async def test_one_time_admin_secret_only_uses_ephemeral_sse_event(
    db,
    monkeypatch,
    capability,
    params,
    api_data,
    secret,
    safe_marker,
) -> None:
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(
        service_module.tool_gateway,
        "authorize",
        lambda *_args, **_kwargs: SimpleNamespace(
            decision=service_module.policy_engine.ALLOW,
            reason="allow",
        ),
    )

    async def fake_execute(_user, spec, _params, *, request_id):
        return {
            "capability": spec.code,
            "request_id": request_id,
            "data": api_data,
        }

    events: list[Mapping[str, Any]] = []
    monkeypatch.setattr(service_module.admin_capability_service, "execute_api", fake_execute)
    executor = PrismToolExecutor(
        db,
        SimpleNamespace(id=7, role="admin", token_version=0),
        surface="admin",
        run_id=f"run_{capability.replace('.', '_')}",
        mcp_provider=EmptyMcp(),
        event_sink=lambda event: events.append(event),
    )
    call = ToolCall(
        f"call_{capability.replace('.', '_')}",
        "admin_execute_capability",
        {"capability": capability, "params": params},
        "{}",
    )

    paused = await executor.execute(call)
    assert paused.status == "approval_required"
    completed = await executor.execute(call, approved=True)

    serialized_result = json.dumps(completed.output, ensure_ascii=False)
    ledger = db.query(AgentToolExecution).filter_by(call_id=call.call_id).one()
    assert secret not in serialized_result
    assert safe_marker in serialized_result
    assert secret not in ledger.result_json
    assert secret not in ledger.arguments_json
    sensitive = [event for event in events if event.get("type") == "response.sensitive.result"]
    assert len(sensitive) == 1
    assert sensitive[0]["capability"] == capability
    assert sensitive[0]["values"] == [secret]

    repeated = await executor.execute(call, approved=True)
    assert secret not in json.dumps(repeated.output, ensure_ascii=False)
    assert len([event for event in events if event.get("type") == "response.sensitive.result"]) == 1


def test_sensitive_result_event_is_admin_only_and_not_redacted_in_authorized_stream() -> None:
    event = {
        "type": "response.sensitive.result",
        "run_id": "run_secret",
        "call_id": "call_secret",
        "capability": "beta_codes.generate",
        "title": "新生成的内测码",
        "notice": "仅本次显示",
        "values": ["BETA-EPHEMERAL-ONLY"],
    }

    assert api_module._public_stream_event(event) is None
    public = api_module._public_stream_event(event, allow_sensitive=True)
    assert public is not None
    assert public["values"] == ["BETA-EPHEMERAL-ONLY"]


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
            await event_sink(
                {
                    "type": "response.function_call_arguments.delta",
                    "delta": '{"api_key":"argument-secret-marker"}',
                }
            )
            await event_sink(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "name": "tool",
                        "arguments": '{"authorization":"output-secret-marker"}',
                    },
                }
            )
            await event_sink(
                {
                    "type": "response.tool.started",
                    "call_id": "call-sensitive",
                    "tool_name": "mcp_sensitive_tool",
                    "arguments": {
                        "payload": '{"api_key":"nested-stream-secret-marker","query":"visible"}',
                        "reasoning": "tool-reasoning-secret-marker",
                    },
                }
            )
            await event_sink(
                {
                    "type": "response.tool.failed",
                    "call_id": "call-sensitive",
                    "tool_name": "mcp_sensitive_tool",
                    "error": "Authorization: Bearer stream-error-secret-marker",
                }
            )
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


def test_public_completed_tool_events_includes_inflight_calls(db) -> None:
    """恢复时保留进行中(未落账本)的调用链,不丢失运行状态可见性。"""
    run = AgentResponseRun(
        run_id="run_inflight",
        user_id=7,
        surface="user",
        session_key="session-inflight",
        status="running",
        checkpoint_json="{}",
    )
    db.add(run)
    db.flush()
    db.add(
        AgentToolExecution(
            request_id="req-call-a",
            run_id="run_inflight",
            call_id="call_a",
            user_id=7,
            tool_name="list_projects",
            status="success",
            arguments_json='{"page": 1}',
            result_json='{"status":"success","output":{"total":2}}',
        )
    )
    db.commit()

    checkpoint = {
        "transcript": [
            {
                "type": "function_call",
                "call_id": "call_a",
                "name": "list_projects",
                "arguments": '{"page": 1}',
            },
            {
                "type": "function_call_output",
                "call_id": "call_a",
                "output": '{"status":"success","output":{"total":2}}',
            },
            {
                "type": "function_call",
                "call_id": "call_b",
                "name": "delete_template",
                "arguments": '{"id": 4}',
            },
        ],
        "output_text": "正在处理",
    }

    events = api_module._public_completed_tool_events(db, run, checkpoint)

    call_a = [event for event in events if event.get("call_id") == "call_a"]
    call_b = [event for event in events if event.get("call_id") == "call_b"]
    # 已落账本:started + completed
    assert [event["type"] for event in call_a] == ["response.tool.started", "response.tool.completed"]
    # 进行中:只有 started,状态 running
    assert len(call_b) == 1
    assert call_b[0]["type"] == "response.tool.started"
    assert call_b[0]["status"] == "running"
    assert call_b[0]["tool_name"] == "delete_template"


def test_transcript_function_calls_parses_terminal_output(db) -> None:
    checkpoint = {
        "transcript": [
            {"type": "function_call", "call_id": "ok", "name": "a", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "ok", "output": '{"status":"success"}'},
            {"type": "function_call", "call_id": "err", "name": "b", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "err", "output": '{"status":"error","error":"x"}'},
            {"type": "function_call", "call_id": "hang", "name": "c", "arguments": "{}"},
        ],
    }
    calls = api_module._transcript_function_calls(checkpoint)
    by_id = {call["call_id"]: call for call in calls}
    assert by_id["ok"]["output_status"] == "success"
    assert by_id["err"]["output_status"] == "failed"
    assert by_id["hang"]["output"] is None
    assert [call["call_id"] for call in calls] == ["ok", "err", "hang"]


def test_operations_tool_only_exposed_to_super_admin() -> None:
    """服务器管理工具(admin_execute_operation)标记为仅超级管理员可用。"""
    from app.core.permission_codes import PermissionCode
    from app.services.agent_responses_service import _operations_tool_schema

    schema = _operations_tool_schema()
    assert schema["name"] == "admin_execute_operation"
    assert "仅超级管理员" in schema["description"]
    # 权限点存在,且 SERVER_OPS 前缀权限只授予唯一超级管理员(见 super_admin 常量)
    assert PermissionCode.SERVER_OPS_VIEW == "server_ops:view"
