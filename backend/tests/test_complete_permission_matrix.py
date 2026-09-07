"""全路由认证/授权矩阵与真实 JWT 的项目隔离 HTTP 回归。

只替换数据库连接，不覆盖登录用户、权限依赖或业务服务。所有数据来自隔离
SQLite；TestClient 不启动应用 lifespan，因此不会启动调度器或付费模型。
跨项目资源按现有防枚举契约返回 404，项目内 reviewer 写操作返回 403。
"""

from __future__ import annotations

import inspect
import re
from datetime import datetime

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.core.security import create_access_token
from app.main import app
from app.models.agent_team import AgentTeam
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.review_issue import ReviewIssue
from app.models.review_report import ReviewReport
from app.models.review_task import ReviewTask
from app.models.user import User


def _walk_dependencies(dependant):
    yield dependant
    for child in dependant.dependencies:
        yield from _walk_dependencies(child)


def route_inventory():
    """从实际注册路由提取清单；测试逐条发 HTTP 请求，而非以枚举算通过。"""
    rows = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        calls = [item.call for item in _walk_dependencies(route.dependant)]
        guards = []
        for call in calls:
            name = getattr(call, "__qualname__", "")
            if name == "require_permission.<locals>._dependency":
                guards.append(inspect.getclosurevars(call).nonlocals["permission_code"])
            elif name in {"require_admin", "require_super_admin", "_require_report_export_permission"}:
                guards.append(name)
        for method in sorted(route.methods):
            rows.append({
                "method": method,
                "path": route.path,
                "authenticated": get_current_user in calls,
                "guards": sorted(set(guards)),
                "source": inspect.getsourcefile(route.endpoint),
                "line": inspect.getsourcelines(route.endpoint)[1],
            })
    return rows


ROUTES = route_inventory()
AUTHENTICATED_ROUTES = [row for row in ROUTES if row["authenticated"]]
GUARDED_ROUTES = [row for row in AUTHENTICATED_ROUTES if row["guards"]]


def _route_id(row):
    return f"{row['method']} {row['path']}"


@pytest.fixture(scope="module")
def matrix_env():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    users = {}
    for index, name in enumerate(("owner_a", "member_a", "owner_b", "no_permission", "disabled", "manager"), start=101):
        users[name] = User(id=index, username=f"matrix_{name}", password="isolated-fixture",
                           role="admin" if name == "manager" else "user",
                           status=0 if name == "disabled" else 1, nickname=name)
    db.add_all(users.values())
    codes = {
        "project:view", "project:create", "project:update", "project:delete", "project:member:manage",
        "file:view", "file:upload", "file:edit", "file:delete", "file:download",
        "review:view", "review:cancel", "issue:view", "issue:handle", "issue:batch",
        "report:view", "report:export:json", "report:export:html", "agent:chat", "security:view",
    }
    role = Role(name="隔离验收成员", code="matrix_member", status="active", is_builtin=0)
    db.add(role)
    db.flush()
    for code in sorted(codes):
        permission = Permission(code=code, name=code, module=code.split(":")[0], type="api")
        db.add(permission)
        db.flush()
        db.add(RolePermission(role_id=role.id, permission_id=permission.id))
    for name in ("owner_a", "member_a", "owner_b"):
        db.add(UserRole(user_id=users[name].id, role_id=role.id))
    resources = {}
    resource_owners = (("a", "owner_a"), ("b", "owner_b"), ("none", "no_permission"))
    for index, (name, owner_name) in enumerate(resource_owners, start=201):
        owner = users[owner_name]
        project = Project(id=index, user_id=owner.id, project_name=f"隔离验收项目_{name}", language="python")
        db.add(project)
        db.flush()
        db.add(ProjectMember(project_id=project.id, user_id=owner.id, role_in_project="owner"))
        if name == "a":
            db.add(ProjectMember(project_id=project.id, user_id=users["member_a"].id, role_in_project="reviewer"))
        file = CodeFile(project_id=project.id, file_name=f"{name}.py", file_path=f"src/{name}.py",
                        language="python", content=f"print('{name}')\n", size_bytes=11, line_count=1)
        task = ReviewTask(user_id=owner.id, project_id=project.id, task_name=f"验收任务_{name}",
                          status="success", review_type="standard", total_files=1, processed_files=1,
                          total_issues=1, high_issues=1, score=80, summary=f"验收报告_{name}")
        db.add_all([file, task])
        db.flush()
        issue = ReviewIssue(task_id=task.id, file_id=file.id, file_name=file.file_name,
                            issue_type="安全漏洞", severity="高", title=f"验收问题_{name}",
                            description=f"项目{name}专属证据", status="unfixed")
        report = ReviewReport(task_id=task.id, user_id=owner.id, content_json={},
                              summary=task.summary, score=80, create_time=datetime.now())
        team = AgentTeam(user_id=owner.id, surface="user", session_key=f"matrix-{name}", title=f"团队_{name}",
                         objective="隔离验收", status="completed", trace_id=f"trace-matrix-{name}")
        db.add_all([issue, report, team])
        db.flush()
        resources[name] = {"project": project.id, "file": file.id, "task": task.id,
                           "issue": issue.id, "team": team.id}
    db.commit()

    def override_db():
        yield db

    previous = dict(app.dependency_overrides)
    app.dependency_overrides.clear()
    app.dependency_overrides[get_db] = override_db
    tokens = {name: create_access_token(user.id, user.role) for name, user in users.items()}
    client = TestClient(app)
    try:
        yield {"db": db, "client": client, "users": users, "tokens": tokens, "resources": resources}
    finally:
        client.close()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        db.close()
        engine.dispose()


