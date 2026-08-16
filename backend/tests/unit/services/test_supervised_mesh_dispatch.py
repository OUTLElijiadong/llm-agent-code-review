"""监督式调度闭环后端测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.agent_mesh import AgentMeshConversation, AgentMeshMessage, AgentMeshMessageEvent
from app.schemas.agent_mesh import AgentMeshMessageIn
from app.services import agent_mesh_service


@pytest.fixture(autouse=True)
def _supervision_limit(monkeypatch):
    monkeypatch.setattr(agent_mesh_service.settings, "agent_mesh_supervision_max_rounds", 3)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    AgentMeshConversation.__table__.create(engine)
    AgentMeshMessage.__table__.create(engine)
    AgentMeshMessageEvent.__table__.create(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def user():
    return SimpleNamespace(id=7, role="user", username="owner")


def _register_session(db, user, session_key="session-a1"):
    agent_mesh_service.heartbeat(db, user, surface="user", session_key=session_key, title="来源会话")


def _message(
    *,
    idempotency_key,
    message_type="task.request",
    send_to="agent:monitor",
    sent_from="",
    subject="复核代码修改",
    context=None,
):
    return AgentMeshMessageIn.model_validate({
        "idempotency_key": idempotency_key,
        "trace_id": "trc_supervised_mesh",
        "sent_from": sent_from,
        "send_to": send_to,
        "message_type": message_type,
        "subject": subject,
        "payload": {"task": "验证修复"},
        "context": context or {"run_id": "run_supervised"},
        "delivery": {"requires_ack": True, "max_attempts": 3},
    })


@pytest.mark.parametrize(
    "context",
    [
        pytest.param(
            {"run_id": "run_supervised", "supervision_round": 0},
            id="round-zero",
        ),
        pytest.param(
            {
                "run_id": "run_supervised",
                "supervision_round": 4,
                "supervision_max_rounds": 3,
            },
            id="round-over-max",
        ),
        pytest.param(
            {
                "run_id": "run_supervised",
                "supervision_round": 2,
                "supervision_max_rounds": 3,
            },
            id="round-over-one-without-correlation",
        ),
        pytest.param(
            {
                "run_id": "run_supervised",
                "supervision_round": 4,
                "supervision_max_rounds": 999,
                "supervision_correlation_id": "msg_parent_result",
            },
            id="max-rounds-clamped-to-settings",
        ),
    ],
)
def test_send_rejects_invalid_supervision_envelope(db, user, context):
    _register_session(db, user)

    with pytest.raises(agent_mesh_service.AgentMeshError):
        agent_mesh_service.send_message(
            db,
            user,
            surface="user",
            session_key="session-a1",
            message=_message(idempotency_key="invalid-supervision", context=context),
        )

    assert db.query(AgentMeshMessage).count() == 0


@pytest.mark.parametrize(
    "context",
    [
        pytest.param(
            {
                "run_id": "run_supervised",
                "supervision_objective": "确认修复通过",
                "supervision_round": 1,
                "supervision_max_rounds": 3,
            },
            id="round-one-without-correlation",
        ),
        pytest.param(
            {
                "run_id": "run_supervised",
                "supervision_objective": "确认修复通过",
                "supervision_round": 2,
                "supervision_max_rounds": 3,
                "supervision_correlation_id": "msg_parent_result",
            },
            id="round-two-with-correlation",
        ),
    ],
)
def test_send_accepts_valid_supervision_envelope(db, user, context):
    _register_session(db, user)

    created = agent_mesh_service.send_message(
        db,
        user,
        surface="user",
        session_key="session-a1",
        message=_message(idempotency_key="valid-supervision", context=context),
    )

    assert created["status"] == "queued"
    stored = db.query(AgentMeshMessage).one()
    stored_context = json.loads(stored.context_json)
    for key, value in context.items():
        assert stored_context[key] == value


def test_complete_supervised_request_writes_supervision_metadata_to_result(db, user):
    _register_session(db, user)
    context = {
        "run_id": "run_supervised",
        "supervision_objective": "确认修复通过",
        "supervision_round": 1,
        "supervision_max_rounds": 3,
    }
    created = agent_mesh_service.send_message(
        db,
        user,
        surface="user",
        session_key="session-a1",
        message=_message(idempotency_key="dispatch-supervised", context=context),
    )
    claimed = agent_mesh_service.claim_dispatch_message(
        db, user, created["message_id"], target_address="agent:monitor"
    )

    completed = agent_mesh_service.complete_dispatch_message(
        db,
        user,
        created["message_id"],
        target_address="agent:monitor",
        target_name="监控服务 Agent",
        lease_token=claimed["lease_token"],
        success=True,
        summary={"status": "completed", "summary": "修复已通过"},
    )

    assert completed["status"] == "completed"
    reply = db.query(AgentMeshMessage).filter_by(
        correlation_id=created["message_id"], message_type="task.result"
    ).one()
    reply_context = json.loads(reply.context_json)
    assert reply_context["supervision_objective"] == "确认修复通过"
    assert reply_context["supervision_round"] == 1
    assert reply_context["supervision_max_rounds"] == 3
    assert reply_context["supervision_correlation_id"] == created["message_id"]
    assert reply_context["run_id"] == "run_supervised"
    assert created["subject"] in reply.subject


def test_complete_unsupervised_request_keeps_result_context_without_supervision(db, user):
    _register_session(db, user)
    created = agent_mesh_service.send_message(
        db,
        user,
        surface="user",
        session_key="session-a1",
        message=_message(idempotency_key="dispatch-unsupervised"),
    )
    claimed = agent_mesh_service.claim_dispatch_message(
        db, user, created["message_id"], target_address="agent:monitor"
    )

    agent_mesh_service.complete_dispatch_message(
        db,
        user,
        created["message_id"],
        target_address="agent:monitor",
        target_name="监控服务 Agent",
        lease_token=claimed["lease_token"],
        success=True,
        summary={"status": "completed", "summary": "已完成"},
    )

    reply = db.query(AgentMeshMessage).filter_by(
        correlation_id=created["message_id"], message_type="task.result"
    ).one()
    reply_context = json.loads(reply.context_json)
    assert reply_context["run_id"] == "run_supervised"
    for key in (
        "supervision_objective",
        "supervision_round",
        "supervision_max_rounds",
        "supervision_correlation_id",
    ):
        assert key not in reply_context


def test_prepare_message_run_appends_review_protocol_for_supervised_result(db, user):
    _register_session(db, user)
    objective = "确认修复通过并补齐测试"
    created = agent_mesh_service.send_message(
        db,
        user,
        surface="user",
        session_key="session-a1",
        trusted_source=True,
        message=_message(
            idempotency_key="supervised-result-inbox",
            message_type="task.result",
            send_to="session:user:session-a1",
            sent_from="agent:monitor",
            subject="监控服务 Agent回复：复核代码修改",
            context={
                "run_id": "run_supervised",
                "supervision_objective": objective,
                "supervision_round": 1,
                "supervision_max_rounds": 3,
                "supervision_correlation_id": "msg_original_request",
            },
        ),
    )

    _row, system_input = agent_mesh_service.prepare_message_run(
        db,
        user,
        created["message_id"],
        surface="user",
        session_key="session-a1",
    )
    content = system_input["content"]

    assert "结构化协作消息" in content
    assert "监督" in content
    assert "复核" in content
    assert "supervision_objective" in content
    assert objective in content
    assert "supervision_round" in content
    assert "supervision_max_rounds" in content
    assert "不得再派发" in content
    assert "不得伪装成用户发言" in content
