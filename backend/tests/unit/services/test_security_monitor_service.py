"""安全监控规则引擎、去重、SSE 弹窗与溯源富化回归测试。

通过 monkeypatch 把 ``ops_service.execute`` 换成 fake（返回构造的
ssh/flytrap/nginx/backup/status 数据），验证：
1. 各规则触发（SSH 登录/爆破、蜜罐、Nginx、备份、磁盘）
2. 白名单 IP 只入库不弹窗
3. 同 fingerprint 的 open 告警去重（不重复建、只刷新 last_seen）
4. SSE emit（monkeypatch emit_event 记录）
5. ip_attribution 富化失败不中断告警创建
6. 单动作失败不中断整体巡检
"""
from __future__ import annotations

import json

import pytest

from app.agents.events import AgentEventType
from app.core.config import settings
from app.models.agent_governance import AgentAlert
from app.services import observability_service, ops_service, security_monitor_service


def _fake_execute(payloads, *, ip_attribution=None, fail_actions=()):
    """构造替换 ops_service.execute 的 fake。

    Args:
        payloads: action → 执行器数据负载。
        ip_attribution: ip_attribution 动作返回的负载；None 时返回默认归属。
        fail_actions: 需要抛错的动作集合。

    Returns:
        Callable: 与 ops_service.execute 同签名的 fake。
    """

    def fake(db, actor, *, action, params=None, request_id="", session_db_id=None, source=""):
        if action in fail_actions:
            raise RuntimeError(f"动作 {action} 执行失败(模拟)")
        if action == "ip_attribution":
            data = ip_attribution
            if data is None:
                data = {
                    "ip": (params or {}).get("ip"),
                    "attribution": {"country": "测试", "isp": "test-isp", "as": "AS000"},
                    "command_exit": 0,
                }
        else:
            data = payloads.get(action, {})
        return {
            "id": 1,
            "request_id": request_id,
            "action": action,
            "risk_level": "low",
            "status": "success",
            "result": {"ok": True, "action": action, "result": data, "duplicate": False},
            "error": None,
            "duration_ms": 1,
            "duplicate": False,
        }

    return fake


def _ssh_payload(*, accepted=None, failed_by_ip=None):
    """构造 ssh_login_events 负载。"""
    accepted = accepted or []
    failed_by_ip = failed_by_ip or []
    return {
        "since_hours": 1,
        "focus": "all",
        "accepted_total": len(accepted),
        "failed_total": sum(item["count"] for item in failed_by_ip),
        "accepted_by_ip": [],
        "failed_by_ip": failed_by_ip,
        "recent": accepted,
        "stdout_capped": False,
    }


@pytest.fixture
def emitted():
    """记录 emit_event 调用的事件列表（每条为 (args, kwargs)）。"""
    events: list[tuple[tuple, dict]] = []

    def _emit(*args, **kwargs):
        events.append((args, kwargs))

    return events


@pytest.fixture(autouse=True)
def _patch_emit(monkeypatch, emitted):
    def _capture(*args, **kwargs):
        emitted.append((args, kwargs))

    monkeypatch.setattr(security_monitor_service, "emit_event", _capture)
    return emitted


