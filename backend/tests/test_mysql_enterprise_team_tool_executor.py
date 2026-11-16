"""Real MySQL tests for enterprise assistant team control tools."""
import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
import test_fresh_database_chain as chain
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker
from test_mysql_enterprise_group_shared_lock import _install_live_snapshots, _seed

from app.models.enterprise_agent_team import EnterpriseAgentTeam, EnterpriseAgentTeamTask
from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun
from app.models.user import User
from app.services import enterprise_assistant_runtime_service as runtime
from app.services.deepseek_responses_runtime import RunCheckpoint, ToolCall

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


@pytest.mark.parametrize("tool", ["cancel_enterprise_agent_team", "retry_enterprise_agent_team_task"])
def test_team_tool_control_uses_fresh_transaction_without_committing_caller(fresh_chain, monkeypatch, tool):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, _group, _owner, users, tasks, teams = _seed(
        factory, ordinary_message_indexes={1}, first_user_enterprise_role="admin",
    )
    _install_live_snapshots(factory, company, teams, legacy=True)
    monkeypatch.setattr(runtime.assistant_service, "_require_active_limit", lambda *_a, **_kw: None)
    monkeypatch.setattr(runtime.assistant_service, "_require_subscription", lambda *_a, **_kw: None)
    monkeypatch.setattr(runtime.enterprise_org_service, "require_enterprise_execution", lambda *_a, **_kw: None)
    with factory.begin() as db:
        team = db.get(EnterpriseAgentTeam, teams[0])
        run = db.get(EnterpriseAssistantRun, team.assistant_run_id)
        state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).one()
        run.status = state.status = "running"
        state.lease_expires_at = runtime._now() + timedelta(minutes=5)
        state.checkpoint_json = runtime._json(RunCheckpoint(
            run_id=run.public_id, model="test", transcript=[], tools=[], status="running",
        ).to_dict())
        db.get(EnterpriseAgentTeamTask, tasks[0]).status = "retryable"
        conversation_id, run_public_id = int(run.conversation_id), str(run.public_id)
    def set_timeout(connection, *_args):
        cursor = connection.cursor()
        try:
            cursor.execute("SET SESSION innodb_lock_wait_timeout = 2")
        finally:
            cursor.close()
    event.listen(fresh_chain, "checkout", set_timeout)
    username = f"caller-uncommitted-{uuid4().hex[:12]}"
    try:
        with factory() as db:
            db.add(User(username=username, password="x", role="user", status=1))
            db.flush()
            executor = runtime.EnterpriseAssistantToolExecutor(
                db, enterprise_id=company, conversation_id=conversation_id,
                run_public_id=run_public_id, lease_generation=1, actor_user_id=users[0],
            )
            args = {"team_id": teams[0]}
            if tool.startswith("retry"):
                args["task_id"] = tasks[0]
            result = asyncio.run(executor.execute(ToolCall(
                call_id=f"test-{tool}", name=tool, arguments=args, raw_arguments="{}",
            )))
            assert result.status == "success", result.error
            with factory() as observer:
                assert observer.query(User).filter_by(username=username).count() == 0
            db.rollback()
        with factory() as observer:
            assert observer.query(User).filter_by(username=username).count() == 0
    finally:
        event.remove(fresh_chain, "checkout", set_timeout)


