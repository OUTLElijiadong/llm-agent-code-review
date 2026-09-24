"""管理员副驾驶确认协议和真实写入闭环测试。"""
import json

import pytest

from app.agents.admin_copilot_agent import AdminCopilotAgent
from app.agents.base import AgentResult
from app.agents.event_bus import AgentEventBus
from app.agents.events import AgentEventType
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


def test_ops_failure_receipt_preserves_blocking_checks() -> None:
    receipt = admin_copilot_service._ops_execution_receipt({
        "id": 17,
        "action": "status",
        "status": "failed",
        "duration_ms": 42,
        "error": "存在阻断性生产故障，已停止自动继续",
        "result": {
            "result": {
                "checks": {"blocking_checks": ["https", "backup"]},
            },
        },
    })
    assert receipt["status"] == "failed"
    assert "阻断检查：HTTPS 入口（https）、备份链路（backup）" in receipt["content"]


def test_ops_failure_receipt_does_not_invent_missing_details() -> None:
    receipt = admin_copilot_service._ops_execution_receipt({
        "id": 18,
        "action": "status",
        "status": "failed",
        "duration_ms": 42,
        "error": "生产关键检查失败",
        "result": {"result": {"checks": {"blocking_checks": []}}},
    })
    assert receipt["content"].endswith("原因：生产关键检查失败。")


@pytest.mark.parametrize("has_data", [True, False])
def test_status_failure_reaches_conversation_and_history(db, copilot_data, monkeypatch, has_data) -> None:
    admin, _, _ = copilot_data
    payload = {
        "id": 19,
        "action": "status",
        "status": "failed",
        "error": "生产关键检查失败",
        "result": {"result": {"checks": {"blocking_checks": ["https"]}, "can_continue": False}},
    }
    monkeypatch.setattr(
        "app.agents.operations_agent.OperationsAgent.execute_action",
        lambda *_args, **_kwargs: AgentResult(
            success=False,
            data=payload if has_data else None,
            error="生产关键检查失败",
        ),
    )

    receipt = _message(db, admin, "查看服务器状态")

    assert receipt["status"] == "failed"
    assert "生产关键检查失败" in receipt["content"]
    if has_data:
        assert "运维记录 #19" in receipt["content"]
        assert "HTTPS 入口（https）" in receipt["content"]
    else:
        assert "阻断检查" not in receipt["content"]
    history = admin_chat_history_service.list_history(db, admin, "admin-session-001")
    assert history["messages"][-1]["payload"]["status"] == "failed"
    assert history["messages"][-1]["payload"]["content"] == receipt["content"]


@pytest.mark.parametrize("failure_kind", ["invalid_json", "output_truncated"])
def test_manager_structured_failure_retries_with_distinct_compact_prompt(
    db,
    admin_user,
    monkeypatch,
    failure_kind,
):
    from app.utils.api_resolver import ApiConfig

    agent = AdminCopilotAgent()
    prompts = []

    def fake_call_json(prompt, *_args, **_kwargs):
        prompts.append(prompt)
        if len(prompts) == 1:
            return AgentResult(
                success=False,
                error="结构化输出不完整",
                failure_kind=failure_kind,
            )
        return AgentResult(
            success=True,
            data={"mode": "answer", "answer": "已恢复", "agent_code": "", "task": ""},
        )

    monkeypatch.setattr(agent, "call_json", fake_call_json)
    monkeypatch.setattr(agent, "_log_call", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "app.agents.admin_copilot_agent.resolve_api_config",
        lambda *_args, **_kwargs: ApiConfig(api_key="test", base_url="https://example.invalid", model="test"),
    )

    result = agent.plan(
        db,
        admin_user,
        message="总结当前状态",
        history=[{"role": "user", "content": "历史上下文" * 500}],
        snapshot={"users": 18},
        agents=[{"code": "code_reviewer"}],
        trace_id="trace-compact-retry",
    )

    assert result.success is True
    assert len(prompts) == 2
    assert prompts[0] != prompts[1]
    assert json.loads(prompts[1])["管理员问题"] == "总结当前状态"
    assert "历史上下文" in prompts[1]