def test_ssh_accepted_non_whitelist_creates_high_alert_and_popup(db, super_admin_user, monkeypatch, emitted):
    """非白名单成功登录应生成 high 告警并推送 SSE 弹窗。"""
    payloads = {
        "ssh_login_events": _ssh_payload(
            accepted=[{"method": "publickey", "user": "root", "ip": "8.8.8.8", "detail": " (RSA SHA256:abc)"}],
        ),
        "flytrap_attack_events": {"total": 0, "by_ip": [], "recent": []},
        "nginx_attack_events": {"total": 0, "by_ip": [], "by_detail": [], "recent": []},
        "backup_audit": {"sql_gz_count": 0, "sql_gz_bytes": 0, "other_bytes": 0, "recent": [], "newest": None},
        "status": {"checks": {"disk": {"used_percent": 40}}},
    }
    monkeypatch.setattr(ops_service, "execute", _fake_execute(payloads))

    result = security_monitor_service.run_security_monitor(db)

    assert result["success"] is True
    assert len(result["created_alerts"]) == 1
    alert = db.query(AgentAlert).filter(AgentAlert.fingerprint == "login:8.8.8.8:root").one()
    assert alert.severity == "high"
    assert alert.category == "login"
    assert alert.source == "security_monitor"
    assert alert.user_id == super_admin_user.id
    assert alert.title == "SSH 登录：8.8.8.8（root）"
    detail = json.loads(alert.detail_json)
    assert detail["last_seen"]
    assert detail.get("attribution", {}).get("isp") == "test-isp"
    # 弹窗:高危来源 IP 触发 SSE
    assert len(emitted) == 1
    event_args, event_kwargs = emitted[0]
    assert event_args[0] == AgentEventType.ADMIN_ALERT
    assert event_kwargs["agent"] == "operations"
    assert event_kwargs["user_id"] == super_admin_user.id
    assert event_kwargs["message"] == "SSH 登录：8.8.8.8（root）"
    assert event_kwargs["payload"]["alert_id"] == alert.id
    assert event_kwargs["payload"]["severity"] == "high"
    assert event_kwargs["payload"]["category"] == "login"
    assert event_kwargs["payload"]["suggestion"] == "确认是否本人操作；非本人应立即吊销对应密钥并轮换"


def test_ssh_accepted_whitelist_info_without_popup(db, super_admin_user, monkeypatch, emitted):
    """白名单 IP 成功登录只入库不弹窗（severity=info，无 SSE）。"""
    monkeypatch.setattr(settings, "security_ssh_allowlist_cidrs", ["10.0.0.0/8"])
    payloads = {
        "ssh_login_events": _ssh_payload(
            accepted=[{"method": "password", "user": "deploy", "ip": "10.0.0.5", "detail": ""}],
        ),
        "flytrap_attack_events": {"total": 0, "by_ip": [], "recent": []},
        "nginx_attack_events": {"total": 0, "by_ip": [], "by_detail": [], "recent": []},
        "backup_audit": {"sql_gz_count": 0, "sql_gz_bytes": 0, "other_bytes": 0, "recent": [], "newest": None},
        "status": {"checks": {"disk": {"used_percent": 40}}},
    }
    monkeypatch.setattr(ops_service, "execute", _fake_execute(payloads))

    result = security_monitor_service.run_security_monitor(db)

    assert result["success"] is True
    alert = db.query(AgentAlert).filter(AgentAlert.fingerprint == "login:10.0.0.5:deploy").one()
    assert alert.severity == "info"
    assert alert.category == "login"
    assert emitted == []


def test_ssh_failed_brute_force_threshold(db, super_admin_user, monkeypatch, emitted):
    """同 IP 失败登录达到阈值触发 warning 爆破告警；低于阈值不告警。"""
    payloads = {
        "ssh_login_events": _ssh_payload(
            failed_by_ip=[{"value": "1.2.3.4", "count": 20}, {"value": "5.6.7.8", "count": 3}],
        ),
        "flytrap_attack_events": {"total": 0, "by_ip": [], "recent": []},
        "nginx_attack_events": {"total": 0, "by_ip": [], "by_detail": [], "recent": []},
        "backup_audit": {"sql_gz_count": 0, "sql_gz_bytes": 0, "other_bytes": 0, "recent": [], "newest": None},
        "status": {"checks": {"disk": {"used_percent": 40}}},
    }
    monkeypatch.setattr(ops_service, "execute", _fake_execute(payloads))

    result = security_monitor_service.run_security_monitor(db)

    assert len(result["created_alerts"]) == 1
    alert = db.query(AgentAlert).filter(AgentAlert.fingerprint == "brute:1.2.3.4").one()
    assert alert.severity == "warning"
    assert alert.category == "brute_force"
    assert db.query(AgentAlert).filter(AgentAlert.fingerprint == "brute:5.6.7.8").first() is None
    assert len(emitted) == 1