def _request(env, account, method, path, **kwargs):
    headers = {} if account == "anonymous" else {"Authorization": f"Bearer {env['tokens'][account]}"}
    return env["client"].request(method, path, headers=headers, **kwargs)


@pytest.mark.parametrize("route", AUTHENTICATED_ROUTES, ids=_route_id)
@pytest.mark.parametrize("account,expected", [("anonymous", 401), ("disabled", 403)])
def test_every_authenticated_route_rejects_missing_or_disabled_identity(matrix_env, route, account, expected):
    path = re.sub(r"\{[^}]+\}", "1", route["path"])
    response = _request(matrix_env, account, route["method"], path, json={})
    assert response.status_code == expected, response.text


@pytest.mark.parametrize("route", GUARDED_ROUTES, ids=_route_id)
def test_every_guarded_route_rejects_real_account_without_permissions(matrix_env, route):
    path = re.sub(r"\{[^}]+\}", "1", route["path"])
    response = _request(matrix_env, "no_permission", route["method"], path, json={})
    assert response.status_code == 403, response.text


@pytest.mark.parametrize("route", [row for row in GUARDED_ROUTES
                                   if "require_super_admin" in row["guards"]], ids=_route_id)
def test_ordinary_admin_cannot_bypass_unique_super_admin_routes(matrix_env, route):
    response = _request(matrix_env, "manager", route["method"], re.sub(r"\{[^}]+\}", "1", route["path"]), json={})
    assert response.status_code == 403, response.text


@pytest.mark.parametrize("format", ["json", "html", "word", "pdf"])
def test_report_export_format_permissions_are_independent(matrix_env, format):
    ids = matrix_env["resources"]["none"]
    response = _request(matrix_env, "no_permission", "GET", f"/api/reports/tasks/{ids['task']}/export?format={format}")
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["required_permission"] == f"report:export:{format}"


@pytest.mark.parametrize("format", ["word", "pdf"])
def test_legacy_export_urls_do_not_bypass_format_permission(matrix_env, format):
    ids = matrix_env["resources"]["none"]
    response = _request(matrix_env, "no_permission", "GET", f"/api/reports/{ids['task']}/export/{format}")
    assert response.status_code == 403, response.text


@pytest.mark.parametrize("suffix", ["roles", "permissions", "menus", "data-scope"])
def test_rbac_self_service_is_open_but_other_accounts_are_forbidden(matrix_env, suffix):
    for account in ("no_permission", "member_a"):
        user_id = matrix_env["users"][account].id
        own = _request(matrix_env, account, "GET", f"/api/rbac/users/{user_id}/{suffix}")
        assert own.status_code == 200, own.text
        foreign = _request(matrix_env, account, "GET", f"/api/rbac/users/103/{suffix}")
        assert foreign.status_code == 403, foreign.text


