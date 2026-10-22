"""Agent Mesh 服务端消费者、租约与结果回传测试。"""

import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agents.base import AgentResult
from app.models.agent_mesh import AgentMeshConversation, AgentMeshMessage, AgentMeshMessageEvent
from app.models.rbac import Role, UserRole
from app.models.user import User
from app.schemas.agent_mesh import AgentMeshMessageIn
from app.services import agent_mesh_dispatcher, agent_mesh_service
from app.services.ai_usage_context import UsageAccountingError


def test_orchestrator_is_a_protected_session_only_target():
    assert agent_mesh_dispatcher.dispatch_state("agent:orchestrator") == "session_only"


@pytest.mark.parametrize("value", [-1, 10_000_001, True, "1", None])
def test_mesh_line_offset_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="line_offset"):
        agent_mesh_dispatcher._validated_line_offset(value)


@pytest.mark.parametrize("value", [0, 1, 10_000_000])
def test_mesh_line_offset_accepts_supported_values(value):
    assert agent_mesh_dispatcher._validated_line_offset(value) == value


@pytest.mark.parametrize(
    ("failure_kind", "expected_retryable"),
    [
        ("output_truncated", False),
        ("coverage_incomplete", False),
        ("invalid_response", False),
        ("invalid_json", False),
        ("incomplete_response", False),
        ("billing_error", False),
        ("usage_accounting_error", False),
        ("transport_error", True),
    ],
)
def test_builtin_review_mesh_failure_preserves_retry_policy(failure_kind, expected_retryable):
    result = AgentResult(
        success=False,
        error="审查覆盖不完整",
        failure_kind=failure_kind,
    )

    mesh_result = agent_mesh_dispatcher._as_mesh_result(result, action="代码审查")

    assert mesh_result["status"] == "failed"
    assert mesh_result["retryable"] is expected_retryable
    assert mesh_result["errors"][0]["code"] == failure_kind


def test_unclassified_review_failure_is_not_replayed_as_same_source():
    result = AgentResult(success=False, error="审查器未返回有效结果")

    mesh_result = agent_mesh_dispatcher._as_mesh_result(result, action="代码审查")

    assert mesh_result["status"] == "blocked"
    assert mesh_result["retryable"] is False


@pytest.mark.parametrize("operation", ["run_review", "start_roundtable"])
def test_static_review_operation_is_not_blocked_by_readonly_task_marker(operation):
    data = {
        "operation": operation,
        "execution_mode": "read_only",
        "instructions": "只读静态分析当前账号可见源码",
    }
    assert agent_mesh_dispatcher._readonly_team_task(data) is True
    assert agent_mesh_dispatcher._authorized_static_review_operation(data) is True


@pytest.mark.parametrize("target_field", ["remote_target_url", "target_url", "external_target_url"])
def test_static_review_operation_does_not_bypass_remote_target_guard(target_field):
    data = {
        "operation": "start_roundtable",
        "execution_mode": "read_only",
        target_field: "https://external.example/target",
    }
    assert agent_mesh_dispatcher._authorized_static_review_operation(data) is False


@pytest.mark.parametrize("data", [
    {"operation": "run_project_tests", "execution_mode": "read_only", "instructions": "只读静态分析"},
    {"operation": "restart_service", "execution_mode": "read_only", "instructions": "只读静态分析"},
])
def test_readonly_task_guard_remains_for_non_static_or_remote_operations(data):
    assert agent_mesh_dispatcher._readonly_team_task(data) is True
    assert agent_mesh_dispatcher._authorized_static_review_operation(data) is False


