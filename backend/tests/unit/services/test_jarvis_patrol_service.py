"""JARVIS 全自动运维巡逻单元测试。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.agent_capability import SandboxEnvironment
from app.models.agent_governance import AgentAlert
from app.models.agent_mesh import AgentMeshConversation, AgentMeshMessage, AgentMeshMessageEvent
from app.models.agent_response_run import AgentResponseRun
from app.models.user import User
from app.services import jarvis_patrol_service as module


def _make_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'jarvis.db'}")
    SandboxEnvironment.__table__.create(engine)
    AgentAlert.__table__.create(engine)
    AgentMeshConversation.__table__.create(engine)
    AgentMeshMessage.__table__.create(engine)
    AgentMeshMessageEvent.__table__.create(engine)
    AgentResponseRun.__table__.create(engine)
    User.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(module, "SessionLocal", lambda: factory())
    return engine, factory


def _seed(db, *, online=True):
    now = datetime.now(timezone.utc)
    db.add(User(id=1, username="admin", role="admin", status=1, password="x", token_version=0))
    db.add(AgentMeshConversation(
        user_id=1, surface="admin", session_key="admin-session-a1", title="管理对话",
        status="active", last_seen_at=now if online else now - timedelta(hours=2),
    ))
    db.commit()


def test_patrol_delivers_brief_for_high_alert(tmp_path, monkeypatch):
    engine, factory = _make_db(tmp_path, monkeypatch)
    db = factory()
    _seed(db)
    db.add(AgentAlert(
        alert_type="sandbox_stuck", severity="critical", status="open",
        title="沙箱卡死", detail_json='{"reason":"心跳超时"}',
        category="sandbox_stuck", source="sandbox_watchdog",
    ))
    db.commit()

    result = module.patrol_once()

    assert result["delivered"] == 1
    message = db.query(AgentMeshMessage).filter_by(send_to="session:admin:admin-session-a1").one()
    assert message.message_type == "status.update"
    assert message.status == "queued"
    assert message.subject == "JARVIS 运维简报"
    assert "sandbox_stuck" in message.payload_json
    assert message.idempotency_key.startswith("jarvis:")
    assert len(message.idempotency_key) <= 160
    db.close()
    engine.dispose()


def test_patrol_skips_when_no_anomalies(tmp_path, monkeypatch):
    engine, factory = _make_db(tmp_path, monkeypatch)
    db = factory()
    _seed(db)

    result = module.patrol_once()

    assert result == {"alerts": 0, "delivered": 0, "skipped": 0}
    assert db.query(AgentMeshMessage).count() == 0
    db.close()
    engine.dispose()


def test_patrol_is_idempotent_for_unchanged_anomalies(tmp_path, monkeypatch):
    engine, factory = _make_db(tmp_path, monkeypatch)
    db = factory()
    _seed(db)
    db.add(AgentAlert(
        alert_type="brute_force", severity="high", status="open",
        title="爆破尝试", detail_json="{}", category="brute_force", source="security_monitor",
    ))
    db.commit()

    assert module.patrol_once()["delivered"] == 1
    assert module.patrol_once()["delivered"] == 1
    assert db.query(AgentMeshMessage).count() == 1
    db.close()
    engine.dispose()


def test_failed_run_count_change_refreshes_brief(tmp_path, monkeypatch):
    engine, factory = _make_db(tmp_path, monkeypatch)
    db = factory()
    _seed(db)
    run_row = AgentResponseRun(
        run_id="run-failed-1", user_id=1, surface="admin",
        session_key="admin-session-a1", mesh_message_id="",
        status="failed", checkpoint_json="{}", version=1,
        update_time=datetime.now(timezone.utc),
    )
    db.add(run_row)
    db.commit()

    assert module.patrol_once()["delivered"] == 1
    first = db.query(AgentMeshMessage).filter(AgentMeshMessage.idempotency_key.like("jarvis:%")).one()
    first_key = first.idempotency_key

    # 同一小时再新增一次失败,计数变化应生成新简报,而不是被旧幂等键吞掉。
    db.add(AgentResponseRun(
        run_id="run-failed-2", user_id=1, surface="admin",
        session_key="admin-session-a1", mesh_message_id="",
        status="failed", checkpoint_json="{}", version=1,
        update_time=datetime.now(timezone.utc),
    ))
    db.commit()
    assert module.patrol_once()["delivered"] == 1
    keys = [row.idempotency_key for row in db.query(AgentMeshMessage).all()]
    assert len(set(keys)) == 2
    assert first_key != keys[-1]
    db.close()
    engine.dispose()


def test_patrol_skips_offline_admin_session(tmp_path, monkeypatch):
    engine, factory = _make_db(tmp_path, monkeypatch)
    db = factory()
    _seed(db, online=False)
    db.add(AgentAlert(
        alert_type="brute_force", severity="critical", status="open",
        title="爆破尝试", detail_json="{}", category="brute_force", source="security_monitor",
    ))
    db.commit()

    result = module.patrol_once()

    assert result["delivered"] == 0
    assert db.query(AgentMeshMessage).count() == 0
    db.close()
    engine.dispose()


def test_patrol_includes_stuck_sandbox_evidence(tmp_path, monkeypatch):
    """卡死沙箱(>2h 未终态)进入简报证据,jarvis 具备沙箱健康巡逻能力。"""
    engine, factory = _make_db(tmp_path, monkeypatch)
    db = factory()
    _seed(db, online=True)
    db.add(SandboxEnvironment(
        public_id="sbx_stuck_1", project_id=1, owner_id=1, agent_code="sandbox_deployer",
        purpose="deploy", language="python", test_mode="full", status="queued",
        runtime="runsc", image_ref="python@sha256:x", image_digest="sha256:x",
        source_sha256="x" * 64, resource_policy_json="{}", agent_config_json="{}",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        create_time=datetime.now(timezone.utc) - timedelta(hours=3),
    ))
    db.commit()
    stats = module.patrol_once()
    assert stats["alerts"] >= 1
    msg = db.query(AgentMeshMessage).filter(
        AgentMeshMessage.message_type == "status.update",
        AgentMeshMessage.subject == "JARVIS 运维简报",
    ).first()
    assert msg is not None
    import json as _json
    payload = _json.loads(msg.payload_json or "{}")
    kinds = [item["alert_type"] for item in payload.get("evidence", [])]
    assert "stuck_sandbox" in kinds
    # 处置手册随简报下发
    assert "auto_runbook" in payload and "stuck_sandbox" in payload["auto_runbook"]
    db.close()
    engine.dispose()
