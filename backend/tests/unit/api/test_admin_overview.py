"""管理员总览大屏实时状态测试。"""
from datetime import datetime, timedelta, timezone

from app.agents.event_bus import AgentEventBus
from app.agents.events import AgentEvent, AgentEventType
from app.api.v1.admin_overview import _agent_activity, _login_geo
from app.models.agent_governance import AgentProfile, ToolCallLog
from app.models.audit_log import AuditLog


def test_agent_activity_prefers_live_event_and_expires_to_idle(db):
    """最新事件驱动阶段状态，事件过期后不沿用历史调用的 working。"""
    db.add(AgentProfile(code="code_reviewer", name="代码审查Agent", category="quality", is_enabled=1))
    db.commit()
    AgentEventBus._instance = AgentEventBus()
    AgentEventBus.instance().publish(AgentEvent(
        type=AgentEventType.THINKING,
        agent="code_reviewer",
        trace_id="trace-live",
        message="正在分析依赖关系",
    ))

    live = _agent_activity(db)[0]
    assert live["status"] == "thinking"
    assert live["activity_source"] == "event_bus"
    assert live["purpose"] == "正在分析依赖关系"

    AgentEventBus.instance()._history.clear()
    db.add(ToolCallLog(
        agent_code="code_reviewer",
        tool_code="source_reader",
        action="source.read",
        resource="project:1",
        status="success",
        risk_level="low",
        decision="allow",
        create_time=datetime.now(timezone.utc) - timedelta(minutes=5),
    ))
    db.commit()

    expired = _agent_activity(db)[0]
    assert expired["status"] == "idle"
    assert expired["activity_source"] == "none"


def test_login_geo_uses_successful_real_logins_only(db, monkeypatch):
    """来源地图使用真实审计记录,但不把失败登录当成已建立会话。"""
    now = datetime.utcnow()
    db.add_all([
        AuditLog(action="login", status="success", ip="203.0.113.10", create_time=now),
        AuditLog(action="login", status="success", ip="203.0.113.10", create_time=now),
        AuditLog(action="login", status="failed", ip="203.0.113.10", create_time=now),
        AuditLog(action="login", status="success", ip="198.51.100.20", create_time=now),
        AuditLog(action="user", status="success", ip="203.0.113.10", create_time=now),
        AuditLog(action="login", status="success", ip="192.0.2.30", create_time=now - timedelta(days=31)),
    ])
    db.commit()

    monkeypatch.setattr(
        "app.api.v1.admin_overview.locate_ip_cached",
        lambda ip: {
            "country": "测试国家",
            "city": "测试城市",
            "latitude": 1.0,
            "longitude": 2.0,
        },
    )

    points = _login_geo(db)

    assert [(point["ip"], point["count"]) for point in points] == [
        ("203.0.113.10", 2),
        ("198.51.100.20", 1),
    ]
