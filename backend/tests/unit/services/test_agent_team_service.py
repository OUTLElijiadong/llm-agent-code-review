"""动态子 Agent 团队服务契约测试。"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Update

from app.models.agent_capability import SandboxEnvironment
from app.models.agent_governance import AgentMemory
from app.models.agent_mesh import AgentMeshConversation, AgentMeshMessage
from app.models.agent_team import AgentTeam, AgentTeamEvent, AgentTeamMember, AgentTeamTask
from app.models.code_file import CodeFile
from app.models.custom_agent import CustomAgent, CustomAgentRelease, CustomAgentVersion
from app.models.project import Project
from app.models.project_source_revision import ProjectSourceRevision
from app.models.user import User
from app.schemas.agent_team import AgentTeamCreateIn
from app.services import agent_responses_service, agent_team_service, sandbox_service
from app.services.declarative_agent_runtime import DeclarativeReviewAgentFactory


@pytest.fixture()
def team_user():
    return SimpleNamespace(id=7, role="user", username="owner")


def _payload(**overrides):
    value = {
        "surface": "user",
        "session_id": "session-a1",
        "title": "发布前验证",
        "objective": "执行多 Agent 验证",
        "members": [
            {
                "member_key": "reader",
                "display_name": "读取 Agent",
                "address": "agent:project_analyzer",
                "role": "worker",
            },
            {
                "member_key": "reviewer",
                "display_name": "验证 Agent",
                "address": "agent:code_reviewer",
                "role": "verifier",
            },
        ],
        "tasks": [
            {
                "task_key": "read",
                "member_key": "reader",
                "title": "读取项目",
                "instructions": "读取项目",
                "depends_on": [],
            },
            {
                "task_key": "verify",
                "member_key": "reviewer",
                "title": "验证结果",
                "instructions": "验证读取结果",
                "depends_on": ["read"],
            },
        ],
    }
    value.update(overrides)
    return AgentTeamCreateIn.model_validate(value)


def _branching_payload(*, root_attempts=1):
    return _payload(tasks=[
        {"task_key": "read", "member_key": "reader", "title": "主分支", "instructions": "读取主分支",
         "priority": 100, "max_attempts": root_attempts},
        {"task_key": "sibling", "member_key": "reader", "title": "独立分支", "instructions": "读取独立分支",
         "priority": 90},
        {"task_key": "spare", "member_key": "reader", "title": "排队分支", "instructions": "读取排队分支",
         "priority": 80},
        {"task_key": "followup", "member_key": "reader", "title": "独立分支后继", "instructions": "处理独立结果",
         "depends_on": ["sibling"]},
        {"task_key": "verify", "member_key": "reviewer", "title": "最终复核", "instructions": "复核全部分支",
         "depends_on": ["read", "spare", "followup"]},
    ])


def _fail_claim(db, team_id, claim, *, retryable=False):
    return agent_team_service.complete_task(
        db, team_id, claim["task_id"], lease_token=claim["lease_token"],
        result={"status": "failed", "summary": "确定性失败", "retryable": retryable},
        success=False, error="确定性失败",
    )


def _claimed_deploy_and_verifier(db, team_user):
    db.add(User(id=team_user.id, username="handoff-owner", password="x", role="user", status=1))
    project = Project(
        id=31,
        user_id=team_user.id,
        project_name="资源交接项目",
        description="",
        language="python",
        status="active",
    )
    revision = ProjectSourceRevision(
        id=41,
        project_id=project.id,
        owner_id=team_user.id,
        revision_no=1,
        source_sha256="a" * 64,
        parent_sha256="b" * 64,
        repaired_files_json="[]",
        repair_notes="",
        archive_blob=b"zip",
        create_time=datetime.now(timezone.utc),
        update_time=datetime.now(timezone.utc),
    )
    db.add_all([project, revision])
    db.commit()
    created = agent_team_service.create_team(
        db,
        team_user,
        _payload(
            members=[
                {
                    "member_key": "deployer",
                    "display_name": "部署 Agent",
                    "address": "agent:sandbox_deployer",
                    "role": "worker",
                },
                {
                    "member_key": "verifier",
                    "display_name": "验证 Agent",
                    "address": "agent:test_verifier",
                    "role": "verifier",
                },
            ],
            tasks=[
                {
                    "task_key": "deploy",
                    "member_key": "deployer",
                    "title": "部署",
                    "instructions": "在隔离沙箱部署",
                    "input": {
                        "operation": "deploy",
                        "project_id": project.id,
                        "language": "python",
                        "source_revision_id": revision.id,
                    },
                },
                {
                    "task_key": "verify",
                    "member_key": "verifier",
                    "title": "验证",
                    "instructions": "执行黑白盒验证",
                    "depends_on": ["deploy"],
                    "input": {
                        "operation": "run_full_project_validation",
                        "project_id": project.id,
                        "language": "python",
                        "source_revision_id": revision.id,
                    },
                },
            ],
        ),
    )
    deploy_claim = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    environment = SandboxEnvironment(
        public_id="sbx_ready_deploy",
        project_id=project.id,
        owner_id=team_user.id,
        worker_id=None,
        agent_code="sandbox_deployer",
        purpose="deploy",
        language="python",
        test_mode="deploy",
        status="ready",
        runtime="runsc",
        image_ref="python@sha256:fixed",
        image_digest="sha256:fixed",
        source_sha256=revision.source_sha256,
        resource_policy_json="{}",
        agent_config_json=json.dumps(
            {
                "source_revision_id": revision.id,
                "agent_team": {
                    "team_id": created["team_id"],
                    "task_id": deploy_claim["task_id"],
                    "attempt": deploy_claim["attempt_count"],
                },
            }
        ),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db.add(environment)
    agent_team_service.attach_task_runtime_resource(
        db,
        owner_user_id=team_user.id,
        team_id=created["team_id"],
        task_id=deploy_claim["task_id"],
        lease_token=deploy_claim["lease_token"],
        resource_type="sandbox_environment",
        resource_id=environment.public_id,
        metadata={"purpose": "deploy"},
    )
    db.commit()
    deploy_result = {
        "status": "completed",
        "summary": "部署已就绪",
        "artifacts": [{"type": "sandbox_environment", "data": {"public_id": environment.public_id}}],
    }
    agent_team_service.complete_task(
        db,
        created["team_id"],
        deploy_claim["task_id"],
        lease_token=deploy_claim["lease_token"],
        result=deploy_result,
    )
    verifier_claim = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    return created, deploy_claim, verifier_claim, environment, deploy_result


def test_create_team_persists_graph_and_dependency_state(db, team_user):
    created = agent_team_service.create_team(db, team_user, _payload())
    assert created["status"] == "queued"
    assert created["tasks"][0]["status"] == "queued"
    assert created["tasks"][1]["status"] == "waiting_dependency"


def test_claim_and_complete_events_carry_task_key(db, team_user):
    """事件 detail 统一带 task_key:前端子Agent工作卡片日志「开始工作 <task_key>」依赖它。"""
    created = agent_team_service.create_team(db, team_user, _payload())
    claim = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    assert claim["task_key"]
    agent_team_service.complete_task(
        db,
        created["team_id"],
        claim["task_id"],
        lease_token=claim["lease_token"],
        result={"status": "completed", "summary": "ok"},
    )
    detail = agent_team_service.get_team(db, team_user, created["team_id"])
    claimed_events = [item for item in detail["events"] if item["event_type"] == "task.claimed"]
    completed_events = [item for item in detail["events"] if item["event_type"] == "task.completed"]
    assert claimed_events[0]["team_id"] == created["team_id"]
    assert claimed_events and claimed_events[0]["detail"]["task_key"] == claim["task_key"]
    assert claimed_events[0]["detail"]["member_key"] == "reader"
    assert completed_events and completed_events[0]["detail"]["task_key"] == claim["task_key"]
    assert db.query(AgentTeam).count() == 1
    assert db.query(AgentTeamTask).count() == 2
    assert (
        db.query(AgentMeshConversation)
        .filter_by(user_id=team_user.id, surface="user", session_key="session-a1")
        .count()
        == 1
    )
    assert created["counts"] == {
        "total": 2,
        "completed": 0,
        "running": 0,
        "queued": 2,
        "failed": 0,
        "blocked": 0,
    }


def test_team_retry_budget_caps_each_task(db, team_user):
    created = agent_team_service.create_team(
        db,
        team_user,
        _payload(
            max_attempts=2,
            tasks=[
                {
                    "task_key": "read",
                    "member_key": "reader",
                    "title": "读取项目",
                    "instructions": "读取项目",
                    "depends_on": [],
                    "max_attempts": 9,
                },
                {
                    "task_key": "verify",
                    "member_key": "reviewer",
                    "title": "验证项目",
                    "instructions": "复核读取结果",
                    "depends_on": ["read"],
                },
            ],
            members=[
                {
                    "member_key": "reader",
                    "display_name": "读取 Agent",
                    "address": "agent:project_analyzer",
                    "role": "worker",
                },
                {
                    "member_key": "reviewer",
                    "display_name": "验证 Agent",
                    "address": "agent:code_reviewer",
                    "role": "verifier",
                },
            ],
        ),
    )
    assert created["max_attempts"] == 2
    assert created["tasks"][0]["max_attempts"] == 2


def test_create_team_auto_adds_verifier_task_when_member_exists(db, team_user):
    payload = _payload(
        members=[
            {
                "member_key": "reader",
                "display_name": "读取 Agent",
                "address": "agent:project_analyzer",
                "role": "worker",
            },
            {
                "member_key": "reviewer",
                "display_name": "验证 Agent",
                "address": "agent:code_reviewer",
                "role": "verifier",
            },
        ],
        tasks=[
            {
                "task_key": "read",
                "member_key": "reader",
                "title": "读取项目",
                "instructions": "读取项目",
                "depends_on": [],
            }
        ],
    )
    created = agent_team_service.create_team(db, team_user, payload)
    assert any(item["task_key"] == "auto_summary" for item in created["tasks"])


def test_published_custom_member_uses_server_release_snapshot(db):
    admin = SimpleNamespace(id=7, role="admin", username="manager")
    custom = CustomAgent(
        code="published_reviewer",
        name="发布验证 Agent",
        description="",
        owner_id=admin.id,
        status="published",
        is_enabled=1,
    )
    db.add(custom)
    db.flush()
    version = CustomAgentVersion(
        agent_id=custom.id,
        version_number=1,
        prompt="复核前置任务证据",
        review_focus="可靠性",
        model_config_json="{}",
        input_schema_json="{}",
        output_schema_json="{}",
        checksum="v" * 64,
        status="published",
        original_author_id=admin.id,
    )
    db.add(version)
    db.flush()
    custom.current_published_version_id = version.id
    release = CustomAgentRelease(
        agent_id=custom.id,
        agent_version_id=version.id,
        package_manifest_json="{}",
        package_checksum="p" * 64,
        status="published",
        published_by=admin.id,
        published_at=datetime.now(timezone.utc),
    )
    db.add(release)
    db.commit()

    created = agent_team_service.create_team(
        db,
        admin,
        _payload(
            members=[
                {
                    "member_key": "reader",
                    "display_name": "读取 Agent",
                    "address": "agent:project_analyzer",
                    "role": "worker",
                },
                {
                    "member_key": "reviewer",
                    "display_name": "发布验证 Agent",
                    "address": "custom:published_reviewer",
                    "role": "verifier",
                    "capabilities": {
                        "release_id": 999,
                        "code": "spoofed",
                        "api_key": "must-not-persist",
                    },
                },
            ],
            tasks=[
                {"task_key": "read", "member_key": "reader", "title": "读取项目", "instructions": "读取项目"},
                {"task_key": "verify", "member_key": "reviewer", "title": "验证结果",
                 "instructions": "验证读取结果", "depends_on": ["read"],
                 "input": {"code": "print('v1')", "language": "python"}},
            ],
        ),
    )
    member = next(item for item in created["members"] if item["member_key"] == "reviewer")
    assert member["template_id"] == custom.id
    assert member["template_version_id"] == version.id
    assert member["capabilities"]["release_id"] == release.id
    assert member["capabilities"]["code"] == custom.code
    assert member["capabilities"]["requested_capabilities"]["release_id"] == 999
    assert member["capabilities"]["requested_capabilities"]["api_key"] == "[REDACTED]"

    version_v2 = CustomAgentVersion(
        agent_id=custom.id,
        version_number=2,
        prompt="新版本提示词",
        review_focus="新版本",
        model_config_json="{}",
        input_schema_json="{}",
        output_schema_json="{}",
        checksum="n" * 64,
        status="published",
        original_author_id=admin.id,
    )
    db.add(version_v2)
    db.flush()
    db.add(
        CustomAgentRelease(
            agent_id=custom.id,
            agent_version_id=version_v2.id,
            package_manifest_json="{}",
            package_checksum="q" * 64,
            status="published",
            published_by=admin.id,
            published_at=datetime.now(timezone.utc),
        )
    )
    custom.current_published_version_id = version_v2.id
    db.commit()

    current = DeclarativeReviewAgentFactory.resolve_published(db, custom.code, user=admin)
    frozen = DeclarativeReviewAgentFactory.resolve_release(
        db,
        custom.code,
        release_id=release.id,
        version_id=version.id,
        package_checksum="p" * 64,
        template_checksum="v" * 64,
        user=admin,
    )
    assert current is not None and current.version_id == version_v2.id
    assert frozen is not None and frozen.version_id == version.id
    assert frozen.system_prompt == "复核前置任务证据"

    first = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    assert first is not None
    agent_team_service.complete_task(
        db,
        created["team_id"],
        first["task_id"],
        lease_token=first["lease_token"],
        result={"status": "completed", "summary": "前置读取完成"},
    )
    verifier = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    assert verifier is not None
    assert verifier["member_snapshot"] == {
        "template_id": custom.id,
        "version_id": version.id,
        "release_id": release.id,
        "package_checksum": "p" * 64,
        "template_checksum": "v" * 64,
    }


def test_custom_team_file_input_is_validated_before_queueing(db, super_admin_user):
    project = Project(user_id=super_admin_user.id, project_name="文件输入项目", language="python", status="active")
    db.add(project)
    db.flush()
    source = CodeFile(
        project_id=project.id, file_name="auth.py", language="python", content="def authorize():\n    return True\n",
        size_bytes=33, raw_size=33, line_count=2, version_no=1, is_binary=0, status="active",
    )
    db.add(source)
    db.commit()
    def task(value):
        return SimpleNamespace(task_key="review", input=value)

    agent_team_service._validate_task_scope(
        db, super_admin_user, task({"project_id": project.id, "file_id": source.id}), "custom:reviewer",
    )
    for invalid in ({}, {"project_id": project.id}, {"code": " ", "file_id": source.id},
                    {"project_id": project.id, "file_id": True}, {"project_id": project.id, "file_id": source.id,
                     "code": "print('duplicate')"}):
        with pytest.raises(agent_team_service.AgentTeamValidationError):
            agent_team_service._validate_task_scope(db, super_admin_user, task(invalid), "custom:reviewer")

    source.is_binary = 1
    db.commit()
    with pytest.raises(agent_team_service.AgentTeamValidationError, match="文本文件"):
        agent_team_service._validate_task_scope(
            db, super_admin_user, task({"project_id": project.id, "file_id": source.id}), "custom:reviewer",
        )


def test_public_redaction_fallback_is_fail_closed(monkeypatch):
    def broken_redactor(_value):
        raise RuntimeError("redactor unavailable")

    monkeypatch.setattr(agent_responses_service, "redact_agent_event_value", broken_redactor)
    public = agent_team_service._public(
        {
            "api_key": "secret-value",
            "nested": {"authorization": "Bearer top-secret"},
            "message": "token=top-secret",
        }
    )
    assert public["api_key"] == "[REDACTED]"
    assert public["nested"]["authorization"] == "[REDACTED]"
    assert "top-secret" not in public["message"]


def test_create_team_rejects_dependency_cycle(db, team_user):
    payload = _payload(
        tasks=[
            {
                "task_key": "read",
                "member_key": "reader",
                "title": "读取项目",
                "instructions": "读取项目",
                "depends_on": ["verify"],
            },
            {
                "task_key": "verify",
                "member_key": "reviewer",
                "title": "验证结果",
                "instructions": "验证读取结果",
                "depends_on": ["read"],
            },
        ]
    )
    with pytest.raises(agent_team_service.AgentTeamValidationError, match="环"):
        agent_team_service.create_team(db, team_user, payload)


def test_team_isolation_and_cancel_are_idempotent(db, team_user):
    created = agent_team_service.create_team(db, team_user, _payload())
    other = SimpleNamespace(id=8, role="user", username="other")
    with pytest.raises(agent_team_service.AgentTeamNotFoundError):
        agent_team_service.get_team(db, other, created["team_id"])
    cancelled = agent_team_service.cancel_team(db, team_user, created["team_id"], reason="用户取消")
    assert cancelled["status"] == "cancelled"
    assert agent_team_service.cancel_team(db, team_user, created["team_id"], reason="重复取消")["status"] == "cancelled"


def test_monitor_team_rejects_server_fact_object_before_queueing(db, team_user):
    payload = _payload(
        members=[
            {
                "member_key": "monitor",
                "display_name": "监控 Agent",
                "address": "agent:monitor",
                "role": "worker",
            },
            {
                "member_key": "summary",
                "display_name": "汇总 Agent",
                "address": "agent:reporter",
                "role": "summarizer",
            },
        ],
        tasks=[
            {
                "task_key": "monitor",
                "member_key": "monitor",
                "title": "指标巡检",
                "instructions": "读取指标",
                "input": {"window_minutes": 60, "metrics": {"server_status": {"overall": "error"}}},
            },
            {
                "task_key": "summary",
                "member_key": "summary",
                "title": "汇总",
                "instructions": "汇总结果",
                "depends_on": ["monitor"],
            },
        ],
    )

    with pytest.raises(agent_team_service.AgentTeamValidationError, match="metrics 必须是非空指标名字符串列表"):
        agent_team_service.create_team(db, team_user, payload)


def test_operations_team_requires_unique_super_admin_and_readonly_action(db, monkeypatch):
    payload = _payload(
        surface="admin",
        members=[
            {
                "member_key": "ops",
                "display_name": "运维 Agent",
                "address": "agent:operations",
                "role": "worker",
            },
            {
                "member_key": "summary",
                "display_name": "汇总 Agent",
                "address": "agent:reporter",
                "role": "summarizer",
            },
        ],
        tasks=[
            {
                "task_key": "ops",
                "member_key": "ops",
                "title": "服务器状态",
                "instructions": "执行只读运维状态查询",
                "input": {"action": "status", "params": {}},
            },
            {
                "task_key": "summary",
                "member_key": "summary",
                "title": "汇总",
                "instructions": "汇总运维结果",
                "depends_on": ["ops"],
            },
        ],
    )
    admin = SimpleNamespace(id=1, role="super_admin", username="admin")
    monkeypatch.setattr("app.services.rbac_service.is_super_admin_user", lambda *_args: True)
    created = agent_team_service.create_team(db, admin, payload)
    assert created["members"][0]["capabilities"]["dispatch_state"] == "team_governed"

    payload.tasks[0].input = {"action": "restart_service", "params": {"service": "backend"}}
    with pytest.raises(agent_team_service.AgentTeamValidationError, match="只能执行运维只读动作"):
        agent_team_service.create_team(db, admin, payload)


def test_dependency_failure_recursively_blocks_all_descendants(db, team_user):
    created = agent_team_service.create_team(
        db,
        team_user,
        _payload(
            tasks=[
                {
                    "task_key": "read",
                    "member_key": "reader",
                    "title": "读取",
                    "instructions": "读取",
                    "max_attempts": 1,
                },
                {
                    "task_key": "verify",
                    "member_key": "reviewer",
                    "title": "复核",
                    "instructions": "复核",
                    "depends_on": ["read"],
                },
                {
                    "task_key": "summary",
                    "member_key": "reviewer",
                    "title": "汇总",
                    "instructions": "汇总",
                    "depends_on": ["verify"],
                },
            ]
        ),
    )
    claimed = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    result = agent_team_service.complete_task(
        db,
        created["team_id"],
        claimed["task_id"],
        lease_token=claimed["lease_token"],
        result={"status": "failed", "summary": "契约错误", "retryable": False},
        success=False,
        error="契约错误",
    )

    assert {item["task_key"]: item["status"] for item in result["tasks"]} == {
        "read": "failed",
        "verify": "blocked",
        "summary": "blocked",
    }
    assert all(item["completed_at"] for item in result["tasks"])


@pytest.mark.parametrize(("retryable", "root_status"), [(False, "failed"), (True, "dead_letter")])
def test_terminal_failure_closes_independent_queued_and_waiting_tasks(db, team_user, retryable, root_status):
    """失败根节点、独立排队分支及其等待后继必须在同一提交中闭合。"""
    created = agent_team_service.create_team(db, team_user, _branching_payload())
    claim = agent_team_service.claim_next_task(db, created["team_id"])
    result = _fail_claim(db, created["team_id"], claim, retryable=retryable)
    tasks = {item["task_key"]: item for item in result["tasks"]}
    assert result["status"] == "failed"
    assert tasks["read"]["status"] == root_status
    assert {tasks[key]["status"] for key in ("sibling", "spare", "followup", "verify")} == {"blocked"}
    assert result["counts"]["queued"] == result["counts"]["running"] == 0
    assert all(task["completed_at"] and task["next_attempt_at"] is None for task in tasks.values())
    for key in ("sibling", "spare", "followup"):
        assert any(error["code"] == "team_failed" and "read" in error["failed_tasks"] for error in tasks[key]["errors"])
    assert tasks["verify"]["errors"][0]["code"] == "dependency_failed"
    assert all(member["status"] == "failed" for member in result["members"])
    detail = agent_team_service.get_team(db, team_user, created["team_id"])
    blocked = [event for event in detail["events"] if event["event_type"] == "task.blocked"]
    assert {event["detail"]["task_key"] for event in blocked} == {"sibling", "spare", "followup", "verify"}
    terminal_event = [event for event in detail["events"] if event["to_status"] == "failed"][-1]
    assert terminal_event["detail"]["counts"]["queued"] == 0
    assert all(row.lease_token is None and row.lease_expires_at is None for row in db.query(AgentTeamTask).all())
    assert agent_team_service.claim_next_task(db, created["team_id"]) is None


def test_terminal_failure_and_block_events_are_atomic(db, team_user, monkeypatch):
    """阻断事件写入失败时，失败父、失败子和兄弟阻断必须一起回滚。"""
    created = agent_team_service.create_team(db, team_user, _branching_payload())
    claim = agent_team_service.claim_next_task(db, created["team_id"])
    before = {task.task_key: task.status for task in db.query(AgentTeamTask).all()}
    event_count = db.query(AgentTeamEvent).count()
    original_event = agent_team_service._event

    def fail_block_event(*args, **kwargs):
        if args[2] == "task.blocked" and kwargs["task"].task_key == "spare":
            raise RuntimeError("blocked event write failed")
        return original_event(*args, **kwargs)

    monkeypatch.setattr(agent_team_service, "_event", fail_block_event)
    with pytest.raises(RuntimeError, match="blocked event write failed"):
        _fail_claim(db, created["team_id"], claim)
    db.rollback()
    assert {task.task_key: task.status for task in db.query(AgentTeamTask).all()} == before
    assert db.get(AgentTeam, created["team_id"]).status == "running"
    assert db.get(AgentTeamTask, claim["task_id"]).lease_token == claim["lease_token"]
    assert db.query(AgentTeamEvent).count() == event_count


def test_retryable_failure_keeps_independent_branches_schedulable(db, team_user):
    """尚有自动重试预算不构成团队终态失败，也不阻断其他分支。"""
    created = agent_team_service.create_team(db, team_user, _branching_payload(root_attempts=2))
    claim = agent_team_service.claim_next_task(db, created["team_id"])
    result = _fail_claim(db, created["team_id"], claim, retryable=True)
    assert result["status"] == "queued"
    assert result["completed_at"] is None
    assert {item["task_key"]: item["status"] for item in result["tasks"]} == {
        "read": "queued", "sibling": "queued", "spare": "queued",
        "followup": "waiting_dependency", "verify": "waiting_dependency",
    }
    assert db.query(AgentTeamEvent).filter_by(event_type="task.blocked").count() == 0
    retry = agent_team_service.claim_next_task(db, created["team_id"])
    assert retry["task_id"] == claim["task_id"] and retry["attempt_count"] == 2
    assert agent_team_service.claim_next_task(db, created["team_id"])["task_key"] == "sibling"


@pytest.mark.parametrize("sibling_success", [True, False])
def test_fail_fast_drains_running_sibling_without_starting_more_work(db, team_user, sibling_success):
    """在途兄弟保留租约；禁止新领取，真实返回后才归约失败父。"""
    created = agent_team_service.create_team(db, team_user, _branching_payload())
    root = agent_team_service.claim_next_task(db, created["team_id"])
    sibling = agent_team_service.claim_next_task(db, created["team_id"])
    running = _fail_claim(db, created["team_id"], root)
    by_key = {item["task_key"]: item for item in running["tasks"]}
    assert running["status"] == "running" and running["completed_at"] is None
    assert by_key["sibling"]["status"] == "running" and by_key["sibling"]["completed_at"] is None
    assert by_key["spare"]["status"] == by_key["followup"]["status"] == "blocked"
    assert db.get(AgentTeamTask, sibling["task_id"]).lease_token == sibling["lease_token"]
    assert agent_team_service.claim_next_task(db, created["team_id"]) is None
    final = agent_team_service.complete_task(
        db, created["team_id"], sibling["task_id"], lease_token=sibling["lease_token"],
        result={"status": "completed" if sibling_success else "failed", "retryable": True},
        success=sibling_success, error="" if sibling_success else "兄弟暂时失败",
    )
    assert final["status"] == "failed"
    assert final["counts"]["queued"] == final["counts"]["running"] == 0
    sibling_task = db.get(AgentTeamTask, sibling["task_id"])
    assert sibling_task.status == ("completed" if sibling_success else "blocked")
    assert agent_team_service.claim_next_task(db, created["team_id"]) is None


@pytest.mark.parametrize("terminal", ["failed", "completed", "cancelled", "expired"])
def test_terminal_parent_rejects_late_complete_without_claiming_resource_stopped(db, team_user, terminal):
    """防御已有异常组合：拒绝晚到结果，不改写运行任务或假称资源已停止。"""
    created = agent_team_service.create_team(db, team_user, _payload())
    claim = agent_team_service.claim_next_task(db, created["team_id"])
    team = db.get(AgentTeam, created["team_id"])
    team.status = terminal
    team.completed_at = datetime.now(timezone.utc)
    db.commit()
    before = db.query(AgentTeamEvent).count()
    for _attempt in range(2):
        with pytest.raises(agent_team_service.AgentTeamLeaseError):
            agent_team_service.complete_task(
                db, team.id, claim["task_id"], lease_token=claim["lease_token"], result={"summary": "晚到成功"},
            )
    db.expire_all()
    assert db.get(AgentTeam, team.id).status == terminal
    task = db.get(AgentTeamTask, claim["task_id"])
    assert task.status == "running" and task.lease_token == claim["lease_token"]
    assert task.result_json == "{}" and task.completed_at is None
    assert db.query(AgentTeamEvent).count() == before


def test_stale_session_cannot_claim_after_another_session_fails_team(db, team_user):
    """两个会话交错：旧 identity map 不得领取或复活另一个会话已失败的团队。"""
    created = agent_team_service.create_team(db, team_user, _branching_payload())
    claim = agent_team_service.claim_next_task(db, created["team_id"])
    with Session(bind=db.get_bind(), autoflush=False, expire_on_commit=False) as stale_db:
        stale_team = stale_db.get(AgentTeam, created["team_id"])
        stale_db.commit()
        _fail_claim(db, created["team_id"], claim)
        assert stale_team.status == "running"
        event_count = db.query(AgentTeamEvent).count()
        assert agent_team_service.claim_next_task(stale_db, created["team_id"]) is None
        stale_db.rollback()
        db.expire_all()
        assert db.get(AgentTeam, created["team_id"]).status == "failed"
        assert db.query(AgentTeamEvent).count() == event_count


def test_stale_session_cannot_complete_an_old_attempt_after_retry_claim(db, team_user):
    """两个会话交错：旧缓存中的 token 不能覆盖新的领取与尝试。"""
    created = agent_team_service.create_team(db, team_user, _branching_payload(root_attempts=2))
    old_claim = agent_team_service.claim_next_task(db, created["team_id"])
    with Session(bind=db.get_bind(), autoflush=False, expire_on_commit=False) as stale_db:
        stale_team = stale_db.get(AgentTeam, created["team_id"])
        stale_task = stale_db.get(AgentTeamTask, old_claim["task_id"])
        stale_db.commit()
        _fail_claim(db, created["team_id"], old_claim, retryable=True)
        current_claim = agent_team_service.claim_next_task(db, created["team_id"])
        assert stale_team.status == "running" and stale_task.lease_token == old_claim["lease_token"]
        with pytest.raises(agent_team_service.AgentTeamLeaseError):
            agent_team_service.complete_task(
                stale_db, created["team_id"], old_claim["task_id"],
                lease_token=old_claim["lease_token"], result={"summary": "旧尝试晚到"},
            )
        stale_db.rollback()
        db.expire_all()
        current = db.get(AgentTeamTask, current_claim["task_id"])
        assert current.status == "running" and current.lease_token == current_claim["lease_token"]
        assert current.attempt_count == 2


def test_claim_lost_cas_has_no_running_transition_or_event(db, team_user, monkeypatch):
    """可控模拟 CAS 竞争失败，失败领取必须回滚且不得生成执行事件。"""
    created = agent_team_service.create_team(db, team_user, _payload())
    event_count = db.query(AgentTeamEvent).count()
    original_execute = db.execute
    attempted = []

    def lose_claim(statement, *args, **kwargs):
        if isinstance(statement, Update) and statement.table.name == "agent_team_task":
            attempted.append(True)
            return SimpleNamespace(rowcount=0)
        return original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db, "execute", lose_claim)
    assert agent_team_service.claim_next_task(db, created["team_id"]) is None
    assert attempted == [True]
    assert db.query(AgentTeamTask).filter_by(status="running").count() == 0
    assert db.query(AgentTeamEvent).count() == event_count


def test_failed_team_refresh_and_repeated_completion_do_not_reopen_or_duplicate(db, team_user):
    """重复回调或状态归约不得复活终态，也不重复写失败/阻断事件。"""
    created = agent_team_service.create_team(db, team_user, _branching_payload())
    claim = agent_team_service.claim_next_task(db, created["team_id"])
    _fail_claim(db, created["team_id"], claim)
    team = db.get(AgentTeam, created["team_id"])
    original_error = team.error_json
    event_count = db.query(AgentTeamEvent).count()
    for _attempt in range(2):
        agent_team_service._refresh_team_status(db, team)
        assert agent_team_service.claim_next_task(db, created["team_id"]) is None
        with pytest.raises(agent_team_service.AgentTeamLeaseError):
            _fail_claim(db, created["team_id"], claim)
    assert team.status == "failed" and team.error_json == original_error
    assert db.query(AgentTeamEvent).count() == event_count
    for task in db.query(AgentTeamTask).all():
        task.status = "completed"
    db.flush()
    agent_team_service._refresh_team_status(db, team)
    assert team.status == "failed" and team.error_json == original_error
    assert db.query(AgentTeamEvent).count() == event_count


def test_expired_last_attempt_closes_independent_pending_tasks(db, team_user):
    """租约恢复耗尽预算时也必须闭合独立分支，而非只闭合依赖后继。"""
    created = agent_team_service.create_team(db, team_user, _branching_payload())
    claim = agent_team_service.claim_next_task(db, created["team_id"])
    db.get(AgentTeamTask, claim["task_id"]).lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    assert agent_team_service.recover_expired_leases(db) == 1
    detail = agent_team_service.get_team(db, team_user, created["team_id"])
    assert detail["status"] == "failed"
    assert detail["counts"]["queued"] == detail["counts"]["running"] == 0
    assert db.get(AgentTeamTask, claim["task_id"]).status == "dead_letter"


def test_failed_branch_waits_for_sibling_cleanup_before_terminal_reduction(db, team_user, monkeypatch):
    """清理未确认时兄弟仍运行；确认后停止调度重试并归约失败父。"""
    created = agent_team_service.create_team(db, team_user, _branching_payload())
    root = agent_team_service.claim_next_task(db, created["team_id"])
    sibling = agent_team_service.claim_next_task(db, created["team_id"])
    _fail_claim(db, created["team_id"], root)
    outcomes = iter([False, True])
    monkeypatch.setattr(agent_team_service, "_cleanup_task_runtime_resources", lambda *args, **kwargs: next(outcomes))
    for expected in (0, 1):
        task = db.get(AgentTeamTask, sibling["task_id"])
        task.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        assert agent_team_service.recover_expired_leases(db) == expected
        if expected == 0:
            assert db.get(AgentTeamTask, sibling["task_id"]).status == "running"
            assert db.get(AgentTeam, created["team_id"]).status == "running"
            assert agent_team_service.claim_next_task(db, created["team_id"]) is None
    detail = agent_team_service.get_team(db, team_user, created["team_id"])
    assert detail["status"] == "failed" and detail["counts"]["queued"] == detail["counts"]["running"] == 0
    assert db.get(AgentTeamTask, sibling["task_id"]).status == "blocked"


def test_explicit_root_retry_restores_its_fail_fast_blocked_branches(db, team_user):
    """显式改变根节点方案后，可恢复由它全局阻断的节点，不改变原始 DAG。"""
    created = agent_team_service.create_team(db, team_user, _branching_payload())
    claim = agent_team_service.claim_next_task(db, created["team_id"])
    failed = _fail_claim(db, created["team_id"], claim)
    assert {item["status"] for item in failed["tasks"] if item["task_key"] != "read"} == {"blocked"}
    retried = agent_team_service.retry_team(
        db, team_user, created["team_id"], task_keys=["read"],
        strategy_changes={"read": "改用第二路径并先缩小输入范围"},
    )
    assert {item["task_key"]: item["status"] for item in retried["tasks"]} == {
        "read": "queued", "sibling": "queued", "spare": "queued",
        "followup": "waiting_dependency", "verify": "waiting_dependency",
    }
    assert [item["depends_on"] for item in retried["tasks"]] == [item["depends_on"] for item in created["tasks"]]
    next_claim = agent_team_service.claim_next_task(db, created["team_id"])
    assert next_claim["task_id"] == claim["task_id"] and next_claim["attempt_count"] == 2
    with pytest.raises(agent_team_service.AgentTeamLeaseError):
        _fail_claim(db, created["team_id"], claim)


def test_independent_branching_team_still_completes_normally(db, team_user):
    """没有不可恢复失败时，所有独立分支与验证节点保持原有正常完成语义。"""
    created = agent_team_service.create_team(db, team_user, _branching_payload())
    for _attempt in range(5):
        claim = agent_team_service.claim_next_task(db, created["team_id"])
        assert claim is not None
        result = agent_team_service.complete_task(
            db, created["team_id"], claim["task_id"], lease_token=claim["lease_token"],
            result={"status": "completed", "summary": "真实结果"},
        )
    assert result["status"] == "completed" and result["counts"]["completed"] == 5
    assert db.query(AgentTeamEvent).filter_by(event_type="task.blocked").count() == 0


@pytest.mark.parametrize("last_success", [True, False])
def test_missing_verifier_drains_running_tasks_before_failing(db, team_user, last_success):
    created = agent_team_service.create_team(db, team_user, _branching_payload())
    first = agent_team_service.claim_next_task(db, created["team_id"])
    second = agent_team_service.claim_next_task(db, created["team_id"])
    verifier = db.query(AgentTeamMember).filter_by(team_id=created["team_id"], role="verifier").one()
    verifier.role = "worker"
    db.commit()

    assert agent_team_service.claim_next_task(db, created["team_id"]) is None
    parent = db.get(AgentTeam, created["team_id"])
    assert parent.status == "running" and parent.completed_at is None
    for claim in (first, second):
        task = db.get(AgentTeamTask, claim["task_id"])
        assert task.status == "running" and task.lease_token == claim["lease_token"]
    assert db.query(AgentTeamTask).filter(AgentTeamTask.status.in_(["queued", "waiting_dependency"])).count() == 0

    partial = agent_team_service.complete_task(
        db, created["team_id"], first["task_id"], lease_token=first["lease_token"],
        result={"status": "completed", "summary": "已完成读取"},
    )
    assert partial["status"] == "running" and partial["completed_at"] is None
    final = agent_team_service.complete_task(
        db, created["team_id"], second["task_id"], lease_token=second["lease_token"],
        result={"status": "completed" if last_success else "failed", "retryable": False},
        success=last_success, error="" if last_success else "读取失败",
    )
    assert final["status"] == "failed" and final["completed_at"] is not None
    assert db.query(AgentTeamTask).filter_by(team_id=created["team_id"], status="running").count() == 0
    assert db.query(AgentTeamEvent).filter_by(
        team_id=created["team_id"], event_type="team.status_changed", to_status="failed",
    ).count() == 1


def test_missing_verifier_without_running_tasks_still_fails_immediately(db, team_user):
    created = agent_team_service.create_team(db, team_user, _branching_payload())
    verifier = db.query(AgentTeamMember).filter_by(team_id=created["team_id"], role="verifier").one()
    verifier.role = "worker"
    db.commit()
    assert agent_team_service.claim_next_task(db, created["team_id"]) is None
    parent = db.get(AgentTeam, created["team_id"])
    assert parent.status == "failed" and parent.completed_at is not None
    assert {task.status for task in db.query(AgentTeamTask).filter_by(team_id=parent.id)} == {"blocked"}


def test_claim_respects_team_concurrency_and_lease_cas(db, team_user):
    payload = _payload(
        max_active_children=3,
        tasks=[
            {
                "task_key": f"read-{index}",
                "member_key": "reader",
                "title": f"读取{index}",
                "instructions": "读取",
                "depends_on": [],
            }
            for index in range(4)
        ]
        + [
            {
                "task_key": "verify",
                "member_key": "reviewer",
                "title": "验证结果",
                "instructions": "复核读取结果",
                "depends_on": [f"read-{index}" for index in range(4)],
            }
        ],
        members=[
            {
                "member_key": "reader",
                "display_name": "读取 Agent",
                "address": "agent:project_analyzer",
                "role": "worker",
            },
            {
                "member_key": "reviewer",
                "display_name": "验证 Agent",
                "address": "agent:code_reviewer",
                "role": "verifier",
            },
        ],
    )
    created = agent_team_service.create_team(db, team_user, payload)
    claimed = [agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60) for _ in range(4)]
    assert sum(item is not None for item in claimed) == 3
    assert db.query(AgentTeamTask).filter(AgentTeamTask.status == "queued").count() == 1
    assert db.query(AgentTeamTask).filter(AgentTeamTask.status == "waiting_dependency").count() == 1
    with pytest.raises(agent_team_service.AgentTeamLeaseError):
        agent_team_service.complete_task(db, created["team_id"], claimed[0]["task_id"], lease_token="stale", result={})


def test_create_team_auto_covers_worker_leaf_with_final_verifier(db, team_user):
    payload = _payload(
        tasks=[
            {
                "task_key": "read-a",
                "member_key": "reader",
                "title": "读取 A",
                "instructions": "读取 A",
                "depends_on": [],
            },
            {
                "task_key": "read-b",
                "member_key": "reader",
                "title": "读取 B",
                "instructions": "读取 B",
                "depends_on": [],
            },
            {
                "task_key": "verify",
                "member_key": "reviewer",
                "title": "验证 A",
                "instructions": "只验证 A",
                "depends_on": ["read-a"],
            },
        ],
    )

    created = agent_team_service.create_team(db, team_user, payload)
    verify = next(item for item in created["tasks"] if item["task_key"] == "verify")
    assert "read-b" in verify["depends_on"]


def test_sandbox_team_validates_source_revision_scope_and_allows_governed_agents(db, team_user):
    project = Project(
        id=31,
        user_id=team_user.id,
        project_name="沙箱项目",
        description="",
        language="python",
        status="active",
    )
    revision = ProjectSourceRevision(
        id=41,
        project_id=project.id,
        owner_id=team_user.id,
        revision_no=1,
        source_sha256="a" * 64,
        parent_sha256="b" * 64,
        repaired_files_json="[]",
        repair_notes="",
        archive_blob=b"zip",
        create_time=datetime.now(timezone.utc),
        update_time=datetime.now(timezone.utc),
    )
    db.add_all([project, revision])
    db.commit()
    payload = _payload(
        members=[
            {
                "member_key": "deployer",
                "display_name": "沙箱部署 Agent",
                "address": "agent:sandbox_deployer",
                "role": "worker",
            },
            {
                "member_key": "verifier",
                "display_name": "测试验证 Agent",
                "address": "agent:test_verifier",
                "role": "verifier",
            },
        ],
        tasks=[
            {
                "task_key": "deploy",
                "member_key": "deployer",
                "title": "部署沙箱",
                "instructions": "在隔离沙箱部署",
                "input": {
                    "operation": "deploy",
                    "project_id": project.id,
                    "language": "python",
                    "source_revision_id": revision.id,
                },
            },
            {
                "task_key": "verify",
                "member_key": "verifier",
                "title": "全量验证",
                "instructions": "执行黑白盒组合测试",
                "depends_on": ["deploy"],
                "input": {
                    "operation": "run_full_project_validation",
                    "project_id": project.id,
                    "language": "python",
                    "source_revision_id": revision.id,
                },
            },
        ],
    )

    created = agent_team_service.create_team(db, team_user, payload)
    assert [item["address"] for item in created["members"]] == [
        "agent:sandbox_deployer",
        "agent:test_verifier",
    ]

    revision.project_id = 999
    db.commit()
    with pytest.raises(agent_team_service.AgentTeamValidationError, match="源码修订"):
        agent_team_service.create_team(db, team_user, payload)


def test_create_team_rolls_back_partial_rows_and_rejects_host_paths(db, team_user):
    payload = _payload(
        members=[
            {"member_key": "reader", "display_name": "读取 Agent", "address": "agent:project_analyzer"},
            {
                "member_key": "missing",
                "display_name": "不存在",
                "address": "agent:not_registered",
                "role": "verifier",
            },
        ],
        tasks=[
            {"task_key": "read", "member_key": "reader", "title": "读取", "instructions": "读取", "depends_on": []},
            {
                "task_key": "missing-check",
                "member_key": "missing",
                "title": "验证",
                "instructions": "验证",
                "depends_on": [],
            },
        ],
    )
    with pytest.raises(agent_team_service.AgentTeamValidationError, match="不存在"):
        agent_team_service.create_team(db, team_user, payload)
    assert db.query(AgentTeam).count() == 0
    assert db.query(AgentTeamMember).count() == 0

    with pytest.raises(agent_team_service.AgentTeamValidationError, match="宿主机路径"):
        agent_team_service.create_team(
            db,
            team_user,
            _payload(
                tasks=[
                    {
                        "task_key": "read",
                        "member_key": "reader",
                        "title": "读取",
                        "instructions": "读取",
                        "depends_on": [],
                        "input": {"source_path": "/Users/li/secret"},
                    }
                ]
            ),
        )
    assert db.query(AgentTeam).count() == 0


def test_list_teams_filters_current_surface_and_session_with_real_total(db, team_user):
    first = agent_team_service.create_team(db, team_user, _payload(session_id="session-a1", title="会话 A"))
    agent_team_service.create_team(db, team_user, _payload(session_id="session-b1", title="会话 B"))

    listed = agent_team_service.list_teams(
        db,
        team_user,
        surface="user",
        session_id="session-a1",
        limit=1,
        offset=0,
    )
    assert listed["total"] == 1
    assert [item["team_id"] for item in listed["items"]] == [first["team_id"]]


def test_dependency_handoff_mesh_ledger_and_verifier_status(db, team_user):
    created = agent_team_service.create_team(db, team_user, _payload())
    first = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    assert first is not None
    assert first["dependency_context"] == {}
    request = db.query(AgentMeshMessage).filter_by(message_type="task.request").one()
    assert request.status == "processing"
    assert request.sent_from == "session:user:session-a1"
    assert request.send_to == "agent:project_analyzer"

    after_worker = agent_team_service.complete_task(
        db,
        created["team_id"],
        first["task_id"],
        lease_token=first["lease_token"],
        result={
            "status": "completed",
            "summary": "读取完成",
            "artifacts": [{"kind": "source_revision", "id": 81}],
            "api_key": "sk-team-secret",
        },
    )
    assert after_worker["status"] == "verifying"
    assert after_worker["tasks"][0]["artifacts"][0]["id"] == 81
    assert after_worker["tasks"][0]["result"]["api_key"] == "[REDACTED]"

    messages = db.query(AgentMeshMessage).order_by(AgentMeshMessage.id.asc()).all()
    assert [item.message_type for item in messages] == ["task.request", "task.result", "coordination"]
    # 回投会话的任务结果必须可被会话 inbox 拉取,触发小菱自动续跑汇报;
    # 成员间移交仍为终态账本证据。
    assert messages[1].send_to == "session:user:session-a1"
    assert messages[1].status == "queued"
    assert messages[-1].sent_from == "agent:project_analyzer"
    assert messages[-1].send_to == "agent:code_reviewer"
    assert messages[-1].status == "completed"
    detail_messages = agent_team_service.get_team(db, team_user, created["team_id"])["messages"]
    assert {"trace_id", "correlation_id", "causation_id"} <= set(detail_messages[0])
    assert detail_messages[1]["causation_id"] == detail_messages[0]["message_id"]

    verifier = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    assert verifier is not None
    assert verifier["dependency_context"]["read"]["result"]["summary"] == "读取完成"
    assert agent_team_service.get_team(db, team_user, created["team_id"])["status"] == "verifying"

    completed = agent_team_service.complete_task(
        db,
        created["team_id"],
        verifier["task_id"],
        lease_token=verifier["lease_token"],
        result={"status": "completed", "summary": "独立复核通过"},
    )
    assert completed["status"] == "completed"
    assert completed["counts"]["completed"] == 2
    assert db.query(AgentMemory).filter(AgentMemory.memory_type == "execution_strategy").count() == 2


def test_cancel_reclaims_running_tasks_and_invalidates_lease(db, team_user):
    created = agent_team_service.create_team(db, team_user, _payload())
    claimed = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    cancelled = agent_team_service.cancel_team(db, team_user, created["team_id"], reason="停止")

    assert cancelled["status"] == "cancelled"
    assert {item["status"] for item in cancelled["tasks"]} == {"cancelled"}
    assert {item["status"] for item in cancelled["members"]} == {"reclaimed"}
    row = db.get(AgentTeamTask, claimed["task_id"])
    assert row.lease_token is None and row.lease_expires_at is None
    with pytest.raises(agent_team_service.AgentTeamLeaseError, match="取消"):
        agent_team_service.complete_task(
            db,
            created["team_id"],
            claimed["task_id"],
            lease_token=claimed["lease_token"],
            result={},
        )


def test_expired_lease_stops_persisted_runtime_resource_before_requeue(db, team_user, monkeypatch):
    db.add(User(id=team_user.id, username="lease-owner", password="x", role="user", status=1))
    db.commit()
    project = Project(user_id=team_user.id, project_name="lease-cleanup", language="python", status="active")
    db.add(project)
    db.commit()
    created = agent_team_service.create_team(
        db,
        team_user,
        _payload(
            members=[
                {
                    "member_key": "deployer",
                    "display_name": "部署 Agent",
                    "address": "agent:sandbox_deployer",
                    "role": "worker",
                },
                {
                    "member_key": "reviewer",
                    "display_name": "验证 Agent",
                    "address": "agent:code_reviewer",
                    "role": "verifier",
                },
            ],
            tasks=[
                {
                    "task_key": "deploy",
                    "member_key": "deployer",
                    "title": "部署",
                    "instructions": "在隔离沙箱部署",
                    "input": {"operation": "deploy", "project_id": project.id, "language": "python"},
                },
                {
                    "task_key": "verify",
                    "member_key": "reviewer",
                    "title": "验证",
                    "instructions": "验证部署结果",
                    "depends_on": ["deploy"],
                },
            ],
        ),
    )
    claimed = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    agent_team_service.attach_task_runtime_resource(
        db,
        owner_user_id=team_user.id,
        team_id=created["team_id"],
        task_id=claimed["task_id"],
        lease_token=claimed["lease_token"],
        resource_type="sandbox_environment",
        resource_id="sbx_crashed_worker",
    )
    task = db.get(AgentTeamTask, claimed["task_id"])
    task.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    stopped = []

    def stop_environment(_db, _user, public_id):
        stopped.append(public_id)
        if len(stopped) == 1:
            raise RuntimeError("worker stop timeout")
        return {"public_id": public_id, "status": "stopped"}

    monkeypatch.setattr(sandbox_service, "stop_environment", stop_environment)

    assert agent_team_service.recover_expired_leases(db) == 0
    db.expire_all()
    task = db.get(AgentTeamTask, claimed["task_id"])
    assert task.status == "running"
    assert task.lease_token != claimed["lease_token"]
    assert agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60) is None
    task.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()

    assert agent_team_service.recover_expired_leases(db) == 1
    db.expire_all()
    task = db.get(AgentTeamTask, claimed["task_id"])
    assert stopped == ["sbx_crashed_worker", "sbx_crashed_worker"]
    assert task.status == "queued"
    assert task.lease_token is None
    cleanup = db.query(AgentTeamEvent).filter_by(task_id=task.id, event_type="task.runtime_resource_stopped").one()
    assert "sbx_crashed_worker" in cleanup.detail_json


def test_handoff_stops_direct_deploy_resource_and_preserves_result(db, team_user, monkeypatch):
    created, deploy_claim, verifier_claim, _environment, deploy_result = _claimed_deploy_and_verifier(db, team_user)
    stopped = []

    def stop_environment(_db, _user, public_id):
        stopped.append(public_id)
        row = db.query(SandboxEnvironment).filter_by(public_id=public_id).one()
        row.status = "stopped"
        db.commit()
        return {"public_id": public_id, "purpose": "deploy", "status": "stopped"}

    monkeypatch.setattr(sandbox_service, "stop_environment", stop_environment)

    released = agent_team_service.handoff_dependency_runtime_resources(
        db,
        owner_user_id=team_user.id,
        team_id=created["team_id"],
        task_id=verifier_claim["task_id"],
        lease_token=verifier_claim["lease_token"],
    )

    assert released == [{"public_id": "sbx_ready_deploy", "purpose": "deploy", "status": "stopped"}]
    assert stopped == ["sbx_ready_deploy"]
    assert agent_team_service._active_task_runtime_resources(db, deploy_claim["task_id"]) == []
    deploy_task = db.get(AgentTeamTask, deploy_claim["task_id"])
    assert agent_team_service._unjson(deploy_task.result_json, {}) == deploy_result
    stopped_event = (
        db.query(AgentTeamEvent)
        .filter_by(task_id=deploy_claim["task_id"], event_type="task.runtime_resource_stopped")
        .one()
    )
    assert "dependency_handoff_to_validation" in stopped_event.detail_json


def test_handoff_rejects_non_terminal_stop_and_does_not_close_ledger(db, team_user, monkeypatch):
    created, deploy_claim, verifier_claim, _environment, _result = _claimed_deploy_and_verifier(db, team_user)
    monkeypatch.setattr(
        sandbox_service,
        "stop_environment",
        lambda *_args, **_kwargs: {
            "public_id": "sbx_ready_deploy",
            "purpose": "deploy",
            "status": "stopping",
        },
    )

    with pytest.raises(agent_team_service.AgentTeamStateError, match="未达到终态"):
        agent_team_service.handoff_dependency_runtime_resources(
            db,
            owner_user_id=team_user.id,
            team_id=created["team_id"],
            task_id=verifier_claim["task_id"],
            lease_token=verifier_claim["lease_token"],
        )

    assert agent_team_service._active_task_runtime_resources(db, deploy_claim["task_id"]) == [
        {"resource_type": "sandbox_environment", "resource_id": "sbx_ready_deploy"}
    ]
    assert (
        db.query(AgentTeamEvent)
        .filter_by(task_id=deploy_claim["task_id"], event_type="task.runtime_resource_stop_failed")
        .count()
        == 1
    )


def test_handoff_rolls_back_poisoned_session_before_recording_stop_failure(db, team_user, monkeypatch):
    created, deploy_claim, verifier_claim, _environment, _result = _claimed_deploy_and_verifier(db, team_user)
    real_rollback = db.rollback
    rollback_calls: list[bool] = []

    def tracked_rollback():
        rollback_calls.append(True)
        real_rollback()

    monkeypatch.setattr(db, "rollback", tracked_rollback)

    def poison_real_session(*_args, **_kwargs):
        db.add(User(username="handoff-owner", password="x", role="user", status=1))
        db.flush()

    monkeypatch.setattr(sandbox_service, "stop_environment", poison_real_session)

    with pytest.raises(agent_team_service.AgentTeamStateError, match="未达到终态"):
        agent_team_service.handoff_dependency_runtime_resources(
            db,
            owner_user_id=team_user.id,
            team_id=created["team_id"],
            task_id=verifier_claim["task_id"],
            lease_token=verifier_claim["lease_token"],
        )

    assert rollback_calls
    assert agent_team_service._active_task_runtime_resources(db, deploy_claim["task_id"]) == [
        {"resource_type": "sandbox_environment", "resource_id": "sbx_ready_deploy"}
    ]
    failed_event = (
        db.query(AgentTeamEvent)
        .filter_by(task_id=deploy_claim["task_id"], event_type="task.runtime_resource_stop_failed")
        .one()
    )
    assert "UNIQUE constraint failed" in failed_event.detail_json


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("project_id", 999, "项目不一致"),
        ("source_revision_id", 999, "源码修订不一致"),
        ("owner_id", 88, "所属账户不一致"),
    ],
)
def test_handoff_rejects_environment_outside_dependency_scope(db, team_user, monkeypatch, field, value, message):
    created, _deploy_claim, verifier_claim, environment, _result = _claimed_deploy_and_verifier(db, team_user)
    if field == "source_revision_id":
        config = json.loads(environment.agent_config_json)
        config["source_revision_id"] = value
        environment.agent_config_json = json.dumps(config)
    else:
        setattr(environment, field, value)
    db.commit()
    stopped = []
    monkeypatch.setattr(
        sandbox_service,
        "stop_environment",
        lambda *_args, **_kwargs: stopped.append(True) or {"status": "stopped"},
    )

    with pytest.raises(agent_team_service.AgentTeamStateError, match=message):
        agent_team_service.handoff_dependency_runtime_resources(
            db,
            owner_user_id=team_user.id,
            team_id=created["team_id"],
            task_id=verifier_claim["task_id"],
            lease_token=verifier_claim["lease_token"],
        )

    assert stopped == []


def test_handoff_ignores_forged_result_snapshot_and_non_direct_resource(db, team_user, monkeypatch):
    created, deploy_claim, verifier_claim, direct_environment, _result = _claimed_deploy_and_verifier(db, team_user)
    deploy_task = db.get(AgentTeamTask, deploy_claim["task_id"])
    deploy_task.result_json = json.dumps(
        {
            "artifacts": [
                {
                    "type": "sandbox_environment",
                    "data": {"public_id": "sbx_other_account", "project_id": 999, "purpose": "deploy"},
                }
            ]
        }
    )
    other_member = AgentTeamMember(
        team_id=created["team_id"],
        member_key="other-deployer",
        display_name="非直接部署 Agent",
        address="agent:sandbox_deployer",
        kind="builtin",
        role="worker",
        capabilities_json="{}",
        status="completed",
    )
    db.add(other_member)
    db.flush()
    other_task = AgentTeamTask(
        team_id=created["team_id"],
        member_id=other_member.id,
        task_key="other-deploy",
        title="非直接部署",
        instructions="不属于当前验证任务的直接依赖",
        dependency_keys_json="[]",
        input_json=json.dumps(
            {
                "operation": "deploy",
                "project_id": direct_environment.project_id,
                "language": "python",
                "source_revision_id": 41,
            }
        ),
        status="completed",
        attempt_count=1,
        max_attempts=3,
        result_json="{}",
        artifacts_json="[]",
        errors_json="[]",
    )
    db.add(other_task)
    db.flush()
    other_environment = SandboxEnvironment(
        public_id="sbx_non_direct",
        project_id=direct_environment.project_id,
        owner_id=team_user.id,
        worker_id=None,
        agent_code="sandbox_deployer",
        purpose="deploy",
        language="python",
        test_mode="deploy",
        status="ready",
        runtime="runsc",
        image_ref="python@sha256:fixed",
        image_digest="sha256:fixed",
        source_sha256="a" * 64,
        resource_policy_json="{}",
        agent_config_json=json.dumps(
            {
                "source_revision_id": 41,
                "agent_team": {
                    "team_id": created["team_id"],
                    "task_id": other_task.id,
                    "attempt": 1,
                },
            }
        ),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db.add(other_environment)
    agent_team_service._event(
        db,
        db.get(AgentTeam, created["team_id"]),
        "task.runtime_resource_attached",
        task=other_task,
        member=other_member,
        detail={
            "resource_type": "sandbox_environment",
            "resource_id": other_environment.public_id,
            "attempt": 1,
            "lease_fingerprint": "persisted-ledger-entry",
        },
    )
    db.commit()
    stopped = []

    def stop_environment(_db, _user, public_id):
        stopped.append(public_id)
        row = db.query(SandboxEnvironment).filter_by(public_id=public_id).one()
        row.status = "stopped"
        db.commit()
        return {"public_id": public_id, "purpose": "deploy", "status": "stopped"}

    monkeypatch.setattr(sandbox_service, "stop_environment", stop_environment)
    agent_team_service.handoff_dependency_runtime_resources(
        db,
        owner_user_id=team_user.id,
        team_id=created["team_id"],
        task_id=verifier_claim["task_id"],
        lease_token=verifier_claim["lease_token"],
    )
    assert stopped == ["sbx_ready_deploy"]
    assert db.query(SandboxEnvironment).filter_by(public_id="sbx_non_direct").one().status == "ready"
    assert agent_team_service._active_task_runtime_resources(db, other_task.id) == [
        {"resource_type": "sandbox_environment", "resource_id": "sbx_non_direct"}
    ]


def test_handoff_cleans_old_deploy_but_rechecks_cancelled_verifier_lease(db, team_user, monkeypatch):
    created, _deploy_claim, verifier_claim, _environment, _result = _claimed_deploy_and_verifier(db, team_user)

    def stop_and_cancel(_db, _user, public_id):
        environment = db.query(SandboxEnvironment).filter_by(public_id=public_id).one()
        environment.status = "stopped"
        team = db.get(AgentTeam, created["team_id"])
        task = db.get(AgentTeamTask, verifier_claim["task_id"])
        team.status = "cancelled"
        task.status = "cancelled"
        task.lease_token = None
        db.commit()
        return {"public_id": public_id, "purpose": "deploy", "status": "stopped"}

    monkeypatch.setattr(sandbox_service, "stop_environment", stop_and_cancel)

    with pytest.raises(agent_team_service.AgentTeamLeaseError, match="租约"):
        agent_team_service.handoff_dependency_runtime_resources(
            db,
            owner_user_id=team_user.id,
            team_id=created["team_id"],
            task_id=verifier_claim["task_id"],
            lease_token=verifier_claim["lease_token"],
        )

    assert db.query(SandboxEnvironment).filter_by(public_id="sbx_ready_deploy").one().status == "stopped"


def test_handoff_retry_is_idempotent_after_stop_event_write_crash(db, team_user, monkeypatch):
    created, _deploy_claim, verifier_claim, _environment, _result = _claimed_deploy_and_verifier(db, team_user)
    original_event = agent_team_service._event
    event_write_attempts = []

    def stop_environment(_db, _user, public_id):
        row = db.query(SandboxEnvironment).filter_by(public_id=public_id).one()
        row.status = "stopped"
        db.commit()
        return {"public_id": public_id, "purpose": "deploy", "status": "stopped"}

    def fail_once(*args, **kwargs):
        if args[2] == "task.runtime_resource_stopped" and not event_write_attempts:
            event_write_attempts.append("failed")
            raise RuntimeError("event commit crash")
        return original_event(*args, **kwargs)

    monkeypatch.setattr(sandbox_service, "stop_environment", stop_environment)
    monkeypatch.setattr(agent_team_service, "_event", fail_once)
    with pytest.raises(RuntimeError, match="event commit crash"):
        agent_team_service.handoff_dependency_runtime_resources(
            db,
            owner_user_id=team_user.id,
            team_id=created["team_id"],
            task_id=verifier_claim["task_id"],
            lease_token=verifier_claim["lease_token"],
        )
    db.rollback()

    released = agent_team_service.handoff_dependency_runtime_resources(
        db,
        owner_user_id=team_user.id,
        team_id=created["team_id"],
        task_id=verifier_claim["task_id"],
        lease_token=verifier_claim["lease_token"],
    )
    assert released[0]["status"] == "stopped"


def test_failure_is_learned_and_retry_requires_changed_strategy(db, team_user):
    created = agent_team_service.create_team(
        db,
        team_user,
        _payload(
            tasks=[
                {
                    "task_key": "read",
                    "member_key": "reader",
                    "title": "读取",
                    "instructions": "使用路径 A",
                    "depends_on": [],
                    "max_attempts": 1,
                },
                {
                    "task_key": "verify",
                    "member_key": "reviewer",
                    "title": "验证",
                    "instructions": "复核读取结果",
                    "depends_on": ["read"],
                },
            ]
        ),
    )
    claimed = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    failed = agent_team_service.complete_task(
        db,
        created["team_id"],
        claimed["task_id"],
        lease_token=claimed["lease_token"],
        result={"status": "failed", "summary": "路径 A 上游超时"},
        success=False,
        error="上游超时",
    )
    assert failed["status"] == "failed"
    memory = db.query(AgentMemory).filter(AgentMemory.memory_type == "execution_strategy").one()
    assert memory.outcome == "failure"

    with pytest.raises(agent_team_service.AgentTeamValidationError, match="改变方案"):
        agent_team_service.retry_team(db, team_user, created["team_id"], task_keys=["read"], strategy_changes={})

    retried = agent_team_service.retry_team(
        db,
        team_user,
        created["team_id"],
        task_keys=["read"],
        strategy_changes={"read": "先刷新实时状态，再使用路径 B 并缩小读取范围"},
    )
    assert retried["status"] == "queued"
    assert "路径 B" in retried["tasks"][0]["instructions"]
    assert retried["tasks"][0]["attempt_count"] == 1
    event = (
        db.query(AgentTeamEvent)
        .filter_by(event_type="task.retry_requested", task_id=created["tasks"][0]["task_id"])
        .one()
    )
    detail = agent_team_service._unjson(event.detail_json, {})
    assert detail["previous_strategy_hash"] != detail["new_strategy_hash"]


def test_automatic_retry_honors_bounded_resource_backoff(db, team_user, monkeypatch):
    created = agent_team_service.create_team(db, team_user, _payload())
    claimed = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    completed_at = datetime(2026, 8, 12, 1, 2, 3, tzinfo=timezone.utc)
    monkeypatch.setattr(agent_team_service, "_now", lambda: completed_at)

    agent_team_service.complete_task(
        db,
        created["team_id"],
        claimed["task_id"],
        lease_token=claimed["lease_token"],
        result={
            "status": "failed",
            "summary": "Worker 资源交接失败",
            "strategy_change": "重新核对资源账本后再尝试交接",
            "retry_after_seconds": 5,
        },
        success=False,
        error="Worker 资源交接失败",
    )

    task = db.get(AgentTeamTask, claimed["task_id"])
    persisted_next_attempt = task.next_attempt_at
    if persisted_next_attempt.tzinfo is None:
        persisted_next_attempt = persisted_next_attempt.replace(tzinfo=timezone.utc)
    assert persisted_next_attempt == completed_at + timedelta(seconds=5)
    event = db.query(AgentTeamEvent).filter_by(event_type="task.retry_queued", task_id=task.id).one()
    assert agent_team_service._unjson(event.detail_json, {})["retry_after_seconds"] == 5

    monkeypatch.setattr(agent_team_service, "_now", lambda: completed_at + timedelta(seconds=4))
    assert agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60) is None
    monkeypatch.setattr(agent_team_service, "_now", lambda: completed_at + timedelta(seconds=5))
    retry_claim = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    assert retry_claim["task_id"] == claimed["task_id"]

    second_completed_at = completed_at + timedelta(seconds=6)
    monkeypatch.setattr(agent_team_service, "_now", lambda: second_completed_at)
    agent_team_service.complete_task(
        db,
        created["team_id"],
        retry_claim["task_id"],
        lease_token=retry_claim["lease_token"],
        result={
            "status": "failed",
            "summary": "Worker 仍然不可用",
            "strategy_change": "延长等待后切换到其他可用 Worker",
            "retry_after_seconds": 999,
        },
        success=False,
        error="Worker 仍然不可用",
    )
    capped_task = db.get(AgentTeamTask, retry_claim["task_id"])
    capped_next_attempt = capped_task.next_attempt_at
    if capped_next_attempt.tzinfo is None:
        capped_next_attempt = capped_next_attempt.replace(tzinfo=timezone.utc)
    assert capped_next_attempt == second_completed_at + timedelta(seconds=300)


def test_retry_failed_root_releases_blocked_descendants_without_reusing_attempt_ids(db, team_user):
    created = agent_team_service.create_team(
        db,
        team_user,
        _payload(
            tasks=[
                {
                    "task_key": "read",
                    "member_key": "reader",
                    "title": "读取项目",
                    "instructions": "读取项目",
                    "depends_on": [],
                    "max_attempts": 1,
                },
                {
                    "task_key": "verify",
                    "member_key": "reviewer",
                    "title": "验证结果",
                    "instructions": "验证读取结果",
                    "depends_on": ["read"],
                },
            ]
        ),
    )
    first = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    failed = agent_team_service.complete_task(
        db,
        created["team_id"],
        first["task_id"],
        lease_token=first["lease_token"],
        result={"status": "failed", "summary": "路径 A 失败"},
        success=False,
        error="路径 A 失败",
    )
    assert {item["task_key"]: item["status"] for item in failed["tasks"]} == {
        "read": "dead_letter",
        "verify": "blocked",
    }

    retried = agent_team_service.retry_team(
        db,
        team_user,
        created["team_id"],
        task_keys=["read"],
        strategy_changes={"read": "改用路径 B 并先缩小输入范围"},
    )
    assert {item["task_key"]: item["status"] for item in retried["tasks"]} == {
        "read": "queued",
        "verify": "waiting_dependency",
    }
    second = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    assert second["attempt_count"] == 2
    assert second["request_message_id"] != first["request_message_id"]


def test_failure_with_remaining_budget_is_automatically_requeued_with_new_strategy(db, team_user):
    created = agent_team_service.create_team(
        db,
        team_user,
        _payload(
            tasks=[
                {
                    "task_key": "read",
                    "member_key": "reader",
                    "title": "读取",
                    "instructions": "读取项目",
                    "depends_on": [],
                    "max_attempts": 2,
                },
                {
                    "task_key": "verify",
                    "member_key": "reviewer",
                    "title": "验证",
                    "instructions": "复核项目",
                    "depends_on": ["read"],
                },
            ]
        ),
    )
    claimed = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    retried = agent_team_service.complete_task(
        db,
        created["team_id"],
        claimed["task_id"],
        lease_token=claimed["lease_token"],
        result={"status": "failed", "summary": "读取超时"},
        success=False,
        error="读取超时",
    )
    read_task = next(item for item in retried["tasks"] if item["task_key"] == "read")
    assert read_task["status"] == "queued"
    assert "自动改道" in read_task["instructions"]
    strategy = read_task["input"]["_execution_strategy"]
    assert strategy["attempt"] == 2
    assert strategy["mode"] != "repeat_same_input"
    second = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    assert second is not None
    assert second["input"]["_execution_strategy"]["instruction"]


def test_automatic_retry_preserves_full_long_instruction(db, team_user):
    created = agent_team_service.create_team(db, team_user, _payload(
        tasks=[{
            "task_key": "read", "member_key": "reader", "title": "读取",
            "instructions": "原始上下文：" + "中段证据" * 2995,
            "depends_on": [], "max_attempts": 2,
        }]
    ))
    claimed = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    retried = agent_team_service.complete_task(
        db, created["team_id"], claimed["task_id"], lease_token=claimed["lease_token"],
        result={"status": "failed", "summary": "读取超时"}, success=False, error="读取超时",
    )
    read_task = db.get(AgentTeamTask, claimed["task_id"])
    assert len(read_task.instructions) > 12000
    assert read_task.instructions.endswith("先复核前置依赖并缩小输入范围，再执行本任务")


def test_team_detail_pages_all_messages_with_a_stable_ledger_cursor(db, team_user):
    created = agent_team_service.create_team(db, team_user, _payload())
    base_time = datetime(2026, 8, 12, tzinfo=timezone.utc)
    for index in range(505):
        db.add(
            AgentMeshMessage(
                message_id=f"bulk-{index}",
                user_id=team_user.id,
                idempotency_key=f"bulk-{index}",
                trace_id=created["trace_id"],
                sent_from="agent:reader",
                send_to="agent:reviewer",
                message_type="task.progress",
                subject=f"message {index}",
                payload_json="{}",
                context_json="{}",
                artifacts_json="[]",
                errors_json="[]",
                status="completed",
                create_time=base_time + timedelta(seconds=504 - index),
            )
        )
    db.commit()

    detail = agent_team_service.get_team(db, team_user, created["team_id"])

    assert len(detail["messages"]) == 500
    assert detail["messages"][0]["message_id"] == "bulk-5"
    assert detail["messages"][-1]["message_id"] == "bulk-504"
    assert detail["message_page"]["total"] == 505
    assert detail["message_page"]["has_more"] is True

    earlier = agent_team_service.list_team_messages(
        db,
        team_user,
        created["team_id"],
        before_id=detail["message_page"]["next_before_id"],
        limit=500,
    )

    assert [item["message_id"] for item in earlier["items"]] == [f"bulk-{index}" for index in range(5)]
    assert earlier["has_more"] is False
    assert earlier["total"] == 505


def test_team_detail_uses_id_to_break_equal_message_timestamps(db, team_user):
    created = agent_team_service.create_team(db, team_user, _payload())
    same_time = datetime(2026, 8, 12, tzinfo=timezone.utc)
    for index in range(501):
        db.add(
            AgentMeshMessage(
                message_id=f"tie-{index}",
                user_id=team_user.id,
                idempotency_key=f"tie-{index}",
                trace_id=created["trace_id"],
                sent_from="agent:reader",
                send_to="agent:reviewer",
                message_type="task.progress",
                subject=f"message {index}",
                payload_json="{}",
                context_json="{}",
                artifacts_json="[]",
                errors_json="[]",
                status="completed",
                create_time=same_time,
            )
        )
    db.commit()

    detail = agent_team_service.get_team(db, team_user, created["team_id"])

    assert len(detail["messages"]) == 500
    assert detail["messages"][0]["message_id"] == "tie-1"
    assert detail["messages"][-1]["message_id"] == "tie-500"


def test_team_message_pages_remain_scoped_to_the_team_owner(db, team_user):
    created = agent_team_service.create_team(db, team_user, _payload())

    with pytest.raises(agent_team_service.AgentTeamNotFoundError):
        agent_team_service.list_team_messages(
            db,
            SimpleNamespace(id=team_user.id + 1, role="user", username="other"),
            created["team_id"],
            before_id=0,
            limit=100,
        )


def test_non_retryable_business_failure_stays_failed_without_automatic_requeue(db, team_user):
    created = agent_team_service.create_team(db, team_user, _payload())
    claimed = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)

    after_failure = agent_team_service.complete_task(
        db,
        created["team_id"],
        claimed["task_id"],
        lease_token=claimed["lease_token"],
        result={
            "status": "failed",
            "summary": "黑盒安全基线未通过",
            "retryable": False,
            "next_action": {"fix_business_source": True},
        },
        success=False,
        error="AssertionError: expected 403, received 200",
    )

    failed_task = next(item for item in after_failure["tasks"] if item["task_key"] == "read")
    assert failed_task["status"] == "failed"
    assert failed_task["next_attempt_at"] is None


def test_normalize_readonly_task_inputs_fixes_invalid_operations() -> None:
    payload = AgentTeamCreateIn.model_validate({
        "surface": "user",
        "session_id": "session-a1",
        "title": "只读团队",
        "objective": "全程只读验收，严禁运行新测试",
        "members": [
            {"member_key": "ro", "display_name": "只读审查", "address": "agent:review_orchestrator", "role": "worker"},
            {"member_key": "tv", "display_name": "测试验证", "address": "agent:test_verifier", "role": "verifier"},
            {"member_key": "pa", "display_name": "项目分析", "address": "agent:project_analyzer", "role": "worker"},
            {"member_key": "cfm", "display_name": "文件清单", "address": "agent:code_file_manager", "role": "worker"},
            {"member_key": "db", "display_name": "看板", "address": "agent:dashboard", "role": "worker"},
            {"member_key": "sum", "display_name": "汇总", "address": "agent:reporter", "role": "summarizer"},
        ],
        "tasks": [
            {"task_key": "ro", "member_key": "ro", "title": "核验审查记录", "instructions": "只读核验审查记录", "input": {"operation": "start_review"}},  # noqa: E501
            {"task_key": "tv", "member_key": "tv", "title": "核验测试结果", "instructions": "只读核验历史测试结果", "input": {"operation": "run_project_tests", "project_id": 153}},  # noqa: E501
            {"task_key": "pa", "member_key": "pa", "title": "项目详情", "instructions": "只读查项目详情", "input": {"project_id": 153}},  # noqa: E501
            {"task_key": "cfm", "member_key": "cfm", "title": "文件清单", "instructions": "只读列文件", "input": {"project_id": 153}},  # noqa: E501
            {"task_key": "db", "member_key": "db", "title": "看板汇总", "instructions": "只读看板汇总", "input": {}},
            {"task_key": "sum", "member_key": "sum", "title": "汇总", "instructions": "汇总", "depends_on": ["ro", "tv", "pa", "cfm", "db"]},  # noqa: E501
        ],
    })
    normalized = agent_team_service._normalize_readonly_task_inputs(payload)
    by_key = {item.task_key: item.input for item in normalized.tasks}
    assert by_key["ro"]["operation"] == "list"
    assert by_key["tv"]["operation"] == "inspect_existing_results"
    assert by_key["pa"]["operation"] == "inspect_project"
    assert by_key["cfm"]["operation"] == "list"
    assert by_key["db"]["operation"] == "summary"


def test_non_readonly_team_keeps_original_task_inputs() -> None:
    payload = AgentTeamCreateIn.model_validate({
        "surface": "user",
        "session_id": "session-a1",
        "title": "执行测试团队",
        "objective": "实际运行项目测试",
        "members": [
            {"member_key": "tv", "display_name": "测试验证", "address": "agent:test_verifier", "role": "verifier"},
        ],
        "tasks": [
            {"task_key": "run", "member_key": "tv", "title": "运行测试", "instructions": "运行项目测试", "input": {"operation": "run_project_tests", "project_id": 153, "language": "python"}},  # noqa: E501
        ],
    })
    normalized = agent_team_service._normalize_readonly_task_inputs(payload)
    assert normalized.tasks[0].input["operation"] == "run_project_tests"


def test_list_team_events_incremental_pagination(db, team_user):
    """增量事件流:首次全量,之后只拿 after_id 之后的新事件,支撑前端思考城市实时渲染。"""
    created = agent_team_service.create_team(db, team_user, _payload())
    team_id = created["team_id"]

    first = agent_team_service.list_team_events(db, team_user, team_id, after_id=0, limit=500)
    assert first["items"], "建队时应已产生 team.created 等事件"
    assert first["team_status"] == "queued"
    first_ids = [item["event_id"] for item in first["items"]]
    assert first_ids == sorted(first_ids), "事件必须按 id 升序"
    assert first["next_after_id"] == first_ids[-1]

    # 推进一次任务,产生新事件
    claim = agent_team_service.claim_next_task(db, team_id, lease_seconds=60)
    agent_team_service.complete_task(
        db,
        team_id,
        claim["task_id"],
        lease_token=claim["lease_token"],
        result={"status": "completed", "summary": "ok"},
    )

    incremental = agent_team_service.list_team_events(
        db, team_user, team_id, after_id=first["next_after_id"], limit=500
    )
    assert incremental["items"], "after_id 之后应能拿到 task.claimed/task.completed"
    new_types = {item["event_type"] for item in incremental["items"]}
    assert "task.claimed" in new_types
    assert "task.completed" in new_types
    # 增量结果不应与首次重复
    assert not ({item["event_id"] for item in incremental["items"]} & set(first_ids))


def test_list_team_events_limit_and_has_more(db, team_user):
    created = agent_team_service.create_team(db, team_user, _payload())
    team_id = created["team_id"]
    page = agent_team_service.list_team_events(db, team_user, team_id, after_id=0, limit=1)
    assert len(page["items"]) == 1
    if page["has_more"]:
        nxt = agent_team_service.list_team_events(
            db, team_user, team_id, after_id=page["next_after_id"], limit=500
        )
        assert nxt["items"][0]["event_id"] > page["items"][0]["event_id"]