@pytest.mark.parametrize("condition", ["generation_changed", "clarification_changed", "token_expired"])
def test_team_retry_rechecks_current_authority_and_clarification(fresh_chain, monkeypatch, condition):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, _group, _owner, users, tasks, teams = _seed(
        factory, ordinary_message_indexes={1}, first_user_enterprise_role="admin",
    )
    _install_live_snapshots(factory, company, teams, legacy=True)
    monkeypatch.setattr(runtime.assistant_service, "_require_active_limit", lambda *_a, **_kw: None)
    monkeypatch.setattr(runtime.assistant_service, "_require_subscription", lambda *_a, **_kw: None)
    monkeypatch.setattr(runtime.enterprise_org_service, "require_enterprise_execution", lambda *_a, **_kw: None)
    with factory.begin() as db:
        team = db.get(EnterpriseAgentTeam, teams[0])
        run = db.get(EnterpriseAssistantRun, team.assistant_run_id)
        state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).one()
        run.status = state.status = "running"
        state.lease_expires_at = runtime._now() + timedelta(minutes=5)
        state.checkpoint_json = runtime._json(RunCheckpoint(
            run_id=run.public_id, model="test", transcript=[], tools=[], status="running",
        ).to_dict())
        db.get(EnterpriseAgentTeamTask, tasks[0]).status = "retryable"
        conversation_id, public_id = int(run.conversation_id), str(run.public_id)
        if condition == "generation_changed":
            state.lease_generation += 1
    username = f"uncommitted-denied-{uuid4().hex[:12]}"
    with factory() as db:
        db.add(User(username=username, password="x", role="user", status=1))
        db.flush()
        executor = runtime.EnterpriseAssistantToolExecutor(
            db, enterprise_id=company, conversation_id=conversation_id,
            run_public_id=public_id, lease_generation=1, actor_user_id=users[0],
        )
        if condition == "clarification_changed":
            executor._clarification_request = (999999999, 1)
        elif condition == "token_expired":
            executor._clarification_token = (0, 1)
        result = asyncio.run(executor.execute(ToolCall(
            call_id=f"denied-{condition}", name="retry_enterprise_agent_team_task",
            arguments={"team_id": teams[0], "task_id": tasks[0]}, raw_arguments="{}",
        )))
        assert result.status == "error"
        assert db.query(User).filter_by(username=username).count() == 1
        db.rollback()
    with factory() as db:
        assert db.get(EnterpriseAgentTeamTask, tasks[0]).status == "retryable"
        assert db.get(EnterpriseAgentTeam, teams[0]).status == "queued"
        assert db.query(User).filter_by(username=username).count() == 0


def test_team_cancel_rechecks_member_after_concurrent_revocation(fresh_chain, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from app.models.enterprise_org import EnterpriseMember
    from app.services import enterprise_agent_team_runtime_service as team_runtime

    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, _group, _owner, users, tasks, teams = _seed(
        factory, ordinary_message_indexes={1}, first_user_enterprise_role="admin",
    )
    _install_live_snapshots(factory, company, teams, legacy=True)
    monkeypatch.setattr(runtime.assistant_service, "_require_active_limit", lambda *_a, **_kw: None)
    monkeypatch.setattr(runtime.enterprise_org_service, "require_enterprise_execution", lambda *_a, **_kw: None)
    with factory.begin() as db:
        team = db.get(EnterpriseAgentTeam, teams[0])
        run = db.get(EnterpriseAssistantRun, team.assistant_run_id)
        state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).one()
        run.status = state.status = "running"
        state.lease_expires_at = runtime._now() + timedelta(minutes=5)
        state.checkpoint_json = runtime._json(RunCheckpoint(
            run_id=run.public_id, model="test", transcript=[], tools=[], status="running",
        ).to_dict())
        conversation_id, public_id = int(run.conversation_id), str(run.public_id)
    reached, release = Event(), Event()
    original = team_runtime._lock_assistant_control_runs

    def pause_before_lock(*args, **kwargs):
        reached.set()
        if not release.wait(10):
            raise TimeoutError("revocation barrier timed out")
        return original(*args, **kwargs)

    monkeypatch.setattr(team_runtime, "_lock_assistant_control_runs", pause_before_lock)

    def control():
        with factory() as db:
            executor = runtime.EnterpriseAssistantToolExecutor(
                db, enterprise_id=company, conversation_id=conversation_id,
                run_public_id=public_id, lease_generation=1, actor_user_id=users[0],
            )
            return asyncio.run(executor.execute(ToolCall(
                call_id="revoked", name="cancel_enterprise_agent_team",
                arguments={"team_id": teams[0]}, raw_arguments="{}",
            )))

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(control)
            assert reached.wait(10)
            with factory.begin() as db:
                db.query(EnterpriseMember).filter_by(enterprise_id=company, user_id=users[0]).one().status = "removed"
            release.set()
            assert future.result(timeout=10).status == "error"
        with factory() as db:
            assert db.get(EnterpriseAgentTeam, teams[0]).status == "queued"
            assert db.get(EnterpriseAgentTeamTask, tasks[0]).status == "queued"
    finally:
        release.set()