@pytest.mark.parametrize("method,path_key,body", [
    ("GET", "/api/projects/{project}", None),
    ("PUT", "/api/projects/{project}", {"description": "越权修改"}),
    ("DELETE", "/api/projects/{project}", None),
    ("GET", "/api/projects/{project}/members", None),
    ("POST", "/api/projects/{project}/members", {"user_id": 104, "role_in_project": "reviewer"}),
    ("PUT", "/api/projects/{project}/members/103", {"role_in_project": "reviewer"}),
    ("DELETE", "/api/projects/{project}/members/103", None),
    ("GET", "/api/projects/{project}/source-archive", None),
    ("GET", "/api/projects/{project}/audit-source-archive", None),
    ("GET", "/api/projects/{project}/audit-source-archive/result", None),
    ("GET", "/api/code-files?project_id={project}", None),
    ("POST", "/api/code-files", {"project_id": "{project}", "file_name": "forbidden.py", "content": "print(1)"}),
    ("GET", "/api/code-files/{file}", None),
    ("GET", "/api/code-files/{file}/meta", None),
    ("PUT", "/api/code-files/{file}", {"content": "forbidden"}),
    ("POST", "/api/code-files/{file}/rename", {"file_name": "forbidden.py"}),
    ("DELETE", "/api/code-files/{file}", None),
    ("GET", "/api/code-files/{file}/versions", None),
    ("GET", "/api/review/tasks/{task}", None),
    ("GET", "/api/review/tasks/{task}/issues", None),
    ("POST", "/api/review/tasks/{task}/cancel", None),
    ("DELETE", "/api/review/tasks/{task}", None),
    ("GET", "/api/issues/{issue}", None),
    ("PUT", "/api/issues/{issue}/status", {"status": "fixed"}),
    ("PUT", "/api/issues/{issue}/review-decision", {"decision": "accepted", "note": "forbidden"}),
    ("GET", "/api/reports/{task}", None),
    ("GET", "/api/reports/tasks/{task}", None),
    ("GET", "/api/reports/tasks/{task}/export?format=json", None),
    ("DELETE", "/api/reports/{task}", None),
])
@pytest.mark.parametrize("account", ["owner_a", "member_a"])
def test_other_project_resources_are_hidden_and_unchanged(matrix_env, account, method, path_key, body):
    ids = matrix_env["resources"]["b"]
    body = {key: (int(value.format(**ids)) if value == "{project}" else value)
            for key, value in body.items()} if body else None
    response = _request(matrix_env, account, method, path_key.format(**ids), json=body)
    expected = 403 if method == "DELETE" and path_key == "/api/reports/{task}" else 404
    assert response.status_code == expected, response.text
    db = matrix_env["db"]
    db.expire_all()
    assert db.get(Project, ids["project"]).status == "active"
    assert db.get(Project, ids["project"]).description is None
    assert db.get(CodeFile, ids["file"]).content == "print('b')\n"
    assert db.get(ReviewTask, ids["task"]).status == "success"
    assert db.get(ReviewIssue, ids["issue"]).status == "unfixed"


@pytest.mark.parametrize("method,path,body", [
    ("PUT", "/api/projects/{project}", {"description": "reviewer 写入"}),
    ("DELETE", "/api/projects/{project}", None),
    ("PUT", "/api/code-files/{file}", {"content": "forbidden"}),
    ("POST", "/api/code-files/{file}/rename", {"file_name": "forbidden.py"}),
    ("DELETE", "/api/code-files/{file}", None),
    ("POST", "/api/projects/{project}/members", {"user_id": 104, "role_in_project": "reviewer"}),
    ("PUT", "/api/projects/{project}/members/102", {"role_in_project": "owner"}),
    ("DELETE", "/api/projects/{project}/members/102", None),
    ("PUT", "/api/issues/{issue}/status", {"status": "fixed"}),
    ("POST", "/api/review/tasks/{task}/cancel", None),
    ("DELETE", "/api/review/tasks/{task}", None),
])
def test_project_reviewer_with_api_permission_still_cannot_write(matrix_env, method, path, body):
    ids = matrix_env["resources"]["a"]
    response = _request(matrix_env, "member_a", method, path.format(**ids), json=body)
    assert response.status_code == 403, response.text


