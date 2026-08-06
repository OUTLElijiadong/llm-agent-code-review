"""管理员副驾驶确认协议和真实写入闭环测试。"""
import pytest

from app.agents.base import AgentResult
from app.agents.event_bus import AgentEventBus
from app.agents.events import AgentEventType
from app.core.exceptions import ValidationError
from app.models.agent_governance import AgentProfile, ApprovalItem, ToolCallLog
from app.models.audit_log import AuditLog
from app.models.user import User
from app.services import admin_chat_history_service, admin_copilot_service, ops_service


@pytest.fixture
def copilot_data(db, admin_user):
    target = User(username="reviewer01", password="x", role="user", status=1)
    manager = AgentProfile(code="manager", name="管理Agent", category="governance", status="idle", is_enabled=1)
    reviewer = AgentProfile(code="code_reviewer", name="代码审查Agent", category="quality", status="idle", is_enabled=1)
    db.add_all([target, manager, reviewer])
    db.commit()
    return admin_user, target, reviewer


def _message(db, admin, text, **extra):
    return admin_copilot_service.handle_message(
        db,
        admin,
        message=text,
        session_id="admin-session-001",
        **extra,
    )


def test_unconfirmed_write_only_returns_preview_and_writes_nothing(db, copilot_data):
    admin, target, _ = copilot_data
    result = _message(db, admin, f"删除用户 {target.id}")

    assert result["type"] == "danger_confirm"
    assert db.query(ApprovalItem).count() == 0
    assert db.query(ToolCallLog).count() == 0
    db.refresh(target)
    assert target.status == 1


def test_dangerous_write_requires_exact_confirmation_phrase(db, copilot_data):
    admin, target, _ = copilot_data
    preview = _message(db, admin, f"删除用户 {target.id}")

    with pytest.raises(ValidationError, match="确认执行"):
        _message(
            db,
            admin,
            "",
            action_token=preview["action_token"],
            decision="confirm",
            confirmation_text="确认",
        )

    assert db.query(ApprovalItem).count() == 0
    assert db.query(ToolCallLog).count() == 0


def test_role_change_is_idempotent_and_applies_after_chat_confirmation(db, copilot_data):
    admin, target, _ = copilot_data
    event_bus = AgentEventBus.instance()
    event_bus._history.clear()
    preview = _message(db, admin, f"把用户 {target.id} 的角色改为 reviewer")
    first = _message(
        db,
        admin,
        "",
        action_token=preview["action_token"],
        decision="confirm",
    )
    first_events = list(event_bus._history)
    second = _message(
        db,
        admin,
        "",
        action_token=preview["action_token"],
        decision="confirm",
    )

    assert first["status"] == "confirmed"
    assert second["status"] == "confirmed"
    assert db.query(ApprovalItem).count() == 0
    assert db.query(ToolCallLog).count() == 1
    call = db.query(ToolCallLog).one()
    assert call.copilot_request_id
    assert call.status == "success"
    assert [event.type for event in first_events] == [
        AgentEventType.DISPATCH,
        AgentEventType.PROGRESS,
        AgentEventType.COMPLETE,
    ]
    assert all(event.agent == "manager" and event.user_id == admin.id for event in first_events)
    assert first_events[-1].payload == {
        "operation": "user.set_role",
        "resource": f"user:{target.id}",
    }
    db.refresh(target)
    assert target.role == "reviewer"


def test_delete_and_agent_toggle_apply_after_chat_confirmation(db, copilot_data):
    admin, target, reviewer = copilot_data
    delete_preview = _message(db, admin, f"删除用户 {target.id}")
    _message(
        db,
        admin,
        "",
        action_token=delete_preview["action_token"],
        decision="confirm",
        confirmation_text="确认执行",
    )
    db.refresh(target)
    assert target.status == -1

    toggle_preview = _message(db, admin, "停用 Agent code_reviewer")
    _message(
        db,
        admin,
        "",
        action_token=toggle_preview["action_token"],
        decision="confirm",
    )
    db.refresh(reviewer)
    assert reviewer.is_enabled == 0
    assert reviewer.status == "disabled"
    assert db.query(ApprovalItem).count() == 0
    assert db.query(ToolCallLog).filter(ToolCallLog.status == "success").count() == 2


