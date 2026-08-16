"""Agent Mesh 服务端消费者、租约与结果回传测试。"""

import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.agent_mesh import AgentMeshConversation, AgentMeshMessage, AgentMeshMessageEvent
from app.models.user import User
from app.schemas.agent_mesh import AgentMeshMessageIn
from app.services import agent_mesh_dispatcher, agent_mesh_service


def _factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'mesh-dispatch.db'}",
        connect_args={"check_same_thread": False},
    )
    AgentMeshConversation.__table__.create(engine)
    AgentMeshMessage.__table__.create(engine)
    AgentMeshMessageEvent.__table__.create(engine)
    User.__table__.create(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _message(*, message_type="task.request", expires_at=None, max_attempts=3):
    return AgentMeshMessageIn.model_validate({
        "idempotency_key": f"agent-dispatch-{message_type}",
        "trace_id": "trc_agent_dispatch_001",
        "send_to": "agent:monitor",
        "message_type": message_type,
        "subject": "查询运行异常",
        "payload": {"task": "查询最近一小时是否有异常指标"},
        "context": {"run_id": "run_dispatch_001"},
        "delivery": {"requires_ack": True, "max_attempts": max_attempts, "expires_at": expires_at},
    })


def _seed(factory, message=None):
    db = factory()
    user = SimpleNamespace(id=7, role="user", username="owner")
    agent_mesh_service.heartbeat(db, user, surface="user", session_key="session-a1", title="来源会话")
    created = agent_mesh_service.send_message(
        db, user, surface="user", session_key="session-a1", message=message or _message()
    )
    db.close()
    return user, created


def test_agent_message_claim_and_result_reply_are_traceable(tmp_path):
    engine, factory = _factory(tmp_path)
    user, created = _seed(factory)
    db = factory()
    try:
        assert created["status"] == "queued"
        claimed = agent_mesh_service.claim_dispatch_message(
            db, user, created["message_id"], target_address="agent:monitor"
        )
        assert claimed["status"] == "processing"
        assert claimed["lease_token"].startswith("lease_")

        completed = agent_mesh_service.complete_dispatch_message(
            db,
            user,
            created["message_id"],
            target_address="agent:monitor",
            target_name="监控服务 Agent",
            lease_token=claimed["lease_token"],
            success=True,
            summary={"status": "needs_clarification", "summary": "没有可用指标事实"},
        )
        assert completed["status"] == "completed"
        replies = db.query(AgentMeshMessage).filter(
            AgentMeshMessage.send_to == "session:user:session-a1",
            AgentMeshMessage.message_type == "task.result",
        ).all()
        assert len(replies) == 1
        assert replies[0].trace_id == created["trace_id"]
        assert replies[0].correlation_id == created["message_id"]
        assert replies[0].causation_id == created["message_id"]
        assert replies[0].status == "queued"
        inbox = agent_mesh_service.pull_inbox(
            db, user, surface="user", session_key="session-a1", limit=10
        )
        assert inbox[0]["status"] == "delivered"
    finally:
        db.close()
        engine.dispose()


def test_agent_message_claim_is_single_winner_across_sessions(tmp_path):
    engine, factory = _factory(tmp_path)
    user, created = _seed(factory)
    barrier = threading.Barrier(2)
    outcomes = []

    def worker():
        db = factory()
        try:
            barrier.wait()
            outcomes.append(agent_mesh_service.claim_dispatch_message(
                db, user, created["message_id"], target_address="agent:monitor"
            ))
        finally:
            db.close()

    threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(item is not None for item in outcomes) == 1
    engine.dispose()


def test_old_worker_cannot_complete_after_lease_changes(tmp_path):
    engine, factory = _factory(tmp_path)
    user, created = _seed(factory)
    db = factory()
    try:
        claimed = agent_mesh_service.claim_dispatch_message(
            db, user, created["message_id"], target_address="agent:monitor"
        )
        row = db.query(AgentMeshMessage).filter_by(message_id=created["message_id"]).one()
        row.lease_token = "lease_new_worker"
        db.commit()
        with pytest.raises(agent_mesh_service.AgentMeshStateError, match="租约已失效"):
            agent_mesh_service.complete_dispatch_message(
                db,
                user,
                created["message_id"],
                target_address="agent:monitor",
                target_name="监控服务 Agent",
                lease_token=claimed["lease_token"],
                success=True,
                summary={"status": "completed", "summary": "旧结果"},
            )
    finally:
        db.close()
        engine.dispose()


def test_expired_lease_is_requeued_and_old_worker_is_rejected(tmp_path):
    engine, factory = _factory(tmp_path)
    user, created = _seed(factory)
    db = factory()
    try:
        claimed = agent_mesh_service.claim_dispatch_message(
            db, user, created["message_id"], target_address="agent:monitor"
        )
        row = db.query(AgentMeshMessage).filter_by(message_id=created["message_id"]).one()
        row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

        assert agent_mesh_service.recover_stale_dispatch_messages(db) == 1
        db.refresh(row)
        assert row.status == "queued"
        assert row.lease_token is None
        with pytest.raises(agent_mesh_service.AgentMeshStateError, match="租约已失效"):
            agent_mesh_service.complete_dispatch_message(
                db,
                user,
                created["message_id"],
                target_address="agent:monitor",
                target_name="监控服务 Agent",
                lease_token=claimed["lease_token"],
                success=True,
                summary={"status": "completed", "summary": "迟到结果"},
            )
    finally:
        db.close()
        engine.dispose()


def test_expired_last_attempt_lease_becomes_dead_letter_with_error_reply(tmp_path):
    engine, factory = _factory(tmp_path)
    user, created = _seed(factory, _message(max_attempts=1))
    db = factory()
    try:
        db.add(User(id=7, username="owner", password="unused", role="user", status=1))
        db.commit()
        agent_mesh_service.claim_dispatch_message(
            db, user, created["message_id"], target_address="agent:monitor"
        )
        row = db.query(AgentMeshMessage).filter_by(message_id=created["message_id"]).one()
        row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

        assert agent_mesh_service.recover_stale_dispatch_messages(db) == 1
        db.refresh(row)
        assert row.status == "dead_letter"
        replies = db.query(AgentMeshMessage).filter_by(
            correlation_id=created["message_id"], message_type="task.error"
        ).all()
        assert len(replies) == 1
    finally:
        db.close()
        engine.dispose()


def test_non_request_and_expired_messages_are_not_claimed(tmp_path):
    engine, factory = _factory(tmp_path)
    user, status_message = _seed(factory, _message(message_type="status.update"))
    db = factory()
    try:
        assert agent_mesh_service.claim_dispatch_message(
            db, user, status_message["message_id"], target_address="agent:monitor"
        ) is None
    finally:
        db.close()

    expired = _message(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    expired.idempotency_key = "agent-dispatch-expired"
    user, expired_message = _seed(factory, expired)
    db = factory()
    try:
        assert agent_mesh_service.claim_dispatch_message(
            db, user, expired_message["message_id"], target_address="agent:monitor"
        ) is None
        assert agent_mesh_service.expire_unclaimed_dispatch_messages(db) == 1
        row = db.query(AgentMeshMessage).filter_by(message_id=expired_message["message_id"]).one()
        assert row.status == "expired"
    finally:
        db.close()
        engine.dispose()


def test_missing_monitor_fields_returns_business_result_without_querying_metrics():
    user = SimpleNamespace(id=7, role="admin")
    result = agent_mesh_dispatcher._monitor_handler(
        SimpleNamespace(),
        user,
        {"payload": {"task": "最近一小时是否异常"}},
    )
    assert result["status"] == "needs_clarification"
    assert result["next_action"]["provide_fields"] == ["window_minutes", "metrics"]


def test_terminal_failure_returns_one_task_error(tmp_path):
    engine, factory = _factory(tmp_path)
    user, created = _seed(factory, _message(max_attempts=1))
    db = factory()
    try:
        claimed = agent_mesh_service.claim_dispatch_message(
            db, user, created["message_id"], target_address="agent:monitor"
        )
        failed = agent_mesh_service.complete_dispatch_message(
            db,
            user,
            created["message_id"],
            target_address="agent:monitor",
            target_name="监控服务 Agent",
            lease_token=claimed["lease_token"],
            success=False,
            summary={"status": "failed", "summary": "upstream failed"},
            error="upstream failed",
        )
        assert failed["status"] == "dead_letter"
        replies = db.query(AgentMeshMessage).filter_by(
            correlation_id=created["message_id"], message_type="task.error"
        ).all()
        assert len(replies) == 1
    finally:
        db.close()
        engine.dispose()


def test_result_with_archived_source_is_dead_lettered_without_retry(tmp_path):
    engine, factory = _factory(tmp_path)
    user, created = _seed(factory)
    db = factory()
    try:
        claimed = agent_mesh_service.claim_dispatch_message(
            db, user, created["message_id"], target_address="agent:monitor"
        )
        conversation = db.query(AgentMeshConversation).filter_by(session_key="session-a1").one()
        conversation.status = "archived"
        db.commit()

        completed = agent_mesh_service.complete_dispatch_message(
            db,
            user,
            created["message_id"],
            target_address="agent:monitor",
            target_name="监控服务 Agent",
            lease_token=claimed["lease_token"],
            success=True,
            summary={"status": "completed", "summary": "执行已完成"},
        )

        assert completed["status"] == "dead_letter"
        assert "结果无法回传" in completed["last_error"]
        assert db.query(AgentMeshMessage).filter_by(correlation_id=created["message_id"]).count() == 0
    finally:
        db.close()
        engine.dispose()


def test_dispatch_once_consumes_request_and_returns_result(tmp_path, monkeypatch):
    engine, factory = _factory(tmp_path)
    db = factory()
    user = User(id=7, username="owner", password="unused", role="admin", status=1)
    db.add(user)
    db.commit()
    agent_mesh_service.heartbeat(db, user, surface="admin", session_key="session-a1", title="来源会话")
    request = _message()
    request.idempotency_key = "dispatch-once-e2e"
    request.payload = {"window_minutes": 60, "metrics": ["latency_p95"]}
    created = agent_mesh_service.send_message(
        db, user, surface="admin", session_key="session-a1", message=request
    )
    db.close()

    monkeypatch.setattr(agent_mesh_dispatcher, "SessionLocal", factory)
    monkeypatch.setattr(
        agent_mesh_dispatcher,
        "_handle",
        lambda _db, _user, _target, _message: (
            "监控服务 Agent",
            {"status": "completed", "summary": "已读取真实快照", "evidence": [{"id": 1}]},
        ),
    )

    stats = agent_mesh_dispatcher.dispatch_once(limit=1)
    assert stats == {
        "candidates": 1,
        "claimed": 1,
        "completed": 1,
        "failed": 0,
        "expired": 0,
        "recovered": 0,
    }
    verify = factory()
    try:
        original = verify.query(AgentMeshMessage).filter_by(message_id=created["message_id"]).one()
        reply = verify.query(AgentMeshMessage).filter_by(
            correlation_id=created["message_id"], message_type="task.result"
        ).one()
        assert original.status == "completed"
        assert reply.status == "queued"
        assert reply.trace_id == original.trace_id
    finally:
        verify.close()
        engine.dispose()


def test_dispatch_once_counts_undeliverable_result_as_failed(tmp_path, monkeypatch):
    engine, factory = _factory(tmp_path)
    db = factory()
    user = User(id=7, username="owner", password="unused", role="admin", status=1)
    db.add(user)
    db.commit()
    agent_mesh_service.heartbeat(db, user, surface="admin", session_key="session-a1", title="来源会话")
    created = agent_mesh_service.send_message(
        db, user, surface="admin", session_key="session-a1", message=_message()
    )
    user.role = "user"
    user.status = -1
    db.commit()
    db.close()

    monkeypatch.setattr(agent_mesh_dispatcher, "SessionLocal", factory)
    stats = agent_mesh_dispatcher.dispatch_once(limit=1)

    assert stats["completed"] == 0
    assert stats["failed"] == 1
    verify = factory()
    try:
        row = verify.query(AgentMeshMessage).filter_by(message_id=created["message_id"]).one()
        assert row.status == "dead_letter"
        assert "结果无法回传" in row.last_error
    finally:
        verify.close()
        engine.dispose()