@pytest.mark.parametrize("suffix,method", [("", "GET"), ("/events", "GET"), ("/messages", "GET"),
                                            ("/cancel", "POST"), ("/retry", "POST"), ("/archive", "POST")])
def test_agent_teams_are_account_private_even_inside_same_project(matrix_env, suffix, method):
    team_id = matrix_env["resources"]["a"]["team"]
    response = _request(matrix_env, "member_a", method, f"/api/agent-teams/{team_id}{suffix}", json={})
    assert response.status_code == 404, response.text
    assert matrix_env["db"].get(AgentTeam, team_id).status == "completed"


@pytest.mark.parametrize("method,path", [("GET", "/api/reports"), ("GET", "/api/reports/{task}"),
                                          ("DELETE", "/api/reports/{task}")])
def test_legacy_report_routes_enforce_rbac_even_for_resource_owner(matrix_env, method, path):
    ids = matrix_env["resources"]["none"]
    response = _request(matrix_env, "no_permission", method, path.format(**ids))
    assert response.status_code == 403, response.text
    assert matrix_env["db"].get(ReviewTask, ids["task"]).status == "success"


@pytest.mark.parametrize("account,expected_project", [("owner_a", "a"), ("member_a", "a"), ("owner_b", "b")])
def test_member_positive_project_file_and_member_read_loop(matrix_env, account, expected_project):
    ids = matrix_env["resources"][expected_project]
    listing = _request(matrix_env, account, "GET", "/api/projects").json()["data"]
    assert {item["id"] for item in listing["items"]} == {ids["project"]}
    assert listing["total"] == 1
    for path in (f"/api/projects/{ids['project']}", f"/api/projects/{ids['project']}/members",
                 f"/api/code-files?project_id={ids['project']}", f"/api/code-files/{ids['file']}",
                 f"/api/code-files/{ids['file']}/meta"):
        response = _request(matrix_env, account, "GET", path)
        assert response.status_code == 200, response.text
        assert response.json()["code"] == 0
    file = _request(matrix_env, account, "GET", f"/api/code-files/{ids['file']}").json()["data"]
    assert file["content"] == f"print('{expected_project}')\n"


@pytest.mark.parametrize("account,expected_project", [("owner_a", "a"), ("member_a", "a"), ("owner_b", "b")])
@pytest.mark.parametrize("path,id_field,resource_key", [("/api/review/tasks", "id", "task"),
                                                       ("/api/issues", "id", "issue"),
                                                       ("/api/reports", "task_id", "task")])
def test_paginated_business_lists_do_not_leak_foreign_project_data(matrix_env, account, expected_project,
                                                                 path, id_field, resource_key):
    response = _request(matrix_env, account, "GET", path)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    # 报告沿用任务发起者可见契约；审查记录/问题为项目成员可见。
    expected_ids = set() if path == "/api/reports" and account == "member_a" else {
        matrix_env["resources"][expected_project][resource_key]
    }
    assert data["total"] == len(expected_ids)
    assert {item[id_field] for item in data["items"]} == expected_ids
    foreign_project = matrix_env["resources"]["b" if expected_project == "a" else "a"]["project"]
    filtered = _request(matrix_env, account, "GET", f"{path}?project_id={foreign_project}")
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["data"]["items"] == []
    assert filtered.json()["data"]["total"] == 0


def test_foreign_batch_issue_update_is_rejected_without_changing_target(matrix_env):
    issue_id = matrix_env["resources"]["b"]["issue"]
    response = _request(matrix_env, "owner_a", "POST", "/api/issues/batch-status",
                        json={"ids": [issue_id], "status": "fixed"})
    assert response.status_code == 404, response.text
    assert matrix_env["db"].get(ReviewIssue, issue_id).status == "unfixed"


