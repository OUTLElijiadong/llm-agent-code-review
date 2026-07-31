"""全服管理 Agent 的权限、审批、幂等与脱敏回归。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.models.agent_governance import ApprovalItem
from app.models.agent_response_run import AgentToolExecution
from app.services import agent_responses_service as responses_module
from app.services import ops_service, policy_engine
from app.services.agent_responses_service import PrismToolExecutor, _operations_tool_schema
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


@pytest.mark.asyncio
async def test_disabled_operations_agent_cannot_execute(db, admin_user, monkeypatch) -> None:
    monkeypatch.setattr(responses_module.agent_governance_service, "is_runtime_enabled", lambda *_args: False)
    executor = _executor(db, admin_user, "run-disabled-ops")
    call = ToolCall("call-status", "admin_execute_operation", {"action": "status", "params": {}}, "")
    result = await executor.execute(call)
    assert result.status == "error"
    assert "已停用" in result.error


@pytest.mark.asyncio
async def test_critical_operation_approval_and_execution_store_only_digests(db, admin_user, monkeypatch) -> None:
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
    executor = _executor(db, admin_user, "run-write-ops", events)
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
