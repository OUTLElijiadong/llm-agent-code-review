"""空会话归档与团队任务契约自动纠正的单元测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.agent_mesh import AgentMeshConversation, AgentMeshMessage
from app.models.agent_response_run import AgentResponseRun
from app.schemas.agent_team import AgentTeamCreateIn
from app.services import agent_mesh_service, agent_team_service


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    AgentMeshConversation.__table__.create(engine)
    AgentMeshMessage.__table__.create(engine)
    AgentResponseRun.__table__.create(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _conversation(db, *, user_id=7, surface="user", session_key="session-a1", age=timedelta(hours=25)):
    row = AgentMeshConversation(
        user_id=user_id,
        surface=surface,
        session_key=session_key,
        title="新对话",
        status="active",
        last_seen_at=datetime.now(timezone.utc) - age,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _message(db, *, session_key, sent_from, send_to):
    row = AgentMeshMessage(
        message_id=f"msg_{session_key}",
        user_id=7,
        schema_version="1.0",
        idempotency_key=f"idem_{session_key}",
        trace_id=f"trace_{session_key}",
        correlation_id="",
        causation_id="",
        sent_from=sent_from,
        send_to=send_to,
        message_type="coordination",
        priority="normal",
        subject="同步结果",
        payload_json="{}",
        context_json="{}",
        artifacts_json="[]",
        errors_json="[]",
        status="completed",
        requires_ack=1,
        max_attempts=3,
        attempt_count=0,
    )
    db.add(row)
    return row


def _run(db, *, session_key):
    row = AgentResponseRun(
        run_id=f"run_{session_key}",
        user_id=7,
        surface="user",
        session_key=session_key,
        mesh_message_id="",
        status="completed",
        checkpoint_json="{}",
        version=1,
    )
    db.add(row)
    return row


def test_archive_empty_conversations_archives_expired_empty_session(db):
    row = _conversation(db, age=timedelta(hours=25))

    result = agent_mesh_service.archive_empty_conversations(db)

    assert result == {"archived": 1}
    db.refresh(row)
    assert row.status == "archived"


def test_archive_empty_conversations_skips_conversations_with_message_or_run(db):
    message_source = _conversation(db, session_key="session-message-source", age=timedelta(hours=25))
    message_target = _conversation(db, session_key="session-message-target", age=timedelta(hours=25))
    run_session = _conversation(db, session_key="session-with-run", age=timedelta(hours=25))
    db.add(
        _message(
            db,
            session_key="session-message-source",
            sent_from="session:user:session-message-source",
            send_to="agent:review_orchestrator",
        )
    )
    db.add(
        _message(
            db,
            session_key="session-message-target",
            sent_from="agent:review_orchestrator",
            send_to="session:user:session-message-target",
        )
    )
    db.add(_run(db, session_key="session-with-run"))
    db.commit()

    result = agent_mesh_service.archive_empty_conversations(db)

    assert result == {"archived": 0}
    for row in (message_source, message_target, run_session):
        db.refresh(row)
        assert row.status == "active"


def test_archive_empty_conversations_skips_recent_empty_session(db):
    row = _conversation(db, age=timedelta(hours=23))

    result = agent_mesh_service.archive_empty_conversations(db)

    assert result == {"archived": 0}
    assert row.status == "active"


def _team(**overrides):
    value = {
        "surface": "user",
        "session_id": "session-a1",
        "title": "契约团队",
        "objective": "执行多 Agent 协作任务",
        "members": [],
        "tasks": [],
    }
    value.update(overrides)
    return AgentTeamCreateIn.model_validate(value)


def _member(key, address, role="worker"):
    return {"member_key": key, "display_name": key, "address": address, "role": role}


def _task(key, member, operation=None):
    input_value = {} if operation is None else {"operation": operation}
    return {
        "task_key": key,
        "member_key": member,
        "title": key,
        "instructions": "执行团队任务",
        "input": input_value,
    }


def test_normalize_task_inputs_fills_missing_operation():
    payload = _team(
        objective="执行多 Agent 项目分析与测试",
        members=[
            _member("ro", "agent:review_orchestrator"),
            _member("tv", "agent:test_verifier", "verifier"),
            _member("pa", "agent:project_analyzer"),
            _member("cfm", "agent:code_file_manager"),
            _member("db", "agent:dashboard"),
            _member("sec", "agent:security_sentinel"),
        ],
        tasks=[
            _task("ro", "ro"),
            _task("tv", "tv"),
            _task("pa", "pa"),
            _task("cfm", "cfm"),
            _task("db", "db"),
            _task("sec", "sec"),
        ],
    )

    normalized = agent_team_service._normalize_task_inputs(payload)
    by_key = {item.task_key: item.input for item in normalized.tasks}

    assert by_key["ro"]["operation"] == "list"
    assert by_key["tv"]["operation"] == "run_project_tests"
    assert by_key["pa"]["operation"] == "inspect_project"
    assert by_key["cfm"]["operation"] == "list"
    assert by_key["db"]["operation"] == "summary"
    assert "operation" not in by_key["sec"]


def test_normalize_task_inputs_forces_readonly_test_verifier_operation():
    payload = _team(
        objective="全程只读验收，严禁运行新测试",
        members=[_member("tv", "agent:test_verifier", "verifier")],
        tasks=[
            _task("tv", "tv", operation="run_project_tests"),
        ],
    )

    normalized = agent_team_service._normalize_task_inputs(payload)

    assert normalized.tasks[0].input["operation"] == "inspect_existing_results"


def test_normalize_task_inputs_replaces_invalid_review_orchestrator_operation():
    payload = _team(
        objective="执行审查任务生命周期管理",
        members=[_member("ro", "agent:review_orchestrator")],
        tasks=[_task("ro", "ro", operation="start_review")],
    )

    normalized = agent_team_service._normalize_task_inputs(payload)

    assert normalized.tasks[0].input["operation"] == "list"


def test_normalize_task_inputs_replaces_invalid_dashboard_operation():
    payload = _team(
        objective="生成看板指标",
        members=[_member("db", "agent:dashboard")],
        tasks=[_task("db", "db", operation="export_report")],
    )

    normalized = agent_team_service._normalize_task_inputs(payload)

    assert normalized.tasks[0].input["operation"] == "summary"


def test_archive_conversation_hides_session_and_is_idempotent(db):
    from app.models.user import User

    row = _conversation(db)
    user = User(id=7, role="user")

    result = agent_mesh_service.archive_conversation(
        db, user, surface="user", session_key="session-a1"
    )

    assert result == {"session_id": "session-a1", "status": "archived"}
    db.refresh(row)
    assert row.status == "archived"

    again = agent_mesh_service.archive_conversation(
        db, user, surface="user", session_key="session-a1"
    )
    assert again["status"] == "archived"

    missing = agent_mesh_service.archive_conversation(
        db, user, surface="user", session_key="session-missing"
    )
    assert missing["status"] == "archived"


def test_archive_conversation_rejects_occupied_run(db):
    from app.models.user import User

    _conversation(db, session_key="session-busy")
    row = _run(db, session_key="session-busy")
    row.status = "running"
    db.commit()
    user = User(id=7, role="user")

    with pytest.raises(agent_mesh_service.AgentMeshStateError):
        agent_mesh_service.archive_conversation(
            db, user, surface="user", session_key="session-busy"
        )


def test_heartbeat_does_not_revive_archived_session(db):
    from app.models.user import User

    row = _conversation(db, session_key="session-archived")
    user = User(id=7, role="user")
    agent_mesh_service.archive_conversation(
        db, user, surface="user", session_key="session-archived"
    )

    result = agent_mesh_service.heartbeat(
        db,
        user,
        surface="user",
        session_key="session-archived",
        title="不该复活",
    )

    db.refresh(row)
    assert row.status == "archived"
    assert row.title != "不该复活"
    assert result["status"] in {"online", "offline"}