def test_flytrap_attack_threshold(db, super_admin_user, monkeypatch, emitted):
    """蜜罐同 IP 触碰达到阈值触发 warning 攻击告警。"""
    payloads = {
        "ssh_login_events": _ssh_payload(),
        "flytrap_attack_events": {
            "total": 15,
            "by_ip": [{"value": "203.0.113.9", "count": 10}, {"value": "203.0.113.10", "count": 5}],
            "recent": [],
        },
        "nginx_attack_events": {"total": 0, "by_ip": [], "by_detail": [], "recent": []},
        "backup_audit": {"sql_gz_count": 0, "sql_gz_bytes": 0, "other_bytes": 0, "recent": [], "newest": None},
        "status": {"checks": {"disk": {"used_percent": 40}}},
    }
    monkeypatch.setattr(ops_service, "execute", _fake_execute(payloads))

    security_monitor_service.run_security_monitor(db)

    alert = db.query(AgentAlert).filter(AgentAlert.fingerprint == "attack:203.0.113.9").one()
    assert alert.severity == "warning"
    assert alert.category == "attack"
    assert db.query(AgentAlert).filter(AgentAlert.fingerprint == "attack:203.0.113.10").first() is None
    assert len(emitted) == 1


def test_nginx_proxy_connect_and_tls_scanner(db, super_admin_user, monkeypatch, emitted):
    """Nginx CONNECT 代理探测 info；TLS 乱码同 IP ≥5 记 scanner（均不弹窗）。"""
    nginx_recent = [
        {
            "ip": "104.249.59.148", "method": "CONNECT", "path": "CONNECT x:443",
            "status": "400", "detail": "proxy_connect",
        },
    ]
    for i in range(5):
        nginx_recent.append({
            "ip": "172.236.228.227", "method": "\\x16\\x03", "path": "\\x16\\x03",
            "status": "400", "detail": "tls_gibberish",
        })
    payloads = {
        "ssh_login_events": _ssh_payload(),
        "flytrap_attack_events": {"total": 0, "by_ip": [], "recent": []},
        "nginx_attack_events": {"total": 6, "by_ip": [], "by_detail": [], "recent": nginx_recent},
        "backup_audit": {"sql_gz_count": 0, "sql_gz_bytes": 0, "other_bytes": 0, "recent": [], "newest": None},
        "status": {"checks": {"disk": {"used_percent": 40}}},
    }
    monkeypatch.setattr(ops_service, "execute", _fake_execute(payloads))

    security_monitor_service.run_security_monitor(db)

    proxy = db.query(AgentAlert).filter(AgentAlert.fingerprint == "proxy:104.249.59.148").one()
    assert proxy.severity == "info"
    assert proxy.category == "proxy_abuse"
    scanner = db.query(AgentAlert).filter(AgentAlert.fingerprint == "scanner:172.236.228.227").one()
    assert scanner.severity == "info"
    assert scanner.category == "scanner"
    assert emitted == []


def test_backup_rules_trigger(db, super_admin_user, monkeypatch, emitted):
    """备份超龄 high / 校验缺失 critical / 目录过大 warning。"""
    payloads = {
        "ssh_login_events": _ssh_payload(),
        "flytrap_attack_events": {"total": 0, "by_ip": [], "recent": []},
        "nginx_attack_events": {"total": 0, "by_ip": [], "by_detail": [], "recent": []},
        "backup_audit": {
            "sql_gz_count": 2,
            "sql_gz_bytes": 9_500_000_000,
            "other_bytes": 600_000_000,
            "newest": {"name": "db-20260805.sql.gz", "age_hours": 48, "has_sha256": False},
            "recent": [{"name": "db-20260805.sql.gz", "has_sha256": False}],
        },
        "status": {"checks": {"disk": {"used_percent": 40}}},
    }
    monkeypatch.setattr(ops_service, "execute", _fake_execute(payloads))

    result = security_monitor_service.run_security_monitor(db)

    assert len(result["created_alerts"]) == 3
    age = db.query(AgentAlert).filter(AgentAlert.fingerprint == "backup:age").one()
    assert age.severity == "high"
    assert age.category == "backup"
    verify = db.query(AgentAlert).filter(AgentAlert.fingerprint == "backup:verify").one()
    assert verify.severity == "critical"
    size = db.query(AgentAlert).filter(AgentAlert.fingerprint == "backup:size").one()
    assert size.severity == "warning"
    assert "备份目录占用过大" in size.title
    # 建议文本随 SSE 弹窗载荷推送
    assert any(kwargs["payload"].get("suggestion") == "清理超期备份与手工产物（审批后执行）"
               for _args, kwargs in emitted)
    assert len(emitted) == 3


