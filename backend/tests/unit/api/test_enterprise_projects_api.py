"""企业项目绑定、目录与角色授权的 HTTP 边界测试。"""

import pytest
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.enterprise_projects import router
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import AppError
from app.models import billing_ledger  # noqa: F401 - registers billing tables used by org membership checks
from app.models.project import Project
from app.models.user import User
from app.services.enterprise_org_service import (
    accept_invitation,
    create_enterprise_from_paid_order,
    create_group,
    create_invitation,
    set_group_member,
)


@pytest.fixture
def setup():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = session_factory()
    owner = User(username="corp_owner", password="x", role="user", status=1)
    leader = User(username="corp_leader", password="x", role="user", status=1)
    employee = User(username="corp_employee", password="x", role="user", status=1)
    other_group_member = User(username="corp_other", password="x", role="user", status=1)
    outsider = User(username="corp_outsider", password="x", role="user", status=1)
    db.add_all((owner, leader, employee, other_group_member, outsider))
    db.flush()
    company = create_enterprise_from_paid_order(
        db, owner_id=owner.id, name="项目授权测试公司", seat_limit=5, paid_order_id="project-api-order",
    )
    for member in (leader, employee, other_group_member):
        accept_invitation(db, create_invitation(db, company.id, owner.id), member.id)
    group = create_group(db, company.id, owner.id, "研发组")
    set_group_member(db, company.id, group.id, owner.id, leader.id, "leader")
    set_group_member(db, company.id, group.id, owner.id, employee.id, "member")
    project = Project(user_id=employee.id, project_name="员工个人项目", language="python")
    db.add(project)
    db.commit()

    app = FastAPI()
    app.include_router(router, prefix="/api")

    @app.exception_handler(AppError)
    async def app_error(_, exc):
        return JSONResponse(status_code=exc.http_status, content={"message": exc.message})

    def current_user(x_test_user: int = Header()):
        return db.get(User, x_test_user)

    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        yield (
            client, db, company.id, group.id, project.id, owner.id, leader.id,
            employee.id, other_group_member.id, outsider.id,
        )
    db.close()
    engine.dispose()


def _headers(user_id: int) -> dict[str, str]:
    return {"x-test-user": str(user_id)}


def test_enterprise_project_binding_scopes_group_and_grant_http_access(setup):
    client, _, enterprise_id, group_id, project_id, owner_id, leader_id, employee_id, other_id, outsider_id = setup
    base = f"/api/enterprises/{enterprise_id}/projects"

    assert client.get(base, headers=_headers(leader_id)).json()["data"] == []
    bound = client.post(
        base,
        headers=_headers(employee_id),
        json={"project_id": project_id, "group_id": group_id},
    )
    assert bound.status_code == 200
    assert bound.json()["data"]["project_id"] == project_id
    assert client.post(base, headers=_headers(employee_id),
                       json={"project_id": project_id, "group_id": group_id}).status_code == 409

    leader_projects = client.get(base, headers=_headers(leader_id)).json()["data"]
    assert leader_projects[0]["role"] == "leader"
    employee_projects = client.get(base, headers=_headers(employee_id)).json()["data"]
    assert employee_projects[0]["role"] == "editor"
    assert client.get(base, headers=_headers(outsider_id)).status_code == 403

    leader_grant = client.put(
        f"{base}/{project_id}/grants/{other_id}",
        headers=_headers(leader_id),
        json={"role": "viewer"},
    )
    assert leader_grant.status_code == 403
    admin_grant = client.put(
        f"{base}/{project_id}/grants/{other_id}",
        headers=_headers(owner_id),
        json={"role": "viewer"},
    )
    assert admin_grant.status_code == 200
    assert client.get(base, headers=_headers(other_id)).json()["data"][0]["role"] == "viewer"
    assert client.delete(f"{base}/{project_id}/grants/{other_id}", headers=_headers(owner_id)).status_code == 200
    assert client.get(base, headers=_headers(other_id)).json()["data"] == []


def test_non_owner_cannot_bind_project_or_borrow_platform_role_for_enterprise_project(setup):
    client, _, enterprise_id, group_id, project_id, _, leader_id, _, _, outsider_id = setup
    base = f"/api/enterprises/{enterprise_id}/projects"

    denied_bind = client.post(
        base,
        headers=_headers(leader_id),
        json={"project_id": project_id, "group_id": group_id},
    )
    assert denied_bind.status_code == 403
    assert client.get(base, headers=_headers(outsider_id)).status_code == 403