def test_team_retry_commits_real_clarification_without_changing_task(fresh_chain, monkeypatch):
    """A higher-authority prior instruction creates a durable gate in the action transaction."""
    from app.models.enterprise_assistant_clarification import EnterpriseAssistantClarification

    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, _group, _owner, users, tasks, teams = _seed(factory, first_user_enterprise_role="admin")
    _install_live_snapshots(factory, company, teams, legacy=True)
    monkeypatch.setattr(runtime.assistant_service, "_require_active_limit", lambda *_a, **_kw: None)
    monkeypatch.setattr(runtime.assistant_service, "_require_subscription", lambda *_a, **_kw: None)
    monkeypatch.setattr(runtime.enterprise_org_service, "require_enterprise_execution", lambda *_a, **_kw: None)
    with factory.begin() as db:
        team = db.get(EnterpriseAgentTeam, teams[1])
        run = db.get(EnterpriseAssistantRun, team.assistant_run_id)
        state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).one()
        run.status = state.status = "running"
        state.lease_expires_at = runtime._now() + timedelta(minutes=5)
        state.checkpoint_json = runtime._json(RunCheckpoint(
            run_id=run.public_id, model="test", transcript=[], tools=[], status="running",
        ).to_dict())
        db.get(EnterpriseAgentTeamTask, tasks[1]).status = "retryable"
        conversation_id, public_id = int(run.conversation_id), str(run.public_id)
    with factory() as db:
        executor = runtime.EnterpriseAssistantToolExecutor(
            db, enterprise_id=company, conversation_id=conversation_id,
            run_public_id=public_id, lease_generation=1, actor_user_id=users[1],
        )
        result = asyncio.run(executor.execute(ToolCall(
            call_id="need-clarification", name="retry_enterprise_agent_team_task",
            arguments={"team_id": teams[1], "task_id": tasks[1]}, raw_arguments="{}",
        )))
        assert result.status == "clarification_required", result.error
    with factory() as db:
        assert db.get(EnterpriseAssistantClarification, result.approval_id) is not None
        assert db.get(EnterpriseAgentTeamTask, tasks[1]).status == "retryable"

    from app.models.enterprise_assistant_clarification import EnterpriseAssistantClarificationDecision
    from app.services import enterprise_assistant_clarification_service as clarity

    with factory.begin() as db:
        decision = db.query(EnterpriseAssistantClarificationDecision).filter_by(request_id=result.approval_id).one()
        source_id = int(decision.source_message_id)
        clarity.decide(db, enterprise_id=company, conversation_id=conversation_id, request_id=result.approval_id,
                       source_message_id=source_id, user_id=users[0], decision="allow")
    with factory.begin() as db:
        clarity.decide(db, enterprise_id=company, conversation_id=conversation_id, request_id=result.approval_id,
                       source_message_id=source_id, user_id=users[0], decision="deny")
    with factory() as db:
        executor = runtime.EnterpriseAssistantToolExecutor(
            db, enterprise_id=company, conversation_id=conversation_id,
            run_public_id=public_id, lease_generation=1, actor_user_id=users[1],
        )
        executor._clarification_request = (result.approval_id, 1)
        denied = asyncio.run(executor.execute(ToolCall(
            call_id="need-clarification", name="retry_enterprise_agent_team_task",
            arguments={"team_id": teams[1], "task_id": tasks[1]}, raw_arguments="{}",
        )))
        assert denied.status == "error" and denied.failure_kind == "clarification_closed"
    with factory() as db:
        assert db.get(EnterpriseAgentTeamTask, tasks[1]).status == "retryable"