def test_disk_high_usage_warning(db, super_admin_user, monkeypatch, emitted):
    """ops-check 磁盘使用率 ≥80 触发 optimization warning。"""
    payloads = {
        "ssh_login_events": _ssh_payload(),
        "flytrap_attack_events": {"total": 0, "by_ip": [], "recent": []},
        "nginx_attack_events": {"total": 0, "by_ip": [], "by_detail": [], "recent": []},
        "backup_audit": {"sql_gz_count": 0, "sql_gz_bytes": 0, "other_bytes": 0, "recent": [], "newest": None},
        "status": {"checks": {"disk": {"used_percent": 85}}},
    }
    monkeypatch.setattr(ops_service, "execute", _fake_execute(payloads))

    security_monitor_service.run_security_monitor(db)

    alert = db.query(AgentAlert).filter(AgentAlert.fingerprint == "opt:disk").one()
    assert alert.severity == "warning"
    assert alert.category == "optimization"
    assert len(emitted) == 1


def test_same_fingerprint_open_alert_dedup(db, super_admin_user, monkeypatch, emitted):
    """同 fingerprint 的 open 告警去重：只刷新 last_seen，不重复建、不重复弹窗。"""
    payloads = {
        "ssh_login_events": _ssh_payload(
            accepted=[{"method": "publickey", "user": "root", "ip": "8.8.8.8", "detail": " (RSA SHA256:abc)"}],
        ),
        "flytrap_attack_events": {"total": 0, "by_ip": [], "recent": []},
        "nginx_attack_events": {"total": 0, "by_ip": [], "by_detail": [], "recent": []},
        "backup_audit": {"sql_gz_count": 0, "sql_gz_bytes": 0, "other_bytes": 0, "recent": [], "newest": None},
        "status": {"checks": {"disk": {"used_percent": 40}}},
    }
    monkeypatch.setattr(ops_service, "execute", _fake_execute(payloads))

    first = security_monitor_service.run_security_monitor(db)
    alert_id = first["created_alerts"][0]["alert_id"]
    first_detail = json.loads(db.get(AgentAlert, alert_id).detail_json)

    second = security_monitor_service.run_security_monitor(db)

    assert second["created_alerts"] == []
    alerts = db.query(AgentAlert).filter(AgentAlert.fingerprint == "login:8.8.8.8:root").all()
    assert len(alerts) == 1
    second_detail = json.loads(db.get(AgentAlert, alert_id).detail_json)
    assert second_detail["last_seen"] >= first_detail["last_seen"]
    # 去重命中不重复弹窗
    assert len(emitted) == 1


def test_ip_attribution_failure_does_not_block_alert(db, super_admin_user, monkeypatch, emitted):
    """溯源富化失败不影响告警创建。"""
    payloads = {
        "ssh_login_events": _ssh_payload(
            accepted=[{"method": "publickey", "user": "root", "ip": "9.9.9.9", "detail": ""}],
        ),
        "flytrap_attack_events": {"total": 0, "by_ip": [], "recent": []},
        "nginx_attack_events": {"total": 0, "by_ip": [], "by_detail": [], "recent": []},
        "backup_audit": {"sql_gz_count": 0, "sql_gz_bytes": 0, "other_bytes": 0, "recent": [], "newest": None},
        "status": {"checks": {"disk": {"used_percent": 40}}},
    }
    monkeypatch.setattr(ops_service, "execute", _fake_execute(payloads, fail_actions=("ip_attribution",)))

    result = security_monitor_service.run_security_monitor(db)

    assert len(result["created_alerts"]) == 1
    alert = db.query(AgentAlert).filter(AgentAlert.fingerprint == "login:9.9.9.9:root").one()
    assert alert.severity == "high"
    assert "attribution" not in json.loads(alert.detail_json)
    # 弹窗仍正常推送
    assert len(emitted) == 1


