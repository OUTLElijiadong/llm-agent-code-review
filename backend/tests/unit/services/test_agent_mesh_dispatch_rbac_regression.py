"""Agent Mesh 直接分发边界的 RBAC 回归覆盖。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.permission_codes import PermissionCode
from app.models.agent_team import AgentTeamTask
from app.models.project import Project
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.user import User
from app.schemas.agent_team import AgentTeamCreateIn, AgentTeamTaskIn
from app.services import agent_mesh_dispatcher, agent_team_dispatcher, agent_team_service


def _user_with_permissions(db, username: str, codes: set[str]) -> tuple[User, Role]:
    user = User(username=username, password="x", role="reviewer", status=1)
    role = Role(name=f"{username}-role", code="reviewer", status="active", is_builtin=0)
    db.add_all([user, role])
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    for code in sorted(codes):
        permission = Permission(code=code, name=code, module=code.split(":", 1)[0], type="api")
        db.add(permission)
        db.flush()
        db.add(RolePermission(role_id=role.id, permission_id=permission.id))
    db.commit()
    return user, role


def _owned_project(db, user: User) -> Project:
    project = Project(user_id=user.id, project_name=f"rbac-project-{user.username}", status="active")
    db.add(project)
    db.commit()
    return project


@pytest.mark.parametrize(
    ("granted", "missing"),
    [
        ({PermissionCode.FILE_VIEW}, PermissionCode.PROJECT_VIEW),
        ({PermissionCode.PROJECT_VIEW}, PermissionCode.FILE_VIEW),
    ],
    ids=["project-view-missing", "file-view-missing"],
)
def test_project_analyzer_dispatch_requires_each_read_permission(
    monkeypatch, db, granted, missing,
):
    user, _role = _user_with_permissions(db, f"analyzer-{missing.split(':')[0]}", granted)
    project = _owned_project(db, user)
    monkeypatch.setattr(
        "app.agents.orchestrator.get_request_orchestrator",
        lambda *_args, **_kwargs: pytest.fail("RBAC 拒绝前不得装载项目/文件读取服务"),
    )

    result = agent_mesh_dispatcher._runtime_handler(
        db,
        user,
        "project_analyzer",
        {
            "user_id": user.id,
            "payload": {"operation": "inspect_project", "project_id": project.id},
            "context": {"team_id": 71},
        },
        trusted_team_execution=True,
    )

    assert result["status"] == "blocked"
    assert result["errors"] == [{"code": "insufficient_permission", "permissions": [missing]}]


@pytest.mark.parametrize(
    ("granted", "missing"),
    [
        ({PermissionCode.FILE_VIEW}, PermissionCode.PROJECT_VIEW),
        ({PermissionCode.PROJECT_VIEW}, PermissionCode.FILE_VIEW),
    ],
    ids=["project-view-missing", "file-view-missing"],
)
def test_project_analyzer_task_creation_requires_each_read_permission(db, granted, missing):
    user, _role = _user_with_permissions(db, f"create-analyzer-{missing.split(':')[0]}", granted)
    project = _owned_project(db, user)
    task = AgentTeamTaskIn.model_validate({
        "task_key": "inspect",
        "member_key": "reader",
        "title": "读取项目事实",
        "instructions": "读取项目概况和文件清单",
        "input": {"operation": "inspect_project", "project_id": project.id},
    })

    with pytest.raises(agent_team_service.AgentTeamAccessError, match=missing):
        agent_team_service._validate_task_scope(db, user, task, "agent:project_analyzer")


def test_readonly_test_verifier_without_review_view_is_blocked_at_create_and_dispatch(
    monkeypatch, db,
):
    user, _role = _user_with_permissions(db, "readonly-verifier", set())
    project = _owned_project(db, user)
    instructions = "只读核对已有审查任务，严禁运行新测试或创建沙箱"
    task = AgentTeamTaskIn.model_validate({
        "task_key": "verify-history",
        "member_key": "verifier",
        "title": "核对历史结果",
        "instructions": instructions,
        "input": {"project_id": project.id},
    })

    with pytest.raises(agent_team_service.AgentTeamAccessError, match=PermissionCode.REVIEW_VIEW):
        agent_team_service._validate_task_scope(db, user, task, "agent:test_verifier")

    monkeypatch.setattr(
        "app.agents.orchestrator.get_request_orchestrator",
        lambda *_args, **_kwargs: pytest.fail("缺少 review:view 时不得查询历史审查任务"),
    )
    result = agent_mesh_dispatcher._runtime_handler(
        db,
        user,
        "test_verifier",
        {
            "user_id": user.id,
            "payload": {"project_id": project.id, "instructions": instructions},
            "context": {"team_id": 72},
        },
        trusted_team_execution=True,
    )

    assert result["status"] == "blocked"
    assert result["errors"] == [{
        "code": "insufficient_permission",
        "permissions": [PermissionCode.REVIEW_VIEW],
    }]


def test_dependency_reporter_can_summarize_without_report_view(monkeypatch, db):
    user, _role = _user_with_permissions(db, "dependency-reporter", set())
    task = AgentTeamTaskIn.model_validate({
        "task_key": "summary",
        "member_key": "reporter",
        "title": "汇总团队结果",
        "instructions": "只汇总前置任务的结果",
        "depends_on": ["facts"],
        "input": {},
    })
    # 依赖摘要不读取报告表，建队时不应额外要求 report:view。
    agent_team_service._validate_task_scope(db, user, task, "agent:reporter")

    list_reports = Mock(side_effect=AssertionError("团队依赖摘要不应查询报告列表"))
    get_report_detail = Mock(side_effect=AssertionError("团队依赖摘要不应查询报告详情"))
    monkeypatch.setattr(
        "app.agents.orchestrator.get_request_orchestrator",
        lambda *_args, **_kwargs: SimpleNamespace(
            reporter=SimpleNamespace(list_reports=list_reports, get_report_detail=get_report_detail),
        ),
    )
    result = agent_mesh_dispatcher._runtime_handler(
        db,
        user,
        "reporter",
        {
            "user_id": user.id,
            "payload": {"dependency_context": {
                "facts": {
                    "status": "completed",
                    "result": {"status": "completed", "summary": "项目事实已核验"},
                },
            }},
            # 即便携带 task_id，也应优先汇总依赖结果，不能退回数据库读取。
            "context": {"task_id": 999},
        },
        trusted_team_execution=True,
    )

    assert result["status"] == "completed"
    assert result["evidence"][0]["data"]["result"]["summary"] == "项目事实已核验"
    list_reports.assert_not_called()
    get_report_detail.assert_not_called()


@pytest.mark.parametrize("revoked", [PermissionCode.PROJECT_VIEW, PermissionCode.FILE_VIEW])
def test_worker_rechecks_project_analyzer_permissions_after_team_creation(
    monkeypatch, db, revoked,
):
    user, role = _user_with_permissions(
        db,
        f"worker-revoke-{revoked.split(':')[0]}",
        {
            PermissionCode.AGENT_CHAT,
            PermissionCode.PROJECT_VIEW,
            PermissionCode.FILE_VIEW,
        },
    )
    project = _owned_project(db, user)
    payload = AgentTeamCreateIn.model_validate({
        "surface": "user",
        "session_id": f"session-{revoked.split(':')[0]}-worker",
        "title": "权限撤销后的 worker 检查",
        "objective": "读取项目事实",
        "members": [{
            "member_key": "reader",
            "display_name": "项目分析 Agent",
            "address": "agent:project_analyzer",
            "role": "verifier",
        }],
        "tasks": [{
            "task_key": "inspect",
            "member_key": "reader",
            "title": "读取项目概况",
            "instructions": "检查指定项目概况和文件列表",
            "max_attempts": 1,
            "input": {"operation": "inspect_project", "project_id": project.id},
        }],
    })
    created = agent_team_service.create_team_from_xiaoling(db, user, payload)
    claimed = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    assert claimed is not None

    permission = db.query(Permission).filter_by(code=revoked).one()
    role_permission = db.query(RolePermission).filter_by(
        role_id=role.id, permission_id=permission.id,
    ).one()
    db.delete(role_permission)
    db.commit()

    orchestrator = SimpleNamespace(
        get_project_detail=Mock(),
        file_mgr=SimpleNamespace(list_files=Mock()),
    )
    monkeypatch.setattr(
        "app.agents.orchestrator.get_request_orchestrator",
        lambda *_args, **_kwargs: orchestrator,
    )

    def dispatch_through_runtime(worker_db, worker_user, address, message, *, trusted_team_execution=False):
        assert address == "agent:project_analyzer"
        assert trusted_team_execution is True
        return "项目分析 Agent", agent_mesh_dispatcher._runtime_handler(
            worker_db,
            worker_user,
            "project_analyzer",
            message,
            trusted_team_execution=trusted_team_execution,
        )

    monkeypatch.setattr(agent_mesh_dispatcher, "_handle", dispatch_through_runtime)
    monkeypatch.setattr(
        agent_team_dispatcher,
        "SessionLocal",
        sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False),
    )

    outcome = agent_team_dispatcher._execute_claimed(created["team_id"], claimed)

    assert outcome == {"success": False}
    orchestrator.get_project_detail.assert_not_called()
    orchestrator.file_mgr.list_files.assert_not_called()
    db.expire_all()
    saved_task = db.get(AgentTeamTask, claimed["task_id"])
    assert saved_task.status in {"failed", "dead_letter"}
    assert revoked in saved_task.result_json
