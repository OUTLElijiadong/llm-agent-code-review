"""真实 JWT/SQLite 验证审查读取的统一防枚举错误与合法成员回归。"""
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.review import router
from app.api.v1.projects import router as projects_router
from app.core.database import Base, get_db
from app.core.error_handlers import register_handlers
from app.core.exceptions import ServiceUnavailableError
from app.core.security import create_access_token
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.review_report import ReviewReport
from app.models.review_task import ReviewTask
from app.models.user import User
from app.services import project_member_service


@pytest.fixture
def review_http():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    users = {name: User(id=index, username=f'review_access_{name}', password='isolated', role='user', status=1)
             for index, name in enumerate(('owner', 'member', 'outsider', 'no_permission', 'disabled'), 1)}
    users['disabled'].status = 0
    role = Role(id=1, name='普通用户', code='user', status='active', is_builtin=1)
    db.add_all([*users.values(), role])
    for index, code in enumerate(('review:view', 'issue:view', 'project:view'), 1):
        db.add(Permission(id=index, code=code, name=code, module=code.split(':')[0], type='api'))
        db.add(RolePermission(role_id=role.id, permission_id=index))
    for name in ('owner', 'member', 'outsider', 'disabled'):
        db.add(UserRole(user_id=users[name].id, role_id=role.id))
    for index, status in enumerate(('active', 'deleted', 'quarantined'), 11):
        db.add(Project(id=index, user_id=users['owner'].id, project_name=f'private-project-{index}', status=status))
        db.add(ReviewTask(id=index, user_id=users['owner'].id, project_id=index,
                          task_name=f'private-task-{index}', status='success'))
    db.add(ProjectMember(project_id=11, user_id=users['member'].id, role_in_project='reviewer'))
    db.add(ReviewTask(id=14, user_id=users['owner'].id, project_id=999,
                      task_name='private-missing-parent', status='success'))
    db.add(ReviewTask(id=15, user_id=users['owner'].id, project_id=11,
                      task_name='private-deleted-task', status='deleted'))
    db.commit()
    application = FastAPI()
    register_handlers(application)
    application.include_router(router, prefix='/api/review')
    application.include_router(projects_router, prefix='/api/projects')
    application.dependency_overrides[get_db] = lambda: db
    client = TestClient(application, raise_server_exceptions=False)
    writes = []

    def track_write(_conn, _cursor, statement, *_args):
        if statement.lstrip().split(' ', 1)[0].upper() in {'INSERT', 'UPDATE', 'DELETE', 'REPLACE'}:
            writes.append(statement.split(' ', 1)[0])

    event.listen(engine, 'before_cursor_execute', track_write)
    yield client, users, writes, db
    client.close()
    db.close()
    engine.dispose()


def headers(user):
    return {'Authorization': 'Bearer ' + create_access_token(user.id, user.role, user.token_version or 0)}


@pytest.mark.parametrize('suffix', ['', '/issues'])
def test_missing_invisible_deleted_and_parent_unavailable_have_identical_review_error(review_http, suffix):
    client, users, writes, _db = review_http
    bodies = []
    cases = [('owner', 162), ('outsider', 11), ('owner', 12), ('owner', 13), ('owner', 14), ('owner', 15)]
    for actor, task_id in cases:
        response = client.get(f'/api/review/tasks/{task_id}{suffix}', headers=headers(users[actor]))
        assert response.status_code == 404
        body = response.json()
        assert body['code'] == 40400
        assert body.get('detail') is None
        assert 'private-' not in response.text and 'project_id' not in response.text
        body.pop('request_id')
        bodies.append(body)
    assert all(body == bodies[0] for body in bodies)
    assert bodies[0]['message'] == '审查任务不存在或当前账号无权访问'
    assert bodies[0]['retryable'] is False
    assert bodies[0]['next_action'] == '请返回审查任务列表重新选择；如需访问，请联系项目负责人确认权限'
    assert writes == []


@pytest.mark.parametrize('actor', ['owner', 'member'])
@pytest.mark.parametrize('suffix', ['', '/issues'])
def test_visible_owner_and_reviewer_keep_success(review_http, actor, suffix):
    client, users, writes, _db = review_http
    response = client.get(f'/api/review/tasks/11{suffix}', headers=headers(users[actor]))
    assert response.status_code == 200
    assert response.json()['code'] == 0
    assert writes == []


@pytest.mark.parametrize('suffix', ['', '/issues'])
@pytest.mark.parametrize(('actor', 'status', 'code'), [
    (None, 401, 40100), ('no_permission', 403, 40303), ('disabled', 403, 40301),
])
def test_authentication_and_permission_errors_are_not_masked_as_not_found(review_http, suffix, actor, status, code):
    client, users, writes, _db = review_http
    response = client.get(f'/api/review/tasks/162{suffix}', headers=headers(users[actor]) if actor else {})
    assert response.status_code == status
    assert response.json()['code'] == code
    assert writes == []