def test_single_action_failure_does_not_break_whole(db, super_admin_user, monkeypatch, emitted):
    """单个动作失败不中断整体巡检，错误进入 errors。"""
    payloads = {
        "ssh_login_events": _ssh_payload(
            accepted=[{"method": "publickey", "user": "root", "ip": "8.8.8.8", "detail": ""}],
        ),
        "flytrap_attack_events": {"total": 0, "by_ip": [], "recent": []},
        "nginx_attack_events": {"total": 0, "by_ip": [], "by_detail": [], "recent": []},
        "backup_audit": {"sql_gz_count": 0, "sql_gz_bytes": 0, "other_bytes": 0, "recent": [], "newest": None},
        "status": {"checks": {"disk": {"used_percent": 40}}},
    }
    monkeypatch.setattr(ops_service, "execute", _fake_execute(payloads, fail_actions=("nginx_attack_events",)))

    result = security_monitor_service.run_security_monitor(db)

    assert result["success"] is False
    assert result["can_continue"] is True
    assert result["partial_failure"] is True
    assert result["fatal_failure"] is False
    assert "nginx_attack_events" in result["failed_actions"]
    assert "ssh_login_events" in result["completed_actions"]
    assert any(item["action"] == "nginx_attack_events" for item in result["errors"])
    # SSH 告警仍被创建
    assert len(result["created_alerts"]) == 1
    assert db.query(AgentAlert).filter(AgentAlert.fingerprint == "login:8.8.8.8:root").count() == 1


def test_flytrap_upstream_degradation_keeps_local_data_and_notifies_admin(
    db, super_admin_user, monkeypatch, emitted,
):
    """上游同步异常应保留本地攻击数据，并作为可继续降级通知管理员。"""
    payloads = {
        "ssh_login_events": _ssh_payload(),
        "flytrap_attack_events": {
            "ok": True,
            "degraded": True,
            "can_continue": True,
            "reason": "FlyTrap 上游同步异常，本地蜜罐和持久队列继续工作",
            "total": 12,
            "by_ip": [{"value": "203.0.113.9", "count": 12}],
            "recent": [],
            "health": {
                "status": "degraded",
                "issues": [{"code": "flytrap_sync_error", "message": "同步周期超时"}],
            },
            "human_actions": [{
                "code": "flytrap_upstream_recovery",
                "label": "检查 FlyTrap 上游连通性",
                "message": "核对上游服务、防火墙和网络路由，恢复后确认同步成功日志。",
                "requires_human": True,
            }],
        },
        "nginx_attack_events": {"total": 0, "by_ip": [], "by_detail": [], "recent": []},
        "backup_audit": {"sql_gz_count": 0, "sql_gz_bytes": 0, "other_bytes": 0, "recent": [], "newest": None},
        "status": {"checks": {"disk": {"used_percent": 40}}},
    }
    monkeypatch.setattr(ops_service, "execute", _fake_execute(payloads))

    result = security_monitor_service.run_security_monitor(db)

    assert result["success"] is False
    assert result["can_continue"] is True
    assert result["partial_failure"] is True
    assert result["fatal_failure"] is False
    assert "flytrap_attack_events" in result["completed_actions"]
    assert "flytrap_attack_events" in result["degraded_actions"]
    assert "flytrap_attack_events" not in result["failed_actions"]
    assert result["human_actions"][0]["code"] == "flytrap_upstream_recovery"
    assert any(item["action"] == "flytrap_attack_events" and item["degraded"] for item in result["errors"])
    assert db.query(AgentAlert).filter(
        AgentAlert.fingerprint == "integration:flytrap_upstream",
        AgentAlert.status == "open",
    ).count() == 1
    assert db.query(AgentAlert).filter(AgentAlert.fingerprint == "attack:203.0.113.9").count() == 1
    assert any(event[1]["payload"]["category"] == "integration" for event in emitted)


