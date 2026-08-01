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
from app.models.agent_governance import (
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
async def test_admin_transport_sink_buffers_only_text_deltas() -> None:
    events: list[Mapping[str, Any]] = []
    sink = service_module._buffer_admin_text_sink(events.append)

    await sink({"type": "response.output_text.delta", "delta": "未验证文本"})
    await sink({"type": "response.tool.started", "call_id": "call-1"})

    assert events == [{"type": "response.tool.started", "call_id": "call-1"}]


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
