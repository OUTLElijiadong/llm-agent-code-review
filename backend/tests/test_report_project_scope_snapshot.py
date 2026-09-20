"""私有报告同时检查项目当前权限，并展示实际扫描的历史文件元数据。"""
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.reports import router
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.core.error_handlers import register_handlers
from app.models.code_file import CodeFile
from app.models.code_version import CodeVersion
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.user import User
from app.services import report_service
from app.services.review_input_service import freeze_task_inputs


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        yield session
    engine.dispose()


@pytest.fixture
def report_scope(db):
    owner = User(username="report_owner", password="isolated", role="user", status=1)
    reviewer = User(username="report_reviewer", password="isolated", role="user", status=1)
    outsider = User(username="report_outsider", password="isolated", role="user", status=1)
    role = Role(name="普通用户", code="user", status="active", is_builtin=1)
    db.add_all([owner, reviewer, outsider, role])
    db.flush()
    for code in ("report:view", "report:export:json", "review:cancel"):
        permission = Permission(code=code, name=code, module="report", type="api")
        db.add(permission)
        db.flush()
        db.add(RolePermission(role_id=role.id, permission_id=permission.id))
    for user in (owner, reviewer, outsider):
        db.add(UserRole(user_id=user.id, role_id=role.id))
    project = Project(user_id=owner.id, project_name="scope fixture", status="active", language="python")
    db.add(project)
    db.flush()
    member = ProjectMember(project_id=project.id, user_id=reviewer.id, role_in_project="reviewer")
    task = ReviewTask(user_id=reviewer.id, project_id=project.id, task_name="member review",
                      review_type="standard", status="success", total_files=1, processed_files=1)
    code_file = CodeFile(project_id=project.id, file_name="source.py", file_path="src/source.py",
                         language="python", content="value = 1\n", status="active", version_no=1)
    db.add_all([member, task, code_file])
    db.flush()
    db.add(CodeVersion(file_id=code_file.id, version_no=1, content=code_file.content,
                       create_time=datetime.now(timezone.utc)))
    db.flush()
    freeze_task_inputs(db, task.id, [code_file])
    db.commit()
    return owner, reviewer, outsider, project, member, task, code_file


def _client(db, user):
    application = FastAPI()
    register_handlers(application)
    application.include_router(router, prefix="/reports")
    application.dependency_overrides[get_db] = lambda: db
    application.dependency_overrides[get_current_user] = lambda: user
    return TestClient(application, raise_server_exceptions=False)


def test_current_project_member_can_read_own_report(db, report_scope):
    _, reviewer, _, _, _, task, _ = report_scope
    with _client(db, reviewer) as client:
        assert client.get(f"/reports/{task.id}").status_code == 200
        assert client.get(f"/reports/tasks/{task.id}/export?format=json").status_code == 200
        assert client.get("/reports").json()["data"]["total"] == 1


@pytest.mark.parametrize("state", ["removed", "deleted", "quarantined", "missing"])
def test_report_read_and_export_revoke_with_project_visibility(db, report_scope, state):
    _, reviewer, _, project, member, task, _ = report_scope
    if state == "removed":
        db.delete(member)
    elif state == "missing":
        db.delete(project)
    else:
        project.status = state
    db.commit()
    with _client(db, reviewer) as client:
        for path in (f"/reports/{task.id}", f"/reports/tasks/{task.id}",
                     f"/reports/tasks/{task.id}/export?format=json"):
            assert client.get(path).status_code == 404, path
        assert client.get("/reports").json()["data"]["total"] == 0


@pytest.mark.parametrize("actor", ["owner", "outsider"])
def test_other_users_cannot_read_or_export_private_report(db, report_scope, actor):
    owner, _, outsider, _, _, task, _ = report_scope
    with _client(db, owner if actor == "owner" else outsider) as client:
        assert client.get(f"/reports/{task.id}").status_code == 404
        assert client.get(f"/reports/tasks/{task.id}/export?format=json").status_code == 404
        assert client.get("/reports").json()["data"]["total"] == 0


def test_report_delete_is_denied_after_project_membership_revoked(db, report_scope):
    _, reviewer, _, _, member, task, _ = report_scope
    db.delete(member)
    db.commit()
    with _client(db, reviewer) as client:
        assert client.delete(f"/reports/{task.id}").status_code == 404
    assert db.get(ReviewTask, task.id).status == "success"


@pytest.mark.parametrize("remove_current", [False, True])
@pytest.mark.parametrize("has_issue", [False, True])
def test_report_file_summary_keeps_frozen_metadata(db, report_scope, remove_current, has_issue):
    _, reviewer, _, _, _, task, code_file = report_scope
    file_id = code_file.id
    if has_issue:
        db.add(ReviewIssue(task_id=task.id, file_id=file_id, file_name="source.py", severity="高",
                           issue_type="安全漏洞", title="test", description="test", status="unfixed"))
    if remove_current:
        db.delete(code_file)
    else:
        code_file.file_name = "renamed.ts"
        code_file.language = "typescript"
        code_file.content = "const value = 2;"
    db.commit()
    result = report_service.get_report_detail(db, reviewer, task.id)
    assert len(result["files"]) == 1
    summary = result["files"][0]
    assert summary["file_id"] == file_id
    assert summary["file_name"] == "source.py"
    assert summary["language"] == "python"
    assert summary["issue_count"] == int(has_issue)