def test_flytrap_upstream_recovery_resolves_integration_alert(
    db, super_admin_user, monkeypatch, emitted,
):
    """同步恢复后应自动关闭同一告警，避免人工处理入口永久悬挂。"""
    degraded_payload = {
        "ssh_login_events": _ssh_payload(),
        "flytrap_attack_events": {
            "ok": True,
            "degraded": True,
            "reason": "FlyTrap 上游同步异常",
            "total": 0,
            "by_ip": [],
            "recent": [],
            "health": {"status": "degraded", "issues": [{"code": "flytrap_sync_error"}]},
            "human_actions": [{"code": "flytrap_upstream_recovery", "requires_human": True}],
        },
        "nginx_attack_events": {"total": 0, "by_ip": [], "by_detail": [], "recent": []},
        "backup_audit": {"sql_gz_count": 0, "sql_gz_bytes": 0, "other_bytes": 0, "recent": [], "newest": None},
        "status": {"checks": {"disk": {"used_percent": 40}}},
    }
    monkeypatch.setattr(ops_service, "execute", _fake_execute(degraded_payload))
    security_monitor_service.run_security_monitor(db)

    healthy_payload = dict(degraded_payload)
    healthy_payload["flytrap_attack_events"] = {
        "ok": True,
        "degraded": False,
        "total": 0,
        "by_ip": [],
        "recent": [],
        "health": {"status": "ok", "issues": []},
        "human_actions": [],
    }
    monkeypatch.setattr(ops_service, "execute", _fake_execute(healthy_payload))

    result = security_monitor_service.run_security_monitor(db)

    assert result["success"] is True
    alert = db.query(AgentAlert).filter(
        AgentAlert.fingerprint == "integration:flytrap_upstream",
    ).one()
    assert alert.status == "resolved"
    assert alert.resolved_at is not None


def test_all_security_sources_failure_requires_human_recovery(
    db, super_admin_user, monkeypatch, emitted,
):
    """全部采集器不可用时必须暴露安全监控盲区，不能标为可继续。"""
    action_names = {
        "ssh_login_events",
        "flytrap_attack_events",
        "nginx_attack_events",
        "backup_audit",
        "db_threat_signals",
        "db_health",
        "status",
    }
    monkeypatch.setattr(
        ops_service,
        "execute",
        _fake_execute({}, fail_actions=action_names),
    )

    result = security_monitor_service.run_security_monitor(db)

    assert result["success"] is False
    assert result["can_continue"] is False
    assert result["partial_failure"] is False
    assert result["fatal_failure"] is True
    assert result["completed_actions"] == []
    assert set(result["failed_actions"]) == action_names
    assert "人工核验" in result["error"]


def test_query_security_status_aggregates(db, monkeypatch):
    """query_security_status 聚合 SSH/攻击/备份/open 告警。"""
    payloads = {
        "ssh_login_events": _ssh_payload(
            accepted=[{"method": "publickey", "user": "root", "ip": "8.8.8.8", "detail": ""}],
            failed_by_ip=[{"value": "1.2.3.4", "count": 20}],
        ),
        "flytrap_attack_events": {
            "total": 15, "by_ip": [{"value": "203.0.113.9", "count": 10}], "recent": [],
        },
        "nginx_attack_events": {
            "total": 6, "by_ip": [{"value": "104.249.59.148", "count": 1}], "by_detail": [], "recent": [],
        },
        "backup_audit": {
            "sql_gz_count": 2, "sql_gz_bytes": 100, "other_bytes": 200,
            "newest": {"name": "x.sql.gz", "age_hours": 5},
        },
    }
    monkeypatch.setattr(ops_service, "execute", _fake_execute(payloads))
    observability_service.create_alert(
        db,
        alert_type="security.login",
        severity="high",
        title="SSH 登录：8.8.8.8（root）",
        category="login",
        source="security_monitor",
        user_id=1,
        fingerprint="login:8.8.8.8:root",
    )

    status = security_monitor_service.query_security_status(db, since_hours=24)

    assert status["since_hours"] == 24
    assert status["ssh"]["accepted_total"] == 1
    assert status["ssh"]["failed_total"] == 20
    assert status["ssh"]["total"] == 21
    assert status["attacks"]["flytrap_total"] == 15
    assert status["attacks"]["nginx_total"] == 6
    assert status["backup"]["sql_gz_count"] == 2
    assert len(status["open_alerts"]) == 1
    assert status["open_alerts"][0]["title"] == "SSH 登录：8.8.8.8（root）"


