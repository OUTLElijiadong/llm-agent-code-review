"""验证组织 API 在服务端强制隔离，不依赖前端隐藏。"""
import pytest
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.enterprise_org import router
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import AppError
from app.models.user import User
from app.services.enterprise_org_service import create_enterprise_from_paid_order


@pytest.fixture
def setup():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    users = [User(username=name, password="x", role=role, status=1) for name, role in
             (("owner1", "user"), ("owner2", "user"), ("outsider_admin", "admin"), ("employee", "user"))]
    db.add_all(users)
    db.flush()
    owner1, owner2, outsider, employee = users
    first = create_enterprise_from_paid_order(db, owner_id=owner1.id, name="甲公司", seat_limit=2, paid_order_id="p1")
    second = create_enterprise_from_paid_order(db, owner_id=owner2.id, name="乙公司", seat_limit=2, paid_order_id="p2")
    db.commit()
    app = FastAPI()
    app.include_router(router, prefix="/enterprise")

    @app.exception_handler(AppError)
    async def app_error(_, exc):
        return JSONResponse(status_code=exc.http_status, content={"message": exc.message})

    def current_user(x_test_user: int = Header()):
        return db.get(User, x_test_user)

    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        yield client, first.id, second.id, owner1.id, owner2.id, outsider.id, employee.id
    db.close()
    engine.dispose()


def _headers(user_id):
    return {"x-test-user": str(user_id)}


def test_company_invite_join_and_cross_enterprise_denial(setup):
    client, first, second, owner1, owner2, outsider, employee = setup
    invitation = client.post(f"/enterprise/{first}/invitations", headers=_headers(owner1), json={})
    assert invitation.status_code == 200
    token = invitation.json()["data"]["token"]
    accepted = client.post("/enterprise/invitations/accept", headers=_headers(employee), json={"token": token})
    assert accepted.status_code == 200
    assert accepted.json()["data"]["enterprise_id"] == first
    assert client.get(f"/enterprise/{second}/groups", headers=_headers(employee)).status_code == 403
    assert client.get(f"/enterprise/{first}/members", headers=_headers(owner2)).status_code == 403
    assert client.get(f"/enterprise/{first}/members", headers=_headers(outsider)).status_code == 403
    response = client.post(f"/enterprise/{first}/groups", headers=_headers(employee), json={"name": "越权"})
    assert response.status_code == 403


def test_revoked_invitation_cannot_join_and_cross_enterprise_cannot_revoke(setup):
    client, first, second, owner1, owner2, _, employee = setup
    token = client.post(f"/enterprise/{first}/invitations", headers=_headers(owner1), json={}).json()["data"]["token"]
    invitation = client.get(f"/enterprise/{first}/invitations", headers=_headers(owner1)).json()["data"][0]
    invite_id = invitation["id"]
    denied = client.delete(f"/enterprise/{second}/invitations/{invite_id}", headers=_headers(owner2))
    assert denied.status_code == 404
    revoked = client.delete(f"/enterprise/{first}/invitations/{invite_id}", headers=_headers(owner1))
    assert revoked.status_code == 200
    accepted = client.post("/enterprise/invitations/accept", headers=_headers(employee), json={"token": token})
    assert accepted.status_code == 409
    assert all(item["user_id"] != employee for item in
               client.get(f"/enterprise/{first}/members", headers=_headers(owner1)).json()["data"])


def test_my_enterprise_summary_exposes_subscription_and_seat_state(setup):
    client, first, _, owner1, _, _, _ = setup
    response = client.get("/enterprise/mine", headers=_headers(owner1))
    assert response.status_code == 200
    row = next(item for item in response.json()["data"] if item["id"] == first)
    assert row["seat_limit"] == 2
    assert row["active_member_count"] == 1
    assert row["subscription_status"] == "none"
    assert row["subscription_ends_at"] is None
    assert row["over_capacity"] is False


def test_group_role_does_not_become_company_admin(setup):
    client, first, _, owner1, _, _, employee = setup
    token = client.post(f"/enterprise/{first}/invitations", headers=_headers(owner1), json={}).json()["data"]["token"]
    client.post("/enterprise/invitations/accept", headers=_headers(employee), json={"token": token})
    group = client.post(f"/enterprise/{first}/groups", headers=_headers(owner1), json={"name": "研发"}).json()["data"]
    added = client.put(f"/enterprise/{first}/groups/{group['id']}/members", headers=_headers(owner1),
                       json={"user_id": employee, "role": "leader"})
    assert added.status_code == 200
    response = client.get(f"/enterprise/{first}/groups/{group['id']}/members", headers=_headers(employee))
    assert response.status_code == 200
    groups = client.get(f"/enterprise/{first}/groups", headers=_headers(employee)).json()["data"]
    assert groups[0]["my_role"] == "leader"
    assert client.get(f"/enterprise/{first}/members", headers=_headers(employee)).status_code == 403


def test_directory_is_limited_to_active_members_of_the_same_enterprise(setup):
    client, first, second, owner1, owner2, outsider, employee = setup
    token = client.post(f'/enterprise/{first}/invitations', headers=_headers(owner1), json={}).json()['data']['token']
    client.post('/enterprise/invitations/accept', headers=_headers(employee), json={'token': token})
    response = client.get(f'/enterprise/{first}/directory', headers=_headers(employee))
    assert response.status_code == 200
    records = response.json()['data']
    assert {item['user_id'] for item in records} == {owner1, employee}
    assert all('email' not in item for item in records)
    assert client.get(f'/enterprise/{second}/directory', headers=_headers(employee)).status_code == 403
    assert client.get(f'/enterprise/{first}/directory', headers=_headers(outsider)).status_code == 403
