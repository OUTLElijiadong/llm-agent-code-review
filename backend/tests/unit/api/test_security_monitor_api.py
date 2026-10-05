"""安全监控 API（未读/已读/手动巡检/安全态势）回归测试。

覆盖：
1. unread 只返回当前管理员 open 且未读的告警
2. read 标记已读 + 归属校验（非本人 403、不存在 404、user_id 为空放行）
3. run-monitor 普通管理员 403、唯一超级管理员可调
4. status 唯一超级管理员可调
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.main import app
from app.models.admin_chat import OpsExecution
from app.models.agent_governance import AgentAlert, AgentJob, AgentJobRun
from app.models.audit_log import AuditLog
from app.models.rbac import Role, UserRole
from app.models.user import User
from app.services import ops_service, security_center_service, security_monitor_service


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


def _mk_alert(
    db: Session,
    *,
    user_id,
    read=False,
    status="open",
    severity="warning",
    fingerprint=None,
    source="security_monitor",
    alert_type="security.login",
    category="login",
) -> AgentAlert:
    """构造一条 AgentAlert 测试数据。"""
    alert = AgentAlert(
        alert_type=alert_type,
        severity=severity,
        status=status,
        title=f"SSH 登录告警 user={user_id}",
        detail_json="{}",
        category=category,
        source=source,
        user_id=user_id,
        read_at=datetime.now(timezone.utc) if read else None,
        fingerprint=fingerprint or f"login:10.0.0.{user_id}:root",
    )
    db.add(alert)
    db.commit()
    return alert


def test_unread_alerts_only_returns_own_open_unread(db, seed, client_factory):
    """unread 只返回当前管理员 open 且未读、归属自己的告警。"""
    mine = _mk_alert(db, user_id=seed["super_admin"].id)
    _mk_alert(db, user_id=seed["super_admin"].id, read=True)
    _mk_alert(db, user_id=seed["super_admin"].id, status="resolved")
    _mk_alert(db, user_id=999)

    client = client_factory(seed["super_admin"])
    response = client.get("/api/admin/observability/alerts/unread")

    assert response.status_code == 200
    rows = response.json()["data"]
    assert [row["id"] for row in rows] == [mine.id]
    assert rows[0]["category"] == "login"
    assert rows[0]["source"] == "security_monitor"
    assert rows[0]["user_id"] == seed["super_admin"].id
    assert rows[0]["read_at"] is None


def test_mark_alert_read_allows_own_and_null_user(db, seed, client_factory):
    """read：归属自己或 user_id 为空均可标记已读。"""
    mine = _mk_alert(
        db,
        user_id=seed["admin"].id,
        source="agent_governance",
        alert_type="agent.runtime",
        category=None,
    )
    system = _mk_alert(
        db,
        user_id=None,
        source="agent_governance",
        alert_type="agent.runtime",
        category=None,
    )

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


def test_regular_admin_cannot_read_or_change_security_monitor_alerts(db, seed, client_factory):
    """安全监控告警只对唯一超管可见，普通管理员仍能查看常规治理告警。"""
    security_alert = _mk_alert(db, user_id=seed["admin"].id)
    governance_alert = AgentAlert(
        alert_type="agent.runtime",
        severity="warning",
        status="open",
        title="常规治理告警",
        detail_json="{}",
        source="agent_governance",
    )
    db.add(governance_alert)
    db.commit()

    client = client_factory(seed["admin"])
    list_response = client.get("/api/admin/observability/alerts")
    page_response = client.get("/api/admin/observability/alerts/page")
    unread_response = client.get("/api/admin/observability/alerts/unread")

    assert list_response.status_code == 200
    assert [row["title"] for row in list_response.json()["data"]] == ["常规治理告警"]
    assert page_response.status_code == 200
    assert page_response.json()["data"]["total"] == 1
    assert all(row["source"] != "security_monitor" for row in page_response.json()["data"]["items"])
    assert unread_response.status_code == 200
    assert all(row["id"] != security_alert.id for row in unread_response.json()["data"])

    read_response = client.post(f"/api/admin/observability/alerts/{security_alert.id}/read")
    resolve_response = client.post(
        f"/api/admin/observability/alerts/{security_alert.id}/resolve",
        json={"note": "不应写入"},
    )
    assert read_response.status_code == 404
    assert resolve_response.status_code == 404
    db.refresh(security_alert)
    assert security_alert.status == "open"
    assert security_alert.read_at is None
    assert json.loads(security_alert.detail_json) == {}


def test_regular_admin_cannot_list_legacy_security_alert_when_source_is_null(db, seed, client_factory):
    """旧安全告警可能没有 source，安全类型仍不能从通用告警入口泄露。"""
    legacy_security_alert = _mk_alert(db, user_id=seed["admin"].id, source=None)
    client = client_factory(seed["admin"])

    list_response = client.get("/api/admin/observability/alerts")
    page_response = client.get("/api/admin/observability/alerts/page")
    unread_response = client.get("/api/admin/observability/alerts/unread")

    assert list_response.status_code == 200
    assert all(row["id"] != legacy_security_alert.id for row in list_response.json()["data"])
    assert page_response.status_code == 200
    assert all(row["id"] != legacy_security_alert.id for row in page_response.json()["data"]["items"])
    assert page_response.json()["data"]["total"] == 0
    assert unread_response.status_code == 200
    assert all(row["id"] != legacy_security_alert.id for row in unread_response.json()["data"])


def test_regular_admin_cannot_mutate_legacy_security_alert_when_source_is_null(db, seed, client_factory):
    """旧安全告警即使 source 缺失，也不能被普通管理员标记已读或关闭。"""
    legacy_security_alert = _mk_alert(db, user_id=seed["admin"].id, source=None)
    client = client_factory(seed["admin"])

    read_response = client.post(f"/api/admin/observability/alerts/{legacy_security_alert.id}/read")
    resolve_response = client.post(
        f"/api/admin/observability/alerts/{legacy_security_alert.id}/resolve",
        json={"note": "不应写入"},
    )

    assert read_response.status_code == 404
    assert resolve_response.status_code == 404
    db.refresh(legacy_security_alert)
    assert legacy_security_alert.status == "open"
    assert legacy_security_alert.read_at is None
    assert json.loads(legacy_security_alert.detail_json) == {}


def test_security_alert_classification_normalizes_legacy_fields_and_keeps_governance_visible(db, seed, client_factory):
    """旧数据的空来源/空白字符不应绕过隔离，普通无来源治理告警仍保持可见。"""
    padded_security_type = _mk_alert(
        db,
        user_id=seed["admin"].id,
        source=None,
        alert_type=" security.login ",
        category=None,
    )
    blank_source_security_category = _mk_alert(
        db,
        user_id=seed["admin"].id,
        source="",
        alert_type="legacy.login",
        category=" login ",
    )
    tab_newline_security_type = _mk_alert(
        db,
        user_id=seed["admin"].id,
        source="\t\n",
        alert_type="\nsecurity.login\t",
        category=None,
    )
    ordinary_legacy = _mk_alert(
        db,
        user_id=seed["admin"].id,
        source=None,
        alert_type="agent.runtime",
        category=None,
    )
    client = client_factory(seed["admin"])

    list_response = client.get("/api/admin/observability/alerts")
    page_response = client.get("/api/admin/observability/alerts/page")
    unread_response = client.get("/api/admin/observability/alerts/unread")

    visible_ids = {row["id"] for row in list_response.json()["data"]}
    page_ids = {row["id"] for row in page_response.json()["data"]["items"]}
    unread_ids = {row["id"] for row in unread_response.json()["data"]}
    assert list_response.status_code == page_response.status_code == unread_response.status_code == 200
    assert ordinary_legacy.id in visible_ids
    assert ordinary_legacy.id in page_ids
    assert ordinary_legacy.id in unread_ids
    hidden_security_ids = {
        padded_security_type.id,
        blank_source_security_category.id,
        tab_newline_security_type.id,
    }
    assert hidden_security_ids.isdisjoint(visible_ids | page_ids | unread_ids)

    for alert in (padded_security_type, blank_source_security_category, tab_newline_security_type):
        assert client.post(f"/api/admin/observability/alerts/{alert.id}/read").status_code == 404
        assert client.post(
            f"/api/admin/observability/alerts/{alert.id}/resolve",
            json={"note": "不应写入"},
        ).status_code == 404

    ordinary_read = client.post(f"/api/admin/observability/alerts/{ordinary_legacy.id}/read")
    assert ordinary_read.status_code == 200


def test_super_admin_can_read_security_monitor_alerts(db, seed, client_factory):
    alert = _mk_alert(db, user_id=seed["super_admin"].id)
    client = client_factory(seed["super_admin"])

    response = client.get("/api/admin/observability/alerts")

    assert response.status_code == 200
    assert [row["id"] for row in response.json()["data"]] == [alert.id]


def test_mark_alert_read_forbidden_for_other_user(db, seed, client_factory):
    """read：告警归属其他管理员时返回 403。"""
    other = _mk_alert(
        db,
        user_id=999,
        source="agent_governance",
        alert_type="agent.runtime",
        category=None,
    )

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
    monkeypatch.setattr(
        security_monitor_service,
        "run_security_monitor",
        lambda _db, **kwargs: {
            "success": True,
            "created_alerts": [],
            "actions": {},
            "errors": [],
        },
    )
    client = client_factory(seed["admin"])
    response = client.post("/api/admin/observability/security/run-monitor")
    assert response.status_code == 403
    assert response.json()["code"] == 40322


def test_run_monitor_allowed_for_super_admin(db, seed, client_factory, monkeypatch):
    """run-monitor 唯一超级管理员可触发并返回摘要。"""
    received = {}

    def run_for_admin(_db, **kwargs):
        received.update(kwargs)
        return {
            "success": True,
            "created_alerts": [
                {
                    "alert_id": 1,
                    "severity": "high",
                    "category": "login",
                    "title": "SSH 登录",
                    "fingerprint": "login:8.8.8.8:root",
                }
            ],
            "actions": {"ssh_login_events": {"accepted_total": 1}},
            "errors": [],
        }

    monkeypatch.setattr(
        security_monitor_service,
        "run_security_monitor",
        run_for_admin,
    )
    client = client_factory(seed["super_admin"])
    response = client.post("/api/admin/observability/security/run-monitor")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["success"] is True
    assert len(data["created_alerts"]) == 1
    assert data["created_alerts"][0]["category"] == "login"
    assert received == {"actor": seed["super_admin"], "source": "admin_security_center"}


def test_security_policy_nginx_window_uses_its_own_baseline(db, monkeypatch):
    monkeypatch.setattr(settings, "security_nginx_window_hours", 3)
    monkeypatch.setattr(settings, "security_flytrap_window_hours", 9)

    policy = security_center_service.get_policy(db)

    assert policy["nginx_window_hours"] == 3


def test_security_monitor_overview_reports_persisted_schedule(db, monkeypatch):
    monkeypatch.setattr(settings, "security_monitor_interval_minutes", 5)
    db.add(
        AgentJob(
            job_code="security_monitor_test",
            job_type="security_monitor",
            schedule="interval@11m",
            status="enabled",
        )
    )
    db.commit()

    monitoring = security_center_service._monitor_snapshot(db)

    assert monitoring["schedule"] == "interval@11m"
    assert monitoring["schedule_label"] == "每 11 分钟"
    assert monitoring["interval_minutes"] == 11


def test_security_monitor_overview_distinguishes_failed_and_degraded_sources(db):
    """来源执行报错或降级时，总览应指出对应来源而非只报异常数量。"""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    job = AgentJob(
        job_code="security_monitor_source_status",
        job_type="security_monitor",
        schedule="interval@5m",
        status="enabled",
    )
    db.add(job)
    db.flush()
    db.add(
        AgentJobRun(
            job_id=job.id,
            status="success",
            started_at=now,
            finished_at=now,
            result_json=json.dumps(
                {
                    "completed_actions": [
                        "ssh_login_events",
                        "nginx_attack_events",
                        "backup_audit",
                        "db_health",
                        "status",
                    ],
                    "errors": [
                        {"action": "ssh_login_events", "error": "采集器不可用"},
                        {"action": "nginx_attack_events", "error": "结果不完整", "degraded": True},
                    ],
                }
            ),
        )
    )
    db.commit()

    monitoring = security_center_service._monitor_snapshot(db)
    sources = {item["code"]: item["status"] for item in monitoring["sources"]}

    assert sources["ssh_login_events"] == "failed"
    assert sources["nginx_attack_events"] == "degraded"
    assert monitoring["last_run"]["failed_sources"] == 1
    assert monitoring["last_run"]["degraded_sources"] == 1
    assert monitoring["last_run"]["degraded"] is True
    events = security_center_service.list_events(db, hours=24, page_size=100)["items"]
    run_event = next(item for item in events if item["event_type"] == "monitor_run")
    assert run_event["status"] == "warning"
    assert run_event["evidence_summary"]["failed_source_codes"] == ["ssh_login_events"]
    assert run_event["evidence_summary"]["degraded_source_codes"] == ["nginx_attack_events"]


def test_security_status_allowed_for_super_admin(db, seed, client_factory, monkeypatch):
    """status 唯一超级管理员可查询安全态势。"""
    received = {}

    def status_for_admin(_db, since_hours=24, **kwargs):
        received.update(kwargs)
        return {
            "since_hours": since_hours,
            "ssh": {"accepted_total": 1, "failed_total": 0, "total": 1, "accepted_top_ips": [], "failed_top_ips": []},
            "attacks": {"flytrap_total": 0, "flytrap_top_ips": [], "nginx_total": 0, "nginx_top_ips": []},
            "backup": {},
            "open_alerts": [],
            "errors": [],
        }

    monkeypatch.setattr(
        security_monitor_service,
        "query_security_status",
        status_for_admin,
    )
    client = client_factory(seed["super_admin"])
    response = client.get("/api/admin/observability/security/status?since_hours=24")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["since_hours"] == 24
    assert data["ssh"]["accepted_total"] == 1
    assert received == {"actor": seed["super_admin"], "source": "admin_security_center"}


def test_security_center_is_restricted_to_super_admin(db, seed, client_factory):
    """普通管理员不能读取安全中心、时间线或修改主机监控策略。"""
    client = client_factory(seed["admin"])
    for method, path in (
        ("get", "/api/admin/security-center/overview"),
        ("get", "/api/admin/security-center/events"),
        ("get", "/api/admin/security-center/policy"),
        ("put", "/api/admin/security-center/policy"),
    ):
        response = (
            client.put(
                path,
                json={
                    "ssh_failed_threshold": 10,
                    "ssh_window_hours": 1,
                    "nginx_failure_threshold": 10,
                    "nginx_window_hours": 1,
                },
            )
            if method == "put"
            else client.get(path)
        )
        assert response.status_code == 403
        assert response.json()["code"] == 40322


def test_security_center_policy_can_only_increase_monitoring_sensitivity(db, seed, client_factory):
    """策略可收紧并留痕；降低灵敏度的写入被服务端拒绝。"""
    client = client_factory(seed["super_admin"])
    current = security_center_service.get_policy(db)
    update = {
        "ssh_failed_threshold": max(1, current["ssh_failed_threshold"] - 1),
        "ssh_window_hours": min(24, current["ssh_window_hours"] + 1),
        "nginx_failure_threshold": max(1, current["nginx_failure_threshold"] - 1),
        "nginx_window_hours": min(24, current["nginx_window_hours"] + 1),
    }
    response = client.put("/api/admin/security-center/policy", json=update)
    assert response.status_code == 200
    saved = response.json()["data"]
    assert saved["monitoring_mode"] == "monitor_only"
    assert saved["automatic_blocking_enabled"] is False
    assert saved["counterattack_enabled"] is False
    assert saved["revision"] == 1
    log = db.query(AuditLog).filter(AuditLog.action == "security_policy_update").one()
    assert log.actor_id == seed["super_admin"].id
    assert '"after"' in log.detail

    weaker = {**update, "ssh_failed_threshold": min(5000, update["ssh_failed_threshold"] + 1)}
    rejected = client.put("/api/admin/security-center/policy", json=weaker)
    assert rejected.status_code == 400
    assert rejected.json()["message"] == "安全监控设置只能提高或保持检测灵敏度"
    assert db.query(AuditLog).filter(AuditLog.action == "security_policy_update").count() == 1


@pytest.mark.parametrize("action", ["ssh_login_events", "status"])
@pytest.mark.parametrize("status, severity, summary", [
    ("running", "info", "采集进行中，结果尚未返回。"),
    ("success", "info", "只读采集器执行回执；未执行封禁或反制。"),
    ("failed", "warning", "采集失败，相关数据源当前存在监控盲区。"),
    ("unknown", "warning", "采集状态待核验，当前回执尚未确认。"),
    ("queued", "warning", "采集状态待核验，当前回执尚未确认。"),
])
def test_security_collector_summary_matches_recorded_state(db, seed, client_factory, action, status, severity, summary):
    """正在采集不能被读模型误称失败，未知状态也不能假定执行结果。"""
    row = OpsExecution(
        request_id=f"collector-state-{action}-{status}", actor_id=seed["super_admin"].id,
        action=action, risk_level="low", status=status, started_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    client = client_factory(seed["super_admin"])
    group = "inspection" if status == "success" else "activity"
    response = client.get("/api/admin/security-center/events", params={"event_group": group})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total"] == 1
    event = data["items"][0]
    assert event["id"] == f"collector:{row.id}"
    assert event["event_type"] == "collector"
    assert event["status"] == status
    assert event["summary"] == summary
    assert event["severity"] == severity
    if status != "failed":
        assert "采集失败" not in event["summary"]
        assert "监控盲区" not in event["summary"]
    db.refresh(row)
    assert row.status == status


def test_security_center_timeline_redacts_collector_payloads(db, seed, client_factory):
    """时间线只回传白名单证据字段，不泄露原始参数/执行器输出。"""
    now = datetime.now(timezone.utc)
    db.add(
        AgentAlert(
            alert_type="security.brute_force",
            severity="warning",
            status="open",
            title="SSH 爆破：203.0.113.9",
            detail_json='{"ip":"203.0.113.9","failed_count":25,"token":"DO_NOT_EXPOSE"}',
            category="brute_force",
            source="security_monitor",
            create_time=now,
        )
    )
    db.add(
        AgentAlert(
            alert_type="security.login",
            severity="warning",
            status="open",
            title="旧版未标记来源的登录告警",
            detail_json='{"ip":"203.0.113.12"}',
            category="login",
            source=None,
            create_time=now,
        )
    )
    job = AgentJob(
        job_code="security_monitor_test", job_type="security_monitor", schedule="*/5 * * * *", status="enabled"
    )
    db.add(job)
    db.flush()
    db.add(
        AgentJobRun(
            job_id=job.id,
            status="success",
            started_at=now,
            finished_at=now,
            result_json='{"completed_actions":["ssh_login_events"],"errors":[]}',
        )
    )
    db.add(
        OpsExecution(
            request_id="security-test-request",
            actor_id=seed["super_admin"].id,
            action="ssh_login_events",
            risk_level="low",
            status="success",
            params_json='{"token":"DO_NOT_EXPOSE"}',
            result_json='{"private":"DO_NOT_EXPOSE"}',
            started_at=now,
            finished_at=now,
            duration_ms=4,
        )
    )
    db.add(
        AuditLog(
            actor_id=seed["super_admin"].id,
            actor_name="admin",
            action="admin_copilot.ops.ssh_login_events",
            target_type="production_ops",
            target_id="security-test-request",
            detail="运维动作 ssh_login_events：success；source=admin_security_center",
            status="success",
            create_time=now,
        )
    )
    db.add_all(
        [
            AuditLog(
                actor_name=None,
                action="login",
                target_type="user",
                target_id="admin",
                detail="登录失败，内部错误 DO_NOT_EXPOSE",
                status="failed",
                ip="203.0.113.11",
                create_time=now,
            ),
            AuditLog(
                actor_id=seed["super_admin"].id,
                actor_name="admin",
                action="rbac.user_roles_assign",
                target_type="user",
                target_id="9",
                detail="权限明细 DO_NOT_EXPOSE",
                status="success",
                create_time=now,
            ),
        ]
    )
    db.commit()

    response = client_factory(seed["super_admin"]).get("/api/admin/security-center/events?hours=24&page_size=100")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total"] == 6
    overview = security_center_service.get_overview(db)
    assert overview["open_alerts_24h"] == 2
    assert overview["open_alerts_total"] == 2
    encoded = response.text
    assert "DO_NOT_EXPOSE" not in encoded
    assert "params_json" not in encoded
    alert_event = next(
        item for item in data["items"] if item["event_type"] == "alert" and item["title"] == "SSH 爆破：203.0.113.9"
    )
    assert alert_event["evidence_summary"] == {"failed_count": 25, "ip": "203.0.113.9"}
    assert any(item["title"] == "旧版未标记来源的登录告警" for item in data["items"])
    collector_event = next(item for item in data["items"] if item["event_type"] == "collector")
    assert collector_event["actor"] == "admin"
    assert collector_event["evidence_summary"]["source"] == "admin_security_center"
    login_event = next(item for item in data["items"] if item.get("action_code") == "login")
    assert login_event["severity"] == "warning"
    assert login_event["evidence_summary"]["ip"] == "203.0.113.11"
    assert login_event["evidence_summary"]["account"] == "admin"
    assert "不代表攻击者已成功" in login_event["summary"]
    permission_event = next(
        item for item in data["items"] if item.get("action_code") == "rbac.user_roles_assign"
    )
    assert permission_event["severity"] == "high"


def test_resolving_alert_preserves_original_evidence(db, seed, client_factory):
    """关闭告警时原始 evidence 不会被处置备注覆盖。"""
    alert = AgentAlert(
        alert_type="security.login",
        severity="high",
        status="open",
        title="非白名单 SSH 登录",
        detail_json='{"ip":"203.0.113.10","method":"publickey"}',
        source="security_monitor",
    )
    db.add(alert)
    db.commit()
    response = client_factory(seed["super_admin"]).post(
        f"/api/admin/observability/alerts/{alert.id}/resolve",
        json={"note": "已确认并处理"},
    )
    assert response.status_code == 200
    db.refresh(alert)
    detail = json.loads(alert.detail_json)
    assert detail["ip"] == "203.0.113.10"
    assert detail["method"] == "publickey"
    assert detail["resolution"]["note"] == "已确认并处理"
    assert detail["resolution"]["resolved_by_name"] == "admin"


def _automatic_blocking_snapshot(enabled=False):
    return {
        "available": True, "verified": True, "enabled": enabled,
        "policy": {
            "enabled": enabled, "duration_seconds": 900, "window_seconds": 300,
            "ssh_threshold": 20, "web_threshold": 30, "allowlist_cidrs": [], "activated_at": None,
        },
        "protected_sources": [], "active_blocks": [], "recent_blocks": [],
        "last_evaluated_at": None, "errors": [], "backend": "ipset",
    }


@pytest.mark.parametrize("role", ["admin", "reviewer", "user"])
def test_automatic_blocking_all_endpoints_require_super_admin(db, seed, client_factory, monkeypatch, role):
    user = seed["admin"]
    if role != "admin":
        user = User(id=3, username="regular", password="x", role=role, status=1)
        db.add(user)
        db.commit()
    monkeypatch.setattr(ops_service, "_call_executor", lambda *_args: pytest.fail("无权账号不能调用宿主机"))
    client = client_factory(user)
    responses = [
        client.get("/api/admin/security-center/automatic-blocking"),
        client.put("/api/admin/security-center/automatic-blocking", json={"enabled": True}),
        client.post(
            "/api/admin/security-center/automatic-blocking/release", json={"ip": "8.8.8.8", "reason": "错误封禁"},
        ),
    ]
    assert all(response.status_code == 403 for response in responses)
    assert db.query(OpsExecution).count() == 0


def test_automatic_blocking_all_endpoints_require_authentication(db, seed, client_factory, monkeypatch):
    client = client_factory(seed["super_admin"])
    app.dependency_overrides.pop(get_current_user, None)
    monkeypatch.setattr(ops_service, "_call_executor", lambda *_args: pytest.fail("未登录不能调用宿主机"))
    responses = [
        client.get("/api/admin/security-center/automatic-blocking"),
        client.put("/api/admin/security-center/automatic-blocking", json={"enabled": True}),
        client.post(
            "/api/admin/security-center/automatic-blocking/release", json={"ip": "8.8.8.8", "reason": "错误封禁"},
        ),
    ]
    assert all(response.status_code == 401 for response in responses)
    assert db.query(OpsExecution).count() == 0


def test_super_admin_configure_uses_trusted_client_ip_and_retains_audit(db, seed, client_factory, monkeypatch):
    from app.api.v1 import admin_security_center

    captured = []
    monkeypatch.setattr(admin_security_center, "client_ip", lambda _request: "8.8.8.8")

    def executor(action, params, request_id):
        captured.append((action, params, request_id))
        return {"ok": True, "result": _automatic_blocking_snapshot(params.get("enabled", False))}

    monkeypatch.setattr(ops_service, "_call_executor", executor)
    client = client_factory(seed["super_admin"])
    response = client.put("/api/admin/security-center/automatic-blocking", json={"enabled": True})
    assert response.status_code == 200
    assert response.json()["data"]["enabled"] is True
    assert captured[0][0] == "security_block_configure"
    assert captured[0][1]["protected_ip"] == "8.8.8.8"
    assert captured[0][1]["duration_seconds"] == 900
    assert db.query(OpsExecution).one().actor_id == seed["super_admin"].id
    log = db.query(AuditLog).filter(AuditLog.action == "admin_copilot.ops.security_block_configure").one()
    assert log.actor_id == seed["super_admin"].id
    assert "source=security_center" in log.detail
    policy = security_center_service.get_policy(db)
    assert policy["automatic_blocking_enabled"] is True
    assert policy["monitoring_mode"] == "monitor_and_block"
    assert policy["automatic_blocking_confirmed_at"]
    events = security_center_service.list_events(db)["items"]
    defense = next(event for event in events if event["event_type"] == "defense_action")
    assert defense["evidence_summary"]["verified"] is True
    assert "固定规则临时封禁已启用" in defense["summary"]


@pytest.mark.parametrize("extra", [
    {"protected_ip": "1.1.1.1"}, {"command": "arbitrary"},
    {"allowlist_cidrs": ["0.0.0.0/0"]}, {"allowlist_cidrs": ["8.0.0.0/8"]},
    {"allowlist_cidrs": ["::/0"]}, {"allowlist_cidrs": ["2001:4860::/32"]},
    {"allowlist_cidrs": ["8.8.8.8/32"] * 33}, {"duration_seconds": 901},
    {"duration_seconds": True}, {"window_seconds": 59}, {"ssh_threshold": 1}, {"web_threshold": 1},
])
def test_automatic_blocking_rejects_unsafe_or_extra_configuration(db, seed, client_factory, monkeypatch, extra):
    monkeypatch.setattr(ops_service, "_call_executor", lambda *_args: pytest.fail("非法配置不能触及宿主机"))
    response = client_factory(seed["super_admin"]).put(
        "/api/admin/security-center/automatic-blocking", json={"enabled": True, **extra},
    )
    assert response.status_code == 400
    assert response.json()["code"] == 40002
    assert db.query(OpsExecution).count() == 0


def test_automatic_blocking_missing_protected_ip_cannot_enable(db, seed, client_factory, monkeypatch):
    from app.api.v1 import admin_security_center

    monkeypatch.setattr(admin_security_center, "client_ip", lambda _request: "")
    monkeypatch.setattr(ops_service, "_call_executor", lambda *_args: pytest.fail("缺管理员来源不得启用"))
    response = client_factory(seed["super_admin"]).put(
        "/api/admin/security-center/automatic-blocking", json={"enabled": True},
    )
    assert response.status_code == 400
    assert "可信来源" in response.json()["message"]


def test_automatic_blocking_status_failure_is_visible_without_fabricated_success(db, seed, client_factory, monkeypatch):
    monkeypatch.setattr(ops_service, "_call_executor", lambda *_args: {"ok": False, "error": "ipset不可用"})
    response = client_factory(seed["super_admin"]).get("/api/admin/security-center/automatic-blocking")
    assert response.status_code == 200
    snapshot = response.json()["data"]
    assert snapshot["available"] is False
    assert snapshot["enabled"] is False
    assert snapshot["active_blocks"] == []
    assert "ipset不可用" in snapshot["errors"][0]


def test_automatic_blocking_release_sends_reason_and_returns_verified_state(db, seed, client_factory, monkeypatch):
    captured = []
    monkeypatch.setattr(ops_service, "_call_executor", lambda action, params, request_id: (
        captured.append((action, params)) or {"ok": True, "result": _automatic_blocking_snapshot(True)}
    ))
    response = client_factory(seed["super_admin"]).post(
        "/api/admin/security-center/automatic-blocking/release", json={"ip": "8.8.8.8", "reason": "本人来源恢复"},
    )
    assert response.status_code == 200
    assert response.json()["data"]["verified"] is True
    assert captured == [("security_block_release", {"ip": "8.8.8.8", "reason": "本人来源恢复"})]
    assert db.query(OpsExecution).one().action == "security_block_release"


def _seed_security_event_groups(db, seed):
    """建立真实聚合来源，覆盖成功心跳、异常巡检和审计/防御事件。"""
    now = datetime.now(timezone.utc)
    job = AgentJob(job_code="security_event_groups", job_type="security_monitor", schedule="interval@5m")
    db.add(job)
    db.flush()
    activity = []
    inspection = []
    for status in ("open", "resolved"):
        row = AgentAlert(
            alert_type="security.login", source="security_monitor", title=f"规则告警 {status}",
            status=status, severity="warning", create_time=now,
        )
        db.add(row)
        db.flush()
        activity.append(f"alert:{row.id}")
    for status in ("success", "failed"):
        row = AuditLog(action="login", status=status, actor_name="admin", create_time=now)
        db.add(row)
        db.flush()
        activity.append(f"audit:{row.id}")
    policy_log = AuditLog(
        action="security_policy_update", target_type="security_monitor_policy", status="success",
        detail='{"revision":1}', create_time=now,
    )
    db.add(policy_log)
    db.flush()
    activity.append(f"policy:{policy_log.id}")
    for status in ("success", "failed", "running"):
        row = OpsExecution(
            request_id=f"group-collector-{status}", actor_id=seed["super_admin"].id,
            action="ssh_login_events", risk_level="low", status=status, started_at=now,
        )
        db.add(row)
        db.flush()
        (inspection if status == "success" else activity).append(f"collector:{row.id}")
    defense = OpsExecution(
        request_id="group-defense", actor_id=seed["super_admin"].id,
        action="security_block_configure", risk_level="critical", status="success", started_at=now,
        result_json=json.dumps({"ok": True, "result": _automatic_blocking_snapshot(True)}),
    )
    db.add(defense)
    db.flush()
    activity.append(f"defense:{defense.id}")
    routine = OpsExecution(
        request_id="group-defense-routine", action="security_block_reconcile", risk_level="critical",
        status="success", started_at=now,
        result_json=json.dumps({"ok": True, "result": _automatic_blocking_snapshot(True)}),
    )
    db.add(routine)
    db.flush()
    inspection.append(f"defense:{routine.id}")
    for status, errors in (("success", []), ("success", [{"action": "ssh_login_events", "degraded": True}]),
                           ("failed", [{"action": "ssh_login_events"}]), ("running", [])):
        run = AgentJobRun(
            job_id=job.id, status=status, started_at=now,
            result_json=json.dumps({"completed_actions": ["ssh_login_events"] if status == "success" else [],
                                    "errors": errors}),
        )
        db.add(run)
        db.flush()
        (inspection if status == "success" and not errors else activity).append(f"run:{run.id}")
    db.commit()
    return {"activity": set(activity), "inspection": set(inspection), "all": set(activity + inspection)}


@pytest.mark.parametrize("event_group", ["all", "activity", "inspection"])
def test_security_event_groups_filter_before_counting_and_pagination(db, seed, client_factory, event_group):
    expected = _seed_security_event_groups(db, seed)[event_group]
    client = client_factory(seed["super_admin"])
    pages = []
    for page in range(1, (len(expected) + 2) // 3 + 1):
        response = client.get(
            "/api/admin/security-center/events", params={"event_group": event_group, "page": page, "page_size": 3},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["event_group"] == event_group
        assert data["total"] == len(expected)
        assert data["pages"] == (len(expected) + 2) // 3
        assert data["truncated"] is False
        pages.extend(data["items"])
    assert len(pages) == len(expected)
    assert {item["id"] for item in pages} == expected
    if event_group == "activity":
        assert any(item["event_type"] == "monitor_run" and item["status"] == "warning" for item in pages)
        assert any(item["event_type"] == "defense_action" and item["status"] == "success" for item in pages)
    if event_group == "inspection":
        assert all(item["status"] == "success" for item in pages)
        assert all(
            item["event_type"] in {"collector", "monitor_run"}
            or (item["event_type"] == "defense_action" and item["evidence_summary"]["routine_check"] is True)
            for item in pages
        )


def test_security_event_group_defaults_to_all_for_existing_callers(db, seed, client_factory):
    expected = _seed_security_event_groups(db, seed)["all"]
    response = client_factory(seed["super_admin"]).get("/api/admin/security-center/events")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["event_group"] == "all"
    assert data["total"] == len(expected)
    assert {item["id"] for item in data["items"]} == expected


def test_security_activity_sources_do_not_lose_older_exceptions_behind_successful_heartbeats(
    db, seed, client_factory, monkeypatch,
):
    from datetime import timedelta

    monkeypatch.setattr(security_center_service, "_MAX_ROWS_PER_SOURCE", 5)
    now = datetime.now(timezone.utc)
    older = now - timedelta(hours=1)
    job = AgentJob(job_code="security_heartbeat_limit", job_type="security_monitor", schedule="interval@5m")
    db.add(job)
    db.flush()
    failed_collector = OpsExecution(
        request_id="older-failed-collector", action="ssh_login_events", status="failed", risk_level="low",
        started_at=older,
    )
    degraded_run = AgentJobRun(
        job_id=job.id, status="success", started_at=older,
        result_json='{"completed_actions":["ssh_login_events"],"errors":[{"action":"ssh_login_events","degraded":true}]}',
    )
    db.add_all([failed_collector, degraded_run])
    for index in range(10):
        db.add(OpsExecution(
            request_id=f"heartbeat-{index}", action="ssh_login_events", status="success", risk_level="low",
            started_at=now,
        ))
        db.add(AgentJobRun(
            job_id=job.id, status="success", started_at=now,
            result_json='{"completed_actions":["ssh_login_events"],"errors":[]}',
        ))
    db.commit()
    data = client_factory(seed["super_admin"]).get(
        "/api/admin/security-center/events?event_group=activity",
    ).json()["data"]
    assert data["total"] == 2
    assert {item["id"] for item in data["items"]} == {f"collector:{failed_collector.id}", f"run:{degraded_run.id}"}
    assert data["truncated"] is False


def test_security_timeline_preserves_audit_source_500_row_truncation_after_source_lookup(db, seed, client_factory):
    now = datetime.now(timezone.utc)
    db.add_all([AuditLog(action="login", status="failed", create_time=now) for _ in range(500)])
    db.add(OpsExecution(
        request_id="audit-cap-collector", action="ssh_login_events", status="success", risk_level="low", started_at=now,
    ))
    db.add(AuditLog(
        action="admin_copilot.ops.ssh_login_events", target_type="production_ops", target_id="audit-cap-collector",
        detail="source=admin_security_center", status="success", create_time=now,
    ))
    db.commit()
    response = client_factory(seed["super_admin"]).get("/api/admin/security-center/events?event_group=all")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total"] == 501
    assert data["truncated"] is True


@pytest.mark.parametrize("event_group", ["unknown", "", "Activity"])
def test_security_event_group_rejects_invalid_values(db, seed, client_factory, event_group):
    response = client_factory(seed["super_admin"]).get(
        "/api/admin/security-center/events", params={"event_group": event_group},
    )
    assert response.status_code == 400
    assert response.json()["code"] == 40002


@pytest.mark.parametrize("event_group", ["all", "activity", "inspection"])
@pytest.mark.parametrize("role", ["admin", "reviewer", "user", "anonymous"])
def test_security_event_groups_retain_super_admin_boundary(db, seed, client_factory, event_group, role, monkeypatch):
    user = seed["admin"]
    if role in {"reviewer", "user"}:
        user = User(id=3, username="regular", password="x", role=role, status=1)
        db.add(user)
        db.commit()
    client = client_factory(user)
    if role == "anonymous":
        app.dependency_overrides.pop(get_current_user, None)
    monkeypatch.setattr(security_center_service, "list_events", lambda *_args, **_kwargs: pytest.fail("无权账号不能聚合事件"))
    response = client.get("/api/admin/security-center/events", params={"event_group": event_group})
    assert response.status_code == (401 if role == "anonymous" else 403)


@pytest.mark.parametrize("case, expected_group", [
    ("empty_reconcile", "inspection"),
    ("missing_active_blocks", "activity"),
    ("missing_recent_blocks", "activity"),
    ("missing_errors", "activity"),
    ("malformed_active_blocks", "activity"),
    ("unverified", "activity"),
    ("unavailable", "activity"),
    ("outcome_unknown", "activity"),
    ("executor_warning", "activity"),
    ("active_block", "activity"),
    ("recent_release", "activity"),
    ("running", "activity"),
    ("failed", "activity"),
    ("configure", "activity"),
    ("release", "activity"),
    ("apply_anomalies", "activity"),
])
def test_security_defense_reconcile_is_routine_only_with_explicit_verified_empty_receipt(
    db, seed, client_factory, case, expected_group,
):
    payload = _automatic_blocking_snapshot(True)
    action = "security_block_reconcile"
    status = "success"
    entry = {
        "id": "defense-rule", "ip": "8.8.8.8", "rule": "ssh_failed_password", "evidence_count": 20,
        "scope": "host_ingress_and_docker_web", "status": "active",
    }
    if case.startswith("missing_"):
        payload.pop(case.removeprefix("missing_"))
    elif case == "malformed_active_blocks":
        payload["active_blocks"] = [{"ip": "not-an-ip"}]
    elif case == "unverified":
        payload["verified"] = False
    elif case == "unavailable":
        payload["available"] = False
    elif case == "outcome_unknown":
        payload["outcome_unknown"] = True
    elif case == "executor_warning":
        payload["errors"] = ["IPv6 核验异常"]
    elif case == "active_block":
        payload["active_blocks"] = [entry]
    elif case == "recent_release":
        payload["recent_blocks"] = [{**entry, "status": "released"}]
    elif case in {"running", "failed"}:
        status = case
    elif case != "empty_reconcile":
        action = f"security_block_{case}"
    row = OpsExecution(
        request_id=f"routine-{case}", action=action, status=status, risk_level="critical",
        started_at=datetime.now(timezone.utc), result_json=json.dumps({"ok": True, "result": payload}),
    )
    db.add(row)
    db.commit()
    client = client_factory(seed["super_admin"])
    selected = client.get("/api/admin/security-center/events", params={"event_group": expected_group}).json()["data"]
    assert selected["total"] == 1
    event = selected["items"][0]
    assert event["id"] == f"defense:{row.id}"
    assert event["event_type"] == "defense_action"
    assert event["evidence_summary"]["routine_check"] is (expected_group == "inspection")
    if case == "outcome_unknown":
        assert event["status"] == "unknown"
        assert event["severity"] == "warning"
        assert event["evidence_summary"]["verified"] is False
        assert event["evidence_summary"]["active_blocks"] is None
        assert "结果未确认" in event["summary"]
    elif case == "executor_warning":
        assert event["status"] == "warning"
        assert event["severity"] == "warning"
        assert "包含异常" in event["summary"]
    other_group = "activity" if expected_group == "inspection" else "inspection"
    other = client.get("/api/admin/security-center/events", params={"event_group": other_group}).json()["data"]
    assert other["total"] == 0
    assert other["items"] == []


def test_security_activity_finds_real_defense_behind_500_empty_reconcile_receipts(db, seed, client_factory):
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    receipt = json.dumps({"ok": True, "result": _automatic_blocking_snapshot(True)})
    db.add_all([
        OpsExecution(
            request_id=f"empty-reconcile-{index}", action="security_block_reconcile", status="success",
            risk_level="critical", started_at=now, result_json=receipt,
        )
        for index in range(500)
    ])
    changed = OpsExecution(
        request_id="older-defense-change", action="security_block_configure", status="success", risk_level="critical",
        started_at=now - timedelta(hours=1), result_json=receipt,
    )
    db.add(changed)
    db.commit()
    client = client_factory(seed["super_admin"])
    activity = client.get("/api/admin/security-center/events?event_group=activity").json()["data"]
    assert activity["total"] == 1
    assert activity["items"][0]["id"] == f"defense:{changed.id}"
    assert activity["truncated"] is False
    inspection = client.get("/api/admin/security-center/events?event_group=inspection").json()["data"]
    assert inspection["total"] == 500
    assert inspection["truncated"] is True
    assert all(item["evidence_summary"]["routine_check"] is True for item in inspection["items"])