def test_cross_run_control_locks_both_parents_in_stable_order(fresh_chain):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.services import enterprise_agent_team_runtime_service as team_runtime

    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, _group, _owner, _users, _tasks, teams = _seed(factory)
    with factory() as db:
        team_rows = [db.get(EnterpriseAgentTeam, team_id) for team_id in teams]
        runs = [db.get(EnterpriseAssistantRun, row.assistant_run_id) for row in team_rows]
        sources = [(str(run.public_id), int(run.conversation_id)) for run in runs]
    barrier = Barrier(2, timeout=10)

    def lock(source_index, target_index):
        with factory.begin() as db:
            barrier.wait()
            team_runtime._lock_assistant_control_runs(
                db, enterprise_id=company, team_id=teams[target_index],
                source_run_public_id=sources[source_index][0], source_conversation_id=sources[source_index][1],
            )
        return "complete"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(lock, 0, 1), pool.submit(lock, 1, 0)]
        assert [future.result(timeout=10) for future in futures] == ["complete", "complete"]


def test_retry_requires_target_permission_before_requesting_clarification(fresh_chain, monkeypatch):
    from app.models.enterprise_assistant_clarification import EnterpriseAssistantClarification

    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, _group, _owner, users, tasks, teams = _seed(factory, first_user_enterprise_role="admin")
    _install_live_snapshots(factory, company, teams, legacy=True)
    monkeypatch.setattr(runtime.assistant_service, "_require_active_limit", lambda *_a, **_kw: None)
    monkeypatch.setattr(runtime.assistant_service, "_require_subscription", lambda *_a, **_kw: None)
    monkeypatch.setattr(runtime.enterprise_org_service, "require_enterprise_execution", lambda *_a, **_kw: None)
    with factory.begin() as db:
        source_team = db.get(EnterpriseAgentTeam, teams[1])
        run = db.get(EnterpriseAssistantRun, source_team.assistant_run_id)
        state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).one()
        run.status = state.status = "running"
        state.lease_expires_at = runtime._now() + timedelta(minutes=5)
        state.checkpoint_json = runtime._json(RunCheckpoint(
            run_id=run.public_id, model="test", transcript=[], tools=[], status="running",
        ).to_dict())
        db.get(EnterpriseAgentTeamTask, tasks[0]).status = "retryable"
        conversation_id, public_id = int(run.conversation_id), str(run.public_id)
    with factory() as db:
        executor = runtime.EnterpriseAssistantToolExecutor(
            db, enterprise_id=company, conversation_id=conversation_id,
            run_public_id=public_id, lease_generation=1, actor_user_id=users[1],
        )
        result = asyncio.run(executor.execute(ToolCall(
            call_id="unauthorized-target", name="retry_enterprise_agent_team_task",
            arguments={"team_id": teams[0], "task_id": tasks[0]}, raw_arguments="{}",
        )))
        assert result.status == "error"
    with factory() as db:
        assert db.query(EnterpriseAssistantClarification).filter_by(
            enterprise_id=company, call_id="unauthorized-target",
        ).count() == 0
        assert db.get(EnterpriseAgentTeamTask, tasks[0]).status == "retryable"