def test_no_admin_skips_sse_but_alert_created(db, monkeypatch, emitted):
    """找不到唯一超级管理员时跳过 SSE，但告警仍入库。"""
    payloads = {
        "ssh_login_events": _ssh_payload(
            accepted=[{"method": "publickey", "user": "root", "ip": "8.8.8.8", "detail": ""}],
        ),
        "flytrap_attack_events": {"total": 0, "by_ip": [], "recent": []},
        "nginx_attack_events": {"total": 0, "by_ip": [], "by_detail": [], "recent": []},
        "backup_audit": {"sql_gz_count": 0, "sql_gz_bytes": 0, "other_bytes": 0, "recent": [], "newest": None},
        "status": {"checks": {"disk": {"used_percent": 40}}},
    }
    monkeypatch.setattr(ops_service, "execute", _fake_execute(payloads))

    result = security_monitor_service.run_security_monitor(db)

    assert len(result["created_alerts"]) == 1
    alert = db.query(AgentAlert).filter(AgentAlert.fingerprint == "login:8.8.8.8:root").one()
    assert alert.user_id is None
    assert emitted == []


def _db_payload(*, ok=True, destructive=0, dump=0, error=0, reason=None):
    """构造 db_threat_signals 负载。"""
    payload = {
        "ok": ok,
        "destructive_total": destructive,
        "dump_exfil_total": dump,
        "error_total": error,
        "destructive_by_user": [{"value": "root[root] @ localhost []", "count": destructive}] if destructive else [],
        "error_by_user": [{"value": "app[app] @ 10.0.0.1 []", "count": error}] if error else [],
        "samples": {
            "destructive": [{"user_host": "root[root] @ localhost []", "sql": "DROP TABLE ?", "event_time": "2026-08-05 10:00:00"}] * min(destructive, 1),  # noqa: E501
            "dump_exfil": [{"user_host": "app[app] @ 10.0.0.1 []", "sql": "SELECT ? INTO OUTFILE ?", "event_time": "2026-08-05 10:02:00"}] * min(dump, 1),  # noqa: E501
            "error": [],
        },
    }
    if reason:
        payload["reason"] = reason
    return payload


def _base_payloads():
    return {
        "ssh_login_events": _ssh_payload(),
        "flytrap_attack_events": {"total": 0, "by_ip": [], "recent": []},
        "nginx_attack_events": {"total": 0, "by_ip": [], "by_detail": [], "recent": []},
        "backup_audit": {"sql_gz_count": 0, "sql_gz_bytes": 0, "other_bytes": 0, "recent": [], "newest": None},
        "status": {"checks": {"disk": {"used_percent": 40}}},
    }


def test_db_destructive_creates_critical_alert_and_popup(db, super_admin_user, monkeypatch, emitted):
    """数据库破坏性操作应生成 critical 告警并推送 SSE。"""
    payloads = _base_payloads()
    payloads["db_threat_signals"] = _db_payload(destructive=2)
    monkeypatch.setattr(ops_service, "execute", _fake_execute(payloads))

    result = security_monitor_service.run_security_monitor(db)

    assert len(result["created_alerts"]) == 1
    alert = db.query(AgentAlert).filter(AgentAlert.fingerprint == "db:destructive").one()
    assert alert.severity == "critical"
    assert alert.category == "db_threat"
    assert "破坏性操作" in alert.title
    assert len(emitted) == 1
    _, kwargs = emitted[0]
    assert kwargs["payload"]["category"] == "db_threat"
    assert kwargs["payload"]["severity"] == "critical"


def test_db_dump_exfil_creates_critical_alert(db, super_admin_user, monkeypatch, emitted):
    """数据库批量导出/外泄应生成 critical 告警。"""
    payloads = _base_payloads()
    payloads["db_threat_signals"] = _db_payload(dump=1)
    monkeypatch.setattr(ops_service, "execute", _fake_execute(payloads))

    result = security_monitor_service.run_security_monitor(db)

    assert len(result["created_alerts"]) == 1
    alert = db.query(AgentAlert).filter(AgentAlert.fingerprint == "db:dump_exfil").one()
    assert alert.severity == "critical"
    assert "外泄" in alert.title


def test_db_errors_threshold_creates_warning(db, super_admin_user, monkeypatch, emitted):
    """数据库访问异常达阈值生成 warning 告警。"""
    monkeypatch.setattr(settings, "security_db_error_threshold", 3)
    payloads = _base_payloads()
    payloads["db_threat_signals"] = _db_payload(error=3)
    monkeypatch.setattr(ops_service, "execute", _fake_execute(payloads))

    result = security_monitor_service.run_security_monitor(db)

    assert len(result["created_alerts"]) == 1
    alert = db.query(AgentAlert).filter(AgentAlert.fingerprint == "db:errors").one()
    assert alert.severity == "warning"


