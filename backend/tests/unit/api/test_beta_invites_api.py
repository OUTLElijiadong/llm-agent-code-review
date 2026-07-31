"""内测码管理接口及注册验证码共存测试。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1 import auth as auth_api
from app.api.v1 import beta_invites
from app.core.config import settings
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.core.error_handlers import register_handlers
from app.models.beta_invite_code import BetaInviteCode  # noqa: F401
from app.models.rbac import Role
from app.models.user import User


@pytest.fixture
def beta_client(monkeypatch):
    monkeypatch.setattr(settings, "beta_registration_enabled", True)
    monkeypatch.setattr(settings, "beta_code_pepper", "test-beta-code-pepper-32-characters-minimum")
    monkeypatch.setattr(settings, "register_captcha_enabled", True)

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = session_factory()
    admin_user = User(username="admin", password="x", role="admin", status=1)
    role = Role(name="普通成员", code="user", status="active", sort=100, is_builtin=1)
    db.add_all([admin_user, role])
    db.commit()

    app = FastAPI()
    register_handlers(app)
    app.include_router(auth_api.router, prefix="/api/auth")
    app.include_router(beta_invites.router, prefix="/api/admin/beta-codes")

    def override_db():
        yield db

    def override_user():
        return admin_user

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user
    with TestClient(app) as client:
        yield client, db
    db.close()
    engine.dispose()


def test_admin_generate_list_and_revoke_never_expose_hash(beta_client):
    client, db = beta_client
    response = client.post(
        "/api/admin/beta-codes",
        json={"count": 2, "expiry_days": 7, "label": "首批"},
    )
    assert response.status_code == 200, response.text
    generated = response.json()["data"]
    assert len(generated["codes"]) == 2
    assert all(code.startswith("PRISM-") for code in generated["codes"])
    assert "code_hash" not in response.text

    listed_response = client.get("/api/admin/beta-codes")
    listed = listed_response.json()["data"]
    assert listed["total"] == 2
    assert "code_hash" not in listed_response.text
    assert all("PRISM-" in row["display_prefix"] for row in listed["items"])

    invite_id = generated["items"][0]["id"]
    revoked = client.post(f"/api/admin/beta-codes/{invite_id}/revoke")
    assert revoked.status_code == 200
    assert revoked.json()["data"]["status"] == "revoked"
    assert db.get(BetaInviteCode, invite_id).status == "revoked"


def test_registration_requires_captcha_and_beta_code_together(beta_client, monkeypatch):
    client, db = beta_client
    generated = client.post(
        "/api/admin/beta-codes",
        json={"count": 1, "expiry_days": 7},
    ).json()["data"]
    plain = generated["codes"][0]
    invite_id = generated["items"][0]["id"]

    monkeypatch.setattr(auth_api, "verify_captcha", lambda captcha_id, answer: answer == "8")
    bad_captcha = client.post(
        "/api/auth/register",
        json={
            "username": "captcha-fail",
            "password": "secret12",
            "captcha_id": "captcha-1",
            "captcha_answer": "7",
            "beta_code": plain,
        },
    )
    assert bad_captcha.status_code == 400
    assert db.get(BetaInviteCode, invite_id).status == "active"

    success = client.post(
        "/api/auth/register",
        json={
            "username": "beta-success",
            "password": "secret12",
            "captcha_id": "captcha-2",
            "captcha_answer": "8",
            "beta_code": plain,
        },
    )
    assert success.status_code == 200, success.text
    user_id = success.json()["data"]["user_id"]
    assert db.get(BetaInviteCode, invite_id).used_by == user_id
    assert db.query(User).filter(User.username == "beta-success").one().id == user_id

    reuse = client.post(
        "/api/auth/register",
        json={
            "username": "beta-reuse",
            "password": "secret12",
            "captcha_id": "captcha-3",
            "captcha_answer": "8",
            "beta_code": plain,
        },
    )
    assert reuse.status_code == 400
    assert reuse.json()["message"] == "内测码无效、已使用或已过期"


def test_missing_beta_code_uses_same_generic_error(beta_client, monkeypatch):
    client, _ = beta_client
    monkeypatch.setattr(auth_api, "verify_captcha", lambda *_: True)

    response = client.post(
        "/api/auth/register",
        json={
            "username": "no-code",
            "password": "secret12",
            "captcha_id": "captcha",
            "captcha_answer": "1",
        },
    )

    assert response.status_code == 400
    assert response.json()["message"] == "内测码无效、已使用或已过期"


def test_captcha_exposes_only_beta_switch(beta_client):
    client, _ = beta_client

    response = client.get("/api/auth/captcha")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["beta_registration_enabled"] is True
    assert set(data) == {"captcha_id", "question", "beta_registration_enabled"}
