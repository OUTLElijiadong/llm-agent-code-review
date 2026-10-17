"""Responses 管理工具的批量原子性、发布审批与自定义 Agent 委派回归。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.agents.discussion_bus import DiscussionBus
from app.agents.events import DiscussionTurn
from app.models.agent_governance import ApprovalItem, ToolCallLog
from app.models.agent_mesh import AgentMeshMessage
from app.models.agent_response_run import AgentResponseRun
from app.models.audit_log import AuditLog
from app.models.custom_agent import CustomAgentRelease, CustomAgentSkillBinding, CustomAgentVersion
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.user import User
from app.services import admin_agent_tools, agent_studio_service, published_agent_tools
from app.services import agent_responses_service as service_module
from app.services.agent_responses_service import PrismToolExecutor
from app.services.deepseek_responses_runtime import ToolCall


class EmptyMcp:
    async def discover(self) -> list[dict[str, Any]]:
        return []

    def has_tool(self, _: str) -> bool:
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["cancelled", "failed", "partial"])
async def test_roundtable_terminal_outcomes_cannot_start_continuations(
    db,
    admin_user,
    monkeypatch,
    terminal_status: str,
) -> None:
    """只有成功结论可续会，取消/失败/部分结果不应因自动评价生成新圆桌。"""
    # 本用例验证终态续会策略；项目范围/RBAC 由专门测试覆盖。
    monkeypatch.setattr(
        "app.ai.discussion_orchestrator._require_discussion_actor",
        lambda *_args, **_kwargs: None,
    )
    bus = DiscussionBus()
    session = bus.create_session(
        f"roundtable-{terminal_status}", 51, "terminal.py",
        owner_user_id=admin_user.id, project_id=51,
    )
    session.status = (
        "cancelled"
        if terminal_status == "cancelled"
        else "failed"
        if terminal_status == "failed"
        else "concluded"
    )
    session.terminal_status = terminal_status
    session.turns.append(DiscussionTurn(1, "xiaoling", "小菱（主持人）", "agent", "结论"))
    monkeypatch.setattr(DiscussionBus, "instance", lambda: bus)
    executor = _executor(db, admin_user, f"run_{terminal_status}")
    executor._continue_roundtable_discussion = AsyncMock()

    result = await executor._control_roundtable_discussion(
        ToolCall(
            "continue-terminal",
            "control_roundtable_discussion",
            {"session_id": session.session_id, "action": "continue", "content": "请继续核验"},
            "{}",
        )
    )

    assert result.status == "error"
    assert "成功结论" in result.error
    executor._continue_roundtable_discussion.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_roundtable_continues_only_from_explicit_continue_action(
    db,
    admin_user,
    monkeypatch,
) -> None:
    # 本用例验证“新用户明确要求”门槛；项目范围/RBAC 由专门测试覆盖。
    monkeypatch.setattr(
        "app.ai.discussion_orchestrator._require_discussion_actor",
        lambda *_args, **_kwargs: None,
    )
    bus = DiscussionBus()
    session = bus.create_session(
        "roundtable-success", 52, "success.py", owner_user_id=admin_user.id, project_id=52,
    )
    session.status = "concluded"
    session.terminal_status = "success"
    session.turns.append(DiscussionTurn(1, "xiaoling", "小菱（主持人）", "agent", "结论"))
    monkeypatch.setattr(DiscussionBus, "instance", lambda: bus)
    session_key = "roundtable-followup-chat"
    db.add(AgentResponseRun(
        run_id="run_explicit_followup",
        user_id=admin_user.id,
        surface="admin",
        session_key=session_key,
        status="running",
        checkpoint_json=json.dumps({"transcript": [{"role": "user", "content": "看看上一轮结论"}]}),
        version=1,
    ))
    db.commit()
    executor = _executor(db, admin_user, "run_explicit_followup", session_key=session_key)
    executor._continue_roundtable_discussion = AsyncMock(
        return_value=service_module.ToolExecutionResult.success({"session_id": "continued"})
    )

    # 旧的自动评价路径使用 user_input；它不能再隐式创建续会。
    automatic = await executor._control_roundtable_discussion(
        ToolCall(
            "auto-evaluate",
            "control_roundtable_discussion",
            {"session_id": session.session_id, "action": "user_input", "content": "结论是否满足要求"},
            "{}",
        )
    )
    assert automatic.status == "error"
    executor._continue_roundtable_discussion.assert_not_awaited()

    monkeypatch.setattr(
        service_module,
        "_is_automatic_task_result_for_roundtable",
        lambda *_args, **_kwargs: True,
        raising=False,
    )
    auto_continue = await executor._control_roundtable_discussion(
        ToolCall(
            "auto-task-result-continue",
            "control_roundtable_discussion",
            {"session_id": session.session_id, "action": "continue", "content": "自动评价建议继续"},
            "{}",
        )
    )
    assert auto_continue.status == "error"
    executor._continue_roundtable_discussion.assert_not_awaited()

    monkeypatch.setattr(
        service_module,
        "_is_automatic_task_result_for_roundtable",
        lambda *_args, **_kwargs: False,
        raising=False,
    )
    not_explicit = await executor._control_roundtable_discussion(
        ToolCall(
            "non-explicit-continue",
            "control_roundtable_discussion",
            {"session_id": session.session_id, "action": "continue", "content": "继续检查"},
            "{}",
        )
    )
    assert not_explicit.status == "error"
    assert "新的用户消息明确要求继续" in not_explicit.error
    executor._continue_roundtable_discussion.assert_not_awaited()

    run = db.query(AgentResponseRun).filter_by(run_id="run_explicit_followup").one()
    run.checkpoint_json = json.dumps({
        "transcript": [{"role": "user", "content": "请基于成功结论继续复核"}],
    })
    db.commit()
    explicit = await executor._control_roundtable_discussion(
        ToolCall(
            "user-continue",
            "control_roundtable_discussion",
            {"session_id": session.session_id, "action": "continue", "content": "请基于成功结论继续复核"},
            "{}",
        )
    )
    assert explicit.status == "success"
    executor._continue_roundtable_discussion.assert_awaited_once_with(session, "请基于成功结论继续复核")


@pytest.mark.asyncio
async def test_task_result_run_is_identified_as_automatic_roundtable_continuation(db, admin_user) -> None:
    """圆桌 task.result 正在自动回投时，服务器能识别该自动运行来源。"""
    from app.services.agent_responses_service import _is_automatic_task_result_for_roundtable

    session_key = "roundtable-result-session"
    session_id = "discussion-result-123"
    db.add(AgentResponseRun(
        run_id="automatic-result-run",
        user_id=admin_user.id,
        surface="admin",
        session_key=session_key,
        status="running",
        checkpoint_json="{}",
        version=1,
    ))
    db.add(AgentMeshMessage(
        message_id="mesh-result-123",
        user_id=admin_user.id,
        idempotency_key="discussion-result:discussion-result-123",
        trace_id=session_id,
        sent_from="agent:orchestrator",
        send_to=f"session:admin:{session_key}",
        message_type="task.result",
        subject="圆桌讨论结果",
        payload_json=json.dumps({"discussion_session_id": session_id}),
        status="processing",
    ))
    db.commit()

    assert _is_automatic_task_result_for_roundtable(
        db,
        user_id=admin_user.id,
        surface="admin",
        session_key=session_key,
        session_id=session_id,
    ) is True
    assert _is_automatic_task_result_for_roundtable(
        db,
        user_id=admin_user.id,
        surface="admin",
        session_key=session_key,
        session_id="another-session",
    ) is False
    assert _is_automatic_task_result_for_roundtable(
        db,
        user_id=admin_user.id,
        surface="admin",
        session_key=session_key,
        session_id="",
        run_id="automatic-result-run",
    ) is True

    executor = _executor(db, admin_user, "automatic-result-run", session_key=session_key)
    start = await executor._start_roundtable_discussion(ToolCall(
        "auto-start-roundtable",
        "start_roundtable_discussion",
        {},
        "{}",
    ))
    assert start.status == "error"
    assert "task.result 只用于汇报" in start.error


@pytest.mark.asyncio
async def test_roundtable_tool_launches_without_waiting_for_panel_websocket(
    db,
    admin_user,
    monkeypatch,
) -> None:
    """小菱工具启动后立即领取 pending 并启动后台编排，不依赖面板 WebSocket。"""
    pending = object()
    launches: list[object] = []
    monkeypatch.setattr(service_module, "_is_automatic_task_result_for_roundtable", lambda *_a, **_k: False)
    monkeypatch.setattr(
        "app.api.v1.discussion.start_discussion",
        lambda **_kwargs: SimpleNamespace(data={
            "session_id": "discussion-from-agent",
            "ws_url": "/api/ws/discuss/discussion-from-agent",
            "agents": [{"id": "reviewer"}],
            "file_name": "service.py",
        }),
    )
    monkeypatch.setattr("app.api.v1.ws_discussion.take_pending", lambda session_id: pending)
    monkeypatch.setattr("app.api.v1.ws_discussion.launch_pending_discussion", launches.append)

    executor = _executor(db, admin_user, "run_start_roundtable_without_panel")
    result = await executor._start_roundtable_discussion(ToolCall(
        "start-without-panel",
        "start_roundtable_discussion",
        {"project_id": 10, "file_id": 20, "review_type": "full"},
        "{}",
    ))

    assert result.status == "success"
    assert result.output["started_by"] == "user_agent"
    assert result.output["session_id"] == "discussion-from-agent"
    assert launches == [pending]


@pytest.fixture(autouse=True)
def lightweight_orchestrator(monkeypatch):
    monkeypatch.setattr(
        service_module,
        "get_request_orchestrator",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )


def _executor(
    db,
    user: User,
    run_id: str,
    events: list[dict[str, Any]] | None = None,
    *,
    session_key: str = "",
) -> PrismToolExecutor:
    async def sink(event):
        if events is not None:
            events.append(dict(event))

    return PrismToolExecutor(
        db,
        user,
        surface="admin" if user.role in {"admin", "super_admin"} else "user",
        run_id=run_id,
        mcp_provider=EmptyMcp(),
        event_sink=sink,
        session_key=session_key,
    )


def _users_through_id(db, target_id: int) -> None:
    next_id = (db.query(User.id).order_by(User.id.desc()).first() or (0,))[0] + 1
    db.add_all([
        User(username=f"user_{user_id}", password="x", role="user", status=1)
        for user_id in range(next_id, target_id + 1)
    ])
    db.commit()


@pytest.mark.asyncio
async def test_delete_ids_26_through_69_uses_one_approval_and_one_atomic_write(db, admin_user) -> None:
    _users_through_id(db, 75)
    events: list[dict[str, Any]] = []
    executor = _executor(db, admin_user, "run_delete_26_69", events)
    user_ids = list(range(26, 70))
    call = ToolCall(
        call_id="call_delete_26_69",
        name="admin_delete_users",
        arguments={"user_ids": user_ids},
        raw_arguments="",
    )

    paused = await executor.execute(call)

    assert paused.status == "approval_required"
    assert paused.danger is True
    assert paused.preview["count"] == 44
    assert paused.preview["user_ids"] == user_ids
    assert db.query(ApprovalItem).filter(ApprovalItem.action == "responses.admin_delete_users").count() == 1
    assert db.query(User).filter(User.id.in_(user_ids), User.status == 1).count() == 44

    completed = await executor.execute(call, approved=True)

    assert completed.status == "success"
    assert completed.output["deleted_count"] == 44
    assert db.query(User).filter(User.id.in_(user_ids), User.status == -1).count() == 44
    assert db.query(ToolCallLog).filter(ToolCallLog.action == "user.delete_batch").count() == 1
    assert db.query(AuditLog).filter(AuditLog.action == "admin_copilot.delete_user").count() == 44
    repeated = await executor.execute(call, approved=True)
    assert repeated.output["deleted_count"] == 44
    assert db.query(ToolCallLog).filter(ToolCallLog.action == "user.delete_batch").count() == 1
    assert [event["type"] for event in events[-2:]] == [
        "response.tool.started",
        "response.tool.completed",
    ]
    assert events[-1]["cached"] is True


@pytest.mark.asyncio
async def test_batch_delete_blocks_changed_snapshot_without_partial_mutation(db, admin_user) -> None:
    _users_through_id(db, 4)
    executor = _executor(db, admin_user, "run_snapshot_change")
    call = ToolCall("call_snapshot", "admin_delete_users", {"user_ids": [2, 3, 4]}, "")
    paused = await executor.execute(call)
    assert paused.status == "approval_required"

    target = db.get(User, 3)
    target.nickname = "审批后发生变化"
    db.commit()
    failed = await executor.execute(call, approved=True)

    assert failed.status == "error"
    assert "确认后已变化" in failed.error
    assert db.query(User).filter(User.id.in_([2, 3, 4]), User.status == 1).count() == 3
    assert db.query(ToolCallLog).filter(ToolCallLog.action == "user.delete_batch").count() == 0


def test_batch_delete_rolls_back_every_target_when_one_step_fails(db, admin_user, monkeypatch) -> None:
    _users_through_id(db, 3)
    preview = admin_agent_tools.preview_delete_users(db, admin_user, [2, 3])
    assert preview.success is True

    monkeypatch.setattr(
        admin_agent_tools.audit_service,
        "log",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("audit failed")),
    )
    result = admin_agent_tools.admin_delete_users(
        db,
        admin_user,
        [2, 3],
        expected_snapshot=preview.data["target_snapshot"],
        context={"copilot_request_id": "batch-rollback-request"},
    )

    assert result.success is False
    assert db.query(User).filter(User.id.in_([2, 3]), User.status == 1).count() == 2
    assert db.query(ToolCallLog).filter(ToolCallLog.copilot_request_id == "batch-rollback-request").count() == 0


def test_batch_delete_protects_current_and_rejects_role_identity_drift(db, admin_user) -> None:
    self_preview = admin_agent_tools.preview_delete_users(db, admin_user, [admin_user.id])
    assert self_preview.success is False
    assert "当前登录" in self_preview.error

    rbac_admin = User(username="rbac_admin", password="x", role="user", status=1)
    only_legacy_admin = User(username="only_legacy_admin", password="x", role="admin", status=1)
    role = Role(name="管理员", code="admin", status="active", sort=1, is_builtin=1)
    db.add_all([rbac_admin, only_legacy_admin, role])
    db.flush()
    db.add(UserRole(user_id=rbac_admin.id, role_id=role.id))
    admin_user.status = -1
    db.commit()

    last_admin_preview = admin_agent_tools.preview_delete_users(db, rbac_admin, [only_legacy_admin.id])
    assert last_admin_preview.success is False
    assert "仅管理员" in last_admin_preview.error


def _submitted_agent(db, owner: User, suffix: str):
    agent, version = agent_studio_service.create_agent(
        db,
        owner,
        code=f"responses_agent_{suffix}",
        name=f"Responses Agent {suffix}",
        description="安全、可靠性与错误处理代码审查",
        prompt="Inspect the supplied code and return evidence-backed issues only.",
        review_focus="Security, reliability, and error handling",
        model_config={"temperature": 0.1},
    )
    agent_studio_service.test_agent_version(db, owner, version.id, {"issues": []})
    approval = agent_studio_service.submit_agent_version(db, owner, version.id, "ready")
    return agent, version, approval


def test_admin_release_tool_rejects_mismatched_approval_target(db, admin_user) -> None:
    reviewer = User(username="release_target_reviewer", password="x", role="reviewer", status=1)
    db.add(reviewer)
    db.commit()
    _, version, approval = _submitted_agent(db, reviewer, "wrong_target")
    approval.resource = "custom_agent_version:999999"
    db.commit()

    preview = admin_agent_tools.preview_agent_release_decision(
        db, admin_user, approval_id=approval.id, decision="approve"
    )
    result = admin_agent_tools.admin_decide_agent_release(
        db,
        admin_user,
        approval_id=approval.id,
        decision="approve",
        expected_snapshot=admin_agent_tools._release_target_snapshot(db, approval),
    )

    assert preview.success is False
    assert result.success is False
    assert db.get(ApprovalItem, approval.id).status == "pending"
    assert db.get(CustomAgentVersion, version.id).status == "pending_approval"
    assert db.query(CustomAgentRelease).count() == 0


@pytest.mark.asyncio
async def test_release_detail_and_approve_reject_are_responses_approved(db, admin_user) -> None:
    reviewer = User(username="release_reviewer", password="x", role="reviewer", status=1)
    db.add(reviewer)
    db.commit()
    _, version, release_approval = _submitted_agent(db, reviewer, "approve")
    executor = _executor(db, admin_user, "run_release_approve")

    detail_call = ToolCall(
        "call_release_detail",
        "admin_list_agent_release_approvals",
        {"approval_id": release_approval.id},
        "",
    )
    detail = await executor.execute(detail_call)
    assert detail.output[0]["current_authoring"]["prompt"] == version.prompt
    assert detail.output[0]["test_evidence"]
    assert "dependencies" in detail.output[0]

    approve_call = ToolCall(
        "call_release_approve",
        "admin_decide_agent_release",
        {"approval_id": release_approval.id, "decision": "approve", "note": "符合内测要求"},
        "",
    )
    paused = await executor.execute(approve_call)
    assert paused.status == "approval_required"
    assert paused.preview["approval"]["current_authoring"]["prompt"] == version.prompt
    approved = await executor.execute(approve_call, approved=True)
    assert approved.output["status"] == "approved"
    assert db.query(CustomAgentRelease).filter(CustomAgentRelease.approval_id == release_approval.id).count() == 1

    _, rejected_version, reject_approval = _submitted_agent(db, reviewer, "reject")
    reject_executor = _executor(db, admin_user, "run_release_reject")
    reject_call = ToolCall(
        "call_release_reject",
        "admin_decide_agent_release",
        {"approval_id": reject_approval.id, "decision": "reject", "note": "证据不足"},
        "",
    )
    assert (await reject_executor.execute(reject_call)).status == "approval_required"
    rejected = await reject_executor.execute(reject_call, approved=True)
    assert rejected.output["status"] == "rejected"
    assert db.get(CustomAgentVersion, rejected_version.id).status == "rejected"


@pytest.mark.asyncio
async def test_release_approval_blocks_binding_change_after_preview(db, admin_user) -> None:
    reviewer = User(username="release_snapshot_reviewer", password="x", role="reviewer", status=1)
    db.add(reviewer)
    db.commit()
    _, version = agent_studio_service.create_agent(
        db,
        reviewer,
        code="responses_agent_binding_snapshot",
        name="Responses Agent Binding Snapshot",
        description="快照完整性回归",
        prompt="Inspect code.",
        review_focus="Reliability",
        model_config={"temperature": 0.1},
    )
    _, skill_version = agent_studio_service.create_skill(
        db,
        reviewer,
        code="responses_skill_binding_snapshot",
        name="Responses Skill Binding Snapshot",
        description="快照完整性回归",
        skill_type="llm_transform",
        definition={"prompt": "Summarize findings."},
        requested_capabilities=[],
    )
    binding = agent_studio_service.bind_skill(
        db,
        reviewer,
        version.id,
        skill_version_id=skill_version.id,
        position=1,
        config={"mode": "approved"},
    )
    agent_studio_service.test_agent_version(db, reviewer, version.id, {"issues": []})
    release_approval = agent_studio_service.submit_agent_version(db, reviewer, version.id, "ready")
    executor = _executor(db, admin_user, "run_release_binding_snapshot")
    call = ToolCall(
        "call_release_binding_snapshot",
        "admin_decide_agent_release",
        {"approval_id": release_approval.id, "decision": "approve", "note": "批准原配置"},
        "",
    )

    paused = await executor.execute(call)
    assert paused.status == "approval_required"
    assert paused.preview["approval"]["dependencies"][0]["config"] == {"mode": "approved"}

    changed_binding = db.get(CustomAgentSkillBinding, binding.id)
    changed_binding.config_json = '{"mode":"changed-after-preview"}'
    db.commit()

    blocked = await executor.execute(call, approved=True)
    assert blocked.status == "error"
    assert "确认后已变化" in blocked.error
    assert db.get(ApprovalItem, release_approval.id).status == "pending"
    assert db.query(CustomAgentRelease).filter(CustomAgentRelease.approval_id == release_approval.id).count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    [
        ("definition_json", '{"prompt":"Changed transitive behavior after preview."}'),
        ("test_evidence_json", '{"checks":["changed-after-preview"],"passed":true}'),
    ],
)
async def test_release_approval_blocks_transitive_skill_change_after_preview(
    db,
    admin_user,
    changed_field: str,
    changed_value: str,
) -> None:
    reviewer = User(
        username=f"release_transitive_{changed_field}",
        password="x",
        role="reviewer",
        status=1,
    )
    db.add(reviewer)
    db.commit()
    _, leaf_version = agent_studio_service.create_skill(
        db,
        reviewer,
        code=f"responses_leaf_{changed_field}",
        name="Responses Transitive Leaf",
        description="A published workflow dependency",
        skill_type="llm_transform",
        definition={"prompt": "Original transitive behavior."},
        requested_capabilities=[],
    )
    leaf_version.status = "published"
    leaf_version.tested_checksum = leaf_version.checksum
    leaf_version.test_evidence_json = '{"checks":["original"],"passed":true}'
    db.commit()

    _, workflow_version = agent_studio_service.create_skill(
        db,
        reviewer,
        code=f"responses_workflow_{changed_field}",
        name="Responses Workflow Root",
        description="References an exact published Skill version",
        skill_type="sequence_workflow",
        definition={"steps": [{"skill_version_id": leaf_version.id}]},
        requested_capabilities=[],
    )
    _, version = agent_studio_service.create_agent(
        db,
        reviewer,
        code=f"responses_transitive_agent_{changed_field}",
        name="Responses Transitive Agent",
        description="Snapshot closure regression",
        prompt="Inspect code.",
        review_focus="Reliability",
        model_config={"temperature": 0.1},
    )
    agent_studio_service.bind_skill(
        db,
        reviewer,
        version.id,
        skill_version_id=workflow_version.id,
        position=0,
        config={"mode": "workflow"},
    )
    agent_studio_service.test_agent_version(db, reviewer, version.id, {"issues": []})
    release_approval = agent_studio_service.submit_agent_version(db, reviewer, version.id, "ready")
    executor = _executor(db, admin_user, f"run_transitive_{changed_field}")
    call = ToolCall(
        f"call_transitive_{changed_field}",
        "admin_decide_agent_release",
        {"approval_id": release_approval.id, "decision": "approve", "note": "批准原依赖"},
        "",
    )

    paused = await executor.execute(call)
    assert paused.status == "approval_required"
    setattr(db.get(type(leaf_version), leaf_version.id), changed_field, changed_value)
    db.commit()

    blocked = await executor.execute(call, approved=True)
    assert blocked.status == "error"
    assert "确认后已变化" in blocked.error
    assert db.get(ApprovalItem, release_approval.id).status == "pending"
    assert db.query(CustomAgentRelease).filter(CustomAgentRelease.approval_id == release_approval.id).count() == 0


@pytest.mark.asyncio
async def test_release_approval_blocks_delegate_target_change_after_preview(db, admin_user) -> None:
    reviewer = User(username="release_delegate_snapshot", password="x", role="reviewer", status=1)
    db.add(reviewer)
    db.commit()
    delegate_target, delegate_version = agent_studio_service.create_agent(
        db,
        reviewer,
        code="responses_delegate_snapshot_target",
        name="Responses Delegate Target",
        description="Runtime delegate target",
        prompt="Original delegated review instructions.",
        review_focus="Original delegated focus",
        model_config={"temperature": 0.1},
    )
    delegate_target.current_published_version_id = delegate_version.id
    delegate_target.status = "published"
    delegate_target.is_enabled = 1
    delegate_version.status = "published"
    db.commit()
    _, delegate_skill_version = agent_studio_service.create_skill(
        db,
        reviewer,
        code="responses_delegate_snapshot_skill",
        name="Responses Delegate Skill",
        description="Delegates to a published custom Agent",
        skill_type="agent_delegate",
        definition={"agent_code": delegate_target.code, "max_depth": 1},
        requested_capabilities=[],
    )
    _, version = agent_studio_service.create_agent(
        db,
        reviewer,
        code="responses_delegate_snapshot_source",
        name="Responses Delegate Source",
        description="Delegate target snapshot regression",
        prompt="Inspect code.",
        review_focus="Reliability",
        model_config={"temperature": 0.1},
    )
    agent_studio_service.bind_skill(
        db,
        reviewer,
        version.id,
        skill_version_id=delegate_skill_version.id,
        position=0,
        config={"mode": "delegate"},
    )
    agent_studio_service.test_agent_version(db, reviewer, version.id, {"issues": []})
    release_approval = agent_studio_service.submit_agent_version(db, reviewer, version.id, "ready")
    executor = _executor(db, admin_user, "run_delegate_target_snapshot")
    call = ToolCall(
        "call_delegate_target_snapshot",
        "admin_decide_agent_release",
        {"approval_id": release_approval.id, "decision": "approve", "note": "批准原委派目标"},
        "",
    )

    paused = await executor.execute(call)
    assert paused.status == "approval_required"
    delegate_version.prompt = "Changed delegated review instructions after preview."
    db.commit()

    blocked = await executor.execute(call, approved=True)
    assert blocked.status == "error"
    assert "确认后已变化" in blocked.error
    assert db.get(ApprovalItem, release_approval.id).status == "pending"
    assert db.query(CustomAgentRelease).filter(CustomAgentRelease.approval_id == release_approval.id).count() == 0


def _grant_custom_agent_invoke(db, user: User) -> None:
    permission = Permission(
        code="custom_agent:invoke",
        name="调用自定义 Agent",
        module="agent",
        type="api",
    )
    role = Role(name="普通用户", code="user", status="active", sort=20, is_builtin=1)
    db.add_all([permission, role])
    db.flush()
    db.add_all([
        RolePermission(role_id=role.id, permission_id=permission.id),
        UserRole(user_id=user.id, role_id=role.id),
    ])
    db.commit()


@pytest.mark.asyncio
async def test_normal_chatagent_can_fuzzy_search_and_delegate_published_agent(db, admin_user, monkeypatch) -> None:
    reviewer = User(username="delegate_reviewer", password="x", role="reviewer", status=1)
    member = User(username="delegate_member", password="x", role="user", status=1)
    db.add_all([reviewer, member])
    db.commit()
    agent, _, approval = _submitted_agent(db, reviewer, "delegate")
    from app.services import approval_service

    approval_service.decide_item(db, admin_user, approval.id, approve=True)
    _grant_custom_agent_invoke(db, member)
    executor = _executor(db, member, "run_delegate")
    schemas = await executor.tool_schemas()
    names = {schema["name"] for schema in schemas}
    assert {"search_published_agents", "invoke_published_agent"} <= names

    search = await executor.execute(
        ToolCall("call_search", "search_published_agents", {"query": "可靠代码检查"}, "")
    )
    assert search.status == "success"
    assert search.output["requires_clarification"] is True
    assert search.output["candidates"][0]["code"] == agent.code

    calls: list[dict[str, Any]] = []

    def fake_invoke(_db, _user, **kwargs):
        calls.append(kwargs)
        return {"agent_code": kwargs["agent_code"], "summary": "委派完成", "issues": []}

    monkeypatch.setattr(published_agent_tools, "invoke_published_agent", fake_invoke)
    invoked = await executor.execute(
        ToolCall(
            "call_delegate",
            "invoke_published_agent",
            {"agent_code": agent.code, "code": "print(1)", "language": "python"},
            "",
        )
    )
    assert invoked.output["summary"] == "委派完成"
    assert calls[0]["agent_code"] == agent.code


@pytest.mark.asyncio
async def test_tool_sse_lifecycle_redacts_sensitive_arguments(db, super_admin_user) -> None:
    class SecretMcp(EmptyMcp):
        def has_tool(self, name: str) -> bool:
            return name == "mcp_secret_tool"

        def supervisor_details(self, _name: str) -> dict[str, str]:
            return {
                "managed_kind": "",
                "tool_name": "secret_tool",
                "permission": "escalate",
                "requires_approval": "true",
                "risk_level": "high",
                "binding_fingerprint": "external-secret-tool-v1",
            }

        async def call(self, _name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True, "authorization": "must-not-leak"}

    events: list[dict[str, Any]] = []

    async def sink(event):
        events.append(dict(event))

    executor = PrismToolExecutor(
        db,
        super_admin_user,
        surface="admin",
        run_id="run_redaction",
        mcp_provider=SecretMcp(),
        event_sink=sink,
    )
    call = ToolCall(
        "call_secret",
        "mcp_secret_tool",
        {"api_key": "must-not-leak", "payload": "x" * 800},
        "",
    )
    assert (await executor.execute(call)).status == "approval_required"
    assert (await executor.execute(call, approved=True)).status == "success"

    started, completed = events[-2:]
    assert started["type"] == "response.tool.started"
    assert started["arguments"]["api_key"] == "[REDACTED]"
    assert "TRUNCATED" in started["arguments"]["payload"]
    assert completed["type"] == "response.tool.completed"
    assert "must-not-leak" not in completed["output_summary"]
