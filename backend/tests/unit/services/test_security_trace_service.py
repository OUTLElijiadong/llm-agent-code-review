"""来源溯源、防御面审计与流量摘要只采信 root 回执并保持只读边界。"""

from __future__ import annotations

import pytest

from app.services import ops_service, security_trace_service


def _success(payload):
    return {"status": "success", "request_id": "trace-unit-request", "result": {"ok": True, "result": payload}}


def _trace_payload(**extra):
    payload = {
        "ip": "45.155.205.7",
        "generated_at": "2026-10-05T10:00:00+00:00",
        "window_hours": 24,
        "is_public": True,
        "is_protected": False,
        "risk": {"score": 90, "level": "critical", "reasons": ["SSH 密码失败 40 次"], "basis": "本机可信日志证据，不含模型推断；外部归因不参与评分"},
        "ssh": {"count": 40, "accounts_tried": [{"account": "root", "count": 39}],
                "first_seen": "2026-10-05T09:00:00+00:00", "last_seen": "2026-10-05T09:30:00+00:00"},
        "web": {"count": 12, "target_count": 3, "targets": [{"path": "/.env", "count": 6}],
                "methods": {"GET": 12}, "status_codes": {"404": 12},
                "first_seen": None, "last_seen": None},
        "defense_records": [{"id": "lease-1", "ip": "45.155.205.7", "rule": "ssh_failed_password",
                             "source": "deterministic_rule", "status": "active",
                             "started_at": "2026-10-05T09:31:00+00:00", "expires_at": "2026-10-05T09:46:00+00:00",
                             "released_at": None, "evidence_count": 40, "reason": "窗口内失败 40 次"}],
        "attribution": {"ok": True, "attribution": {"country": "Netherlands", "isp": "Example Hosting BV"}},
        "reverse_dns": {"ok": True, "output": "45.155.205.7 vps.example.net"},
        "whois": {"ok": True, "summary": "country=NL"},
        "evidence_sources": ["journalctl -u sshd", "docker logs cr_frontend"],
        "errors": [],
    }
    payload.update(extra)
    return payload


def test_trace_normalizes_verified_payload(db, super_admin_user, monkeypatch):
    monkeypatch.setattr(ops_service, "execute", lambda *_a, **_k: _success(_trace_payload()))
    result = security_trace_service.trace_ip(db, super_admin_user, ip="45.155.205.7")
    assert result["available"] is True and result["verified"] is True
    assert result["ip"] == "45.155.205.7"
    assert result["risk"]["score"] == 90
    assert result["risk"]["level"] == "critical"
    assert "不含模型推断" in result["risk"]["basis"]
    assert result["ssh"]["count"] == 40
    assert result["web"]["target_count"] == 3
    assert result["web"]["methods"] == {"GET": 12}
    assert result["defense_records"][0]["status"] == "active"
    assert result["attribution"]["country"] == "Netherlands"
    assert result["attribution"]["isp"] == "Example Hosting BV"
    assert result["request_id"] == "trace-unit-request"


@pytest.mark.parametrize("status", ["failed", "running"])
def test_trace_never_invents_data_without_success_receipt(db, super_admin_user, monkeypatch, status):
    monkeypatch.setattr(ops_service, "execute", lambda *_a, **_k: {"status": status, "request_id": "r", "result": None,
                                                                  "error": "执行器未确认"})
    result = security_trace_service.trace_ip(db, super_admin_user, ip="45.155.205.7")
    assert result["available"] is False and result["verified"] is False
    assert result["errors"]
    assert "risk" not in result
    assert "ssh" not in result


def test_trace_rejects_network_input_before_calling_executor(db, super_admin_user, monkeypatch):
    called: list[str] = []

    def _forbidden(*_args, **_kwargs):
        called.append("executed")
        raise AssertionError("非法输入不应触发宿主机动作")

    monkeypatch.setattr(ops_service, "execute", _forbidden)
    with pytest.raises(ValueError):
        security_trace_service.trace_ip(db, super_admin_user, ip="45.155.205.0/24")
    assert called == []


def test_trace_drops_unknown_attribution_fields(db, super_admin_user, monkeypatch):
    payload = _trace_payload()
    payload["attribution"] = {"ok": True, "note": "", "attribution": {
        "country": "NL", "secret_token": "should-not-render", "isp": "X",
    }}
    monkeypatch.setattr(ops_service, "execute", lambda *_a, **_k: _success(payload))
    result = security_trace_service.trace_ip(db, super_admin_user, ip="45.155.205.7")
    assert set(result["attribution"]) == {"ok", "country", "region", "city", "isp", "org", "as"}
    assert "secret_token" not in result["attribution"]