@pytest.mark.parametrize('suffix', ['', '/issues'])
def test_project_read_infrastructure_failure_is_not_masked_as_not_found(review_http, monkeypatch, suffix):
    client, users, writes, _db = review_http

    def unavailable(*_args, **_kwargs):
        raise ServiceUnavailableError('隔离测试：连接暂不可用')

    monkeypatch.setattr(project_member_service, 'require_project_access', unavailable)
    response = client.get(f'/api/review/tasks/11{suffix}', headers=headers(users['owner']))
    assert response.status_code == 503
    assert response.json()['message'] == '隔离测试：连接暂不可用'
    assert response.json()['retryable'] is True
    assert writes == []


@pytest.mark.parametrize("review_type", ["sandbox_test", "pentest"])
def test_domain_report_body_is_hidden_from_task_detail_without_report_scope(review_http, review_type):
    client, users, writes, db = review_http
    task = db.get(ReviewTask, 11)
    task.review_type = review_type
    task.summary = "UNIQUE_SECRET_DOMAIN_REPORT_BODY"
    task.total_issues = 17
    task.severe_issues = 3
    task.high_issues = 4
    task.medium_issues = 5
    task.low_issues = 5
    task.score = 72
    task.score_breakdown = {"private_metric": "UNIQUE_SECRET_DOMAIN_REPORT_BODY"}
    if review_type == "sandbox_test":
        db.add(ReviewReport(
            task_id=task.id,
            user_id=task.user_id,
            content_json={"source": "sandbox_test", "report_md": task.summary},
            summary=task.summary,
            score=0,
            create_time=datetime.now(timezone.utc),
        ))
    db.commit()
    writes.clear()

    owner_response = client.get("/api/review/tasks/11", headers=headers(users["owner"]))
    member_response = client.get("/api/review/tasks/11", headers=headers(users["member"]))

    for response in (owner_response, member_response):
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["can_view_report"] is False
        assert data["summary"] is None
        assert data["total_issues"] is None
        assert data["severe_issues"] is None
        assert data["high_issues"] is None
        assert data["medium_issues"] is None
        assert data["low_issues"] is None
        assert data["score"] is None
        assert data["score_breakdown"] is None
        assert "UNIQUE_SECRET_DOMAIN_REPORT_BODY" not in response.text
    assert writes == []


@pytest.mark.parametrize("review_type", ["sandbox_test", "pentest"])
def test_domain_report_body_requires_both_report_permission_and_owner_scope(review_http, review_type):
    client, users, writes, db = review_http
    task = db.get(ReviewTask, 11)
    task.review_type = review_type
    task.summary = "AUTHORIZED_DOMAIN_REPORT_BODY"
    if review_type == "sandbox_test":
        db.add(ReviewReport(
            task_id=task.id,
            user_id=task.user_id,
            content_json={"source": "sandbox_test", "report_md": task.summary},
            summary=task.summary,
            score=0,
            create_time=datetime.now(timezone.utc),
        ))
    report_permission = Permission(id=4, code="report:view", name="查看报告", module="report", type="api")
    db.add(report_permission)
    db.add(RolePermission(role_id=1, permission_id=report_permission.id))
    db.commit()
    writes.clear()

    owner_response = client.get("/api/review/tasks/11", headers=headers(users["owner"]))
    member_response = client.get("/api/review/tasks/11", headers=headers(users["member"]))

    assert owner_response.status_code == member_response.status_code == 200
    owner_data = owner_response.json()["data"]
    member_data = member_response.json()["data"]
    assert owner_data["can_view_report"] is True
    assert owner_data["summary"] == "AUTHORIZED_DOMAIN_REPORT_BODY"
    assert member_data["can_view_report"] is False
    assert member_data["summary"] is None
    assert "AUTHORIZED_DOMAIN_REPORT_BODY" not in member_response.text
    assert writes == []


