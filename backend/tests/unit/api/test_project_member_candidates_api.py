"""项目成员候选搜索的鉴权、隔离和最小化字段回归。"""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1 import project_members
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.core.error_handlers import register_handlers
from app.core.permission_codes import PermissionCode
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.user import User


def _make_user(db, username: str, *, email: str | None = None) -> User:
    user = User(username=username, password="x", role="user", status=1, email=email)
    db.add(user)
    db.flush()
    return user


def test_project_member_candidate_search_is_scoped_and_minimizes_returned_data():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    app = FastAPI()
    register_handlers(app)
    app.include_router(project_members.router, prefix="/projects/{project_id}/members")

    owner = _make_user(db, "owner", email="owner@example.test")
    reviewer = _make_user(db, "reviewer", email="reviewer@example.test")
    candidate = _make_user(db, "find-user", email="private@example.test")
    outsider = _make_user(db, "outsider")
    role = Role(name="普通用户", code="user", status="active", sort=100, is_builtin=1)
    permission = Permission(code=PermissionCode.PROJECT_MEMBER_MANAGE, name="管理项目成员", module="project")
    db.add_all([role, permission])
    db.flush()
    db.add_all([
        UserRole(user_id=owner.id, role_id=role.id),
        UserRole(user_id=reviewer.id, role_id=role.id),
        RolePermission(role_id=role.id, permission_id=permission.id),
    ])
    project = Project(user_id=owner.id, project_name="owner-project", status="active")
    db.add(project)
    db.flush()
    db.add(ProjectMember(project_id=project.id, user_id=reviewer.id, role_in_project="reviewer"))
    db.commit()

    actor = {"user": owner}

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: actor["user"]

    try:
        with TestClient(app) as client:
            response = client.get(f"/projects/{project.id}/members/candidates", params={"q": "find"})
            assert response.status_code == 200, response.text
            candidates = response.json()["data"]
            assert candidates == [{"id": candidate.id, "username": "find-user", "nickname": None}]
            assert "private@example.test" not in response.text

            actor["user"] = reviewer
            forbidden_member = client.get(
                f"/projects/{project.id}/members/candidates", params={"q": "find"}
            )
            assert forbidden_member.status_code == 403

            actor["user"] = outsider
            forbidden_outsider = client.get(
                f"/projects/{project.id}/members/candidates", params={"q": "find"}
            )
            assert forbidden_outsider.status_code == 403

            actor["user"] = owner
            short_query = client.get(f"/projects/{project.id}/members/candidates", params={"q": "x"})
            assert short_query.status_code == 400
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()