def test_trace_denies_non_admin_actor_and_reports_unavailable(db, monkeypatch):
    """非超级管理员调用被执行层拒绝；服务把拒绝转成不可用状态，不返回任何证据。"""
    from app.models.user import User

    member = User(username="trace-member", password="$2b$12$" + "x" * 53, role="user", status=1)
    db.add(member)
    db.commit()
    result = security_trace_service.trace_ip(db, member, ip="45.155.205.7")
    assert result["available"] is False and result["verified"] is False
    assert any("超级管理员" in item for item in result["errors"])
    assert set(result) == {"available", "verified", "kind", "errors"}


def test_trace_denies_disabled_actor(db, monkeypatch):
    """禁用账号即使角色字段是 admin 也必须失败关闭。"""
    from app.models.user import User

    disabled = User(username="trace-disabled", password="$2b$12$" + "x" * 53, role="admin", status=0)
    db.add(disabled)
    db.commit()
    result = security_trace_service.trace_ip(db, disabled, ip="1.1.1.1")
    assert result["available"] is False
    assert any("超级管理员" in item for item in result["errors"])


def test_trace_action_registered_as_internal_read_only():
    assert "security_ip_trace" in ops_service.INTERNAL_SECURITY_ACTIONS
    assert "security_ip_trace" in ops_service.READ_ONLY_ACTIONS
    assert ops_service.ACTION_PARAM_KEYS["security_ip_trace"] == {"ip"}
    assert ops_service.ACTION_REQUIRED_PARAMS["security_ip_trace"] == {"ip"}
    assert "security_ip_trace" not in ops_service.SCHEDULER_READ_ACTIONS


def test_surface_audit_normalizes_listeners_and_applications(db, super_admin_user, monkeypatch):
    payload = {
        "generated_at": "2026-10-05T10:00:00+00:00",
        "listeners": [{"protocol": "tcp", "address": "0.0.0.0", "port": "443", "process": "nginx"},
                      {"protocol": "tcp", "address": "127.0.0.1", "port": "8000", "process": "uvicorn"},
                      {"protocol": "tcp", "address": "0.0.0.0", "port": "not-a-port", "process": "x"}],
        "firewall": {"ipv4": {"tool": "iptables", "chains": [{"chain": "INPUT", "ok": True, "policy": "-P INPUT ACCEPT",
                                                             "rules": ["-A INPUT -j PRISM-SEC-IN"], "note": ""}],
                             "tools_present": ["iptables"]},
                     "ipv6": {"tool": "ip6tables", "chains": [], "tools_present": []}},
        "applications": [{"name": "fail2ban", "purpose": "登录爆破自动处置", "installed": False}],
        "ipset": {"sets": ["prism-sec-v4"], "present": True},
        "blocking": {"enabled": True, "backend": "ipset", "active_leases": 2},
        "ssh_ports": "22",
        "errors": ["whois 未安装"],
    }
    monkeypatch.setattr(ops_service, "execute", lambda *_a, **_k: _success(payload))
    result = security_trace_service.surface_audit(db, super_admin_user)
    assert result["available"] is True
    assert [row["port"] for row in result["listeners"]] == [443, 8000]
    assert result["public_listener_count"] == 1
    assert result["applications"][0]["installed"] is False
    assert result["blocking"] == {"enabled": True, "backend": "ipset", "active_leases": 2}
    assert result["firewall"]["ipv4"]["chains"][0]["ok"] is True
    assert result["errors"] == ["whois 未安装"]


def test_traffic_summary_exposes_metadata_and_never_claims_payload(db, super_admin_user, monkeypatch):
    payload = {
        "generated_at": "2026-10-05T10:00:00+00:00",
        "window_hours": 24,
        "peers": [{"ip": "45.155.205.7", "connections": 3, "protocols": {"tcp": 3}, "peer_ports": {"51022": 3},
                   "processes": ["nginx"], "states": {"ESTAB": 3}, "ssh_failed_count": 40,
                   "sensitive_probe_count": 12, "target_count": 3, "last_seen": "2026-10-05T09:30:00+00:00"},
                  {"ip": "not-an-ip", "connections": 1}],
        "peer_total": 2,
        "current_connections": 3,
        "recent_ssh_failed_sources": 1,
        "recent_probe_sources": 1,
        "payload_captured": False,
        "note": "只采集连接元数据与可信日志计数，不捕获也不存储流量载荷。",
        "errors": [],
    }
    monkeypatch.setattr(ops_service, "execute", lambda *_a, **_k: _success(payload))
    result = security_trace_service.traffic_summary(db, super_admin_user, since_hours=24)
    assert result["available"] is True
    assert len(result["peers"]) == 1
    assert result["peers"][0]["ssh_failed_count"] == 40
    assert result["payload_captured"] is False
    assert result["peer_total"] == 2
    assert "不捕获" in result["note"]


def test_traffic_summary_rejects_out_of_contract_hours(db, super_admin_user, monkeypatch):
    monkeypatch.setattr(ops_service, "execute", lambda *_a, **_k: _success({"peers": []}))
    with pytest.raises(ValueError):
        security_trace_service.traffic_summary(db, super_admin_user, since_hours=240)