@pytest.mark.parametrize("held_lock", ["run", "company"])
def test_control_fails_bounded_when_caller_holds_conflicting_lock(fresh_chain, monkeypatch, held_lock):
    from app.models.enterprise_org import Enterprise

    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, _group, _owner, users, _tasks, teams = _seed(
        factory, ordinary_message_indexes={1}, first_user_enterprise_role="admin",
    )
    _install_live_snapshots(factory, company, teams, legacy=True)
    monkeypatch.setattr(runtime.assistant_service, "_require_active_limit", lambda *_a, **_kw: None)
    monkeypatch.setattr(runtime.enterprise_org_service, "require_enterprise_execution", lambda *_a, **_kw: None)
    with factory.begin() as db:
        team = db.get(EnterpriseAgentTeam, teams[0])
        run = db.get(EnterpriseAssistantRun, team.assistant_run_id)
        state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).one()
        run.status = state.status = "running"
        state.lease_expires_at = runtime._now() + timedelta(minutes=5)
        state.checkpoint_json = runtime._json(RunCheckpoint(
            run_id=run.public_id, model="test", transcript=[], tools=[], status="running",
        ).to_dict())
        conversation_id, public_id, run_id = int(run.conversation_id), str(run.public_id), int(run.id)
    def timeout(connection, *_args):
        cursor = connection.cursor()
        try:
            cursor.execute("SET SESSION innodb_lock_wait_timeout = 2")
        finally:
            cursor.close()
    event.listen(fresh_chain, "checkout", timeout)
    username = f"held-caller-{uuid4().hex[:12]}"
    try:
        with factory() as db:
            db.add(User(username=username, password="x", role="user", status=1))
            db.flush()
            model, ident = (EnterpriseAssistantRun, run_id) if held_lock == "run" else (Enterprise, company)
            db.query(model).filter_by(id=ident).with_for_update().one()
            executor = runtime.EnterpriseAssistantToolExecutor(
                db, enterprise_id=company, conversation_id=conversation_id,
                run_public_id=public_id, lease_generation=1, actor_user_id=users[0],
            )
            result = asyncio.run(executor.execute(ToolCall(
                call_id="caller-held-lock", name="cancel_enterprise_agent_team",
                arguments={"team_id": teams[0]}, raw_arguments="{}",
            )))
            assert result.status == "error" and "OperationalError" in result.error
            assert db.query(User).filter_by(username=username).count() == 1
            db.rollback()
        with factory() as db:
            assert db.query(User).filter_by(username=username).count() == 0
            assert db.get(EnterpriseAgentTeam, teams[0]).status == "queued"
    finally:
        event.remove(fresh_chain, "checkout", timeout)


def test_real_cross_run_team_controls_finish_without_lock_inversion(fresh_chain, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.services import enterprise_agent_team_runtime_service as team_runtime

    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, _group, _owner, users, _tasks, teams = _seed(factory, first_user_enterprise_role="admin")
    from app.models.enterprise_org import EnterpriseMember

    with factory.begin() as db:
        db.query(EnterpriseMember).filter_by(enterprise_id=company, user_id=users[1]).one().role = "admin"
    _install_live_snapshots(factory, company, teams, legacy=True)
    monkeypatch.setattr(runtime.assistant_service, "_require_active_limit", lambda *_a, **_kw: None)
    monkeypatch.setattr(runtime.enterprise_org_service, "require_enterprise_execution", lambda *_a, **_kw: None)
    sources = []
    with factory.begin() as db:
        for team_id in teams:
            team = db.get(EnterpriseAgentTeam, team_id)
            run = db.get(EnterpriseAssistantRun, team.assistant_run_id)
            state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).one()
            run.status = state.status = "running"
            state.lease_expires_at = runtime._now() + timedelta(minutes=5)
            state.checkpoint_json = runtime._json(RunCheckpoint(
                run_id=run.public_id, model="test", transcript=[], tools=[], status="running",
            ).to_dict())
            sources.append((int(run.conversation_id), str(run.public_id)))
    original = team_runtime._lock_assistant_control_runs
    barrier = Barrier(2, timeout=10)

    def concurrent_lock(*args, **kwargs):
        barrier.wait()
        return original(*args, **kwargs)

    monkeypatch.setattr(team_runtime, "_lock_assistant_control_runs", concurrent_lock)

    def control(source_index, target_index):
        with factory() as db:
            executor = runtime.EnterpriseAssistantToolExecutor(
                db, enterprise_id=company, conversation_id=sources[source_index][0],
                run_public_id=sources[source_index][1], lease_generation=1, actor_user_id=users[source_index],
            )
            return asyncio.run(executor.execute(ToolCall(
                call_id=f"cross-{source_index}", name="cancel_enterprise_agent_team",
                arguments={"team_id": teams[target_index]}, raw_arguments="{}",
            )))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(control, 0, 1), pool.submit(control, 1, 0)]
        outcomes = [future.result(timeout=15) for future in futures]
    assert [outcome.status for outcome in outcomes] == ["success", "success"], [
        outcome.error for outcome in outcomes
    ]
    with factory() as db:
        assert [db.get(EnterpriseAgentTeam, team_id).status for team_id in teams] == ["cancelled", "cancelled"]


