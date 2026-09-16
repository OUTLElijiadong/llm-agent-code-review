"""真实报告创建、后台恢复和用量事务边界，不依赖生产数据库。"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.agent_capability import SandboxEnvironment
from app.models.agent_response_run import AgentResponseRun
from app.models.ai_call_log import AiCallLog
from app.models.code_file import CodeFile
from app.models.code_version import CodeVersion
from app.models.review_task import ReviewTask
from app.services.ai_usage_context import current_attribution, model_attribution, record_usage_attempt, usage_context


def _origin(db):
    root = AgentResponseRun(
        user_id=7,
        run_id="background-origin",
        surface="user",
        session_key="unit",
        status="completed",
        checkpoint_json="{}",
    )
    db.add(root)
    db.commit()
    return {"root_agent_run_id": root.id, "agent_run_id": root.id}


@pytest.mark.parametrize("source", ["pentest", "sandbox"])
def test_published_report_inherits_persisted_origin_and_rerun_replaces_scope(db, source):
    from app.services.pentest_service import _publish_pentest_report
    from app.services.sandbox_service import _publish_sandbox_report

    origin = _origin(db)
    row = SimpleNamespace(
        user_id=7,
        owner_id=7,
        project_id=1,
        public_id="unit-publish",
        target_type="web",
        started_at=None,
        completed_at=None,
        rules_version="unit",
        window_start=None,
        window_end=None,
        **origin,
    )

    def publish():
        if source == "pentest":
            return _publish_pentest_report(db, row, [], "unit", {}, {}, 100, {}, {}).id
        return _publish_sandbox_report(db, row, {"passed": True}, "unit")["report_task_id"]

    # 发布时的另一个上下文不得替代领域对象保存的来源。
    with usage_context(8, {"root_agent_run_id": 999}):
        task_id = publish()
    assert model_attribution(db.get(ReviewTask, task_id)) == origin
    row.root_agent_run_id = None
    row.agent_run_id = None
    assert publish() == task_id
    assert model_attribution(db.get(ReviewTask, task_id)) == {}
    assert db.query(ReviewTask).count() == 1


def test_discussion_task_uses_captured_origin_after_thread_context_is_gone(db, monkeypatch):
    from app.ai import discussion_orchestrator as discussion

    origin = _origin(db)
    db.add(
        CodeFile(
            id=5,
            project_id=4,
            file_name="unit.py",
            file_path="unit.py",
            language="python",
            content="x=1",
            version_no=1,
            is_binary=0,
            status="active",
        )
    )
    db.add(CodeVersion(file_id=5, version_no=1, content="x=1", create_time=datetime.now()))
    db.commit()
    monkeypatch.setattr(discussion, "SessionLocal", lambda: db)
    assert current_attribution(7) == {}
    task_id = discussion._create_review_task(
        user_id=7,
        project_id=4,
        file_id=5,
        file_name="unit.py",
        code="x=1",
        language="python",
        review_type="full",
        model_name="unit",
        profiles=(),
        usage_origin=origin,
    )
    assert model_attribution(db.get(ReviewTask, task_id)) == origin


def test_discussion_model_failure_keeps_usage_after_report_rollback(db, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    from app.ai import discussion_orchestrator as discussion

    origin = _origin(db)
    task = ReviewTask(user_id=7, project_id=1, task_name="unit", review_type="discuss", status="running", **origin)
    db.add(task)
    db.commit()
    task_id = task.id
    monkeypatch.setattr(discussion, "SessionLocal", sessionmaker(bind=db.get_bind()))

    def fail_call(**_kwargs):
        assert current_attribution(7) == origin
        record_usage_attempt(
            model_name="unit",
            agent_label="general",
            usage={"prompt_tokens": 0, "completion_tokens": 8, "total_tokens": 8},
            status="failed",
        )
        raise ValueError("invalid JSON after real token use")

    with pytest.raises(ValueError, match="invalid JSON"):
        discussion._call_raw_for_task(SimpleNamespace(call_raw=fail_call), task_id, 7, system_prompt="unit")
    db.rollback()
    log = db.query(AiCallLog).one()
    assert (log.root_agent_run_id, log.total_tokens, log.prompt_tokens) == (origin["root_agent_run_id"], 8, 0)
    assert log.task_id == task_id
    assert current_attribution(7) == {}


def test_current_execution_reviewing_old_task_uses_new_root(db):
    old_origin = _origin(db)
    target = ReviewTask(
        user_id=7, project_id=1, task_name="historical-target", review_type="full", status="success", **old_origin
    )
    new_root = AgentResponseRun(
        user_id=7, run_id="new-inspection", surface="user", session_key="new", status="completed", checkpoint_json="{}"
    )
    db.add_all([target, new_root])
    db.commit()
    new_origin = {"root_agent_run_id": new_root.id, "agent_run_id": new_root.id}
    with usage_context(7, new_origin, db=db):
        record_usage_attempt(
            model_name="unit",
            agent_label="security_sentinel",
            task_id=target.id,
            usage={"total_tokens": 9},
            status="success",
        )
    log = db.query(AiCallLog).one()
    assert log.task_id == target.id
    assert model_attribution(log) == new_origin
    assert model_attribution(target) == old_origin


def test_explicit_system_database_call_records_usage_without_inventing_owner(db):
    log_id = record_usage_attempt(
        db=db, user_id=None, model_name="unit", agent_label="eval_gate", usage={"total_tokens": 7}, status="success"
    )
    assert log_id is not None
    log = db.get(AiCallLog, log_id)
    assert log.user_id is None
    assert model_attribution(log) == {}
    assert log.total_tokens == 7


async def test_asgi_reentry_preserves_scope_through_auth_and_sync_model_endpoint(monkeypatch):
    from unittest.mock import MagicMock

    import httpx
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.agents.base import AgentResult
    from app.agents.security_sentinel_agent import SecuritySentinelAgent
    from app.api.v1 import security as api
    from app.core.database import Base, get_db
    from app.main import app
    from app.models.agent_response_run import AgentToolExecution
    from app.models.user import User
    from app.services.admin_capability_registry import AdminCapabilitySpec
    from app.services.admin_capability_service import execute_api

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    actor = User(id=7, username="asgi-account", password="unit", role="admin", status=1)
    session.add(actor)
    session.commit()
    origin = _origin(session)
    tool = AgentToolExecution(
        request_id="asgi-tool",
        run_id="background-origin",
        call_id="asgi-call",
        user_id=7,
        tool_name="security_scan_file",
        status="executing",
        arguments_json="{}",
    )
    session.add(tool)
    session.commit()
    origin["tool_execution_id"] = tool.id
    sentinel = SecuritySentinelAgent()
    sentinel._api_key, sentinel._max_retries = "unit", 0
    sentinel.bind_usage_source(session, actor)
    monkeypatch.setattr(sentinel, "_emit", lambda *_a, **_k: None)

    def scan_file(**kwargs):
        # 同步API端点运行在 AnyIO worker；使用真实 BaseAgent LLM 入口。
        assert current_attribution(7) == origin
        result = sentinel.call_json("unit", ctx=kwargs["ctx"])
        return AgentResult(success=result.success, data=result.data, error=result.error)

    monkeypatch.setattr(sentinel, "scan_file", scan_file)
    monkeypatch.setattr(api, "get_request_orchestrator", lambda *_a, **_k: SimpleNamespace(security_sentinel=sentinel))
    response = httpx.Response(
        200,
        json={
            "choices": [{"finish_reason": "stop", "message": {"content": '{"findings":[]}'}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
        },
    )
    client = MagicMock()
    client.__enter__.return_value = client
    client.post.return_value = response
    monkeypatch.setattr("app.agents.base.httpx.Client", lambda **_kwargs: client)
    monkeypatch.setattr(
        "app.agents.base.pin_public_http_url",
        lambda _url: SimpleNamespace(
            request_url="https://unit.invalid/chat", host_header="unit.invalid", request_extensions={}
        ),
    )

    def local_db():
        yield session

    overrides = dict(app.dependency_overrides)
    app.dependency_overrides.clear()
    app.dependency_overrides[get_db] = local_db
    try:
        spec = AdminCapabilitySpec("unit.security", "unit", "unit", "POST", "/api/security/scan-file", risk="read")
        with usage_context(7, origin, db=session):
            await execute_api(actor, spec, {"file_id": 1, "scan_depth": "standard"}, request_id="usage-asgi-unit")
        assert client.post.call_count == 1
        log = session.query(AiCallLog).one()
        assert model_attribution(log) == origin
        assert (log.user_id, log.total_tokens) == (7, 7)
        assert current_attribution(7) == {}
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)
        session.close()
        engine.dispose()


def test_sandbox_worker_recovers_persisted_origin_and_resets_context(db, monkeypatch):
    from app.services import sandbox_service as sandbox

    origin = _origin(db)
    row = SandboxEnvironment(
        public_id="unit-sandbox",
        project_id=1,
        owner_id=7,
        agent_code="test_verifier",
        purpose="test",
        language="python",
        test_mode="whitebox",
        status="recovering",
        runtime="remote_http",
        image_ref="unit",
        source_sha256="a" * 64,
        resource_policy_json="{}",
        agent_config_json='{"remote_only":true}',
        expires_at=datetime.now() + timedelta(hours=1),
        **origin,
    )
    db.add(row)
    db.commit()
    row_id = row.id
    monkeypatch.setattr(sandbox, "SessionLocal", lambda: db)
    monkeypatch.setattr(sandbox, "_append_event", lambda *_a, **_k: None)
    monkeypatch.setattr(sandbox, "_commit_execution", lambda *_a, **_k: None)

    class StopProbe(BaseException):
        pass

    def stop_after_restore(*_args, **_kwargs):
        assert current_attribution(7) == origin
        raise StopProbe()

    monkeypatch.setattr(sandbox, "_emit", stop_after_restore)
    with pytest.raises(StopProbe):
        sandbox._execute_environment(row_id, source_archive_base64="unit")
    assert current_attribution(7) == {}
