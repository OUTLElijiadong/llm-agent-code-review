"""后台正式审查须在执行与每次模型请求前复核实时授权。"""
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.review_task import ReviewTask
from app.models.user import User
from app.services import review_service


@pytest.fixture
def review_worker_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'review-worker.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with sessions() as db:
        role = Role(name="审查员", code="reviewer", status="active", is_builtin=1)
        owner = User(username="review-worker-owner", password="isolated", role="user", status=1)
        user = User(username="review-worker-member", password="isolated", role="reviewer", status=1)
        db.add_all([role, owner, user])
        db.flush()
        permission = Permission(code="review:start", name="发起审查", module="review", type="api")
        project = Project(user_id=owner.id, project_name="撤权回归项目", status="active")
        db.add_all([permission, project])
        db.flush()
        db.add_all([
            UserRole(user_id=user.id, role_id=role.id),
            RolePermission(role_id=role.id, permission_id=permission.id),
            ProjectMember(project_id=project.id, user_id=user.id, role_in_project="reviewer"),
        ])
        task = ReviewTask(
            user_id=user.id, project_id=project.id, task_name="撤权回归",
            review_type="quick", status="running", execution_token="lease-review-worker",
            rules_snapshot=[], start_time=datetime.now(timezone.utc),
        )
        db.add(task)
        db.commit()
        yield sessions, user.id, project.id, role.id, permission.id, task.id
    engine.dispose()


@pytest.mark.parametrize("revoke_membership,revoke_permission", [
    (True, False), (False, True), (True, True),
])
def test_worker_blocks_before_execution_when_authorization_was_revoked(
    review_worker_db, monkeypatch, revoke_membership, revoke_permission,
):
    sessions, user_id, project_id, role_id, permission_id, task_id = review_worker_db
    with sessions() as db:
        if revoke_membership:
            db.query(ProjectMember).filter_by(project_id=project_id, user_id=user_id).delete()
        if revoke_permission:
            db.query(RolePermission).filter_by(role_id=role_id, permission_id=permission_id).delete()
        db.commit()

    model_factory = Mock()
    executor = Mock()
    monkeypatch.setattr(review_service, "SessionLocal", sessions)
    monkeypatch.setattr(review_service, "DeepSeekAgent", model_factory)
    monkeypatch.setattr(review_service, "_execute_review", executor)
    monkeypatch.setattr(review_service, "load_task_inputs", lambda *_args: [])
    monkeypatch.setattr(review_service, "_emit_review_event", Mock())

    review_service._run_review_task(task_id, user_id, "lease-review-worker")

    model_factory.assert_not_called()
    executor.assert_not_called()
    with sessions() as db:
        task = db.get(ReviewTask, task_id)
        assert task.status == "failed"
        assert task.coverage["reason"] == "authorization_revoked"
        if revoke_membership:
            assert "项目成员资格已撤销" in task.error_message
        else:
            assert "review:start 权限已撤销" in task.error_message


def test_worker_rechecks_authorization_after_queue_before_entering_executor(
    review_worker_db, monkeypatch,
):
    sessions, user_id, project_id, role_id, permission_id, task_id = review_worker_db
    original_check = review_service._assert_review_execution_authorized
    check_count = 0

    def revoke_before_executor(task_id_arg, user_id_arg, token):
        nonlocal check_count
        check_count += 1
        if check_count == 2:
            with sessions() as db:
                db.query(ProjectMember).filter_by(project_id=project_id, user_id=user_id).delete()
                db.commit()
        return original_check(task_id_arg, user_id_arg, token)

    def executor(*_args, authorization_check, **_kwargs):
        authorization_check()

    monkeypatch.setattr(review_service, "SessionLocal", sessions)
    monkeypatch.setattr(review_service, "_assert_review_execution_authorized", revoke_before_executor)
    monkeypatch.setattr(review_service, "_execute_review", executor)
    monkeypatch.setattr(review_service, "load_task_inputs", lambda *_args: [])
    monkeypatch.setattr(review_service, "_emit_review_event", Mock())
    monkeypatch.setattr(review_service, "_enabled_review_profiles", lambda _db, profiles: profiles)
    monkeypatch.setattr("app.services.declarative_agent_runtime.DeclarativeReviewAgentFactory.snapshot_profiles", lambda *_a, **_k: ())
    monkeypatch.setattr("app.services.experience_service.retrieve", lambda *_a, **_k: [])
    monkeypatch.setattr("app.services.personalization_service.build_review_context", lambda *_a, **_k: "")
    monkeypatch.setattr("app.services.agent_model_service.resolve_subagent_config", lambda _db, config, **_k: config)
    monkeypatch.setattr("app.utils.api_resolver.resolve_api_config", lambda *_a, **_k: object())
    monkeypatch.setattr(review_service, "DeepSeekAgent", lambda **_kwargs: object())

    review_service._run_review_task(task_id, user_id, "lease-review-worker")

    assert check_count == 2
    with sessions() as db:
        task = db.get(ReviewTask, task_id)
        assert task.status == "failed"
        assert task.coverage["reason"] == "authorization_revoked"


def test_base_agent_checks_authorization_before_any_provider_attempt(monkeypatch):
    from app.agents.base import AgentContext, BaseAgent
    from app.core.exceptions import ForbiddenError

    client = Mock()
    monkeypatch.setattr("app.agents.base.httpx.Client", client)
    gate = Mock(side_effect=ForbiddenError("授权已撤销"))
    agent = BaseAgent(model="unused", max_tokens=8)

    with pytest.raises(ForbiddenError, match="授权已撤销"):
        agent.call("只用于测试", ctx=AgentContext(extra={"before_model_call": gate}))

    gate.assert_called_once_with()
    client.assert_not_called()


def test_parallel_agent_rechecks_authorization_before_each_http_retry(monkeypatch):
    import httpx

    from app.ai.deepseek_agent import DeepSeekAgent
    from app.core.exceptions import ForbiddenError

    request = Mock(side_effect=httpx.ConnectError("isolated network failure"))
    monkeypatch.setattr(DeepSeekAgent, "_do_request", request)
    monkeypatch.setattr("app.ai.deepseek_agent.record_usage_attempt", lambda **_kwargs: None)
    monkeypatch.setattr("app.ai.deepseek_agent.time.sleep", lambda *_args: None)
    gate_calls = 0

    def revoke_on_retry():
        nonlocal gate_calls
        gate_calls += 1
        if gate_calls == 2:
            raise ForbiddenError("授权已撤销")

    agent = DeepSeekAgent(
        base_url="https://api.deepseek.com", api_key="isolated", model="unit",
        max_retries=2,
    )
    with pytest.raises(ForbiddenError, match="授权已撤销"):
        agent.call_raw("system", "user", before_request=revoke_on_retry)

    assert gate_calls == 2
    request.assert_called_once()
