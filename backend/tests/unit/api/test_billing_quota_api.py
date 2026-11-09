"""额度管理 HTTP 权限边界与持久化结果。"""

import pytest
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.billing import router
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import AppError
from app.models.enterprise_org import EnterpriseGroup, EnterpriseGroupMember, EnterpriseMember
from app.models.user import User
from app.services.enterprise_org_service import create_enterprise_from_paid_order


@pytest.fixture
def quota_api():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    users = {
        name: User(username=f"quota-api-{name}", password="x", role="user", status=1)
        for name in ("owner", "admin", "leader", "member", "outsider")
    }
    db.add_all(users.values())
    db.flush()
    enterprise = create_enterprise_from_paid_order(
        db, owner_id=users["owner"].id, name="额度 API 企业", seat_limit=8, paid_order_id="quota-api-order",
    )
    db.add_all([
        EnterpriseMember(enterprise_id=enterprise.id, user_id=users["admin"].id,
                         role="admin", status="active"),
        EnterpriseMember(enterprise_id=enterprise.id, user_id=users["leader"].id,
                         role="member", status="active"),
        EnterpriseMember(enterprise_id=enterprise.id, user_id=users["member"].id,
                         role="member", status="active"),
    ])
    group = EnterpriseGroup(enterprise_id=enterprise.id, name="额度小组", status="active")
    other_group = EnterpriseGroup(enterprise_id=enterprise.id, name="未授权小组", status="active")
    db.add_all([group, other_group])
    db.flush()
    db.add_all([
        EnterpriseGroupMember(enterprise_id=enterprise.id, group_id=group.id, user_id=users["leader"].id,
                             role="leader", status="active"),
        EnterpriseGroupMember(enterprise_id=enterprise.id, group_id=group.id, user_id=users["member"].id,
                             role="member", status="active"),
    ])
    db.commit()

    app = FastAPI()
    app.include_router(router, prefix="/api")

    @app.exception_handler(AppError)
    async def app_error_handler(_, exc):
        return JSONResponse(status_code=exc.http_status, content={"message": exc.message})

    def current_user(x_test_user: int = Header()):
        return db.get(User, x_test_user)

    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        yield client, db, enterprise, group, other_group, users
    db.close()
    engine.dispose()


def _headers(user: User):
    return {"x-test-user": str(user.id)}


def test_quota_api_limits_scope_to_company_and_managed_group(quota_api):
    client, _, enterprise, group, other_group, users = quota_api
    body = {"period": "2026-12", "scope": "group", "limit_credits": 80, "group_id": group.id}
    created = client.put(f"/api/billing/enterprises/{enterprise.id}/limits",
                         headers=_headers(users["leader"]), json=body)
    assert created.status_code == 200
    assert created.json()["data"]["scope"] == "group"
    assert created.json()["data"]["remaining_milli"] == 80_000

    visible_to_admin = client.get(f"/api/billing/enterprises/{enterprise.id}/limits?period=2026-12&group_id={group.id}",
                                  headers=_headers(users["admin"]))
    assert visible_to_admin.status_code == 200
    assert visible_to_admin.json()["data"]["items"][0]["limit_milli"] == 80_000

    denied_group = client.get(
        f"/api/billing/enterprises/{enterprise.id}/limits?period=2026-12&group_id={other_group.id}",
        headers=_headers(users["leader"]),
    )
    denied_company_cap = client.put(f"/api/billing/enterprises/{enterprise.id}/limits",
                                    headers=_headers(users["leader"]),
                                    json={"period": "2026-12", "scope": "enterprise", "limit_credits": 1})
    denied_outsider = client.get(f"/api/billing/enterprises/{enterprise.id}/limits?period=2026-12",
                                 headers=_headers(users["outsider"]))
    assert denied_group.status_code == denied_company_cap.status_code == denied_outsider.status_code == 403
