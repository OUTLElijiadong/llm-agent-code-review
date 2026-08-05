"""安全监控 API（未读/已读/手动巡检/安全态势）回归测试。

覆盖：
1. unread 只返回当前管理员 open 且未读的告警
2. read 标记已读 + 归属校验（非本人 403、不存在 404、user_id 为空放行）
3. run-monitor 普通管理员 403、唯一超级管理员可调
4. status 唯一超级管理员可调
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.main import app
from app.models.agent_governance import AgentAlert
from app.models.rbac import Role, UserRole
from app.models.user import User
from app.services import security_monitor_service


@pytest.fixture
def db() -> Session:
    """创建共享内存 SQLite 会话（StaticPool 保证 TestClient 与 fixture 共用连接）。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def seed(db: Session) -> Dict[str, Any]:
    """植入普通管理员与唯一超级管理员。"""
    admin = User(
        id=1,
        username="manager",
        password="x",
        email="manager@t.com",
        nickname="管理员",
        role="admin",
        status=1,
    )
    super_admin = User(
        id=2,
        username="admin",
        password="x",
        email="admin@t.com",
        nickname="超级管理员",
        role="super_admin",
        status=1,
    )
    db.add_all([admin, super_admin])
    db.flush()
    role = Role(name="超级管理员", code="super_admin", status="active", sort=0, is_builtin=1)
    db.add(role)
    db.flush()
    db.add(UserRole(user_id=super_admin.id, role_id=role.id))
    db.commit()
    return {"admin": admin, "super_admin": super_admin}


@pytest.fixture
def client_factory(db: Session):
    """创建 TestClient 的工厂：覆盖 get_db 与 get_current_user 依赖。"""

    def _make(user: User) -> TestClient:
        def override_db():
            yield db

        def override_user() -> User:
            return user

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_user] = override_user
        return TestClient(app)

    yield _make
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


def _mk_alert(db: Session, *, user_id, read=False, status="open", severity="warning", fingerprint=None) -> AgentAlert:
    """构造一条 AgentAlert 测试数据。"""
    alert = AgentAlert(
        alert_type="security.login",
        severity=severity,
        status=status,
        title=f"SSH 登录告警 user={user_id}",
        detail_json="{}",
        category="login",
        source="security_monitor",
        user_id=user_id,
        read_at=datetime.now(timezone.utc) if read else None,
        fingerprint=fingerprint or f"login:10.0.0.{user_id}:root",
    )
    db.add(alert)
    db.commit()
    return alert


def test_unread_alerts_only_returns_own_open_unread(db, seed, client_factory):
    """unread 只返回当前管理员 open 且未读、归属自己的告警。"""
    mine = _mk_alert(db, user_id=seed["admin"].id)
    _mk_alert(db, user_id=seed["admin"].id, read=True)
    _mk_alert(db, user_id=seed["admin"].id, status="resolved")
    _mk_alert(db, user_id=999)

    client = client_factory(seed["admin"])
    response = client.get("/api/admin/observability/alerts/unread")

    assert response.status_code == 200
    rows = response.json()["data"]
    assert [row["id"] for row in rows] == [mine.id]
    assert rows[0]["category"] == "login"
    assert rows[0]["source"] == "security_monitor"
    assert rows[0]["user_id"] == seed["admin"].id
    assert rows[0]["read_at"] is None


def test_mark_alert_read_allows_own_and_null_user(db, seed, client_factory):
    """read：归属自己或 user_id 为空均可标记已读。"""
    mine = _mk_alert(db, user_id=seed["admin"].id)
    system = _mk_alert(db, user_id=None)

    client = client_factory(seed["admin"])
    mine_resp = client.post(f"/api/admin/observability/alerts/{mine.id}/read")
    assert mine_resp.status_code == 200
    assert mine_resp.json()["data"]["read_at"] is not None
    db.refresh(mine)
    assert mine.read_at is not None

    system_resp = client.post(f"/api/admin/observability/alerts/{system.id}/read")
    assert system_resp.status_code == 200
    db.refresh(system)
    assert system.read_at is not None


def test_mark_alert_read_forbidden_for_other_user(db, seed, client_factory):
    """read：告警归属其他管理员时返回 403。"""
    other = _mk_alert(db, user_id=999)

    client = client_factory(seed["admin"])
    response = client.post(f"/api/admin/observability/alerts/{other.id}/read")

    assert response.status_code == 403
    assert response.json()["code"] == 40300


def test_mark_alert_read_not_found(db, seed, client_factory):
    """read：告警不存在返回 404。"""
    client = client_factory(seed["admin"])
    response = client.post("/api/admin/observability/alerts/12345/read")
    assert response.status_code == 404


def test_run_monitor_forbidden_for_ordinary_admin(db, seed, client_factory, monkeypatch):
    """run-monitor 仅唯一超级管理员可调用，普通管理员 403。"""
    monkeypatch.setattr(security_monitor_service, "run_security_monitor", lambda _db, **kwargs: {
        "success": True,
        "created_alerts": [],
        "actions": {},
        "errors": [],
    })
    client = client_factory(seed["admin"])
    response = client.post("/api/admin/observability/security/run-monitor")
    assert response.status_code == 403
    assert response.json()["code"] == 40322


def test_run_monitor_allowed_for_super_admin(db, seed, client_factory, monkeypatch):
    """run-monitor 唯一超级管理员可触发并返回摘要。"""
    monkeypatch.setattr(security_monitor_service, "run_security_monitor", lambda _db, **kwargs: {
        "success": True,
        "created_alerts": [
            {
                "alert_id": 1, "severity": "high", "category": "login",
                "title": "SSH 登录", "fingerprint": "login:8.8.8.8:root",
            }
        ],
        "actions": {"ssh_login_events": {"accepted_total": 1}},
        "errors": [],
    })
    client = client_factory(seed["super_admin"])
    response = client.post("/api/admin/observability/security/run-monitor")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["success"] is True
    assert len(data["created_alerts"]) == 1
    assert data["created_alerts"][0]["category"] == "login"


def test_security_status_allowed_for_super_admin(db, seed, client_factory, monkeypatch):
    """status 唯一超级管理员可查询安全态势。"""
    monkeypatch.setattr(security_monitor_service, "query_security_status", lambda _db, since_hours=24: {
        "since_hours": 24,
        "ssh": {"accepted_total": 1, "failed_total": 0, "total": 1, "accepted_top_ips": [], "failed_top_ips": []},
        "attacks": {"flytrap_total": 0, "flytrap_top_ips": [], "nginx_total": 0, "nginx_top_ips": []},
        "backup": {},
        "open_alerts": [],
        "errors": [],
    })
    client = client_factory(seed["super_admin"])
    response = client.get("/api/admin/observability/security/status?since_hours=24")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["since_hours"] == 24
    assert data["ssh"]["accepted_total"] == 1
