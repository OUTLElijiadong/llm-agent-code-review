"""取消、租约接管和账户停用后的执行边界回归。"""

import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.ai.multi_agent import get_agent_profiles
from app.models.agent_team import AgentTeam, AgentTeamMember, AgentTeamTask
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.review_task import ReviewTask
from app.models.user import User
from app.services import agent_mesh_dispatcher, agent_team_dispatcher, agent_team_service, review_service


def _team_rows(db, *, account_status=1, team_status="running", task_status="running", token="current"):
    user = User(username="execution-guard", password="x", role="user", status=account_status)
    db.add(user)
    db.flush()
    team = AgentTeam(user_id=user.id, surface="user", session_key="guard-session", title="执行边界",
                     objective="执行边界核验", status=team_status, trace_id="guard-trace", max_active_children=2,
                     max_attempts=1)
    db.add(team)
    db.flush()
    member = AgentTeamMember(team_id=team.id, member_key="reviewer", display_name="代码审查",
                             address="agent:code_reviewer", kind="runtime", role="verifier", status="running")
    db.add(member)
    db.flush()
    task = AgentTeamTask(team_id=team.id, member_id=member.id, task_key="review", title="审查",
                         instructions="审查现有代码", status=task_status, attempt_count=1, max_attempts=1,
                         lease_token=token, lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))
    db.add(task)
    db.commit()
    return user, team, task


@pytest.mark.parametrize("account_status", [0, 2])
def test_disabled_account_cannot_execute_claimed_team_task(db, monkeypatch, account_status):
    _, team, task = _team_rows(db, account_status=account_status)
    handler = Mock(return_value=("代码审查", {"status": "completed", "summary": "完成"}))
    complete = Mock()
    monkeypatch.setattr(agent_team_dispatcher, "SessionLocal", lambda: db)
    monkeypatch.setattr(agent_mesh_dispatcher, "_handle", handler)
    monkeypatch.setattr(agent_team_service, "complete_task", complete)

    result = agent_team_dispatcher._execute_claimed(team.id, {
        "task_id": task.id, "member_id": task.member_id, "lease_token": "current", "attempt_count": 1,
        "address": "agent:code_reviewer", "input": {"code": "print(1)"},
    })

    assert result == {"success": False}
    handler.assert_not_called()
    assert complete.call_args.kwargs["success"] is False
    assert complete.call_args.kwargs["result"]["retryable"] is False


@pytest.mark.parametrize("team_status,task_status,token", [
    ("cancelled", "cancelled", None),
    ("expired", "expired", None),
    ("running", "running", "new-owner"),
])
def test_inactive_claim_is_rejected_before_handler(db, monkeypatch, team_status, task_status, token):
    _, team, task = _team_rows(db, team_status=team_status, task_status=task_status, token=token)
    handler = Mock(return_value=("代码审查", {"status": "completed", "summary": "完成"}))
    monkeypatch.setattr(agent_team_dispatcher, "SessionLocal", lambda: db)
    monkeypatch.setattr(agent_mesh_dispatcher, "_handle", handler)

    result = agent_team_dispatcher._execute_claimed(team.id, {
        "task_id": task.id, "member_id": task.member_id, "lease_token": "current", "attempt_count": 1,
        "address": "agent:code_reviewer", "input": {"code": "print(1)"},
    })

    assert result == {"success": False}
    handler.assert_not_called()