@pytest.mark.parametrize("trusted_team_execution", [True, False])
def test_readonly_roundtable_dispatch_requires_trusted_team_and_reaches_start_service(
    monkeypatch, trusted_team_execution
):
    from app.agents import orchestrator
    from app.services import agent_team_review

    calls = []
    monkeypatch.setattr(orchestrator, "get_request_orchestrator", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(agent_mesh_dispatcher, "_require_runtime_permissions", lambda *_a, **_k: None)
    monkeypatch.setattr(
        agent_team_review,
        "run_team_roundtable",
        lambda *_args, **kwargs: calls.append(kwargs) or {"status": "completed"},
    )
    message = {
        "user_id": 7,
        "trace_id": "readonly-roundtable-dispatch",
        "message_id": "readonly-roundtable-message",
        "payload": {
            "operation": "start_roundtable",
            "project_id": 13,
            "file_id": 512,
            "execution_mode": "read_only",
            "instructions": "只读静态分析当前账号可见源码",
        },
        "context": {
            "team_id": 41,
            "agent_team_task_id": 52,
            "lease_token": "server-issued-lease",
        },
    }

    result = agent_mesh_dispatcher._runtime_handler(
        object(), SimpleNamespace(id=7), "review_orchestrator", message,
        trusted_team_execution=trusted_team_execution,
    )

    if trusted_team_execution:
        assert result == {"status": "completed"}
        assert len(calls) == 1
        assert calls[0]["project_id"] == 13
        assert calls[0]["file_id"] == 512
        assert calls[0]["team_id"] == 41
        assert calls[0]["task_id"] == 52
        assert calls[0]["lease_token"] == "server-issued-lease"
    else:
        assert result["status"] == "approval_required"
        assert calls == []


@pytest.mark.parametrize("failure_kind", ["billing_error", "usage_accounting_error"])
def test_mesh_dispatch_prefers_canonical_nonretryable_error_over_unknown_top_level_kind(failure_kind):
    result = {
        "status": "failed",
        "failure_kind": "model_failure",
        "retryable": True,
        "errors": [{"code": failure_kind}],
    }

    assert agent_mesh_dispatcher._known_non_retryable_failure_kind(result) == failure_kind


def test_builtin_reviewer_receives_structured_retry_metadata_without_prompt_strategy(monkeypatch):
    from app.services import agent_mesh_dispatcher

    captured = {}

    class FakeOrchestrator:
        def review_code(self, code, rules, language, **kwargs):
            captured.update(code=code, rules=rules, language=language, **kwargs)
            return AgentResult(
                success=False,
                error="模型未返回最终内容(finish_reason=stop)",
                failure_kind="invalid_response",
                finish_reason="stop",
            )

    monkeypatch.setattr(agent_mesh_dispatcher, "_require_runtime_permissions", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "app.agents.orchestrator.get_request_orchestrator",
        lambda *_args, **_kwargs: FakeOrchestrator(),
    )
    code = "function readValue() { return input.value; }"
    result = agent_mesh_dispatcher._runtime_handler(
        object(),
        SimpleNamespace(id=7),
        "code_reviewer",
        {
            "user_id": 7,
            "trace_id": "review-retry-test",
            "message_id": "review-retry-message",
            "payload": {
                "code": code,
                "language": "javascript",
                "file_name": "sample.js",
                "_execution_strategy": {
                    "attempt": 2,
                    "instruction": "改成 4 段处理",
                },
            },
            "context": {"project_id": 14, "file_id": 597},
        },
    )

    assert captured["code"] == code
    assert captured["language"] == "javascript"
    assert captured["ctx"].extra["execution_strategy"]["attempt"] == 2
    assert "改成 4 段处理" not in captured["rules"]
    assert result["status"] == "failed"
    assert result["retryable"] is False
    assert result["errors"][0]["code"] == "invalid_response"


def _factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'mesh-dispatch.db'}",
        connect_args={"check_same_thread": False},
    )
    AgentMeshConversation.__table__.create(engine)
    AgentMeshMessage.__table__.create(engine)
    AgentMeshMessageEvent.__table__.create(engine)
    User.__table__.create(engine)
    Role.__table__.create(engine)
    UserRole.__table__.create(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _message(*, message_type="task.request", expires_at=None, max_attempts=3):
    return AgentMeshMessageIn.model_validate({
        "idempotency_key": f"agent-dispatch-{message_type}",
        "trace_id": "trc_agent_dispatch_001",
        "send_to": "agent:code_reviewer",
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
        db,
        user,
        surface="user",
        session_key="session-a1",
        trusted_source=True,
        message=message or _message(),
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
            db, user, created["message_id"], target_address="agent:code_reviewer"
        )
        assert claimed["status"] == "processing"
        assert claimed["lease_token"].startswith("lease_")

        completed = agent_mesh_service.complete_dispatch_message(
            db,
            user,
            created["message_id"],
            target_address="agent:code_reviewer",
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
                db, user, created["message_id"], target_address="agent:code_reviewer"
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
            db, user, created["message_id"], target_address="agent:code_reviewer"
        )
        row = db.query(AgentMeshMessage).filter_by(message_id=created["message_id"]).one()
        row.lease_token = "lease_new_worker"
        db.commit()
        with pytest.raises(agent_mesh_service.AgentMeshStateError, match="租约已失效"):
            agent_mesh_service.complete_dispatch_message(
                db,
                user,
                created["message_id"],
                target_address="agent:code_reviewer",
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
            db, user, created["message_id"], target_address="agent:code_reviewer"
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
                target_address="agent:code_reviewer",
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
            db, user, created["message_id"], target_address="agent:code_reviewer"
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
            db, user, status_message["message_id"], target_address="agent:code_reviewer"
        ) is None
    finally:
        db.close()

    expired = _message(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    expired.idempotency_key = "agent-dispatch-expired"
    user, expired_message = _seed(factory, expired)
    db = factory()
    try:
        assert agent_mesh_service.claim_dispatch_message(
            db, user, expired_message["message_id"], target_address="agent:code_reviewer"
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
        {"sent_from": "session:admin:session-a1", "payload": {"task": "最近一小时是否异常"}},
    )
    assert result["status"] == "needs_clarification"
    assert result["next_action"]["provide_fields"] == ["window_minutes", "metrics"]


def test_monitor_handler_rejects_user_surface_even_for_admin_account():
    user = SimpleNamespace(id=7, role="admin")
    result = agent_mesh_dispatcher._monitor_handler(
        SimpleNamespace(),
        user,
        {
            "sent_from": "session:user:member-session",
            "payload": {"window_minutes": 60, "metrics": ["latency_p95"]},
        },
    )
    assert result["status"] == "blocked"
    assert result["errors"][0]["code"] == "insufficient_scope"


def test_terminal_failure_returns_one_task_error(tmp_path):
    engine, factory = _factory(tmp_path)
    user, created = _seed(factory, _message(max_attempts=1))
    db = factory()
    try:
        claimed = agent_mesh_service.claim_dispatch_message(
            db, user, created["message_id"], target_address="agent:code_reviewer"
        )
        failed = agent_mesh_service.complete_dispatch_message(
            db,
            user,
            created["message_id"],
            target_address="agent:code_reviewer",
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


@pytest.mark.parametrize("failure_kind", ["billing_error", "usage_accounting_error"])
def test_non_retryable_failure_kind_is_preserved_and_dead_lettered_immediately(tmp_path, failure_kind):
    engine, factory = _factory(tmp_path)
    user, created = _seed(factory, _message(max_attempts=3))
    db = factory()
    try:
        claimed = agent_mesh_service.claim_dispatch_message(
            db, user, created["message_id"], target_address="agent:code_reviewer"
        )
        failed = agent_mesh_service.complete_dispatch_message(
            db,
            user,
            created["message_id"],
            target_address="agent:code_reviewer",
            target_name="代码审查 Agent",
            lease_token=claimed["lease_token"],
            success=False,
            summary={"status": "failed", "summary": "模型调用停止", "failure_kind": failure_kind},
            error="模型调用停止",
            failure_kind=failure_kind,
        )

        assert failed["status"] == "dead_letter"
        assert failed["attempt_count"] == 1
        assert failed["errors"][-1]["code"] == failure_kind
        failed_row = db.query(AgentMeshMessage).filter_by(message_id=created["message_id"]).one()
        assert failed_row.next_attempt_at is None
        reply = db.query(AgentMeshMessage).filter_by(
            correlation_id=created["message_id"], message_type="task.error"
        ).one()
        assert reply.payload_json.find(failure_kind) >= 0
        assert reply.errors_json.find(failure_kind) >= 0
        events = db.query(AgentMeshMessageEvent).filter_by(message_id=created["message_id"]).all()
        assert any(failure_kind in event.detail_json for event in events)
    finally:
        db.close()
        engine.dispose()


def test_transient_failure_still_requeues_before_max_attempts(tmp_path):
    engine, factory = _factory(tmp_path)
    user, created = _seed(factory, _message(max_attempts=3))
    db = factory()
    try:
        claimed = agent_mesh_service.claim_dispatch_message(
            db, user, created["message_id"], target_address="agent:code_reviewer"
        )
        failed = agent_mesh_service.complete_dispatch_message(
            db,
            user,
            created["message_id"],
            target_address="agent:code_reviewer",
            target_name="代码审查 Agent",
            lease_token=claimed["lease_token"],
            success=False,
            summary={"status": "failed", "summary": "暂时网络异常"},
            error="暂时网络异常",
        )
        assert failed["status"] == "queued"
        assert failed["attempt_count"] == 1
        row = db.query(AgentMeshMessage).filter_by(message_id=created["message_id"]).one()
        assert row.next_attempt_at is not None
    finally:
        db.close()
        engine.dispose()


@pytest.mark.parametrize("failure_kind", ["billing_error", "usage_accounting_error"])
def test_dispatch_once_keeps_non_retryable_handler_exception_without_requeue(
    tmp_path, monkeypatch, failure_kind
):
    engine, factory = _factory(tmp_path)
    db = factory()
    user = User(id=7, username="owner", password="unused", role="admin", status=1)
    db.add(user)
    db.commit()
    agent_mesh_service.heartbeat(db, user, surface="admin", session_key="session-a1", title="来源会话")
    created = agent_mesh_service.send_message(
        db,
        user,
        surface="admin",
        session_key="session-a1",
        trusted_source=True,
        message=_message(max_attempts=3),
    )
    db.close()

    monkeypatch.setattr(agent_mesh_dispatcher, "SessionLocal", factory)

    class TypedFailure(RuntimeError):
        pass

    def raise_failure(*_args):
        if failure_kind == "usage_accounting_error":
            raise UsageAccountingError("用量审计事务提交失败；不能重发模型请求")
        exc = TypedFailure("模型服务计费或额度拒绝")
        exc.failure_kind = failure_kind
        raise exc

    monkeypatch.setattr(agent_mesh_dispatcher, "_handle", raise_failure)
    stats = agent_mesh_dispatcher.dispatch_once(limit=1)

    assert stats["failed"] == 1
    verify = factory()
    try:
        row = verify.query(AgentMeshMessage).filter_by(message_id=created["message_id"]).one()
        assert row.status == "dead_letter"
        assert row.attempt_count == 1
        assert any(item["code"] == failure_kind for item in agent_mesh_service._load(row.errors_json, []))
        reply = verify.query(AgentMeshMessage).filter_by(
            correlation_id=created["message_id"], message_type="task.error"
        ).one()
        assert agent_mesh_service._load(reply.payload_json, {})["failure_kind"] == failure_kind
        assert agent_mesh_service._load(reply.errors_json, [])[0]["code"] == failure_kind
    finally:
        verify.close()
        engine.dispose()


def test_dispatch_once_still_requeues_unclassified_transient_exception(tmp_path, monkeypatch):
    engine, factory = _factory(tmp_path)
    db = factory()
    user = User(id=7, username="owner", password="unused", role="admin", status=1)
    db.add(user)
    db.commit()
    agent_mesh_service.heartbeat(db, user, surface="admin", session_key="session-a1", title="来源会话")
    request = _message(max_attempts=3)
    request.idempotency_key = "dispatch-transient-exception"
    created = agent_mesh_service.send_message(
        db, user, surface="admin", session_key="session-a1", trusted_source=True, message=request
    )
    db.close()

    monkeypatch.setattr(agent_mesh_dispatcher, "SessionLocal", factory)

    def raise_transient(*_args):
        raise RuntimeError("暂时网络异常")

    monkeypatch.setattr(agent_mesh_dispatcher, "_handle", raise_transient)
    stats = agent_mesh_dispatcher.dispatch_once(limit=1)

    assert stats["failed"] == 1
    verify = factory()
    try:
        row = verify.query(AgentMeshMessage).filter_by(message_id=created["message_id"]).one()
        assert row.status == "queued"
        assert row.attempt_count == 1
        assert row.next_attempt_at is not None
        assert verify.query(AgentMeshMessage).filter_by(
            correlation_id=created["message_id"], message_type="task.error"
        ).count() == 0
    finally:
        verify.close()
        engine.dispose()


def test_dispatch_once_requeues_returned_retryable_agent_failure(tmp_path, monkeypatch):
    engine, factory = _factory(tmp_path)
    db = factory()
    user = User(id=7, username="owner", password="unused", role="admin", status=1)
    db.add(user)
    db.commit()
    agent_mesh_service.heartbeat(db, user, surface="admin", session_key="session-a1", title="来源会话")
    request = _message(max_attempts=3)
    request.idempotency_key = "dispatch-returned-transient-result"
    created = agent_mesh_service.send_message(
        db, user, surface="admin", session_key="session-a1", trusted_source=True, message=request
    )
    db.close()

    monkeypatch.setattr(agent_mesh_dispatcher, "SessionLocal", factory)
    mesh_result = agent_mesh_dispatcher._as_mesh_result(
        AgentResult(success=False, error="上游暂时不可用", failure_kind="transport_error"),
        action="代码审查",
    )
    monkeypatch.setattr(agent_mesh_dispatcher, "_handle", lambda *_args: ("代码审查 Agent", mesh_result))

    stats = agent_mesh_dispatcher.dispatch_once(limit=1)

    assert stats["failed"] == 1
    verify = factory()
    try:
        row = verify.query(AgentMeshMessage).filter_by(message_id=created["message_id"]).one()
        assert row.status == "queued"
        assert row.attempt_count == 1
        assert row.next_attempt_at is not None
        assert verify.query(AgentMeshMessage).filter_by(
            correlation_id=created["message_id"], message_type="task.result"
        ).count() == 0
        assert verify.query(AgentMeshMessage).filter_by(
            correlation_id=created["message_id"], message_type="task.error"
        ).count() == 0
    finally:
        verify.close()
        engine.dispose()


def test_dispatch_once_dead_letters_returned_nonretryable_agent_failure(tmp_path, monkeypatch):
    engine, factory = _factory(tmp_path)
    db = factory()
    user = User(id=7, username="owner", password="unused", role="admin", status=1)
    db.add(user)
    db.commit()
    agent_mesh_service.heartbeat(db, user, surface="admin", session_key="session-a1", title="来源会话")
    request = _message(max_attempts=3)
    request.idempotency_key = "dispatch-returned-nonretryable-result"
    created = agent_mesh_service.send_message(
        db, user, surface="admin", session_key="session-a1", trusted_source=True, message=request
    )
    db.close()

    monkeypatch.setattr(agent_mesh_dispatcher, "SessionLocal", factory)
    mesh_result = agent_mesh_dispatcher._as_mesh_result(
        AgentResult(success=False, error="输出已按领域规则压缩但仍不完整", failure_kind="output_truncated"),
        action="代码审查",
    )
    monkeypatch.setattr(agent_mesh_dispatcher, "_handle", lambda *_args: ("代码审查 Agent", mesh_result))

    stats = agent_mesh_dispatcher.dispatch_once(limit=1)

    assert stats["failed"] == 1
    verify = factory()
    try:
        row = verify.query(AgentMeshMessage).filter_by(message_id=created["message_id"]).one()
        assert row.status == "dead_letter"
        assert row.attempt_count == 1
        assert verify.query(AgentMeshMessage).filter_by(
            correlation_id=created["message_id"], message_type="task.result"
        ).count() == 0
        reply = verify.query(AgentMeshMessage).filter_by(
            correlation_id=created["message_id"], message_type="task.error"
        ).one()
        assert agent_mesh_service._load(reply.payload_json, {})["status"] == "failed"
    finally:
        verify.close()
        engine.dispose()


@pytest.mark.parametrize("failure_kind", ["billing_error", "usage_accounting_error"])
def test_dispatch_once_dead_letters_explicit_non_retryable_agent_result(tmp_path, monkeypatch, failure_kind):
    engine, factory = _factory(tmp_path)
    db = factory()
    user = User(id=7, username="owner", password="unused", role="admin", status=1)
    db.add(user)
    db.commit()
    agent_mesh_service.heartbeat(db, user, surface="admin", session_key="session-a1", title="来源会话")
    request = _message(max_attempts=3)
    request.idempotency_key = f"dispatch-nonretryable-result-{failure_kind}"
    created = agent_mesh_service.send_message(
        db, user, surface="admin", session_key="session-a1", trusted_source=True, message=request
    )
    db.close()

    monkeypatch.setattr(agent_mesh_dispatcher, "SessionLocal", factory)
    monkeypatch.setattr(
        agent_mesh_dispatcher,
        "_handle",
        lambda *_args: ("代码审查 Agent", {
            "status": "failed",
            "summary": "模型调用停止",
            "failure_kind": failure_kind,
            "errors": [{"code": failure_kind}],
            "retryable": False,
        }),
    )
    stats = agent_mesh_dispatcher.dispatch_once(limit=1)

    assert stats["failed"] == 1
    verify = factory()
    try:
        row = verify.query(AgentMeshMessage).filter_by(message_id=created["message_id"]).one()
        assert row.status == "dead_letter"
        assert row.attempt_count == 1
        assert any(item["code"] == failure_kind for item in agent_mesh_service._load(row.errors_json, []))
        reply = verify.query(AgentMeshMessage).filter_by(
            correlation_id=created["message_id"], message_type="task.error"
        ).one()
        assert agent_mesh_service._load(reply.payload_json, {})["failure_kind"] == failure_kind
        assert agent_mesh_service._load(reply.errors_json, [])[0]["code"] == failure_kind
    finally:
        verify.close()
        engine.dispose()


def test_result_with_archived_source_is_dead_lettered_without_retry(tmp_path):
    engine, factory = _factory(tmp_path)
    user, created = _seed(factory)
    db = factory()
    try:
        claimed = agent_mesh_service.claim_dispatch_message(
            db, user, created["message_id"], target_address="agent:code_reviewer"
        )
        conversation = db.query(AgentMeshConversation).filter_by(session_key="session-a1").one()
        conversation.status = "archived"
        db.commit()

        completed = agent_mesh_service.complete_dispatch_message(
            db,
            user,
            created["message_id"],
            target_address="agent:code_reviewer",
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
        db, user, surface="admin", session_key="session-a1", trusted_source=True, message=request
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
        db,
        user,
        surface="admin",
        session_key="session-a1",
        trusted_source=True,
        message=_message(),
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
