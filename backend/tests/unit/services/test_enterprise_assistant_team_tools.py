"""Tests for the enterprise assistant's registered-team tool dispatch."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

from app.services import enterprise_assistant_runtime_service as runtime
from app.services.deepseek_responses_runtime import ToolCall


def _call(name: str, arguments: dict, call_id: str = "tool-call-1") -> ToolCall:
    return ToolCall(
        call_id=call_id,
        name=name,
        arguments=arguments,
        raw_arguments="{}",
    )


def test_registered_team_tools_dispatch_to_enterprise_services(monkeypatch):
    db = MagicMock()
    wallet_query = (
        db.query.return_value.filter_by.return_value.populate_existing.return_value.with_for_update.return_value
    )
    wallet_query.first.return_value = SimpleNamespace(id=92)
    state = SimpleNamespace(initiator_user_id=7)
    executor = runtime.EnterpriseAssistantToolExecutor(
        db,
        enterprise_id=10,
        conversation_id=20,
        run_public_id="enterprise-run",
        lease_generation=3,
        actor_user_id=7,
    )
    monkeypatch.setattr(executor, "_current", lambda: (state, {}))
    monkeypatch.setattr(executor, "_action_clarification", lambda *_args: None)
    monkeypatch.setattr(runtime, "_factory", lambda session: ("session-factory", session))
    subscription_gate = Mock()
    wallet_balance = Mock(return_value=1)
    monkeypatch.setattr(runtime.assistant_service, "_require_subscription", subscription_gate)
    monkeypatch.setattr(runtime.ledger, "available_milli", wallet_balance)

    observed = []
    monkeypatch.setattr(
        runtime.enterprise_agent_team_service,
        "create_team_from_assistant",
        lambda session, **kwargs: observed.append(("create", session, kwargs)) or {"team_id": 41, "status": "queued"},
    )
    monkeypatch.setattr(
        runtime.enterprise_agent_team_service,
        "get_team_detail",
        lambda session, **kwargs: observed.append(("get", session, kwargs)) or {"team_id": kwargs["team_id"]},
    )
    monkeypatch.setattr(
        runtime.enterprise_agent_team_runtime_service,
        "cancel_team",
        lambda factory, **kwargs: observed.append(("cancel", factory, kwargs)) or {"status": "cancelled"},
    )
    monkeypatch.setattr(
        runtime.enterprise_agent_team_runtime_service,
        "retry_task",
        lambda factory, **kwargs: observed.append(("retry", factory, kwargs)) or {"status": "queued"},
    )

    cases = [
        ("create_registered_enterprise_agent_team", {"title": "只读双检"}, "team-call"),
        ("get_enterprise_agent_team", {"team_id": 41}, "get-call"),
        ("cancel_enterprise_agent_team", {"team_id": 41, "reason": "停止"}, "cancel-call"),
        ("retry_enterprise_agent_team_task", {"team_id": 41, "task_id": 51}, "retry-call"),
    ]
    results = [asyncio.run(executor.execute(_call(name, arguments, call_id))) for name, arguments, call_id in cases]

    assert [result.status for result in results] == ["success"] * 4
    assert results[0].output["progress_tool"] == "get_enterprise_agent_team"
    assert [entry[0] for entry in observed] == ["create", "get", "cancel", "retry"]
    assert observed[0][2]["enterprise_id"] == 10
    assert observed[0][2]["user_id"] == 7
    assert observed[0][2]["call_id"] == "team-call"
    subscription_gate.assert_called_once_with(db, 10)
    wallet_balance.assert_called_once_with(db, 92, lock=True)
    assert observed[1][2] == {"enterprise_id": 10, "user_id": 7, "team_id": 41, "expected_conversation_id": 20}
    assert observed[2][2]["actor_user_id"] == 7
    assert observed[3][2] == {"enterprise_id": 10, "team_id": 41, "task_id": 51, "actor_user_id": 7}


def test_registered_team_tools_cannot_be_used_by_non_initiator_or_approval_runner(monkeypatch):
    state = SimpleNamespace(initiator_user_id=7)
    executor = runtime.EnterpriseAssistantToolExecutor(
        object(),
        enterprise_id=10,
        conversation_id=20,
        run_public_id="enterprise-run",
        lease_generation=3,
        actor_user_id=8,
    )
    monkeypatch.setattr(executor, "_current", lambda: (state, {}))
    call = _call("create_registered_enterprise_agent_team", {"title": "不可代建"})

    result = asyncio.run(executor.execute(call))

    assert result.status == "error"
    assert result.failure_kind == "authorization"
    assert "原发起人" in result.error

    executor._actor_user_id = 7
    approved_result = asyncio.run(executor.execute(call, approved=True))

    assert approved_result.status == "error"
    assert approved_result.failure_kind == "authorization"
