"""C04：真实路由/RBAC/对象查询的报告授权矩阵；只替换文件渲染器。"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1 import reports
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.core.error_handlers import register_handlers
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.review_task import ReviewTask
from app.models.user import User

FORMATS = ("json", "html", "pdf", "word")
READ_ENDPOINTS = (
    ("GET", "/{id}", None),
    ("GET", "/tasks/{id}", None),
    *(("POST", "/generate", fmt) for fmt in FORMATS),
    *(("GET", f"/tasks/{{id}}/export?format={fmt}", None) for fmt in FORMATS),
    ("GET", "/{id}/export/word", None),
    ("GET", "/{id}/export/pdf", None),
)


@pytest.fixture
def scope(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    users = {
        name: User(username=f"c04-{name}", password='unit-test-password', role=role, status=1)
        for name, role in (("owner", "user"), ("author", "reviewer"), ("member", "user"),
                           ("outsider", "user"), ("admin", "admin"))
    }
    roles = {code: Role(code=code, name=f"报告权限矩阵-{code}", status="active", is_builtin=1)
             for code in ("user", "reviewer", "admin")}
    db.add_all([*users.values(), *roles.values()])
    db.flush()
    permissions = {}
    for code in ("report:view", "review:cancel", *(f"report:export:{fmt}" for fmt in FORMATS)):
        permission = Permission(code=code, name=code, module="report", type="api")
        db.add(permission)
        db.flush()
        for role_code, role in roles.items():
            link = RolePermission(role_id=role.id, permission_id=permission.id)
            db.add(link)
            if role_code == "reviewer":
                permissions[code] = link
    db.add_all(UserRole(user_id=user.id, role_id=roles[user.role].id) for user in users.values())
    project = Project(user_id=users["owner"].id, project_name="C04 本地隔离项目", status="active")
    db.add(project)
    db.flush()
    memberships = {}
    for name in ("author", "member"):
        memberships[name] = ProjectMember(project_id=project.id, user_id=users[name].id,
                                          role_in_project="reviewer")
        db.add(memberships[name])
    task = ReviewTask(user_id=users["author"].id, project_id=project.id,
                      task_name="PRIVATE_C04_REPORT", summary="PRIVATE_C04_BODY", review_type="full",
                      status="success", total_files=1, processed_files=1, score=100)
    db.add(task)
    db.commit()
    rendered = []

    def text_renderer(*args, **kwargs):
        # 导出器也接受包含页面同源元数据的冻结字典。
        rendered.append(args[0]["id"] if isinstance(args[0], dict) else args[0].id)
        return '"PRIVATE_C04_BODY"'

    def binary_renderer(*args, **kwargs):
        return text_renderer(*args, **kwargs).encode()

    for name in ("export_to_json", "export_to_html"):
        monkeypatch.setattr(reports, name, text_renderer)
    for name in ("export_to_pdf", "export_to_word"):
        monkeypatch.setattr(reports, name, binary_renderer)
    monkeypatch.setattr(reports, "_get_template_content", lambda *_args: "isolated template")
    current = {"user": users["author"]}
    application = FastAPI()
    register_handlers(application)
    application.include_router(reports.router, prefix="/reports")
    application.dependency_overrides[get_db] = lambda: db
    application.dependency_overrides[get_current_user] = lambda: current["user"]
    try:
        with TestClient(application) as client:
            yield {"client": client, "db": db, "users": users, "current": current,
                   "project": project, "task": task, "memberships": memberships,
                   "permissions": permissions, "rendered": rendered}
    finally:
        db.close()
        engine.dispose()


def _read(scope, endpoint, task_id=None):
    method, path, fmt = endpoint
    task_id = scope["task"].id if task_id is None else task_id
    kwargs = {"json": {"task_id": task_id, "format": fmt}} if fmt else {}
    return scope["client"].request(method, "/reports" + path.format(id=task_id), **kwargs)


@pytest.mark.parametrize("endpoint", READ_ENDPOINTS)
@pytest.mark.parametrize("actor", ("author", "admin"))
def test_all_report_outlets_allow_author_and_administrator(scope, endpoint, actor):
    scope["current"]["user"] = scope["users"][actor]
    response = _read(scope, endpoint)
    assert response.status_code == 200, response.text
    assert "PRIVATE_C04_BODY" in response.text


@pytest.mark.parametrize("endpoint", READ_ENDPOINTS)
@pytest.mark.parametrize("actor", ("owner", "member", "outsider"))
def test_all_report_outlets_hide_other_users_report_before_rendering(scope, endpoint, actor):
    scope["current"]["user"] = scope["users"][actor]
    response = _read(scope, endpoint)
    missing = _read(scope, endpoint, task_id=987654)
    assert response.status_code == missing.status_code == 404
    assert response.json()["code"] == missing.json()["code"] == 40400
    assert response.json()["message"] == missing.json()["message"] == "报告不存在"
    assert "PRIVATE_C04" not in response.text
    assert scope["rendered"] == []


@pytest.mark.parametrize("endpoint", READ_ENDPOINTS)
def test_all_report_outlets_recheck_author_project_membership(scope, endpoint):
    assert _read(scope, endpoint).status_code == 200
    scope["db"].delete(scope["memberships"]["author"])
    scope["db"].commit()
    scope["rendered"].clear()
    response = _read(scope, endpoint)
    assert response.status_code == 404
    assert scope["rendered"] == []


@pytest.mark.parametrize("actor", ("author", "admin"))
@pytest.mark.parametrize("project_state", ("deleted", "quarantined", "missing"))
def test_hidden_parent_project_hides_every_report_outlet_even_for_admin(scope, actor, project_state):
    scope["current"]["user"] = scope["users"][actor]
    if project_state == "missing":
        scope["db"].delete(scope["project"])
    else:
        scope["project"].status = project_state
    scope["db"].commit()
    for endpoint in READ_ENDPOINTS:
        assert _read(scope, endpoint).status_code == 404, endpoint
    assert scope["client"].get("/reports").json()["data"]["total"] == 0
    assert scope["rendered"] == []


@pytest.mark.parametrize("task_state", ("pending", "running", "failed", "cancelled", "deleted"))
def test_unavailable_standard_reports_are_hidden_on_every_outlet(scope, task_state):
    scope["task"].status = task_state
    scope["db"].commit()
    for endpoint in READ_ENDPOINTS:
        assert _read(scope, endpoint).status_code == 404, endpoint
    assert scope["client"].get("/reports").json()["data"]["total"] == 0
    assert scope["rendered"] == []


@pytest.mark.parametrize("actor", ("author", "admin", "owner", "member", "outsider"))
def test_report_list_filters_owner_before_counting_and_pagination(scope, actor):
    db, task, users = scope["db"], scope["task"], scope["users"]
    now = datetime.now(timezone.utc)
    task.create_time = now
    own = ReviewTask(user_id=users["author"].id, project_id=scope["project"].id,
                     task_name="second-own", review_type="full", status="success",
                     create_time=now - timedelta(seconds=1))
    other = ReviewTask(user_id=users["member"].id, project_id=scope["project"].id,
                       task_name="member-private", review_type="full", status="success",
                       create_time=now + timedelta(seconds=1))
    db.add_all([own, other])
    db.commit()
    scope["current"]["user"] = users[actor]
    expected = {"author": {task.id, own.id}, "admin": {task.id, own.id, other.id},
                "owner": set(), "member": {other.id}, "outsider": set()}[actor]
    collected = set()
    for page in range(1, 5):
        result = scope["client"].get("/reports", params={"page": page, "page_size": 1,
                                    "project_id": scope["project"].id}).json()["data"]
        assert result["total"] == len(expected)
        collected.update(item["task_id"] for item in result["items"])
    assert collected == expected
    missing = scope["client"].get("/reports", params={"project_id": 987654}).json()["data"]
    assert missing["total"] == 0 and missing["items"] == []


@pytest.mark.parametrize("fmt", FORMATS)
def test_generate_and_download_enforce_each_format_permission_before_rendering(scope, fmt):
    scope["db"].delete(scope["permissions"][f"report:export:{fmt}"])
    scope["db"].commit()
    endpoints = [("POST", "/generate", fmt), ("GET", f"/tasks/{{id}}/export?format={fmt}", None)]
    if fmt in ("word", "pdf"):
        endpoints.append(("GET", f"/{{id}}/export/{fmt}", None))
    for endpoint in endpoints:
        response = _read(scope, endpoint)
        assert response.status_code == 403
        assert response.json()["detail"]["required_permission"] == f"report:export:{fmt}"
    assert scope["rendered"] == []


@pytest.mark.parametrize("actor", ("owner", "member", "outsider"))
def test_other_users_cannot_delete_report(scope, actor):
    scope["current"]["user"] = scope["users"][actor]
    response = scope["client"].delete(f"/reports/{scope['task'].id}")
    assert response.status_code == 403
    assert scope["db"].get(ReviewTask, scope["task"].id).status == "success"


@pytest.mark.parametrize("actor", ("owner", "member", "outsider"))
@pytest.mark.parametrize("source", ("sandbox_test", "pentest"))
def test_domain_reports_check_ownership_before_domain_data_or_renderer(scope, actor, source):
    scope["current"]["user"] = scope["users"][actor]
    scope["task"].review_type = source
    # 失败的沙箱报告仍属于可读报告，不能绕过其归属检查。
    scope["task"].status = "failed" if source == "sandbox_test" else "success"
    scope["db"].commit()
    for endpoint in READ_ENDPOINTS:
        response = _read(scope, endpoint)
        assert response.status_code == 404, endpoint
        assert response.json()["message"] == "报告不存在"
    assert scope["client"].get("/reports").json()["data"]["total"] == 0
    assert scope["rendered"] == []


def test_read_and_generate_routes_require_view_permission(scope):
    scope["db"].delete(scope["permissions"]["report:view"])
    scope["db"].commit()
    for endpoint in READ_ENDPOINTS:
        response = _read(scope, endpoint)
        assert response.status_code == 403
        assert response.json()["detail"]["required_permission"] == "report:view"
    assert scope["client"].get("/reports").status_code == 403
    assert scope["rendered"] == []


def test_export_permission_without_report_view_cannot_read_report_content(scope):
    """格式导出权限不能绕过独立的报告查看权限。"""
    scope["db"].delete(scope["permissions"]["report:view"])
    scope["db"].commit()

    endpoints = [
        ("GET", f"/tasks/{{id}}/export?format={fmt}", None)
        for fmt in FORMATS
    ] + [
        ("GET", "/{id}/export/word", None),
        ("GET", "/{id}/export/pdf", None),
    ]
    for endpoint in endpoints:
        response = _read(scope, endpoint)
        assert response.status_code == 403, endpoint
        assert response.json()["detail"]["required_permission"] == "report:view", endpoint
    assert scope["rendered"] == []


def test_report_delete_requires_cancel_permission(scope):
    scope["db"].delete(scope["permissions"]["review:cancel"])
    scope["db"].commit()
    assert scope["client"].delete(f"/reports/{scope['task'].id}").status_code == 403
    assert scope["db"].get(ReviewTask, scope["task"].id).status == "success"


@pytest.mark.parametrize("actor", ("author", "admin"))
def test_author_and_admin_delete_hides_all_report_outlets(scope, actor):
    scope["current"]["user"] = scope["users"][actor]
    response = scope["client"].delete(f"/reports/{scope['task'].id}")
    assert response.status_code == 200
    assert scope["db"].get(ReviewTask, scope["task"].id).status == "deleted"
    for endpoint in READ_ENDPOINTS:
        assert _read(scope, endpoint).status_code == 404
    assert scope["client"].get("/reports").json()["data"]["total"] == 0
