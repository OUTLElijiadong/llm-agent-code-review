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
from app.services import security_center_service, security_monitor_service


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