def test_manager_compacts_all_old_history_sources_without_dropping_recent_text(
    db, admin_user, monkeypatch,
):
    import re

    from app.utils.api_resolver import ApiConfig

    agent = AdminCopilotAgent()
    prompts = []

    def fake_call_json(prompt, *_args, **_kwargs):
        prompts.append(prompt)
        data = json.loads(prompt)
        if "来源" in data:
            refs = re.findall(r"\[(来源#.+?:片段\d+/\d+)\]", data["来源"])
            return AgentResult(success=True, data={"summary": "来源事实均已归纳", "covered_refs": refs})
        return AgentResult(success=True, data={"mode": "answer", "answer": "完成", "agent_code": "", "task": ""})

    monkeypatch.setattr(agent, "call_json", fake_call_json)
    monkeypatch.setattr(agent, "_log_call", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "app.agents.admin_copilot_agent.resolve_api_config",
        lambda *_args, **_kwargs: ApiConfig(api_key="test", base_url="https://example.invalid", model="test"),
    )
    history = [
        {"source_id": str(index), "role": "user", "content": f"第{index}条约束:" + ("细节" * 1_000)}
        for index in range(20)
    ]
    result = agent.plan(
        db, admin_user, message="按全部历史处理", history=history,
        snapshot={"users": 18}, agents=[], trace_id="trace-full-history",
    )
    assert result.success
    final = json.loads(prompts[-1])
    context = final["对话上下文"]
    assert len(context["最近原文"]) == 4
    assert "第19条约束" in str(context["最近原文"])
    refs = [ref for group in context["压缩历史"] for ref in group["covered_refs"]]
    assert {f"来源#{index}:片段1/1" for index in range(16)} == set(refs)


def test_admin_history_context_reads_all_persisted_messages_for_own_session(db, admin_user):
    session = admin_chat_history_service.get_or_create_session(db, admin_user, "long-admin-history")
    for index in range(15):
        admin_chat_history_service.append_user_text(
            db, session, f"历史约束 {index} " + ("末尾" * 1_100),
        )
    history = admin_chat_history_service.recent_context(db, session)
    assert len(history) == 15
    assert "历史约束 0" in history[0]["content"]
    assert history[0]["content"].endswith("末尾")
    assert len(history[0]["content"]) > 2_000
    assert "历史约束 14" in history[-1]["content"]


def test_manager_rejects_compaction_with_missing_source(db, admin_user, monkeypatch):
    from app.utils.api_resolver import ApiConfig

    agent = AdminCopilotAgent()
    called = []

    def fake_call_json(prompt, *_args, **_kwargs):
        called.append(json.loads(prompt))
        return AgentResult(success=True, data={"summary": "只提取首段", "covered_refs": ["来源#0:片段1/1"]})

    monkeypatch.setattr(agent, "call_json", fake_call_json)
    monkeypatch.setattr(
        "app.agents.admin_copilot_agent.resolve_api_config",
        lambda *_args, **_kwargs: ApiConfig(api_key="test", base_url="https://example.invalid", model="test"),
    )
    history = [
        {"source_id": str(index), "role": "user", "content": "重要约束" * 1_000}
        for index in range(20)
    ]
    result = agent.plan(
        db, admin_user, message="按照全部约束", history=history,
        snapshot={}, agents=[], trace_id="trace-missing-source",
    )
    assert result.success is False
    assert result.failure_kind == "context_compaction_incomplete"
    assert all("来源" in prompt for prompt in called)


def test_unconfirmed_write_only_returns_preview_and_writes_nothing(db, copilot_data):
    admin, target, _ = copilot_data
    result = _message(db, admin, f"删除用户 {target.id}")

    assert result["type"] == "danger_confirm"
    assert db.query(ApprovalItem).count() == 0
    assert db.query(ToolCallLog).count() == 0
    db.refresh(target)
    assert target.status == 1


def test_dangerous_write_confirms_by_button_without_confirmation_text(db, copilot_data):
    admin, target, _ = copilot_data
    preview = _message(db, admin, f"删除用户 {target.id}")

    assert preview["type"] == "danger_confirm"
    result = _message(
        db,
        admin,
        "",
        action_token=preview["action_token"],
        decision="confirm",
    )

    assert result["status"] == "confirmed"
    db.refresh(target)
    assert target.status == -1
    assert db.query(ApprovalItem).count() == 0
    assert db.query(ToolCallLog).filter(ToolCallLog.status == "success").count() == 1


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