@pytest.mark.parametrize("review_type", ["sandbox_test", "pentest"])
def test_domain_report_metrics_require_report_permission_and_owner_scope(review_http, review_type):
    client, users, writes, db = review_http
    task = db.get(ReviewTask, 11)
    task.review_type = review_type
    task.total_issues = 41
    task.severe_issues = 11
    task.high_issues = 12
    task.medium_issues = 13
    task.low_issues = 14
    task.score = 73
    task.score_breakdown = {"private_metric": "REPORT_METRIC_SECRET"}
    task.status = "success"
    if review_type == "sandbox_test":
        db.add(ReviewReport(
            task_id=task.id,
            user_id=task.user_id,
            content_json={
                "source": "sandbox_test",
                "report_md": "## 问题清单\n### [严重] REPORT_METRIC_SECRET\n证据：仅供报告授权用户查看",
            },
            summary="REPORT_METRIC_SECRET",
            score=73,
            create_time=datetime.now(timezone.utc),
        ))

    report_role = Role(id=2, name="审查员", code="reviewer", status="active", is_builtin=0)
    report_permission = Permission(id=4, code="report:view", name="查看报告", module="report", type="api")
    db.add_all([report_role, report_permission])
    db.flush()
    db.query(UserRole).filter(UserRole.user_id == users["owner"].id).delete()
    users["owner"].role = report_role.code
    db.add_all([
        RolePermission(role_id=report_role.id, permission_id=permission_id)
        for permission_id in (1, 2, 3, report_permission.id)
    ])
    db.add(UserRole(user_id=users["owner"].id, role_id=report_role.id))
    db.commit()
    writes.clear()

    member_list = client.get("/api/review/tasks", headers=headers(users["member"]))
    owner_list = client.get("/api/review/tasks", headers=headers(users["owner"]))
    member_detail = client.get("/api/review/tasks/11", headers=headers(users["member"]))
    owner_detail = client.get("/api/review/tasks/11", headers=headers(users["owner"]))
    member_projects = client.get("/api/projects", headers=headers(users["member"]))
    owner_projects = client.get("/api/projects", headers=headers(users["owner"]))
    member_project_detail = client.get("/api/projects/11", headers=headers(users["member"]))
    owner_project_detail = client.get("/api/projects/11", headers=headers(users["owner"]))

    assert all(response.status_code == 200 for response in (
        member_list, owner_list, member_detail, owner_detail,
        member_projects, owner_projects, member_project_detail, owner_project_detail,
    ))
    member_row = next(item for item in member_list.json()["data"]["items"] if item["id"] == task.id)
    owner_row = next(item for item in owner_list.json()["data"]["items"] if item["id"] == task.id)
    member_data = member_detail.json()["data"]
    owner_data = owner_detail.json()["data"]
    member_project_row = next(
        item for item in member_projects.json()["data"]["items"] if item["id"] == 11
    )
    owner_project_row = next(
        item for item in owner_projects.json()["data"]["items"] if item["id"] == 11
    )
    member_recent = member_project_detail.json()["data"]["recent_tasks"][0]
    owner_recent = owner_project_detail.json()["data"]["recent_tasks"][0]

    for hidden in (member_row, member_data):
        assert hidden["total_issues"] is None
        assert hidden["severe_issues"] is None
        assert hidden["high_issues"] is None
        assert hidden["medium_issues"] is None
        assert hidden["low_issues"] is None
        assert hidden["score"] is None
        assert hidden["score_breakdown"] is None
        assert hidden.get("report_issue_summary") is None
        assert hidden["can_view_report"] is False
        assert "REPORT_METRIC_SECRET" not in str(hidden)

    # 项目列表和项目详情的近期任务也必须遵守同一报告权限。
    assert member_project_row["score"] is None
    assert member_recent["score"] is None
    assert member_recent["total_issues"] is None
    assert owner_project_row["score"] == 73
    assert owner_recent["score"] == 73
    assert owner_recent["total_issues"] == (1 if review_type == "sandbox_test" else 41)

    for visible in (owner_row, owner_data):
        assert visible["can_view_report"] is True
        assert visible["total_issues"] == (1 if review_type == "sandbox_test" else 41)
        assert visible["severe_issues"] == 11
        assert visible["high_issues"] == 12
        assert visible["medium_issues"] == 13
        assert visible["low_issues"] == 14
        assert visible["score"] == 73
        assert visible["score_breakdown"] == {"private_metric": "REPORT_METRIC_SECRET"}

    # report:view alone must not broaden the report endpoint's owner/admin object scope.
    db.add(RolePermission(role_id=1, permission_id=report_permission.id))
    db.commit()
    writes.clear()
    member_with_report_permission = client.get("/api/review/tasks", headers=headers(users["member"]))
    member_detail_with_report_permission = client.get("/api/review/tasks/11", headers=headers(users["member"]))
    member_project_with_report_permission = client.get("/api/projects/11", headers=headers(users["member"]))
    member_projects_with_report_permission = client.get("/api/projects", headers=headers(users["member"]))
    for response, key in (
        (member_with_report_permission, "items"),
        (member_detail_with_report_permission, None),
    ):
        assert response.status_code == 200
        payload = response.json()["data"]
        hidden = next(item for item in payload[key] if item["id"] == task.id) if key else payload
        assert hidden["can_view_report"] is False
        assert hidden["total_issues"] is None
        assert hidden["severe_issues"] is None
        assert hidden["score"] is None
        assert hidden.get("report_issue_summary") is None
    assert member_project_with_report_permission.status_code == 200
    assert member_project_with_report_permission.json()["data"]["recent_tasks"][0]["score"] is None
    assert member_project_with_report_permission.json()["data"]["recent_tasks"][0]["total_issues"] is None
    assert next(item for item in member_projects_with_report_permission.json()["data"]["items"]
                if item["id"] == 11)["score"] is None
    assert writes == []


def test_standard_review_metrics_remain_visible_to_project_member(review_http):
    client, users, _writes, db = review_http
    task = db.get(ReviewTask, 11)
    task.review_type = "quick"
    task.score = 81
    task.total_issues = 4
    db.commit()

    listing = client.get("/api/projects", headers=headers(users["member"]))
    detail = client.get("/api/projects/11", headers=headers(users["member"]))

    project_row = next(item for item in listing.json()["data"]["items"] if item["id"] == 11)
    recent = detail.json()["data"]["recent_tasks"][0]
    assert project_row["score"] == 81
    assert recent["score"] == 81
    assert recent["total_issues"] == 4
