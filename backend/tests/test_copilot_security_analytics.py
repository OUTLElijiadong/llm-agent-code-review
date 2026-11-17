"""小菱安全事件导出、趋势聚合与受限路由测试。"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.main import app
from app.models.audit_log import AuditLog
from app.models.rbac import Role, UserRole
from app.models.user import User
from app.services.copilot_security_export_service import (
    build_trend,
    export_events,
    summarize_by_kind,
)

BASE = "/api/admin/copilot-security"


@pytest.fixture
def client_db():
    """与 TestClient 线程共享内存 SQLite，采用现有 API 集成测试的 StaticPool 模式。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def actors(client_db):
    admin = User(
        username="analytics_admin", password="x", email="analytics@example.test",
        nickname="分析管理员", role="super_admin", status=1,
    )
    role = Role(name="超级管理员", code="super_admin", status="active", sort=0, is_builtin=1)
    client_db.add_all([admin, role])
    client_db.flush()
    client_db.add(UserRole(user_id=admin.id, role_id=role.id))
    client_db.commit()
    return {"super_admin": admin}


@pytest.fixture
def client_factory(client_db):
    """用共享测试会话覆盖依赖，TestClient 请求与 fixture 共用同一 SQLite 会话。"""
    def make(user):
        def override_db():
            yield client_db

        def override_user():
            return user

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_user] = override_user
        return TestClient(app)

    yield make
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


def _add_event(db, *, kind, status="success", when=None, actor_name="管理员"):
    row = AuditLog(
        action=f"copilot_security.{kind}", status=status,
        create_time=when or datetime.now(timezone.utc).replace(tzinfo=None),
        actor_id=7, actor_name=actor_name, ip="192.0.2.7", target_id="会话-1",
        detail='{"source":"测试"}',
    )
    db.add(row)
    db.commit()
    return row


def test_csv_export_has_fixed_columns_bom_crlf_and_filename(db):
    _add_event(db, kind="injection_blocked", status="failed")

    content, media_type, filename = export_events(db, fmt="csv")

    assert content.startswith("\ufeffid,create_time,kind,status,actor_id,actor_name,ip,target,detail\r\n")
    parsed = list(csv.DictReader(io.StringIO(content.lstrip("\ufeff"))))
    assert parsed[0]["kind"] == "injection_blocked"
    assert parsed[0]["target"] == "会话-1"
    assert media_type == "text/csv; charset=utf-8"
    assert re.fullmatch(r"copilot-security-events-\d{8}\.csv", filename)


def test_json_export_and_api_download_filename(client_db, actors, client_factory):
    _add_event(client_db, kind="tool_denied", status="failed")

    content, media_type, filename = export_events(client_db, fmt="json")
    client = client_factory(actors["super_admin"])
    response = client.get(f"{BASE}/events/export?format=json")

    assert json.loads(content)[0]["kind"] == "tool_denied"
    assert media_type == "application/json"
    assert re.fullmatch(r"copilot-security-events-\d{8}\.json", filename)
    assert response.status_code == 200
    assert response.headers["content-disposition"].endswith(f'filename="{filename}"')
    assert response.json()[0]["kind"] == "tool_denied"


def test_export_filters_by_days_kind_and_status(db):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    _add_event(db, kind="tool_denied", status="failed", when=now)
    _add_event(db, kind="tool_denied", status="success", when=now)
    _add_event(db, kind="injection_blocked", status="failed", when=now)
    _add_event(db, kind="tool_denied", status="failed", when=now - timedelta(days=20))

    content, _, _ = export_events(
        db, kind="tool_denied", status="failed", days=7, fmt="json",
    )

    events = json.loads(content)
    assert len(events) == 1
    assert events[0]["kind"] == "tool_denied"
    assert events[0]["status"] == "failed"


def test_trend_is_contiguous_and_fills_missing_days_with_zero(db):
    today = datetime.now(timezone.utc).date()
    _add_event(db, kind="tool_denied", status="failed",
               when=datetime.combine(today, datetime.min.time()) + timedelta(hours=1))
    _add_event(db, kind="tool_denied", status="success",
               when=datetime.combine(today - timedelta(days=2), datetime.min.time()) + timedelta(hours=2))

    trend = build_trend(db, days=4)
    tool_denied = next(item for item in trend["序列"] if item["kind"] == "tool_denied")

    assert trend["窗口"] == 4
    assert len(trend["日期轴"]) == 4
    assert tool_denied["points"] == [0, 1, 0, 1]
    assert trend["合计"]["total"] == 2


def test_summary_by_kind_counts_total_blocked_and_latest_time(db):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    _add_event(db, kind="tool_denied", status="failed", when=now - timedelta(minutes=1))
    _add_event(db, kind="tool_denied", status="success", when=now)

    summary = summarize_by_kind(db, days=7)

    assert summary == [{
        "kind": "tool_denied", "label": "工具黑名单拒绝", "total": 2,
        "blocked": 1, "last_seen": now.isoformat(),
    }]


def test_trend_route_returns_trend_and_by_kind(client_db, actors, client_factory):
    _add_event(client_db, kind="injection_blocked", status="failed")

    response = client_factory(actors["super_admin"]).get(f"{BASE}/events/trend?days=3")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["trend"]["窗口"] == 3
    assert data["by_kind"][0]["kind"] == "injection_blocked"