def test_normal_owner_project_file_member_crud_persists_across_http_clients(matrix_env):
    env = matrix_env
    created = _request(env, "owner_a", "POST", "/api/projects", json={"project_name": "普通用户真实 CRUD 闭环"})
    assert created.status_code == 200, created.text
    project_id = created.json()["data"]["id"]
    file = _request(env, "owner_a", "POST", "/api/code-files", json={
        "project_id": project_id, "file_name": "loop.py", "content": "print(1)\n", "language": "python"})
    assert file.status_code == 200, file.text
    file_id = file.json()["data"]["file_id"]
    for method, path, payload in [
        ("PUT", f"/api/projects/{project_id}", {"description": "已更新"}),
        ("PUT", f"/api/code-files/{file_id}", {"content": "print(2)\n", "change_desc": "验收更新"}),
        ("POST", f"/api/code-files/{file_id}/rename", {"file_name": "renamed.py"}),
        ("POST", f"/api/projects/{project_id}/members", {"user_id": 102, "role_in_project": "reviewer"}),
    ]:
        response = _request(env, "owner_a", method, path, json=payload)
        assert response.status_code == 200, response.text
    client = TestClient(app)
    try:
        headers = {"Authorization": f"Bearer {env['tokens']['member_a']}"}
        response = client.get(f"/api/code-files/{file_id}", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["content"] == "print(2)\n"
        assert response.json()["data"]["file_name"] == "renamed.py"
    finally:
        client.close()
    removed = _request(env, "owner_a", "DELETE", f"/api/projects/{project_id}/members/102")
    assert removed.status_code == 200, removed.text
    assert _request(env, "member_a", "GET", f"/api/code-files/{file_id}").status_code == 404
    versions = _request(env, "owner_a", "GET", f"/api/code-files/{file_id}/versions")
    assert versions.status_code == 200, versions.text
    assert versions.json()["data"]["total"] == 2
    assert {item["version_no"] for item in versions.json()["data"]["items"]} == {1, 2}
    deleted_file = _request(env, "owner_a", "DELETE", f"/api/code-files/{file_id}")
    assert deleted_file.status_code == 200, deleted_file.text
    deleted_project = _request(env, "owner_a", "DELETE", f"/api/projects/{project_id}")
    assert deleted_project.status_code == 200, deleted_project.text
    assert env["db"].get(Project, project_id).status == "deleted"
    assert env["db"].get(CodeFile, file_id).status == "deleted"


@pytest.mark.parametrize('method,suffix,body,permission', [
    ('GET', '', None, 'project:view'),
    ('POST', '', {'user_id': 103, 'role_in_project': 'reviewer'}, 'project:member:manage'),
    ('PUT', '/101', {'role_in_project': 'owner'}, 'project:member:manage'),
    ('DELETE', '/101', None, 'project:member:manage'),
])
def test_project_owner_cannot_manage_members_without_explicit_rbac(matrix_env, method, suffix, body, permission):
    env = matrix_env
    db = env['db']
    owner = env['users']['no_permission']
    project = Project(user_id=owner.id, project_name='成员权限独立验收', language='python')
    db.add(project)
    db.flush()
    project_id = project.id
    db.add_all([
        ProjectMember(project_id=project_id, user_id=owner.id, role_in_project='owner'),
        ProjectMember(project_id=project_id, user_id=101, role_in_project='reviewer'),
    ])
    db.commit()
    try:
        before = sorted((m.user_id, m.role_in_project)
                        for m in db.query(ProjectMember).filter_by(project_id=project_id))
        response = _request(env, 'no_permission', method, f'/api/projects/{project_id}/members{suffix}', json=body)
        assert response.status_code == 403, response.text
        assert response.json()['detail']['required_permission'] == permission
        db.expire_all()
        after = sorted((m.user_id, m.role_in_project)
                       for m in db.query(ProjectMember).filter_by(project_id=project_id))
        assert after == before
    finally:
        db.query(ProjectMember).filter_by(project_id=project_id).delete()
        db.query(Project).filter_by(id=project_id).delete()
        db.commit()