def test_cross_mismatched_team_task_does_not_deadlock(fresh_chain, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.models.enterprise_org import EnterpriseMember
    from app.services import enterprise_agent_team_runtime_service as team_runtime

    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, _group, _owner, users, tasks, teams = _seed(factory, first_user_enterprise_role="admin")
    with factory.begin() as db:
        db.query(EnterpriseMember).filter_by(enterprise_id=company, user_id=users[1]).one().role = "admin"
    _install_live_snapshots(factory, company, teams, legacy=True)
    monkeypatch.setattr(runtime.assistant_service, "_require_active_limit", lambda *_a, **_kw: None)
    monkeypatch.setattr(runtime.assistant_service, "_require_subscription", lambda *_a, **_kw: None)
    monkeypatch.setattr(runtime.enterprise_org_service, "require_enterprise_execution", lambda *_a, **_kw: None)
    sources = []
    with factory.begin() as db:
        for team_id in teams:
            team = db.get(EnterpriseAgentTeam, team_id)
            run = db.get(EnterpriseAssistantRun, team.assistant_run_id)
            state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).one()
            run.status = state.status = "running"
            state.lease_expires_at = runtime._now() + timedelta(minutes=5)
            state.checkpoint_json = runtime._json(RunCheckpoint(
                run_id=run.public_id, model="test", transcript=[], tools=[], status="running",
            ).to_dict())
            sources.append((int(run.conversation_id), str(run.public_id)))
        for task in tasks:
            db.get(EnterpriseAgentTeamTask, task).status = "retryable"
    barrier = Barrier(2, timeout=10)
    original = team_runtime._lock_assistant_control_runs

    def synchronized(*args, **kwargs):
        original(*args, **kwargs)
        barrier.wait()

    monkeypatch.setattr(team_runtime, "_lock_assistant_control_runs", synchronized)

    def control(index):
        with factory() as db:
            executor = runtime.EnterpriseAssistantToolExecutor(
                db, enterprise_id=company, conversation_id=sources[index][0],
                run_public_id=sources[index][1], lease_generation=1, actor_user_id=users[index],
            )
            return asyncio.run(executor.execute(ToolCall(
                call_id=f"mismatch-{index}", name="retry_enterprise_agent_team_task",
                arguments={"team_id": teams[index], "task_id": tasks[1-index]}, raw_arguments="{}",
            )))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(control, 0), pool.submit(control, 1)]
        outcomes = [future.result(timeout=15) for future in futures]
    assert all(result.status == "error" and "OperationalError" not in (result.error or "") for result in outcomes), [
        result.error for result in outcomes
    ]
    with factory() as db:
        assert [db.get(EnterpriseAgentTeamTask, task_id).status for task_id in tasks] == ["retryable", "retryable"]


