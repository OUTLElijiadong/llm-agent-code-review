"""「小菱修改自己密码」固定工具的契约、路由与脱敏回归测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.agents.orchestrator as orchestrator_module
from app.agents.base import AgentContext, AgentResult
from app.agents.orchestrator import Orchestrator
from app.agents.tool_contracts import (
    ChangeOwnPasswordArguments,
    FixedToolArgumentError,
    fixed_tool_accepts_ctx,
    is_fixed_tool,
    validate_fixed_tool_arguments,
)
from app.core.exceptions import AuthError
from app.models.agent_governance import ApprovalItem
from app.models.agent_response_run import AgentToolExecution
from app.models.user import User
from app.services import agent_responses_service as service_module
from app.services import auth_service as auth_service_module
from app.services.agent_responses_service import PrismToolExecutor
from app.services.deepseek_responses_runtime import ToolCall


class EmptyMcp:
    """无外部 MCP 工具的最小 Provider。"""

    def has_tool(self, _tool_name: str) -> bool:
        return False


def _bare_orchestrator() -> Orchestrator:
    """绕过 Orchestrator 重初始化，仅保留请求级 DB/用户字段。"""
    orch = Orchestrator.__new__(Orchestrator)
    orch._db = None
    orch._user = None
    return orch


def _change_password_arguments() -> dict[str, str]:
    """返回满足长度约束的测试参数。"""
    return {"old_password": "old-pass-1", "new_password": "new-pass-1"}


@pytest.mark.parametrize(
    ("arguments", "reason"),
    [
        ({"old_password": "abcde", "new_password": "new-pass-1"}, "过短旧密码"),
        ({"old_password": "old-pass-1", "new_password": "abcde"}, "过短新密码"),
        ({"old_password": "old-pass-1"}, "缺失新密码"),
        ({"new_password": "new-pass-1"}, "缺失旧密码"),
        ({}, "全部缺失"),
        ({"old_password": "old-pass-1", "new_password": "x" * 33}, "超长新密码"),
        ({"old_password": "x" * 33, "new_password": "new-pass-1"}, "超长旧密码"),
    ],
)
def test_change_own_password_arguments_reject_short_missing_or_long(arguments: dict[str, str], reason: str) -> None:
    """修改自己密码的工具参数必须严格限制为两个 6-32 位密码。"""
    del reason
    with pytest.raises(FixedToolArgumentError):
        validate_fixed_tool_arguments("change_own_password", arguments)


def test_change_own_password_arguments_accept_normal_and_is_registered() -> None:
    """正常参数应规范化通过，且工具以注入上下文的固定工具注册。"""
    arguments = _change_password_arguments()

    assert validate_fixed_tool_arguments("change_own_password", arguments) == arguments
    assert is_fixed_tool("change_own_password") is True
    assert fixed_tool_accepts_ctx("change_own_password") is True

    schema = ChangeOwnPasswordArguments.model_json_schema()
    assert set(schema["required"]) == {"old_password", "new_password"}
    assert schema["properties"]["old_password"]["minLength"] == 6
    assert schema["properties"]["old_password"]["maxLength"] == 32
    assert schema["properties"]["new_password"]["minLength"] == 6
    assert schema["properties"]["new_password"]["maxLength"] == 32


def test_change_own_password_requires_injected_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """未注入 DB/用户时必须在调用鉴权服务前失败关闭。"""
    orch = _bare_orchestrator()
    change_password = MagicMock()
    monkeypatch.setattr(auth_service_module, "change_password", change_password)

    result = orch.change_own_password("old-pass-1", "new-pass-1")

    assert result.success is False
    assert "DB 或用户上下文未注入" in (result.error or "")
    change_password.assert_not_called()


def test_change_own_password_success_calls_auth_service_without_leaking_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功路径应精确委托鉴权服务，并且返回摘要不包含新旧密码。"""
    orch = _bare_orchestrator()
    db = object()
    user = SimpleNamespace(id=17)
    orch._db = db
    orch._user = user
    change_password = MagicMock()
    monkeypatch.setattr(orchestrator_module.auth_service, "change_password", change_password)

    result = orch.change_own_password(
        "old-pass-1",
        "new-pass-1",
        ctx=AgentContext(user_id=17),
    )

    assert result.success is True
    assert result.data == {"success": True, "message": "密码已修改，请重新登录"}
    assert result.error is None
    assert "old-pass-1" not in json.dumps(result.__dict__, default=str)
    assert "new-pass-1" not in json.dumps(result.__dict__, default=str)
    change_password.assert_called_once_with(db, user, "old-pass-1", "new-pass-1")


