"""现有团队 Agent 接入正式审查的持久化、幂等及取消闭环。"""

import json
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
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.review_task_file import ReviewTaskFile
from app.models.user import User
from app.schemas.agent_team import AgentTeamCreateIn
from app.services import agent_team_review, agent_team_service, review_service


@pytest.fixture
def db(tmp_path):
    # 续租和观察者必须使用独立连接，不能由单连接内存库代替事务边界。
    engine = create_engine(f"sqlite:///{tmp_path / 'team-review.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with sessions() as database:
        yield database
    engine.dispose()


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


@pytest.mark.parametrize("wait_seconds", [0, 600])
def test_observation_timeout_renews_original_lease_without_cancelling_review(
    db, monkeypatch, review_team, wait_seconds,
):
    rows = review_team
    initial = datetime.now(timezone.utc)
    clock = SimpleNamespace(seconds=0)
    rows.task.lease_expires_at = initial + timedelta(seconds=660)
    rows.task.attempt_count = 1
    rows.team.deadline_at = initial + timedelta(seconds=1200)
    db.commit()
    monkeypatch.setattr(agent_team_service, "_now", lambda: initial + timedelta(seconds=clock.seconds))
    monkeypatch.setattr(agent_team_review.settings, "agent_full_validation_wait_seconds", wait_seconds)
    monkeypatch.setattr(agent_team_review.settings, "agent_team_task_lease_seconds", 300)
    snapshots = []

    def advance(_):
        clock.seconds += 30
        with sessionmaker(bind=db.get_bind())() as observer:
            task = observer.get(AgentTeamTask, rows.task.id)
            review = observer.query(ReviewTask).one()
            snapshots.append((review.id, review.execution_token, task.lease_token, task.lease_expires_at))
        if clock.seconds >= 750:
            _finish_review(db)
        assert clock.seconds <= 750, "等待器没有读取真实终态"

    monkeypatch.setattr(agent_team_review, "time", SimpleNamespace(
        sleep=advance, monotonic=lambda: clock.seconds,
    ))
    result = _run(db, rows)
    assert result["status"] == "completed"
    assert clock.seconds == 750
    assert len({(item[0], item[1], item[2]) for item in snapshots}) == 1
    assert len({item[3] for item in snapshots}) == 3  # 初始期限及两次续租；不能每轮都写库。
    assert snapshots[-1][3].replace(tzinfo=timezone.utc) > initial + timedelta(seconds=750)
    assert db.query(ReviewTask).count() == db.query(ReviewTaskFile).count() == 1
    assert db.get(AgentTeamTask, rows.task.id).attempt_count == 1
    assert rows.thread.call_count == 1


def test_observer_handoff_reuses_persisted_review_and_original_frozen_version(db, monkeypatch, review_team):
    rows = review_team
    monkeypatch.setattr(agent_team_review.time, "sleep", Mock(side_effect=RuntimeError("observer interrupted")))
    with pytest.raises(RuntimeError, match="observer interrupted"):
        _run(db, rows)
    review = db.query(ReviewTask).one()
    review_id, original_token = review.id, review.execution_token
    original_hash = db.query(ReviewTaskFile).one().content_sha256
    rows.source.content = "print('new version')\n"
    rows.source.version_no = 2
    db.add(CodeVersion(file_id=rows.source.id, version_no=2, content=rows.source.content,
                       create_time=datetime.now(timezone.utc)))
    # 新 dispatcher 领取后换发团队租约；持久审查映射与审查执行 token 不变。
    rows.task.lease_token = "next-observer"
    db.commit()
    db.rollback()
    with sessionmaker(bind=db.get_bind(), expire_on_commit=False)() as resumed:
        monkeypatch.setattr(agent_team_review.time, "sleep", lambda _: _finish_review(resumed))
        result = agent_team_review.run_team_review(
            resumed, resumed.get(User, rows.user.id), team_id=rows.team.id, task_id=rows.task.id,
            lease_token="next-observer", project_id=rows.project.id,
        )
        stored = resumed.get(ReviewTask, review_id)
        frozen = resumed.query(ReviewTaskFile).one()
        assert result["status"] == "completed" and result["task_id"] == review_id
        assert stored.execution_token == original_token
        assert (frozen.version_no, frozen.content_sha256) == (1, original_hash)
        assert review_service.load_task_inputs(resumed, review_id)[0].content == "print(1)\n"
    assert rows.thread.call_count == 1


@pytest.mark.parametrize("change", ["cancelled", "expired", "replaced", "deadline", "disabled", "member"])
def test_lease_renewal_cannot_revive_stopped_or_reassigned_execution(db, monkeypatch, review_team, change):
    rows = review_team
    monkeypatch.setattr(agent_team_review.time, "sleep", lambda _: _finish_review(db))
    _run(db, rows)
    review = db.query(ReviewTask).one()
    review.status = "running"
    db.commit()
    original_expiry = rows.task.lease_expires_at
    if change == "cancelled":
        agent_team_service.cancel_team(db, rows.user, rows.team.id)
    elif change == "expired":
        rows.task.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    elif change == "replaced":
        rows.task.lease_token = "replacement"
    elif change == "deadline":
        rows.team.deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    elif change == "disabled":
        rows.user.status = 0
    else:
        db.get(AgentTeamMember, rows.task.member_id).address = "agent:reporter"
    db.commit()
    with pytest.raises(agent_team_service.AgentTeamError):
        agent_team_review._renew_review_lease(
            db.get_bind(), user_id=rows.user.id, project_id=rows.project.id,
            team_id=rows.team.id, task_id=rows.task.id, member_id=rows.task.member_id,
            lease_token="lease", review_id=review.id,
        )
    db.expire_all()
    current = db.get(AgentTeamTask, rows.task.id)
    if current.lease_expires_at is not None:
        assert current.lease_expires_at.replace(tzinfo=timezone.utc) <= original_expiry.replace(tzinfo=timezone.utc)
    assert rows.thread.call_count == 1


def test_human_cancelled_review_is_not_restarted_by_observer_or_recovery(db, monkeypatch, review_team):
    rows = review_team
    monkeypatch.setattr(agent_team_review.time, "sleep", lambda _: _finish_review(db, "cancelled"))
    first = _run(db, rows)
    again = _run(db, rows)
    resume = Mock()
    monkeypatch.setattr(review_service, "SessionLocal", sessionmaker(bind=db.get_bind()))
    monkeypatch.setattr(review_service, "_run_review_task", resume)
    review = db.get(ReviewTask, first["task_id"])
    review_service._resume_interrupted_task(review.id, rows.user.id, review.execution_token)
    assert first["status"] == again["status"] == "failed"
    assert db.query(ReviewTask).count() == rows.thread.call_count == 1
    assert db.get(ReviewTask, first["task_id"]).status == "cancelled"
    resume.assert_not_called()


@pytest.mark.parametrize("mutation", ["owner", "project", "frozen_project"])
def test_handoff_rejects_changed_review_binding_without_creating_second_review(
    db, monkeypatch, review_team, mutation,
):
    rows = review_team
    monkeypatch.setattr(agent_team_review.time, "sleep", lambda _: _finish_review(db))
    _run(db, rows)
    review = db.query(ReviewTask).one()
    if mutation == "owner":
        review.user_id = rows.user.id + 100
    elif mutation == "project":
        review.project_id = rows.project.id + 100
    else:
        frozen = db.query(ReviewTaskFile).one()
        frozen.file_snapshot = {**frozen.file_snapshot, "project_id": rows.project.id + 100}
    db.commit()
    with pytest.raises(agent_team_service.AgentTeamStateError):
        _run(db, rows)
    assert db.query(ReviewTask).count() == rows.thread.call_count == 1


def test_project_permission_revocation_stops_existing_formal_review(db, monkeypatch, review_team):
    def revoke(_):
        review_team.project.user_id = review_team.user.id + 100
        db.commit()

    monkeypatch.setattr(agent_team_review.time, "sleep", revoke)
    result = _run(db, review_team)
    assert result["status"] == "blocked"
    assert db.get(ReviewTask, result["task_id"]).status == "cancelled"
    assert review_team.thread.call_count == 1


def test_lease_renewal_caps_existing_expiry_at_hard_deadline(db, monkeypatch, review_team):
    rows = review_team
    monkeypatch.setattr(agent_team_review.time, "sleep", lambda _: _finish_review(db))
    _run(db, rows)
    review = db.query(ReviewTask).one()
    review.status = "running"
    now = datetime.now(timezone.utc)
    rows.task.lease_expires_at = now + timedelta(seconds=300)
    rows.team.deadline_at = now + timedelta(seconds=45)
    db.commit()
    agent_team_review._renew_review_lease(
        db.get_bind(), user_id=rows.user.id, project_id=rows.project.id,
        team_id=rows.team.id, task_id=rows.task.id, member_id=rows.task.member_id,
        lease_token="lease", review_id=review.id,
    )
    db.expire_all()
    assert db.get(AgentTeamTask, rows.task.id).lease_expires_at == db.get(AgentTeam, rows.team.id).deadline_at


def test_lease_renewal_commits_on_separate_connection_without_observer_writes(db, monkeypatch, review_team):
    rows = review_team
    monkeypatch.setattr(agent_team_review.time, "sleep", lambda _: _finish_review(db))
    _run(db, rows)
    review = db.query(ReviewTask).one()
    review.status = "running"
    old_expiry = datetime.now(timezone.utc) + timedelta(seconds=10)
    rows.task.lease_expires_at = old_expiry
    db.commit()
    review_id, team_id, task_id = review.id, rows.team.id, rows.task.id
    user_id, project_id, member_id = rows.user.id, rows.project.id, rows.task.member_id
    db.rollback()
    with db.get_bind().connect() as connection, connection.begin():
        with sessionmaker(bind=connection, autoflush=False)() as observer:
            pending = User(username="uncommitted-observer", password="x", role="user", status=1)
            observer.add(pending)
            agent_team_review._renew_review_lease(
                observer.get_bind(), user_id=user_id, project_id=project_id, team_id=team_id,
                task_id=task_id, member_id=member_id, lease_token="lease", review_id=review_id,
            )
            with sessionmaker(bind=db.get_bind())() as independent:
                renewed = independent.get(AgentTeamTask, task_id).lease_expires_at
                assert renewed.replace(tzinfo=timezone.utc) > old_expiry
                assert independent.query(User).filter_by(username=pending.username).count() == 0
            assert pending in observer.new and pending.id is None
            observer.rollback()


def test_interrupted_team_review_resumes_only_unfinished_files_with_original_task(db, monkeypatch, review_team):
    rows = review_team
    second = CodeFile(project_id=rows.project.id, file_name="two.py", file_path="two.py", language="python",
                      content="print(2)\n", status="active", is_binary=0, version_no=1)
    db.add(second)
    db.flush()
    db.add(CodeVersion(file_id=second.id, version_no=1, content=second.content,
                       create_time=datetime.now(timezone.utc)))
    db.commit()
    monkeypatch.setattr(agent_team_review.time, "sleep", Mock(side_effect=RuntimeError("observer interrupted")))
    with pytest.raises(RuntimeError, match="observer interrupted"):
        _run(db, rows)
    review = db.query(ReviewTask).one()
    review_id, old_token = review.id, review.execution_token
    frozen_before = [(item.file_id, item.version_no, item.content_sha256)
                     for item in db.query(ReviewTaskFile).order_by(ReviewTaskFile.file_id).all()]
    for source in (rows.source, second):
        db.add(ReviewIssue(task_id=review_id, file_id=source.id, file_name=source.file_name,
                           line_number=1, title="原审查证据", severity="高", issue_type="安全漏洞",
                           description="由原冻结文件产生的证据"))
    review.coverage = {"stage": "reviewing", "files": {
        str(rows.source.id): {"status": "complete"}, str(second.id): {"status": "failed"},
    }}
    review.processed_files = 1
    rows.source.content = "print('changed live input')\n"
    rows.source.version_no = 2
    db.add(CodeVersion(file_id=rows.source.id, version_no=2, content=rows.source.content,
                       create_time=datetime.now(timezone.utc)))
    db.commit()
    resume = Mock()
    monkeypatch.setattr(review_service, "SessionLocal", sessionmaker(bind=db.get_bind()))
    monkeypatch.setattr(review_service, "_run_review_task", resume)
    review_service._resume_interrupted_task(review_id, rows.user.id, old_token)
    review_service._resume_interrupted_task(review_id, rows.user.id, old_token)
    db.expire_all()
    stored = db.get(ReviewTask, review_id)
    assert stored.execution_token != old_token
    resume.assert_called_once_with(review_id, rows.user.id, stored.execution_token)
    assert (stored.user_id, stored.project_id, stored.agent_team_id, stored.agent_team_task_id) == (
        rows.user.id, rows.project.id, rows.team.id, rows.task.id,
    )
    assert stored.status == "running" and stored.coverage["stage"] == "recovering"
    assert stored.processed_files == 1
    assert [issue.file_id for issue in db.query(ReviewIssue).all()] == [rows.source.id]
    assert [(item.file_id, item.version_no, item.content_sha256)
            for item in db.query(ReviewTaskFile).order_by(ReviewTaskFile.file_id).all()] == frozen_before
    assert review_service.load_task_inputs(db, review_id)[0].content == "print(1)\n"
    assert db.query(ReviewTask).count() == rows.thread.call_count == 1


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
            agent_team_service.create_team_from_xiaoling(db, rows.user, payload)
    else:
        created = agent_team_service.create_team_from_xiaoling(db, rows.user, payload)
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