@pytest.mark.parametrize("condition", ["task_completed", "unknown_bill"])
def test_unretryable_task_does_not_request_clarification(fresh_chain, monkeypatch, condition):
    from app.models.billing_ledger import BillingReservation
    from app.models.enterprise_assistant_clarification import EnterpriseAssistantClarification

    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, group, _owner, users, tasks, teams = _seed(factory, first_user_enterprise_role="admin")
    _install_live_snapshots(factory, company, teams, legacy=True)
    monkeypatch.setattr(runtime.assistant_service, "_require_active_limit", lambda *_a, **_kw: None)
    monkeypatch.setattr(runtime.assistant_service, "_require_subscription", lambda *_a, **_kw: None)
    monkeypatch.setattr(runtime.enterprise_org_service, "require_enterprise_execution", lambda *_a, **_kw: None)
    with factory.begin() as db:
        team = db.get(EnterpriseAgentTeam, teams[1])
        run = db.get(EnterpriseAssistantRun, team.assistant_run_id)
        state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).one()
        run.status = state.status = "running"
        state.lease_expires_at = runtime._now() + timedelta(minutes=5)
        state.checkpoint_json = runtime._json(RunCheckpoint(
            run_id=run.public_id, model="test", transcript=[], tools=[], status="running",
        ).to_dict())
        task = db.get(EnterpriseAgentTeamTask, tasks[1])
        task.status = "completed" if condition == "task_completed" else "retryable"
        if condition == "unknown_bill":
            db.add(BillingReservation(request_key=f"ent-team:{team.id}:{task.id}:0:1", model_name="test",
                                      enterprise_id=company, group_id=group, user_id=users[1],
                                      requested_milli=1000, status="unknown", usage_state="pending"))
        conversation_id, public_id = int(run.conversation_id), str(run.public_id)
    call_id = f"unretryable-{condition}"
    with factory() as db:
        executor = runtime.EnterpriseAssistantToolExecutor(
            db, enterprise_id=company, conversation_id=conversation_id,
            run_public_id=public_id, lease_generation=1, actor_user_id=users[1],
        )
        result = asyncio.run(executor.execute(ToolCall(
            call_id=call_id, name="retry_enterprise_agent_team_task",
            arguments={"team_id": teams[1], "task_id": tasks[1]}, raw_arguments="{}",
        )))
        assert result.status == "error"
    with factory() as db:
        assert db.query(EnterpriseAssistantClarification).filter_by(enterprise_id=company, call_id=call_id).count() == 0
        if condition == "unknown_bill":
            assert db.query(BillingReservation).filter_by(enterprise_id=company).one().status == "unknown"


def test_v1_retry_different_parent_same_actor_with_newer_message(fresh_chain, monkeypatch):
    """Exercise current v1 attribution and fresh-message reads across two parent runs."""
    from app.models.enterprise_chat import EnterpriseChatMessage, EnterpriseConversation

    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, _group, _owner, users, tasks, teams = _seed(
        factory, ordinary_message_indexes={1}, first_user_enterprise_role="admin",
    )
    _install_live_snapshots(factory, company, teams, legacy=False)
    monkeypatch.setattr(runtime, "_CROSS_SCOPE_ATTRIBUTION_VERIFIED", True)
    monkeypatch.setattr(runtime.assistant_service, "_require_active_limit", lambda *_a, **_kw: None)
    monkeypatch.setattr(runtime.assistant_service, "_require_subscription", lambda *_a, **_kw: None)
    monkeypatch.setattr(runtime.enterprise_org_service, "require_enterprise_execution", lambda *_a, **_kw: None)
    with factory.begin() as db:
        target = db.get(EnterpriseAgentTeam, teams[0])
        original = db.get(EnterpriseAssistantRun, target.assistant_run_id)
        # A second assistant run from the same account can target an earlier team.
        fields = {col.name: getattr(original, col.name) for col in EnterpriseAssistantRun.__table__.columns
                  if col.name not in {"id", "create_time", "update_time", "public_id", "request_key"}}
        suffix = uuid4().hex[:12]
        message = EnterpriseChatMessage(
            enterprise_id=company, conversation_id=original.conversation_id, sender_id=users[0],
            sender_type="human", client_message_id=f"source-{suffix}", content="继续处理团队",
            sender_authority_rank=2,
        )
        db.add(message)
        db.flush()
        fields["source_message_id"] = message.id
        new_run = EnterpriseAssistantRun(**fields, public_id=f"source-{suffix}", request_key=f"source-{suffix}")
        new_run.status = "running"
        db.add(new_run)
        db.flush()
        snapshot = runtime._role_snapshot(
            db, enterprise_id=company, room=db.get(EnterpriseConversation, original.conversation_id),
            messages=[message], initiator_id=users[0],
        )
        source_state = EnterpriseAssistantRuntimeState(
            assistant_run_id=new_run.id, enterprise_id=company, conversation_id=new_run.conversation_id,
            group_id=new_run.group_id, initiator_user_id=new_run.initiator_user_id,
            source_message_id=new_run.source_message_id, status="running",
            checkpoint_json=runtime._json(RunCheckpoint(
                run_id=new_run.public_id, model="test", transcript=[], tools=[], status="running",
            ).to_dict()),
            permission_snapshot_json=runtime._json(snapshot),
            permission_snapshot_sha256=runtime._digest(snapshot),
            lease_generation=1, lease_expires_at=runtime._now() + timedelta(minutes=5),
            billing_invocation_count=1, version=1,
        )
        db.add(source_state)
        db.get(EnterpriseAgentTeamTask, tasks[0]).status = "retryable"
        public_id, conversation_id = str(new_run.public_id), int(new_run.conversation_id)
    def timeout(connection, *_args):
        cursor = connection.cursor()
        try:
            cursor.execute("SET SESSION innodb_lock_wait_timeout = 2")
        finally:
            cursor.close()
    event.listen(fresh_chain, "checkout", timeout)
    try:
        with factory() as db:
            executor = runtime.EnterpriseAssistantToolExecutor(
                db, enterprise_id=company, conversation_id=conversation_id,
                run_public_id=public_id, lease_generation=1, actor_user_id=users[0],
            )
            result = asyncio.run(executor.execute(ToolCall(
                call_id="v1-same-actor", name="retry_enterprise_agent_team_task",
                arguments={"team_id": teams[0], "task_id": tasks[0]}, raw_arguments="{}",
            )))
            assert result.status == "success", result.error
    finally:
        event.remove(fresh_chain, "checkout", timeout)