@pytest.mark.parametrize(
    ("error", "expected_fragment"),
    [
        (AuthError("旧密码错误", code=40001), "旧密码错误"),
        (RuntimeError("internal failure"), "internal failure"),
    ],
)
def test_change_own_password_failure_returns_safe_error(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_fragment: str,
) -> None:
    """旧密码错误或未预期异常都应返回失败结果，且不泄露输入密码。"""
    orch = _bare_orchestrator()
    orch._db = object()
    orch._user = SimpleNamespace(id=17)
    change_password = MagicMock(side_effect=error)
    monkeypatch.setattr(orchestrator_module.auth_service, "change_password", change_password)

    result = orch.change_own_password("old-pass-1", "new-pass-1")

    assert result.success is False
    assert expected_fragment in (result.error or "")
    assert "old-pass-1" not in json.dumps(result.__dict__, default=str)
    assert "new-pass-1" not in json.dumps(result.__dict__, default=str)
    change_password.assert_called_once()


def test_service_whitelist_marks_change_own_password_as_approved_write() -> None:
    """工具必须走固定工具 invoke_tool 路径，并作为写操作强制审批。"""
    assert "change_own_password" in service_module._WRITE_TOOLS
    assert "change_own_password" not in service_module._DANGER_TOOLS
    assert "change_own_password" not in service_module._USER_CAPABILITY_NAMES
    assert service_module.is_fixed_tool("change_own_password") is True


def test_persisted_and_event_arguments_redact_both_password_fields() -> None:
    """任何账本/审批/SSE 持久化路径都不得写出明文新旧密码。"""
    arguments = _change_password_arguments()
    persisted = service_module._persisted_tool_arguments("change_own_password", arguments)
    serialized = json.dumps(persisted, ensure_ascii=False, default=str)

    assert "old-pass-1" not in serialized
    assert "new-pass-1" not in serialized
    assert persisted["old_password"].startswith("[REDACTED]:hmac-sha256:")
    assert persisted["new_password"].startswith("[REDACTED]:hmac-sha256:")

    event = service_module._redact_event_value(persisted)
    assert event["old_password"] == "[REDACTED]"
    assert event["new_password"] == "[REDACTED]"

    call = ToolCall(
        call_id="call-change-password",
        name="change_own_password",
        arguments=arguments,
        raw_arguments=json.dumps(arguments, ensure_ascii=False),
    )
    persisted_call = PrismToolExecutor._persisted_arguments(call)
    assert "old-pass-1" not in json.dumps(persisted_call, ensure_ascii=False, default=str)
    assert "new-pass-1" not in json.dumps(persisted_call, ensure_ascii=False, default=str)


@pytest.mark.asyncio
async def test_prism_executor_requires_approval_and_persists_redacted_arguments(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PrismToolExecutor 应先审批后执行，审批与执行账本均只保存脱敏参数。"""
    user = User(username="password_owner", password="x", role="user", status=1)
    db.add(user)
    db.commit()
    db.refresh(user)

    fake_orch = SimpleNamespace(
        invoke_tool=MagicMock(
            return_value=AgentResult(
                success=True,
                data={"success": True, "message": "密码已修改，请重新登录"},
            )
        )
    )
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_args, **_kwargs: fake_orch)
    executor = PrismToolExecutor(
        db,
        user,
        surface="user",
        run_id="run-change-password",
        mcp_provider=EmptyMcp(),
    )
    arguments = _change_password_arguments()
    call = ToolCall(
        call_id="call-change-password-db",
        name="change_own_password",
        arguments=arguments,
        raw_arguments=json.dumps(arguments, ensure_ascii=False),
    )

    paused = await executor.execute(call)

    assert paused.status == "approval_required"
    approval = db.query(ApprovalItem).one()
    approval_payload = json.dumps(approval.request_json or "{}", ensure_ascii=False)
    assert "old-pass-1" not in approval_payload
    assert "new-pass-1" not in approval_payload
    assert "[REDACTED]" in approval_payload

    completed = await executor.execute(call, approved=True)

    assert completed.status == "success"
    fake_orch.invoke_tool.assert_called_once_with(
        "change_own_password",
        arguments,
        executor._agent_context(),
    )
    execution_row = db.query(AgentToolExecution).one()
    assert "old-pass-1" not in (execution_row.arguments_json or "")
    assert "new-pass-1" not in (execution_row.arguments_json or "")
    assert "[REDACTED]" in (execution_row.arguments_json or "")