@pytest.mark.parametrize("signal", [review_service.TaskCancelledError, review_service.TaskSupersededError])
def test_review_interruption_is_not_saved_as_file_failure(db, monkeypatch, signal):
    user = User(username="review-guard", password="x", role="admin", status=1)
    db.add(user)
    db.flush()
    project = Project(user_id=user.id, project_name="执行边界", status="active")
    db.add(project)
    db.flush()
    source = CodeFile(project_id=project.id, file_name="one.py", file_path="one.py", content="print(1)\n",
                      language="python", status="active", size_bytes=9, line_count=1, is_binary=0)
    db.add(source)
    db.flush()
    task = ReviewTask(user_id=user.id, project_id=project.id, review_type="full", status="running",
                      total_files=1, processed_files=0, execution_token="old-owner", coverage={})
    db.add(task)
    db.commit()
    observations = []

    def interrupt(*args, **kwargs):
        task.execution_token = "new-owner"
        task.coverage = {"stage": "cancelled" if signal is review_service.TaskCancelledError else "recovering"}
        if signal is review_service.TaskCancelledError:
            task.status = "cancelled"
        db.commit()
        raise signal("执行权已终止")

    original_commit = review_service._safe_commit

    def record_commit(database, current_task=None):
        observations.append(dict(task.coverage or {}))
        original_commit(database, current_task)

    monkeypatch.setattr(review_service, "_review_one_file", interrupt)
    monkeypatch.setattr(review_service, "_safe_commit", record_commit)
    monkeypatch.setattr(review_service, "_emit_review_event", Mock())
    review_service._execute_review(db, None, None, task, user, [source], [], get_agent_profiles("full"), "",
                                   execution_token="old-owner")

    assert all(not item.get("files") for item in observations)
    db.refresh(task)
    assert task.coverage == {"stage": "cancelled" if signal is review_service.TaskCancelledError else "recovering"}
    assert task.execution_token == "new-owner"


def test_expired_team_lease_cannot_commit_result(db):
    _, team, task = _team_rows(db)
    task.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    with pytest.raises(agent_team_service.AgentTeamLeaseError):
        agent_team_service.complete_task(db, team.id, task.id, lease_token="current",
                                         result={"status": "completed"})


def test_existing_full_review_profiles_run_at_the_same_time(db, monkeypatch):
    profiles = get_agent_profiles("full")
    gate = threading.Barrier(len(profiles), timeout=2)
    active = set()
    guard = threading.Lock()

    def call(profile, *args, **kwargs):
        with guard:
            active.add(profile.code)
        gate.wait()
        return '{"summary":"未发现问题","score":100,"issues":[]}', {}

    monkeypatch.setattr(review_service, "_call_single_agent", call)
    monkeypatch.setattr(review_service, "_emit_review_event", Mock())
    result = review_service._review_chunk_collaborative(
        db, None, None, SimpleNamespace(id=1, project_id=1),
        SimpleNamespace(id=1, file_name="one.py", language="python", content="print(1)\n"), [],
        SimpleNamespace(id=1), profiles, 0, SimpleNamespace(text="print(1)\n", start_line=1),
    )
    assert result == []
    assert active == {profile.code for profile in profiles}


def test_security_team_lease_covers_the_semantic_audit_deadline(db, monkeypatch):
    _, team, task = _team_rows(db, task_status="queued", token=None)
    db.get(AgentTeamMember, task.member_id).address = "agent:security_sentinel"
    db.commit()
    monkeypatch.setattr(agent_team_service.settings, "security_semantic_timeout_seconds", 3600)

    claimed = agent_team_service.claim_next_task(db, team.id, lease_seconds=660)

    assert claimed is not None
    db.refresh(task)
    deadline = task.lease_expires_at.replace(tzinfo=timezone.utc)
    assert (deadline - datetime.now(timezone.utc)).total_seconds() > 3650


def test_disabled_account_cannot_start_queued_formal_review(db, monkeypatch):
    user = User(username="disabled-review-owner", password="x", role="user", status=0)
    db.add(user)
    db.flush()
    project = Project(user_id=user.id, project_name="停用账户项目", status="active")
    db.add(project)
    db.flush()
    task = ReviewTask(user_id=user.id, project_id=project.id, status="running", review_type="full",
                      execution_token="queued-token")
    db.add(task)
    db.commit()
    task_id, user_id = task.id, user.id
    model_factory = Mock()
    monkeypatch.setattr(review_service, "SessionLocal", lambda: db)
    monkeypatch.setattr(review_service, "DeepSeekAgent", model_factory)
    monkeypatch.setattr(review_service, "_emit_review_event", Mock())

    review_service._run_review_task(task_id, user_id, "queued-token")

    model_factory.assert_not_called()
    task = db.get(ReviewTask, task_id)
    assert task.status == "failed"
    assert "停用" in task.error_message
