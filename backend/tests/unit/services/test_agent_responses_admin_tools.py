"""Responses 管理工具的批量原子性、发布审批与自定义 Agent 委派回归。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.models.agent_governance import ApprovalItem, ToolCallLog
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


@pytest.fixture(autouse=True)
def lightweight_orchestrator(monkeypatch):
    monkeypatch.setattr(
        service_module,
        "get_request_orchestrator",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )


def _executor(db, user: User, run_id: str, events: list[dict[str, Any]] | None = None) -> PrismToolExecutor:
    async def sink(event):
        if events is not None:
            events.append(dict(event))

    return PrismToolExecutor(
        db,
        user,
        surface="admin" if user.role == "admin" else "user",
        run_id=run_id,
        mcp_provider=EmptyMcp(),
        event_sink=sink,
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


def test_batch_delete_protects_current_and_last_available_admin(db, admin_user) -> None:
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
    assert "最后一个" in last_admin_preview.error


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
    role = Role(name="普通成员", code="member_custom_agent", status="active", sort=20)
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
async def test_tool_sse_lifecycle_redacts_sensitive_arguments(db, admin_user) -> None:
    class SecretMcp(EmptyMcp):
        def has_tool(self, name: str) -> bool:
            return name == "mcp_secret_tool"

        async def call(self, _name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True, "authorization": "must-not-leak"}

    events: list[dict[str, Any]] = []

    async def sink(event):
        events.append(dict(event))

    executor = PrismToolExecutor(
        db,
        admin_user,
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