def test_team_control_sees_new_authority_message_after_old_mysql_snapshot(fresh_chain, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from app.models.enterprise_chat import EnterpriseChatMessage
    from app.services import enterprise_agent_team_runtime_service as team_runtime

    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, _group, _owner, users, _tasks, teams = _seed(
        factory, ordinary_message_indexes={1}, first_user_enterprise_role="admin",
    )
    _install_live_snapshots(factory, company, teams, legacy=True)
    monkeypatch.setattr(runtime.assistant_service, "_require_active_limit", lambda *_a, **_kw: None)
    monkeypatch.setattr(runtime.enterprise_org_service, "require_enterprise_execution", lambda *_a, **_kw: None)
    with factory.begin() as db:
        team = db.get(EnterpriseAgentTeam, teams[0])
        run = db.get(EnterpriseAssistantRun, team.assistant_run_id)
        state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).one()
        run.status = state.status = "running"
        state.lease_expires_at = runtime._now() + timedelta(minutes=5)
        state.checkpoint_json = runtime._json(RunCheckpoint(
            run_id=run.public_id, model="test", transcript=[], tools=[], status="running",
        ).to_dict())
        conversation_id, public_id = int(run.conversation_id), str(run.public_id)
    reached, release = Event(), Event()
    original = team_runtime._lock_assistant_control_runs

    def pause_after_snapshot(*args, **kwargs):
        original(*args, **kwargs)
        reached.set()
        if not release.wait(10):
            raise TimeoutError("message barrier timed out")

    monkeypatch.setattr(team_runtime, "_lock_assistant_control_runs", pause_after_snapshot)

    def control():
        with factory() as db:
            executor = runtime.EnterpriseAssistantToolExecutor(
                db, enterprise_id=company, conversation_id=conversation_id,
                run_public_id=public_id, lease_generation=1, actor_user_id=users[0],
            )
            try:
                return asyncio.run(executor.execute(ToolCall(
                    call_id="newer-authority", name="cancel_enterprise_agent_team",
                    arguments={"team_id": teams[0]}, raw_arguments="{}",
                )))
            except runtime.SupersededByAuthorityDirectiveError:
                return "superseded"

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(control)
            assert reached.wait(10)
            with factory.begin() as db:
                db.add(EnterpriseChatMessage(
                    enterprise_id=company, conversation_id=conversation_id, sender_id=users[1],
                    sender_type="human", client_message_id=f"new-authority-{uuid4().hex[:10]}",
                    content="停止之前的操作", sender_authority_rank=3,
                ))
            release.set()
            assert future.result(timeout=10) == "superseded"
        with factory() as db:
            assert db.get(EnterpriseAgentTeam, teams[0]).status == "queued"
    finally:
        release.set()
