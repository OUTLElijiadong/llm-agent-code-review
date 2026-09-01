"""全服管理 Agent 的权限、审批、幂等与脱敏回归。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.core.exceptions import ConflictError
from app.models.admin_chat import OpsExecution
from app.models.agent_governance import ApprovalItem, ToolCallLog
from app.models.agent_response_run import AgentToolExecution
from app.services import agent_responses_service as responses_module
from app.services import ops_service, policy_engine
from app.services.agent_responses_service import (
    AgentSessionExpiredError,
    PrismToolExecutor,
    _operations_tool_schema,
)
from app.services.deepseek_responses_runtime import ToolCall


class EmptyMcp:
    async def discover(self):
        return []

    def has_tool(self, _name: str) -> bool:
        return False


@pytest.fixture(autouse=True)
def lightweight_orchestrator(monkeypatch):
    monkeypatch.setattr(responses_module, "get_request_orchestrator", lambda *_args, **_kwargs: SimpleNamespace())


def _executor(db, admin_user, run_id: str, events=None) -> PrismToolExecutor:
    async def sink(event):
        if events is not None:
            events.append(dict(event))

    return PrismToolExecutor(
        db,
        admin_user,
        surface="admin",
        run_id=run_id,
        mcp_provider=EmptyMcp(),
        event_sink=sink,
    )


def test_operation_schema_has_one_strict_variant_per_action() -> None:
    parameters = _operations_tool_schema()["parameters"]
    variants = parameters["oneOf"]
    assert {item["properties"]["action"]["const"] for item in variants} == set(ops_service.ACTION_RISKS)
    assert all(item["additionalProperties"] is False for item in variants)
    assert all(item["properties"]["params"]["additionalProperties"] is False for item in variants)


def test_backend_rejects_extra_wrong_type_and_conditional_missing_params() -> None:
    with pytest.raises(ValueError, match="未允许参数"):
        ops_service.validate_action_params("status", {"command": "id"})
    with pytest.raises(ValueError, match="必须是整数"):
        ops_service.validate_action_params("list_directory", {"path": "/tmp", "limit": "20"})
    with pytest.raises(ValueError, match="public_key"):
        ops_service.validate_action_params(
            "ssh_authorized_key_action", {"operation": "add", "username": "deploy"},
        )


def test_scheduler_cannot_execute_a_write_action(db, monkeypatch) -> None:
    called = False

    def fake_executor(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"ok": True}

    monkeypatch.setattr(ops_service, "_call_executor", fake_executor)
    with pytest.raises(PermissionError, match="只读"):
        ops_service.execute(db, None, action="backup_database", source="scheduler")
    assert called is False


@pytest.mark.parametrize("action", ["host_inventory", "list_directory", "read_text_file", "journal_query"])
def test_scheduler_system_identity_cannot_read_host_details(db, monkeypatch, action) -> None:
    called = False

    def fake_executor(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"ok": True}

    params = {
        "host_inventory": {},
        "list_directory": {"path": "/tmp"},
        "read_text_file": {"path": "/tmp/example"},
        "journal_query": {"unit": "backend"},
    }[action]
    monkeypatch.setattr(ops_service, "_call_executor", fake_executor)
    with pytest.raises(PermissionError, match="只读"):
        ops_service.execute(db, None, action=action, params=params, source="scheduler")
    assert called is False


@pytest.mark.asyncio
async def test_disabled_operations_agent_cannot_execute(db, super_admin_user, monkeypatch) -> None:
    monkeypatch.setattr(responses_module.agent_governance_service, "is_runtime_enabled", lambda *_args: False)
    executor = _executor(db, super_admin_user, "run-disabled-ops")
    call = ToolCall("call-status", "admin_execute_operation", {"action": "status", "params": {}}, "")
    result = await executor.execute(call)
    assert result.status == "error"
    assert "已停用" in result.error


@pytest.mark.asyncio
async def test_ordinary_admin_cannot_discover_or_forge_server_tools(db, admin_user, monkeypatch) -> None:
    monkeypatch.setattr(responses_module.agent_governance_service, "is_runtime_enabled", lambda *_args: True)
    executor = _executor(db, admin_user, "run-ordinary-admin")

    names = {schema["name"] for schema in await executor.tool_schemas()}
    assert "admin_execute_operation" not in names
    assert "admin_system_status" not in names
    capability_tool = next(
        schema
        for schema in await executor.tool_schemas()
        if schema["name"] == "admin_execute_capability"
    )
    capability_codes = capability_tool["parameters"]["properties"]["capability"]["enum"]
    assert "overview.system" not in capability_codes
    assert "llm.config.get" not in capability_codes
    assert "llm.config.update" not in capability_codes
    assert "llm.config.test" not in capability_codes
    assert "embedding.config.get" not in capability_codes
    assert "embedding.config.update" not in capability_codes

    forged = await executor.execute(
        ToolCall("call-forged-status", "admin_execute_operation", {"action": "status", "params": {}}, "")
    )
    assert forged.status == "error"
    assert "超级管理员" in forged.error


@pytest.mark.asyncio
async def test_ordinary_user_cannot_discover_or_forge_global_evolution_tools(db, monkeypatch) -> None:
    """用户 Agent 不得越过管理端权限触发全局 Skill 或演进提案。"""

    from app.models.user import User

    user = User(username="member", password="x", role="user", status=1)
    db.add(user)
    db.commit()
    executor = PrismToolExecutor(
        db,
        user,
        surface="user",
        run_id="run-member-evolution",
        mcp_provider=EmptyMcp(),
    )

    schemas = await executor.tool_schemas()
    names = {schema["name"] for schema in schemas}
    assert "trigger_evolution" not in names
    assert not any(name.startswith("skill_") for name in names)

    forged = await executor.execute(
        ToolCall(
            "call-forged-evolution",
            "trigger_evolution",
            {"agent_name": "evolution", "window_days": 30},
            "{}",
        ),
        approved=True,
    )
    assert forged.status == "error"
    assert "Agent 配置权限" in forged.error


@pytest.mark.asyncio
async def test_super_admin_discovers_server_capabilities(db, super_admin_user, monkeypatch) -> None:
    monkeypatch.setattr(responses_module.agent_governance_service, "is_runtime_enabled", lambda *_args: True)
    executor = _executor(db, super_admin_user, "run-super-admin")

    schemas = await executor.tool_schemas()
    names = {schema["name"] for schema in schemas}
    assert "admin_execute_operation" in names
    assert "admin_system_status" in names
    capability_tool = next(
        schema for schema in schemas if schema["name"] == "admin_execute_capability"
    )
    capability_codes = capability_tool["parameters"]["properties"]["capability"]["enum"]
    assert "overview.system" in capability_codes
    assert "llm.config.get" in capability_codes
    assert "llm.config.update" in capability_codes
    assert "embedding.config.get" in capability_codes


@pytest.mark.asyncio
async def test_session_is_rechecked_after_execution_ledger_commit(db, super_admin_user) -> None:
    """占位账本提交后失效的旧设备不得触发真正的工具副作用。"""

    side_effects: list[str] = []
    executor = PrismToolExecutor(
        db,
        super_admin_user,
        surface="admin",
        run_id="run-session-race",
        mcp_provider=EmptyMcp(),
        session_validator=lambda: False,
    )
    call = ToolCall("call-race", "custom_test_tool", {}, "{}")

    result = await executor._execute_once(call, lambda: side_effects.append("executed"))

    assert result.status == "error"
    assert "另一台设备登录" in result.error
    assert side_effects == []
    row = db.query(AgentToolExecution).filter_by(run_id="run-session-race", call_id="call-race").one()
    assert row.status == "failed"


@pytest.mark.asyncio
async def test_external_mcp_is_hidden_and_rejected_for_ordinary_admin(db, admin_user) -> None:
    class DangerousMcp:
        def __init__(self) -> None:
            self.discover_calls = 0
            self.call_calls = 0

        async def discover(self):
            self.discover_calls += 1
            return [{
                "type": "function",
                "name": "mcp_ops_reboot_server",
                "description": "reboot",
                "parameters": {"type": "object", "properties": {}},
            }]

        def has_tool(self, name: str) -> bool:
            return name == "mcp_ops_reboot_server"

        async def call(self, _name, _arguments):
            self.call_calls += 1
            return {"ok": True}

    mcp = DangerousMcp()
    executor = PrismToolExecutor(
        db,
        admin_user,
        surface="admin",
        run_id="run-ordinary-mcp",
        mcp_provider=mcp,
    )
    names = {schema["name"] for schema in await executor.tool_schemas()}
    assert "mcp_ops_reboot_server" not in names
    assert mcp.discover_calls == 0

    forged = await executor.execute(
        ToolCall("call-forged-mcp", "mcp_ops_reboot_server", {}, ""),
        approved=True,
    )
    assert forged.status == "error"
    assert "超级管理员" in forged.error
    assert mcp.call_calls == 0


@pytest.mark.asyncio
async def test_external_mcp_is_available_to_unique_super_admin(db, super_admin_user) -> None:
    class ReadOnlyMcp:
        async def discover(self):
            return [{
                "type": "function",
                "name": "mcp_ops_status",
                "description": "status",
                "parameters": {"type": "object", "properties": {}},
            }]

        def has_tool(self, name: str) -> bool:
            return name == "mcp_ops_status"

        async def call(self, _name, _arguments):
            return {"ok": True}

    executor = PrismToolExecutor(
        db,
        super_admin_user,
        surface="admin",
        run_id="run-super-mcp",
        mcp_provider=ReadOnlyMcp(),
    )
    assert "mcp_ops_status" in {schema["name"] for schema in await executor.tool_schemas()}
    call = ToolCall("call-mcp-status", "mcp_ops_status", {}, "")
    assert (await executor.execute(call)).status == "approval_required"
    assert (await executor.execute(call, approved=True)).status == "success"


def test_ops_service_final_guard_rejects_ordinary_admin(db, admin_user, monkeypatch) -> None:
    monkeypatch.setattr(ops_service, "_call_executor", lambda *_args, **_kwargs: {"ok": True})
    with pytest.raises(PermissionError, match="超级管理员"):
        ops_service.execute(db, admin_user, action="status")


def test_ops_request_id_cannot_be_rebound_to_other_action(db, monkeypatch) -> None:
    calls: list[str] = []

    def fake_executor(action, _params, _request_id):
        calls.append(action)
        return {"ok": True, "action": action, "result": {}}

    monkeypatch.setattr(ops_service, "_call_executor", fake_executor)
    ops_service.execute(
        db,
        None,
        action="status",
        source="scheduler",
        request_id="ops-fixed-request-01",
    )

    with pytest.raises(ConflictError, match="已绑定其他运维请求"):
        ops_service.execute(
            db,
            None,
            action="certificate_status",
            source="scheduler",
            request_id="ops-fixed-request-01",
        )
    assert calls == ["status"]


def test_running_ops_receipt_reconciles_from_executor_ledger(db, monkeypatch) -> None:
    row = OpsExecution(
        request_id="ops-reconcile-request-01",
        action="status",
        risk_level="low",
        status="running",
        params_json="{}",
        started_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    monkeypatch.setattr(
        ops_service,
        "_call_executor",
        lambda action, params, request_id: {
            "ok": True,
            "action": action,
            "result": {"health_status": "degraded", "can_continue": True},
            "duplicate": True,
        },
    )

    result = ops_service.execute(
        db,
        None,
        action="status",
        source="scheduler",
        request_id="ops-reconcile-request-01",
    )

    assert result["status"] == "success"
    assert result["duplicate"] is True
    assert result["result"]["result"]["health_status"] == "degraded"
    assert db.query(ToolCallLog).filter_by(copilot_request_id="ops-reconcile-request-01").count() == 1


@pytest.mark.asyncio
async def test_expired_agent_session_is_rejected_before_any_tool(db, super_admin_user) -> None:
    executor = PrismToolExecutor(
        db,
        super_admin_user,
        surface="admin",
        run_id="run-expired-session",
        mcp_provider=EmptyMcp(),
        session_validator=lambda: False,
    )

    with pytest.raises(AgentSessionExpiredError, match="另一台设备"):
        await executor.execute(
            ToolCall(
                "call-expired-status",
                "admin_execute_operation",
                {"action": "status", "params": {}},
                "",
            )
        )


@pytest.mark.asyncio
async def test_critical_operation_approval_and_execution_store_only_digests(db, super_admin_user, monkeypatch) -> None:
    events = []
    monkeypatch.setattr(responses_module.agent_governance_service, "is_runtime_enabled", lambda *_args: True)
    monkeypatch.setattr(
        responses_module.tool_gateway,
        "authorize",
        lambda *_args, **_kwargs: SimpleNamespace(decision=policy_engine.ALLOW, reason="test"),
    )
    monkeypatch.setattr(
        ops_service,
        "execute",
        lambda *_args, **_kwargs: {"status": "success", "request_id": "request", "result": {"ok": True}},
    )
    executor = _executor(db, super_admin_user, "run-write-ops", events)
    secret_content = "TOKEN=must-not-be-persisted"
    call = ToolCall(
        "call-write",
        "admin_execute_operation",
        {"action": "write_text_file", "params": {"path": "/tmp/prism-test.conf", "content": secret_content}},
        "",
    )

    paused = await executor.execute(call)
    assert paused.status == "approval_required"
    assert paused.danger is True
    approval = db.query(ApprovalItem).one()
    assert secret_content not in approval.request_json
    assert "content_sha256" in approval.request_json

    completed = await executor.execute(call, approved=True)
    assert completed.status == "success"
    execution = db.query(AgentToolExecution).one()
    assert secret_content not in execution.arguments_json
    assert "content_sha256" in execution.arguments_json
    assert secret_content not in json.dumps(events, ensure_ascii=False)
