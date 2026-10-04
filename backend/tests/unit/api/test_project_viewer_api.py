"""Real route/RBAC dependencies against local SQLite; no external API or queue execution."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agents.ai_prompt_agent import AiPromptAgent
from app.api.v1 import ai_prompt, discussion, project_members, reports, review
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.error_handlers import register_handlers
from app.core.permission_codes import PermissionCode
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.review_task import ReviewTask

from tests.unit.services.test_project_viewer_execution import seed


@pytest.fixture
def db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.core.database import Base
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close(); engine.dispose()


def client_for(db, actor, monkeypatch, *, permissions=None):
    permissions = permissions if permissions is not None else [
        PermissionCode.ISSUE_VIEW, PermissionCode.REVIEW_VIEW, PermissionCode.PROJECT_VIEW,
        PermissionCode.REVIEW_CANCEL, PermissionCode.REVIEW_START, PermissionCode.PROJECT_MEMBER_MANAGE,
    ]
    role = Role(code="user", name="local authorized actor", status="active")
    db.add(role); db.flush()
    db.add(UserRole(user_id=actor.id, role_id=role.id))
    for code in permissions:
        permission = Permission(code=code, name=code, module="local")
        db.add(permission); db.flush()
        db.add(RolePermission(role_id=role.id, permission_id=permission.id))
    db.commit()
    app = FastAPI(); register_handlers(app)
    for router, prefix in [(ai_prompt.router, "/ai-prompts"), (discussion.router, "/agent"),
                           (review.router, "/review"), (reports.router, "/reports"),
                           (project_members.router, "/projects/{project_id}/members")]:
        app.include_router(router, prefix=prefix)
    def override_db():
        yield db
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: actor
    def orchestrator(database, *, user):
        agent = AiPromptAgent(); agent.inject(database, user=user)
        monkeypatch.setattr(agent, "_polish_with_llm", lambda *_a: pytest.fail("model reached"))
        return SimpleNamespace(**{
            "generate_ai_prompt_for_" + name: getattr(agent, "execute_for_" + name)
            for name in ("issue", "task", "project")
        })
    monkeypatch.setattr(ai_prompt, "get_request_orchestrator", orchestrator)
    return TestClient(app)


@pytest.mark.parametrize("source", ["issue", "task", "project"])
def test_http_viewer_template_success_llm_forbidden_and_invisible_source_404(db, monkeypatch, source):
    actor, project, _, _, task, issue = seed(db)
    identifier = {"issue": issue.id, "task": task.id, "project": project.id}[source]
    with client_for(db, actor, monkeypatch) as client:
        payload = {source + "_id": identifier, "use_llm": False}
        template = client.post("/ai-prompts/" + source, json=payload)
        assert template.status_code == 200, template.text
        assert template.json()["data"]["prompts"][0]["issue_id"] == issue.id
        denied = client.post("/ai-prompts/" + source, json={**payload, "use_llm": True})
        assert denied.status_code == 403, denied.text
        assert client.post("/ai-prompts/" + source, json={**payload, source + "_id": 99999}).status_code == 404


@pytest.mark.parametrize("source", ["issue", "task", "project"])
def test_http_ai_prompt_global_permission_denied_before_resource_orchestration(db, monkeypatch, source):
    actor, project, _, _, task, issue = seed(db, "reviewer")
    identifier = {"issue": issue.id, "task": task.id, "project": project.id}[source]
    with client_for(db, actor, monkeypatch, permissions=[]) as client:
        response = client.post("/ai-prompts/" + source, json={source + "_id": identifier, "use_llm": False})
        assert response.status_code == 403
        assert "需要 " in response.json()["message"]


def test_http_viewer_denies_formal_roundtable_and_member_mutation_with_global_grants(db, monkeypatch):
    actor, project, _, file, _, _ = seed(db)
    with client_for(db, actor, monkeypatch) as client:
        assert client.post("/review/start", json={"project_id": project.id, "file_ids": [file.id]}).status_code == 403
        assert client.post("/agent/discuss/start", params={"project_id": project.id, "file_id": file.id}).status_code == 403
        assert client.get(f"/projects/{project.id}/members/candidates", params={"q": "local"}).status_code == 403
        assert db.query(ReviewTask).count() == 1


@pytest.mark.parametrize("member_role,permissions,expected", [
    ("viewer", [PermissionCode.REVIEW_CANCEL], 403),
    ("reviewer", [], 403),
    ("reviewer", [PermissionCode.REVIEW_CANCEL], 200),
])
def test_http_report_delete_preserves_actual_review_cancel_and_author_boundary(db, monkeypatch, member_role, permissions, expected):
    actor, _, _, _, task, _ = seed(db, member_role)
    task.status = "success"; db.commit()
    with client_for(db, actor, monkeypatch, permissions=permissions) as client:
        response = client.delete(f"/reports/{task.id}")
        assert response.status_code == expected, response.text
        db.refresh(task)
        assert task.status == ("deleted" if expected == 200 else "success")
