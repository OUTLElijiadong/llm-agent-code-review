"""小菱安全运营（自动化攻防编排）单测。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.exceptions import ForbiddenError, ValidationError
from app.models.audit_log import AuditLog
from app.models.pentest import PentestEngagement
from app.models.rbac import Role, UserRole
from app.models.user import User
from app.services import secops_service


@pytest.fixture
def super_admin(db):
    user = User(username="admin", password="x", email="root@t.c", nickname="超管", role="super_admin", status=1)
    role = Role(name="超级管理员", code="super_admin", status="active", sort=0, is_builtin=1)
    db.add_all([user, role])
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.commit()
    return user


@pytest.fixture
def ops_stub(monkeypatch):
    """把宿主机执行器替换成内存替身，单测不碰真实主机。"""
    calls = []

    def fake_execute(db, actor, *, action, params=None, request_id="", session_db_id=None, source="admin_copilot"):
        calls.append({"action": action, "params": params or {}, "source": source})
        if action == "nginx_attack_events":
            return {"events": [
                {"ip": "45.135.228.155", "type": "sql_injection"},
                {"ip": "45.135.228.155", "type": "path_traversal"},
                {"ip": "203.0.113.9", "type": "xss"},
            ]}
        if action == "ssh_login_events":
            return {"events": [
                {"ip": "45.135.228.155", "type": "failed_password"},
                {"ip": "203.0.113.9", "type": "failed_password"},
            ]}
        if action == "flytrap_attack_events":
            return {"events": [{"ip": "45.135.228.155", "type": "honeypot_probe"}]}
        if action == "db_threat_signals":
            return {"events": []}
        if action == "security_block_candidates":
            return {"candidates": [{"ip": "45.135.228.155", "hits": 4}]}
        if action == "certificate_status":
            return {"data": {"not_after": "2027-01-01"}}
        if action == "security_surface_audit":
            return {"data": {"ports": [80, 443]}}
        if action == "backup_audit":
            return {"data": {"last": "ok"}}
        return {"ok": True}

    monkeypatch.setattr(secops_service.ops_service, "execute", fake_execute)
    return calls


class TestThreatDigest:
    def test_aggregates_sources_across_sensors(self, db, ops_stub):
        digest = secops_service.threat_digest(db, None, hours=24)
        assert digest["window_hours"] == 24
        top = digest["sources"][0]
        assert top["ip"] == "45.135.228.155"
        assert top["hits"] == 4
        assert set(top["sensors"]) >= {"nginx_attack_events", "ssh_login_events", "flytrap_attack_events"}
        assert top["severity"] == "critical"
        assert top["recommended"] == "block"
        # 单源命中不会因为条数多就被判 critical
        weak = [row for row in digest["sources"] if row["ip"] == "203.0.113.9"][0]
        assert weak["severity"] in {"low", "medium"}

    def test_all_sensors_go_through_security_center_source(self, db, ops_stub):
        secops_service.threat_digest(db, None)
        assert ops_stub
        assert {call["source"] for call in ops_stub} == {"security_center"}

    def test_digest_is_audited(self, db, ops_stub):
        secops_service.threat_digest(db, None)
        assert db.query(AuditLog).filter(AuditLog.action == "secops.threat_digest").count() == 1


class TestBlocking:
    def test_block_requires_confirm(self, db, super_admin, ops_stub):
        with pytest.raises(ValidationError):
            secops_service.apply_block(db, super_admin, ip="45.135.228.155", reason="扫描", confirm=False)

    def test_block_rejects_loopback(self, db, super_admin, ops_stub):
        with pytest.raises(ValidationError):
            secops_service.apply_block(db, super_admin, ip="127.0.0.1", reason="误伤", confirm=True)

    def test_block_refuses_allowlisted_source(self, db, super_admin, ops_stub):
        from app.services import copilot_security_service as cs

        cs.save_policy(db, patch={"ip_allowlist": ["10.0.0.0/8"]})
        with pytest.raises(ValidationError):
            secops_service.apply_block(db, super_admin, ip="10.1.2.3", reason="自家出口", confirm=True)

    def test_block_needs_super_admin(self, db, ops_stub):
        plain = User(username="ops", password="x", email="o@t.c", nickname="运维", role="admin", status=1)
        db.add(plain)
        db.commit()
        with pytest.raises(ForbiddenError):
            secops_service.apply_block(db, plain, ip="45.135.228.155", reason="x", confirm=True)

    def test_block_happy_path_is_audited(self, db, super_admin, ops_stub):
        result = secops_service.apply_block(
            db, super_admin, ip="45.135.228.155", reason="暴力破解", duration_seconds=600, confirm=True,
        )
        assert result["blocked"] is True
        assert any(call["action"] == "security_block_apply_anomalies" for call in ops_stub)
        row = db.query(AuditLog).filter(AuditLog.action == "secops.block_apply").first()
        assert row is not None and row.status == "success"


class TestPentestGate:
    def _engagement(self, db, super_admin, **overrides):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        engagement = PentestEngagement(
            public_id="pt_test_0001",
            user_id=int(super_admin.id),
            project_id=1,
            target_type="web",
            scope_json=overrides.pop("scope_json", '{"remote_target_url": "https://lijiadong.cn"}'),
            status=overrides.pop("status", "authorized"),
            rules_ack=1,
            window_start=overrides.pop("window_start", now - timedelta(hours=1)),
            window_end=overrides.pop("window_end", now + timedelta(hours=1)),
            summary_json="{}",
        )
        db.add(engagement)
        db.commit()
        return engagement

    def test_rejects_unlisted_target_host(self, db, super_admin, ops_stub):
        self._engagement(db, super_admin)
        with pytest.raises(ForbiddenError) as exc:
            secops_service.start_authorized_pentest(db, super_admin, public_id="pt_test_0001")
        assert exc.value.code == 40344  # 未配置资产清单

    def test_rejects_expired_window(self, db, super_admin, ops_stub):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        self._engagement(db, super_admin, window_start=now - timedelta(hours=5), window_end=now - timedelta(hours=1))
        with pytest.raises(ForbiddenError) as exc:
            secops_service.start_authorized_pentest(db, super_admin, public_id="pt_test_0001")
        assert exc.value.code == 40343

    def test_rejects_draft_status(self, db, super_admin, ops_stub):
        self._engagement(db, super_admin, status="draft")
        with pytest.raises(ForbiddenError) as exc:
            secops_service.start_authorized_pentest(db, super_admin, public_id="pt_test_0001")
        assert exc.value.code == 40340

    def test_allows_listed_own_asset(self, db, super_admin, ops_stub, monkeypatch):
        from app.core.config import settings
        from app.services import pentest_service

        monkeypatch.setattr(settings, "secops_asset_hosts", ["lijiadong.cn"])
        monkeypatch.setattr(pentest_service, "start_engagement", lambda *_a, **_k: {"status": "running"})
        self._engagement(db, super_admin)
        result = secops_service.start_authorized_pentest(db, super_admin, public_id="pt_test_0001")
        assert result["started"] is True


class TestBaseline:
    def test_baseline_without_base_url_skips_network(self, db, ops_stub):
        report = secops_service.baseline_check(db, None)
        assert report["base_url"] is None
        openapi = [item for item in report["checks"] if item["key"] == "openapi"][0]
        assert openapi["passed"] is None  # 未配置 → 不计分
        assert report["score"] >= 0

    def test_patrol_runs_and_alerts(self, db, ops_stub, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "secops_auto_block_enabled", False)
        result = secops_service.patrol_once(db)
        assert result["critical"] >= 1
        assert result["auto_blocked"] == []
        row = db.query(AuditLog).filter(AuditLog.action == "secops.patrol").first()
        assert row is not None