def test_query_protocol_uses_real_rows_and_all_six_types(db, copilot_data):
    admin, target, _ = copilot_data

    assert _message(db, admin, "你好")["type"] == "text"
    assert _message(db, admin, "生成日报")["type"] == "report"
    user_table = _message(db, admin, "查询用户")
    assert user_table["type"] == "table"
    assert user_table["total"] == 2
    assert _message(db, admin, f"把用户 {target.id} 的角色改为 reviewer")["type"] == "confirm"
    assert _message(db, admin, f"删除用户 {target.id}")["type"] == "danger_confirm"

    from app.services import observability_service

    observability_service.create_alert(db, alert_type="test", severity="high", title="测试开放告警")
    assert _message(db, admin, "查看异常")["type"] == "alert"


def test_operations_restart_requires_chat_confirmation_and_persists_history(db, copilot_data, monkeypatch):
    admin, _, _ = copilot_data
    calls = []

    def fake_execute(self, _db, actor, **kwargs):
        calls.append((actor.id, kwargs))
        return AgentResult(success=True, data={
            "id": 77,
            "action": kwargs["action"],
            "status": "success",
            "duration_ms": 123,
            "result": {"ok": True},
            "duplicate": False,
        })

    monkeypatch.setattr("app.agents.operations_agent.OperationsAgent.execute_action", fake_execute)
    preview = _message(db, admin, "重启后端")
    assert preview["type"] == "confirm"
    assert calls == []

    receipt = _message(
        db,
        admin,
        "",
        action_token=preview["action_token"],
        decision="confirm",
    )
    assert receipt["status"] == "confirmed"
    assert "运维记录 #77" in receipt["content"]
    assert calls[0][1]["action"] == "restart_service"
    assert calls[0][1]["params"] == {"service": "backend"}

    history = admin_chat_history_service.list_history(db, admin, "admin-session-001")
    assert [row["role"] for row in history["messages"]] == ["user", "assistant", "assistant"]
    assert history["messages"][1]["payload"]["status"] == "confirmed"


def test_manager_uses_llm_planner_and_can_delegate_enabled_agent(db, copilot_data, monkeypatch):
    admin, _, _ = copilot_data
    manager_calls = []
    delegate_calls = []

    def fake_plan(self, _db, _admin, **kwargs):
        manager_calls.append(kwargs)
        return AgentResult(success=True, data={"mode": "answer", "answer": "来自 DeepSeek 管理 Agent 的结论"})

    def fake_run(self, _db, _admin, **kwargs):
        delegate_calls.append((self.name, kwargs))
        return AgentResult(success=True, data="代码审查 Agent 已分析")

    monkeypatch.setattr("app.agents.admin_copilot_agent.AdminCopilotAgent.plan", fake_plan)
    monkeypatch.setattr("app.agents.admin_copilot_agent.DelegatedAdminAgent.run", fake_run)

    answer = _message(db, admin, "请解释当前治理机制")
    assert answer["content"] == "来自 DeepSeek 管理 Agent 的结论"
    assert manager_calls and manager_calls[0]["agents"]

    delegated = _message(db, admin, "调用 code_reviewer 分析当前平台状态")
    assert delegated["content"] == "代码审查 Agent 已分析"
    assert delegate_calls[0][0] == "code_reviewer"

    risk_delegated = _message(db, admin, "调用 code_reviewer 指出一项治理风险")
    assert risk_delegated["content"] == "代码审查 Agent 已分析"
    assert delegate_calls[1][0] == "code_reviewer"


def test_scheduler_operations_are_written_to_system_audit(db, monkeypatch):
    monkeypatch.setattr(
        ops_service,
        "_call_executor",
        lambda action, params, request_id: {"ok": True, "action": action, "result": {"checks": {"status": "ok"}}},
    )

    result = ops_service.execute(db, None, action="status", source="scheduler")

    assert result["status"] == "success"
    audit = db.query(AuditLog).filter(AuditLog.action == "admin_copilot.ops.status").one()
    assert audit.actor_id is None
    assert audit.status == "success"
    assert "source=scheduler" in audit.detail
