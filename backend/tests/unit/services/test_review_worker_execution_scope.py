"""正式审查原生线程及重启接续必须具备持久、可跨线程轮询的执行边界。"""

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.agent_team import AgentTeam, AgentTeamMember, AgentTeamTask
from app.models.code_file import CodeFile
from app.models.code_version import CodeVersion
from app.models.project import Project
from app.models.review_task import ReviewTask
from app.models.user import User
from app.schemas.review import ReviewStartIn
from app.services import agent_scheduler_runtime as runtime
from app.services import review_service
from app.services.ai_usage_context import usage_context
from app.services.review_input_service import freeze_task_inputs


@pytest.fixture
def rows(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'worker-scope.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(review_service, "SessionLocal", sessions)
    monkeypatch.setattr("app.services.rbac_service.check_permission", lambda *_a, **_k: True)
    monkeypatch.setattr(review_service, "_emit_review_event", Mock())
    monkeypatch.setattr(review_service, "get_enabled_rules", lambda *_a, **_k: [])
    monkeypatch.setattr(review_service, "DeepSeekAgent", lambda **_k: SimpleNamespace(model="isolated"))
    monkeypatch.setattr(review_service, "_enabled_review_profiles", lambda _db, profiles: profiles)
    monkeypatch.setattr("app.services.declarative_agent_runtime.DeclarativeReviewAgentFactory.snapshot_profiles",
                        lambda *_a, **_k: ())
    monkeypatch.setattr("app.services.experience_service.retrieve", lambda *_a, **_k: [])
    monkeypatch.setattr("app.services.personalization_service.build_review_context", lambda *_a, **_k: "")
    monkeypatch.setattr("app.services.agent_model_service.resolve_subagent_config", lambda *_a, **_k: None)
    monkeypatch.setattr("app.utils.api_resolver.resolve_api_config", lambda *_a, **_k: None)
    with sessions() as db:
        user = User(username="worker-scope", password="unused", role="admin", status=1)
        db.add(user)
        db.flush()
        project = Project(user_id=user.id, project_name="worker scope", status="active", language="python")
        db.add(project)
        db.flush()
        source = CodeFile(project_id=project.id, file_name="one.py", file_path="one.py", content="print(1)\n",
                          language="python", status="active", is_binary=0, version_no=1)
        team = AgentTeam(user_id=user.id, surface="user", session_key="worker-scope", title="正式审查",
                         objective="完成代码审查", status="running", trace_id="worker-scope")
        db.add_all([source, team])
        db.flush()
        db.add(CodeVersion(file_id=source.id, version_no=1, content=source.content,
                           create_time=datetime.now(timezone.utc)))
        member = AgentTeamMember(team_id=team.id, member_key="review", display_name="审查编排",
                                 address="agent:review_orchestrator", kind="runtime", role="worker", status="running")
        db.add(member)
        db.flush()
        team_task = AgentTeamTask(team_id=team.id, member_id=member.id, task_key="review", title="正式审查",
                                  instructions="审查项目", status="running", lease_token="team-token",
                                  lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                                  input_json=json.dumps({"operation": "run_review", "project_id": project.id,
                                                         "review_type": "full"}))
        db.add(team_task)
        db.flush()
        review = ReviewTask(user_id=user.id, project_id=project.id, status="running", review_type="full",
                            total_files=1, processed_files=0, execution_token="review-token", rules_snapshot=[],
                            agent_team_id=team.id, agent_team_task_id=team_task.id,
                            start_time=datetime.now(timezone.utc))
        db.add(review)
        db.flush()
        freeze_task_inputs(db, review.id, [source])
        db.commit()
        yield SimpleNamespace(sessions=sessions, db=db, user=user, project=project, source=source,
                              team=team, team_task=team_task, review=review, engine=engine)
    engine.dispose()


def test_native_review_thread_preserves_callers_execution_scope(rows, monkeypatch):
    observations = []
    threads = []
    native_thread = threading.Thread
    monkeypatch.setattr(review_service, "_run_review_task",
                        lambda *_a: observations.append(runtime.has_agent_execution_scope()))

    def spawn(**kwargs):
        thread = native_thread(**kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(review_service.threading, "Thread", spawn)
    with runtime.agent_execution_scope(lambda: None), usage_context(rows.user.id, {
        "agent_team_id": rows.team.id, "agent_team_task_id": rows.team_task.id,
    }, db=rows.db):
        review_service.start(rows.db, rows.user, ReviewStartIn(
            project_id=rows.project.id, file_ids=[rows.source.id], review_type="full",
        ))
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert observations == [True]


@pytest.mark.parametrize("entry", ["worker", "recovery"])
def test_worker_cancellation_cancels_inflight_model_and_prevents_followup(rows, monkeypatch, entry):
    observed = []
    calls = []
    cleaned = []

    def execute(*_args, **_kwargs):
        observed.append(runtime.has_agent_execution_scope())

        async def provider():
            calls.append("first")
            with rows.sessions() as cancellation:
                team = cancellation.get(AgentTeam, rows.team.id)
                review = cancellation.get(ReviewTask, rows.review.id)
                team.status = "cancelled"
                review.status = "cancelled"
                review.execution_token = "user-stopped"
                cancellation.commit()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.append(True)

        async def request():
            try:
                await asyncio.wait_for(runtime.await_agent_coroutine(provider()), timeout=1.5)
                calls.append("followup")
            except runtime.AgentExecutionCancelled:
                observed.append("cancelled")
                raise
            except asyncio.TimeoutError:
                observed.append("unguarded-timeout")

        asyncio.run(request())

    monkeypatch.setattr(review_service, "_execute_review", execute)
    if entry == "worker":
        review_service._run_review_task(rows.review.id, rows.user.id, "review-token")
    else:
        review_service._resume_interrupted_task(rows.review.id, rows.user.id, "review-token")
    assert observed == [True, "cancelled"]
    assert calls == ["first"] and cleaned == [True]
    dispatch = Mock()
    monkeypatch.setattr(review_service, "_run_review_task", dispatch)
    review_service._resume_interrupted_task(rows.review.id, rows.user.id, "user-stopped")
    dispatch.assert_not_called()
    with rows.sessions() as db:
        assert db.get(ReviewTask, rows.review.id).status == "cancelled"


@pytest.mark.parametrize("changed", [
    "cancelled", "expired", "foreign-owner", "wrong-project", "lease-expired", "deadline-expired",
    "wrong-member", "wrong-operation", "partial-binding", "wrong-review-owner",
])
def test_recovery_rejects_invalid_team_before_claim_or_dispatch(rows, monkeypatch, changed):
    with rows.sessions() as db:
        if changed in {"cancelled", "expired"}:
            db.get(AgentTeam, rows.team.id).status = changed
        elif changed == "foreign-owner":
            db.get(AgentTeam, rows.team.id).user_id = rows.user.id + 100
        elif changed == "wrong-project":
            task = db.get(AgentTeamTask, rows.team_task.id)
            task.input_json = json.dumps({"operation": "run_review", "project_id": rows.project.id + 100})
        elif changed == "lease-expired":
            task = db.get(AgentTeamTask, rows.team_task.id)
            task.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        elif changed == "deadline-expired":
            db.get(AgentTeam, rows.team.id).deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        elif changed == "wrong-member":
            db.get(AgentTeamMember, rows.team_task.member_id).address = "agent:code_reviewer"
        elif changed == "wrong-operation":
            db.get(AgentTeamTask, rows.team_task.id).input_json = json.dumps({
                "operation": "run_roundtable", "project_id": rows.project.id,
            })
        elif changed == "partial-binding":
            db.get(ReviewTask, rows.review.id).agent_team_task_id = None
        else:
            db.get(ReviewTask, rows.review.id).user_id = rows.user.id + 100
        db.commit()
    dispatch = Mock()
    monkeypatch.setattr(review_service, "_run_review_task", dispatch)
    review_service._resume_interrupted_task(rows.review.id, rows.user.id, "review-token")
    dispatch.assert_not_called()
    with rows.sessions() as db:
        stored = db.get(ReviewTask, rows.review.id)
        assert stored.execution_token == "review-token" and stored.status == "running"


@pytest.mark.parametrize("changed", ["lease", "member", "input"])
def test_worker_does_not_adopt_replaced_team_authorization(rows, monkeypatch, changed):
    calls = []

    def execute(*_args, authorization_check, **_kwargs):
        calls.append("first")
        with rows.sessions() as db:
            task = db.get(AgentTeamTask, rows.team_task.id)
            if changed == "lease":
                task.lease_token = "next-owner"
            elif changed == "member":
                member = AgentTeamMember(
                    team_id=rows.team.id, member_key="replacement", display_name="接管者",
                    address="agent:review_orchestrator", kind="runtime", role="worker", status="running",
                )
                db.add(member)
                db.flush()
                task.member_id = member.id
            else:
                task.input_json = json.dumps({
                    "operation": "run_review", "project_id": rows.project.id, "review_type": "full",
                    "file_ids": [rows.source.id],
                })
            db.commit()
        authorization_check()
        calls.append("followup")

    monkeypatch.setattr(review_service, "_execute_review", execute)
    review_service._run_review_task(rows.review.id, rows.user.id, "review-token")
    assert calls == ["first"]


def test_guard_detaches_from_callers_connection_transaction(rows, monkeypatch):
    with rows.engine.connect() as connection, connection.begin():
        sessions = sessionmaker(bind=connection)
        monkeypatch.setattr(review_service, "SessionLocal", sessions)
        review_service._assert_review_execution_authorized(rows.review.id, rows.user.id, "review-token")
        with rows.sessions() as db:
            db.get(AgentTeam, rows.team.id).status = "cancelled"
            db.commit()
        with pytest.raises(review_service.TaskSupersededError):
            review_service._assert_review_execution_authorized(rows.review.id, rows.user.id, "review-token")
        assert connection.in_transaction()


def test_worker_rejects_foreign_review_without_mutating_it(rows, monkeypatch):
    with rows.sessions() as db:
        db.get(ReviewTask, rows.review.id).user_id = rows.user.id + 100
        db.commit()
    execute = Mock()
    monkeypatch.setattr(review_service, "_execute_review", execute)
    review_service._run_review_task(rows.review.id, rows.user.id, "review-token")
    execute.assert_not_called()
    with rows.sessions() as db:
        assert db.get(ReviewTask, rows.review.id).status == "running"


def test_native_worker_cannot_adopt_lease_replaced_before_thread_starts(rows, monkeypatch):
    from app.services import agent_team_service

    release = threading.Event()
    threads = []
    native_thread = threading.Thread
    execute = Mock()
    monkeypatch.setattr(review_service, "_execute_review", execute)

    def spawn(**kwargs):
        target = kwargs.pop("target")
        args = kwargs.pop("args")

        def deferred():
            assert release.wait(timeout=3)
            target(*args)

        thread = native_thread(target=deferred, **kwargs)
        threads.append(thread)
        return thread

    def inherited_guard():
        with rows.sessions() as db:
            agent_team_service.require_active_task_lease(
                db, team_id=rows.team.id, task_id=rows.team_task.id,
                owner_user_id=rows.user.id, lease_token="team-token",
            )

    monkeypatch.setattr(review_service.threading, "Thread", spawn)
    with runtime.agent_execution_scope(inherited_guard), usage_context(rows.user.id, {
        "agent_team_id": rows.team.id, "agent_team_task_id": rows.team_task.id,
    }, db=rows.db):
        task = review_service.start(rows.db, rows.user, ReviewStartIn(
            project_id=rows.project.id, file_ids=[rows.source.id], review_type="full",
        ))
    with rows.sessions() as db:
        db.get(AgentTeamTask, rows.team_task.id).lease_token = "next-owner"
        db.commit()
    release.set()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    execute.assert_not_called()
    with rows.sessions() as db:
        assert db.get(ReviewTask, task.id).status == "running"


def test_recovery_cannot_adopt_lease_replaced_between_cas_and_dispatch(rows, monkeypatch):
    commit = review_service._safe_commit

    def replace_lease(db, task):
        commit(db, task)
        with rows.sessions() as writer:
            writer.get(AgentTeamTask, rows.team_task.id).lease_token = "next-owner"
            writer.commit()

    monkeypatch.setattr(review_service, "_safe_commit", replace_lease)
    dispatch = Mock()
    monkeypatch.setattr(review_service, "_run_review_task", dispatch)
    review_service._resume_interrupted_task(rows.review.id, rows.user.id, "review-token")
    dispatch.assert_not_called()
