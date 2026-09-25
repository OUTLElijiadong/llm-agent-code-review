"""现有团队 Agent 接入正式审查的持久化、幂等及取消闭环。"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.models.agent_team import AgentTeam, AgentTeamMember, AgentTeamTask
from app.models.code_file import CodeFile
from app.models.code_version import CodeVersion
from app.models.project import Project
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.review_task_file import ReviewTaskFile
from app.models.user import User
from app.schemas.agent_team import AgentTeamCreateIn
from app.services import agent_team_review, agent_team_service, review_service


@pytest.fixture
def review_team(db, monkeypatch):
    user = User(username="review-team-owner", password="x", role="user", status=1)
    db.add(user)
    db.flush()
    project = Project(user_id=user.id, project_name="正式审查项目", status="active", language="python")
    db.add(project)
    db.flush()
    source = CodeFile(project_id=project.id, file_name="one.py", file_path="one.py", language="python",
                      content="print(1)\n", status="active", is_binary=0, version_no=1)
    db.add(source)
    db.flush()
    db.add(CodeVersion(file_id=source.id, version_no=1, content=source.content, create_time=datetime.now(timezone.utc)))
    team = AgentTeam(user_id=user.id, surface="user", session_key="review-session", title="现有 Agent 团队",
                     objective="完成正式并行审查", status="running", trace_id="formal-review-trace")
    db.add(team)
    db.flush()
    member = AgentTeamMember(team_id=team.id, member_key="coordinator", display_name="审查总指挥",
                             address="agent:review_orchestrator", kind="runtime", role="worker", status="running")
    db.add(member)
    db.flush()
    task = AgentTeamTask(team_id=team.id, member_id=member.id, task_key="review", title="完整审查项目",
                         instructions="由现有专业 Agent 并行审查并聚合证据", status="running", lease_token="lease",
                         lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
                         input_json=json.dumps({"operation": "run_review", "project_id": project.id,
                                                "review_type": "full"}))
    db.add(task)
    db.commit()
    monkeypatch.setattr("app.services.rbac_service.check_permission", lambda *args, **kwargs: True)
    monkeypatch.setattr(review_service, "get_enabled_rules", lambda *args, **kwargs: [])
    monkeypatch.setattr(review_service, "DeepSeekAgent", lambda **kwargs: SimpleNamespace(model="mock-model"))
    monkeypatch.setattr("app.utils.api_resolver.resolve_api_config", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.services.agent_model_service.resolve_subagent_config", lambda *args, **kwargs: None)
    thread = Mock()
    monkeypatch.setattr(review_service.threading, "Thread", thread)
    return SimpleNamespace(user=user, project=project, source=source, team=team, task=task, thread=thread)


def _run(db, rows):
    return agent_team_review.run_team_review(db, rows.user, team_id=rows.team.id, task_id=rows.task.id,
                                            lease_token="lease", project_id=rows.project.id)


def _finish_review(db, status="success", *, coverage_complete=True):
    review = db.query(ReviewTask).one()
    review.status = status
    review.processed_files = review.total_files if coverage_complete else 0
    review.coverage = {**(review.coverage or {}),
                       "stage": "complete" if status == "success" and coverage_complete else "failed"}
    db.commit()


def test_team_creates_one_formal_review_freezes_input_and_reuses_on_retry(db, monkeypatch, review_team):
    rows = review_team
    monkeypatch.setattr(agent_team_review.time, "sleep", lambda _: _finish_review(db))

    first = _run(db, rows)
    second = _run(db, rows)

    assert first["status"] == second["status"] == "completed"
    assert first["task_id"] == second["task_id"]
    assert db.query(ReviewTask).count() == 1
    review = db.query(ReviewTask).one()
    assert (review.agent_team_id, review.agent_team_task_id) == (rows.team.id, rows.task.id)
    assert review.review_type == "full"
    assert db.query(ReviewTaskFile).filter_by(task_id=review.id).one().content_sha256
    assert first["artifacts"][0]["report_url"] == f"/reports/{review.id}"
    assert rows.thread.call_count == 1


@pytest.mark.parametrize("status,coverage_complete", [("failed", False), ("cancelled", False), ("success", False)])
def test_partial_or_cancelled_review_cannot_complete_team_task(db, monkeypatch, review_team, status, coverage_complete):
    monkeypatch.setattr(agent_team_review.time, "sleep",
                        lambda _: _finish_review(db, status, coverage_complete=coverage_complete))
    result = _run(db, review_team)
    assert result["status"] == "failed"
    assert result["errors"]
    assert result["artifacts"][0]["report_url"] is None


def test_cancel_team_revokes_formal_review_and_keeps_unrelated_review(db, monkeypatch, review_team):
    rows = review_team
    unrelated = ReviewTask(user_id=rows.user.id, project_id=rows.project.id, status="running", review_type="full",
                           execution_token="unrelated-token")
    db.add(unrelated)
    db.commit()

    def cancel(_):
        agent_team_service.cancel_team(db, rows.user, rows.team.id)

    monkeypatch.setattr(agent_team_review.time, "sleep", cancel)
    result = _run(db, rows)
    review = db.get(ReviewTask, result["task_id"])
    assert result["status"] == review.status == "cancelled"
    assert review.coverage["stage"] == "cancelled"
    db.refresh(unrelated)
    assert unrelated.status == "running"
    assert unrelated.execution_token == "unrelated-token"


def test_lost_team_lease_does_not_cancel_review_owned_by_next_waiter(db, monkeypatch, review_team):
    rows = review_team

    def takeover(_):
        rows.task.lease_token = "new-lease"
        db.commit()

    monkeypatch.setattr(agent_team_review.time, "sleep", takeover)
    result = _run(db, rows)
    assert result["status"] == "cancelled"
    assert db.get(ReviewTask, result["task_id"]).status == "running"
    _finish_review(db)
    resumed = agent_team_review.run_team_review(db, rows.user, team_id=rows.team.id, task_id=rows.task.id,
                                               lease_token="new-lease", project_id=rows.project.id)
    assert resumed["status"] == "completed"
    assert resumed["task_id"] == result["task_id"]
    assert rows.thread.call_count == 1


def test_wait_timeout_cancels_review_and_never_claims_success(db, monkeypatch, review_team):
    monkeypatch.setattr(agent_team_review.settings, "agent_full_validation_wait_seconds", 0)
    result = _run(db, review_team)
    assert result["status"] == "failed"
    assert result["retryable"] is False
    review = db.get(ReviewTask, result["task_id"])
    assert review.status == "cancelled"
    assert "超时" in review.error_message


@pytest.mark.parametrize("mutation", ["readonly", "wrong_member", "wrong_operation", "wrong_project", "disabled"])
def test_untrusted_scope_is_rejected_before_creating_review(db, review_team, mutation):
    rows = review_team
    if mutation == "readonly":
        rows.team.objective = "只读核对既有审查"
    elif mutation == "wrong_member":
        db.get(AgentTeamMember, rows.task.member_id).address = "agent:code_reviewer"
    elif mutation == "wrong_operation":
        rows.task.input_json = json.dumps({"operation": "list", "project_id": rows.project.id})
    elif mutation == "wrong_project":
        rows.task.input_json = json.dumps({"operation": "run_review", "project_id": rows.project.id + 1})
    else:
        rows.user.status = 0
    db.commit()
    with pytest.raises(agent_team_service.AgentTeamError):
        _run(db, rows)
    assert db.query(ReviewTask).count() == 0


def test_permission_revocation_stops_existing_formal_review(db, monkeypatch, review_team):
    def revoke(_):
        monkeypatch.setattr("app.services.rbac_service.check_permission", lambda *args, **kwargs: False)

    monkeypatch.setattr(agent_team_review.time, "sleep", revoke)
    result = _run(db, review_team)
    assert result["status"] == "blocked"
    assert db.get(ReviewTask, result["task_id"]).status == "cancelled"


def test_default_selection_records_binary_and_empty_exclusions(db, monkeypatch, review_team):
    rows = review_team
    db.add_all([
        CodeFile(project_id=rows.project.id, file_name="image.png", language="binary", content="image",
                 status="active", is_binary=1),
        CodeFile(project_id=rows.project.id, file_name="empty.py", language="python", content=" \n",
                 status="active", is_binary=0),
    ])
    db.commit()
    monkeypatch.setattr(agent_team_review.time, "sleep", lambda _: _finish_review(db))

    result = _run(db, rows)

    assert result["status"] == "completed"
    assert {item["reason"] for item in result["coverage"]["excluded_files"]} == {"binary", "empty_text"}
    assert result["coverage"]["total_files"] == 1
    assert db.query(ReviewTaskFile).count() == 1


@pytest.mark.parametrize("readonly", [False, True])
def test_team_creation_preserves_run_review_contract_and_enforces_readonly(db, review_team, readonly):
    rows = review_team
    payload = AgentTeamCreateIn.model_validate({
        "surface": "user", "session_id": "new-formal-session", "title": "正式团队",
        "objective": "只读核对已有结果" if readonly else "正式并行审查",
        "members": [{"member_key": "coordinator", "display_name": "审查总指挥",
                     "address": "agent:review_orchestrator", "role": "worker"}],
        "tasks": [{"task_key": "formal-review", "member_key": "coordinator", "title": "正式审查",
                   "instructions": "检查代码", "input": {"operation": "run_review",
                                                        "project_id": rows.project.id, "review_type": "full"}}],
    })
    if readonly:
        with pytest.raises(agent_team_service.AgentTeamValidationError, match="只读"):
            agent_team_service.create_team(db, rows.user, payload)
    else:
        created = agent_team_service.create_team(db, rows.user, payload)
        stored = db.query(AgentTeamTask).filter_by(team_id=created["team_id"], task_key="formal-review").one()
        assert json.loads(stored.input_json)["operation"] == "run_review"
        assert {item["address"] for item in created["members"]} == {"agent:review_orchestrator", "agent:reporter"}


@pytest.mark.parametrize("status", ["success", "failed"])
def test_formal_result_keeps_all_findings_without_source_or_secret_fields(db, monkeypatch, review_team, status):
    def finish(_):
        review = db.query(ReviewTask).one()
        db.add_all([ReviewIssue(
            task_id=review.id, file_id=review_team.source.id, file_name="one.py", line_number=index + 1,
            title=f"问题 {index + 1}", severity="高", issue_type="安全漏洞", description="private-long-description",
            fixed_code="secret-source-content", suggestion="private-remediation",
        ) for index in range(101)])
        db.commit()
        _finish_review(db, status, coverage_complete=status == "success")

    monkeypatch.setattr(agent_team_review.time, "sleep", finish)
    result = _run(db, review_team)
    assert result["status"] == ("completed" if status == "success" else "failed")
    assert result["finding_count_total"] == 101
    assert result["findings_truncated"] is False
    assert len(result["findings"]) == 101
    assert result["findings"][0]["project_id"] == review_team.project.id
    assert result["findings"][0]["file_id"] == review_team.source.id
    assert set(result["findings"][0]) == {
        "issue_id", "project_id", "task_id", "file_id", "file_name", "line_number", "title", "severity", "issue_type",
    }
    assert "private-long-description" not in json.dumps(result)
    assert "secret-source-content" not in json.dumps(result)
