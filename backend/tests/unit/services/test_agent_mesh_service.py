"""小菱 Agent Mesh 的会话发现、消息投递与追踪测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agents.contracts import CONTRACTS
from app.models.agent_mesh import AgentMeshConversation, AgentMeshMessage, AgentMeshMessageEvent
from app.schemas.agent_mesh import AgentMeshAckIn, AgentMeshMessageIn
from app.services import agent_mesh_service


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    AgentMeshConversation.__table__.create(engine)
    AgentMeshMessage.__table__.create(engine)
    AgentMeshMessageEvent.__table__.create(engine)
    from app.models.agent_response_run import AgentResponseRun

    AgentResponseRun.__table__.create(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def user():
    return SimpleNamespace(id=7, role="admin", username="owner")


def _message(**overrides) -> AgentMeshMessageIn:
    payload = {
        "schema_version": "1.0",
        "idempotency_key": "handoff-001",
        "trace_id": "trc_mesh_001",
        "correlation_id": "",
        "causation_id": "",
        "sent_from": "session:user:session-a1",
        "send_to": "session:user:session-b1",
        "message_type": "coordination",
        "priority": "normal",
        "subject": "同步页面异常结论",
        "payload": {"summary": "配置变更导致页面异常"},
        "context": {"project_id": 11, "run_id": "run-a"},
        "artifacts": [],
        "errors": [],
        "delivery": {"requires_ack": True, "max_attempts": 3},
    }
    payload.update(overrides)
    return AgentMeshMessageIn.model_validate(payload)


def test_message_schema_rejects_free_form_and_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        AgentMeshMessageIn.model_validate({"send_to": "session:user:session-b1", "text": "裸文本"})

    with pytest.raises(ValidationError):
        _message(message_type="unregistered.type")


def test_heartbeat_and_list_agents_include_all_contracts_and_same_user_sessions(db, user) -> None:
    agent_mesh_service.heartbeat(
        db,
        user,
        surface="user",
        session_key="session-a1",
        title="页面测试",
    )
    agent_mesh_service.heartbeat(
        db,
        user,
        surface="admin",
        session_key="session-admin1",
        title="运维管理",
    )
    other = SimpleNamespace(id=8, role="user", username="other")
    agent_mesh_service.heartbeat(
        db,
        other,
        surface="user",
        session_key="session-other",
        title="其他账户会话",
    )

    result = agent_mesh_service.list_agents(db, user)
    builtins = [item for item in result["items"] if item["kind"] in {"runtime", "service"}]
    sessions = [item for item in result["items"] if item["kind"] == "session"]

    assert {item["address"] for item in builtins} == {f"agent:{code}" for code in CONTRACTS}
    assert {item["address"] for item in sessions} == {
        "session:user:session-a1",
        "session:admin:session-admin1",
    }
    assert result["by_kind"]["runtime"] > 0
    assert result["by_kind"]["service"] > 0


def test_list_agents_marks_governed_runtime_agents_as_team_members(db, user) -> None:
    result = agent_mesh_service.list_agents(db, user)
    by_address = {item["address"]: item for item in result["items"]}
    for code in ("test_verifier", "sandbox_deployer", "operations"):
        item = by_address.get(f"agent:{code}")
        assert item is not None
        if item["dispatch_state"] == "approval_required":
            assert item.get("team_dispatch_state") == "team_governed"


def test_send_message_is_owner_scoped_idempotent_and_traceable(db, user) -> None:
    for key in ("session-a1", "session-b1"):
        agent_mesh_service.heartbeat(db, user, surface="user", session_key=key, title=key)

    created = agent_mesh_service.send_message(
        db,
        user,
        surface="user",
        session_key="session-a1",
        message=_message(),
    )
    repeated = agent_mesh_service.send_message(
        db,
        user,
        surface="user",
        session_key="session-a1",
        message=_message(),
    )

    assert created["message_id"] == repeated["message_id"]
    assert created["status"] == "queued"
    assert db.query(AgentMeshMessage).count() == 1
    trace = agent_mesh_service.get_trace(db, user, "trc_mesh_001")
    assert trace["messages"][0]["subject"] == "同步页面异常结论"
    assert [event["status"] for event in trace["messages"][0]["events"]] == ["queued"]


def test_non_task_message_to_agent_is_recorded_without_entering_dispatch_queue(db, user) -> None:
    agent_mesh_service.heartbeat(db, user, surface="admin", session_key="session-admin", title="管理会话")
    created = agent_mesh_service.send_message(
        db,
        user,
        surface="admin",
        session_key="session-admin",
        message=_message(
            idempotency_key="status-update-001",
            sent_from="session:admin:session-admin",
            send_to="agent:monitor",
            message_type="status.update",
            subject="已收到监控结果",
        ),
    )

    assert created["status"] == "completed"
    trace = agent_mesh_service.get_trace(db, user, created["trace_id"])
    assert [event["status"] for event in trace["messages"][0]["events"]] == ["queued", "completed"]


def test_send_message_rejects_spoofed_source_and_cross_user_target(db, user) -> None:
    agent_mesh_service.heartbeat(db, user, surface="user", session_key="session-a1", title="A")
    other = SimpleNamespace(id=8, role="user", username="other")
    agent_mesh_service.heartbeat(db, other, surface="user", session_key="session-b1", title="B")

    with pytest.raises(agent_mesh_service.AgentMeshAccessError):
        agent_mesh_service.send_message(
            db,
            user,
            surface="user",
            session_key="session-a1",
            message=_message(sent_from="session:admin:forged"),
        )

    with pytest.raises(agent_mesh_service.AgentMeshTargetError):
        agent_mesh_service.send_message(
            db,
            user,
            surface="user",
            session_key="session-a1",
            message=_message(),
        )


def test_inbox_delivery_ack_and_terminal_state_are_ordered(db, user) -> None:
    for key in ("session-a1", "session-b1"):
        agent_mesh_service.heartbeat(db, user, surface="user", session_key=key, title=key)
    created = agent_mesh_service.send_message(
        db,
        user,
        surface="user",
        session_key="session-a1",
        message=_message(),
    )

    inbox = agent_mesh_service.pull_inbox(
        db,
        user,
        surface="user",
        session_key="session-b1",
        limit=10,
    )
    assert [item["message_id"] for item in inbox] == [created["message_id"]]
    assert inbox[0]["status"] == "delivered"

    agent_mesh_service.ack_message(
        db,
        user,
        created["message_id"],
        surface="user",
        session_key="session-b1",
        acknowledgement=AgentMeshAckIn(status="processing"),
    )
    finished = agent_mesh_service.ack_message(
        db,
        user,
        created["message_id"],
        surface="user",
        session_key="session-b1",
        acknowledgement=AgentMeshAckIn(status="completed", summary="已调整数据校验范围"),
    )

    assert finished["status"] == "completed"
    trace = agent_mesh_service.get_trace(db, user, "trc_mesh_001")
    assert [event["status"] for event in trace["messages"][0]["events"]] == [
        "queued",
        "delivered",
        "processing",
        "completed",
    ]


def test_disabled_jarvis_message_is_completed_without_preparing_model_run(db, user, monkeypatch) -> None:
    for key in ("session-admin-a", "session-admin-b"):
        agent_mesh_service.heartbeat(db, user, surface="admin", session_key=key, title=key)
    created = agent_mesh_service.send_message(
        db,
        user,
        surface="admin",
        session_key="session-admin-a",
        message=_message(
            idempotency_key="jarvis-cost-guard-001",
            sent_from="session:admin:session-admin-a",
            send_to="session:admin:session-admin-b",
            message_type="status.update",
            subject="JARVIS 运维简报",
            payload={"patrol_kind": "jarvis", "evidence": []},
        ),
    )
    agent_mesh_service.pull_inbox(
        db,
        user,
        surface="admin",
        session_key="session-admin-b",
        limit=10,
    )
    monkeypatch.setattr(agent_mesh_service.settings, "agent_jarvis_auto_dispatch_enabled", False)

    with pytest.raises(agent_mesh_service.AgentMeshStateError, match="自动派发已关闭"):
        agent_mesh_service.prepare_message_run(
            db,
            user,
            created["message_id"],
            surface="admin",
            session_key="session-admin-b",
        )

    row = db.query(AgentMeshMessage).filter_by(message_id=created["message_id"]).one()
    assert row.status == "completed"


def test_expired_message_is_not_delivered(db, user) -> None:
    for key in ("session-a1", "session-b1"):
        agent_mesh_service.heartbeat(db, user, surface="user", session_key=key, title=key)
    expired = datetime.now(timezone.utc) - timedelta(seconds=1)
    agent_mesh_service.send_message(
        db,
        user,
        surface="user",
        session_key="session-a1",
        message=_message(
            idempotency_key="expired-001",
            delivery={"requires_ack": True, "max_attempts": 3, "expires_at": expired},
        ),
    )

    assert agent_mesh_service.pull_inbox(
        db,
        user,
        surface="user",
        session_key="session-b1",
        limit=10,
    ) == []
    trace = agent_mesh_service.get_trace(db, user, "trc_mesh_001")
    assert trace["messages"][0]["status"] == "expired"


def test_agent_collaboration_boundary_is_enforced(db, user) -> None:
    agent_mesh_service.heartbeat(db, user, surface="user", session_key="session-a1", title="A")
    with pytest.raises(agent_mesh_service.AgentMeshAccessError):
        agent_mesh_service.send_message(
            db,
            user,
            surface="user",
            session_key="session-a1",
            message=_message(
                sent_from="agent:language_detector",
                send_to="agent:operations",
                idempotency_key="bad-agent-route",
            ),
            trusted_source=True,
        )

    accepted = agent_mesh_service.send_message(
        db,
        user,
        surface="user",
        session_key="session-a1",
        message=_message(
            sent_from="agent:orchestrator",
            send_to="agent:code_reviewer",
            idempotency_key="good-agent-route",
        ),
        trusted_source=True,
    )
    assert accepted["status"] == "completed"


def test_list_agents_uses_authoritative_run_status_over_heartbeat() -> None:
    """ListAgents 必须用 agent_response_run 账本覆盖心跳快照,避免陈旧 busy。"""
    from app.models.agent_response_run import AgentResponseRun

    engine = create_engine("sqlite:///:memory:")
    AgentMeshConversation.__table__.create(engine)
    AgentMeshMessage.__table__.create(engine)
    AgentMeshMessageEvent.__table__.create(engine)
    AgentResponseRun.__table__.create(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    user = SimpleNamespace(id=7, role="user", username="owner")
    try:
        agent_mesh_service.heartbeat(
            db,
            user,
            surface="user",
            session_key="session-a1",
            title="旧会话",
            active_run_id="run-stale",
            active_run_status="running",
        )
        run = AgentResponseRun(
            run_id="run-done",
            user_id=7,
            surface="user",
            session_key="session-a1",
            mesh_message_id="",
            status="completed",
            checkpoint_json='{"run_id":"run-done","model":"deepseek-v4-pro","transcript":[],"tools":[],"status":"completed"}',
            version=1,
        )
        db.add(run)
        db.commit()

        listed = agent_mesh_service.list_agents(db, user)
        session_items = [item for item in listed["items"] if item["kind"] == "session"]
        assert session_items and session_items[0]["session_id"] == "session-a1"
        assert session_items[0]["active_run_id"] == "run-done"
        assert session_items[0]["active_run_status"] == "completed"
    finally:
        db.close()
        engine.dispose()
