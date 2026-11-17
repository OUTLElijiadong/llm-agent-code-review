"""小菱安全运营 API 集成测试（权限口径与处置门禁）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.main import app
from app.models.rbac import Role, UserRole
from app.models.user import User
from app.services import secops_service

BASE = "/api/admin/secops"


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def actors(db: Session) -> dict:
    super_admin = User(username="admin", password="x", email="r@t.c", nickname="超管", role="super_admin", status=1)
    ops = User(username="ops", password="x", email="o@t.c", nickname="运维", role="admin", status=1)
    role = Role(name="超级管理员", code="super_admin", status="active", sort=0, is_builtin=1)
    admin_role = Role(name="管理员", code="admin", status="active", sort=10, is_builtin=1)
    db.add_all([super_admin, ops, role, admin_role])
    db.flush()
    db.add_all([UserRole(user_id=super_admin.id, role_id=role.id),
                UserRole(user_id=ops.id, role_id=admin_role.id)])
    db.commit()
    return {"super_admin": super_admin, "ops": ops}


@pytest.fixture
def client_factory(db: Session):
    def _make(user: User | None) -> TestClient:
        def override_db():
            yield db

        def override_user():
            return user

        app.dependency_overrides[get_db] = override_db
        if user is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = override_user
        return TestClient(app)

    yield _make
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture(autouse=True)
def ops_stub(monkeypatch):
    def fake_execute(db, actor, *, action, params=None, request_id="", session_db_id=None, source="admin_copilot"):
        if action == "nginx_attack_events":
            return {"events": [{"ip": "45.135.228.155", "type": "sql_injection"}]}
        if action == "ssh_login_events":
            return {"events": [{"ip": "45.135.228.155", "type": "failed_password"}]}
        if action == "flytrap_attack_events":
            return {"events": [{"ip": "45.135.228.155", "type": "honeypot_probe"}]}
        if action == "db_threat_signals":
            return {"events": []}
        if action == "security_block_candidates":
            return {"candidates": [{"ip": "45.135.228.155"}]}
        if action == "certificate_status":
            return {"data": {"not_after": "2027-01-01"}}
        if action == "security_surface_audit":
            return {"data": {"ports": [443]}}
        if action == "backup_audit":
            return {"data": {"last": "ok"}}
        return {"ok": True}

    monkeypatch.setattr(secops_service.ops_service, "execute", fake_execute)


class TestAuth:
    def test_anonymous_rejected(self, client_factory):
        assert client_factory(None).get(f"{BASE}/digest").status_code == 401

    def test_secondary_admin_cannot_block(self, client_factory, actors):
        response = client_factory(actors["ops"]).post(
            f"{BASE}/block", json={"ip": "45.135.228.155", "reason": "x", "confirm": True},
        )
        assert response.status_code == 403

    def test_secondary_admin_can_read_digest(self, client_factory, actors):
        response = client_factory(actors["ops"]).get(f"{BASE}/digest")
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["totals"]["sources"] >= 1
        assert data["sources"][0]["ip"] == "45.135.228.155"


class TestReads:
    def test_baseline(self, client_factory, actors):
        response = client_factory(actors["super_admin"]).get(f"{BASE}/baseline")
        assert response.status_code == 200, response.text
        assert "checks" in response.json()["data"]

    def test_patrol(self, client_factory, actors):
        response = client_factory(actors["super_admin"]).get(f"{BASE}/patrol")
        assert response.status_code == 200
        assert "auto_block_enabled" in response.json()["data"]

    def test_tools_catalog(self, client_factory, actors):
        response = client_factory(actors["super_admin"]).get(f"{BASE}/tools")
        assert response.status_code == 200
        tools = response.json()["data"]["tools"]
        assert any(item["tool"] == "secops.threat_digest" for item in tools)


class TestWrites:
    def test_block_requires_confirm(self, client_factory, actors):
        response = client_factory(actors["super_admin"]).post(
            f"{BASE}/block", json={"ip": "45.135.228.155", "reason": "x"},
        )
        assert response.status_code in {400, 422}

    def test_block_happy_path(self, client_factory, actors):
        response = client_factory(actors["super_admin"]).post(
            f"{BASE}/block", json={"ip": "45.135.228.155", "reason": "暴力破解", "confirm": True},
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["blocked"] is True

    def test_block_rejects_loopback(self, client_factory, actors):
        response = client_factory(actors["super_admin"]).post(
            f"{BASE}/block", json={"ip": "127.0.0.1", "reason": "x", "confirm": True},
        )
        assert response.status_code in {400, 422}

    def test_pentest_start_unknown_engagement(self, client_factory, actors):
        response = client_factory(actors["super_admin"]).post(
            f"{BASE}/pentest/pt_missing/start", json={},
        )
        assert response.status_code == 404