def test_db_general_log_disabled_no_alert(db, super_admin_user, monkeypatch, emitted):
    """general_log 未开启（ok=False）不应产生任何数据库告警。"""
    payloads = _base_payloads()
    payloads["db_threat_signals"] = _db_payload(ok=False, reason="general_log 未开启")
    monkeypatch.setattr(ops_service, "execute", _fake_execute(payloads))

    result = security_monitor_service.run_security_monitor(db)

    assert result["created_alerts"] == []
    assert db.query(AgentAlert).filter(AgentAlert.category == "db_threat").count() == 0


def test_collector_source_failure_is_reported_without_blocking_other_sources(
    db, super_admin_user, monkeypatch, emitted,
):
    payloads = _base_payloads()
    payloads["ssh_login_events"] = {
        "ok": False,
        "source_exit_code": 1,
        "source_error": "journal unavailable",
    }
    monkeypatch.setattr(ops_service, "execute", _fake_execute(payloads))

    result = security_monitor_service.run_security_monitor(db)

    assert result["success"] is False
    assert result["errors"] == [{"action": "ssh_login_events", "error": "journal unavailable"}]
    assert result["actions"]["backup_audit"] == payloads["backup_audit"]


def test_db_monitor_disabled_skips_action(db, super_admin_user, monkeypatch, emitted):
    """security_db_monitor_enabled=False 时不调用 db_threat_signals 动作。"""
    monkeypatch.setattr(settings, "security_db_monitor_enabled", False)
    called = []

    def spy(db_, actor, *, action, params=None, request_id="", session_db_id=None, source=""):
        called.append(action)
        return _fake_execute(_base_payloads())(db_, actor, action=action, params=params, request_id=request_id)

    monkeypatch.setattr(ops_service, "execute", spy)
    result = security_monitor_service.run_security_monitor(db)

    assert "db_threat_signals" not in called
    assert "db_threat_signals" not in result["actions"]


def test_query_security_status_includes_db_threat(db, super_admin_user, monkeypatch):
    """安全态势聚合应包含数据库威胁统计。"""
    payloads = _base_payloads()
    payloads["db_threat_signals"] = _db_payload(destructive=1, error=2)
    monkeypatch.setattr(ops_service, "execute", _fake_execute(payloads))

    status = security_monitor_service.query_security_status(db)

    assert status["db_threat"]["enabled"] is True
    assert status["db_threat"]["ok"] is True
    assert status["db_threat"]["destructive_total"] == 1
    assert status["db_threat"]["error_total"] == 2


def test_db_health_restart_creates_critical_alert(db, super_admin_user, monkeypatch, emitted):
    """数据库近24h异常重启/崩溃恢复应生成 critical 告警并弹窗。"""
    payloads = _base_payloads()
    payloads["db_health"] = {
        "restart_count": 1,
        "recovery_detected": True,
        "container_restart_count": "1",
        "mem_usage": "455MiB / 640MiB",
        "restart_lines": ["ready for connections"],
        "recovery_lines": ["InnoDB: Starting crash recovery"],
    }
    monkeypatch.setattr(ops_service, "execute", _fake_execute(payloads))

    result = security_monitor_service.run_security_monitor(db)

    assert len(result["created_alerts"]) == 1
    alert = db.query(AgentAlert).filter(AgentAlert.fingerprint == "db:health_restart").one()
    assert alert.severity == "critical"
    assert alert.category == "db_threat"
    assert "异常重启" in alert.title
    assert len(emitted) == 1


def test_db_health_clean_no_alert(db, super_admin_user, monkeypatch, emitted):
    """数据库健康（无重启/恢复）不产生告警。"""
    payloads = _base_payloads()
    payloads["db_health"] = {"restart_count": 0, "recovery_detected": False, "restart_lines": []}
    monkeypatch.setattr(ops_service, "execute", _fake_execute(payloads))

    result = security_monitor_service.run_security_monitor(db)

    assert result["created_alerts"] == []
    assert db.query(AgentAlert).filter(AgentAlert.fingerprint == "db:health_restart").count() == 0
