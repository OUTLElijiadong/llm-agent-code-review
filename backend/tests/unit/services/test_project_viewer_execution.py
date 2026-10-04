"""Benign local execution gates; every external/model boundary is a rejecting stub."""
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.agents.ai_prompt_agent import AiPromptAgent
from app.core.exceptions import ForbiddenError
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.user import User
from app.schemas.review import ReviewStartIn
from app.services import agent_team_dispatcher, agent_team_review, agent_team_service
from app.services import rbac_service, review_service, temporary_agent_runtime
from app.services import project_member_service as members


def seed(db, role="viewer"):
    owner = User(id=7301, username="execution-owner", password="local", role="user", status=1)
    actor = User(id=7302, username="execution-actor", password="local", role="user", status=1)
    project = Project(id=7301, user_id=owner.id, project_name="local execution", status="active")
    member = ProjectMember(project_id=project.id, user_id=actor.id, role_in_project=role)
    source = CodeFile(id=7301, project_id=project.id, file_name="local.py", file_path="local.py",
                      language="python", content="print('local')\n", status="active", is_binary=0)
    task = ReviewTask(id=7301, project_id=project.id, user_id=actor.id, task_name="local review",
                      review_type="standard", status="running", execution_token="local-token")
    issue = ReviewIssue(id=7301, task_id=task.id, file_id=source.id, file_name=source.file_name,
                        issue_type="代码规范", severity="低", title="local issue", description="local")
    db.add_all([owner, actor, project, member, source, task, issue]); db.commit()
    return actor, project, member, source, task, issue


def test_formal_start_denies_viewer_before_loading_rules_or_model(db, monkeypatch):
    actor, project, _, source, _, _ = seed(db)
    monkeypatch.setattr(review_service, "get_enabled_rules", lambda *_a, **_k: pytest.fail("rules reached"))
    with pytest.raises(ForbiddenError):
        review_service.start(db, actor, ReviewStartIn(project_id=project.id, file_ids=[source.id]))


def test_queued_review_worker_reloads_downgraded_role(db, monkeypatch):
    actor, _, member, _, task, _ = seed(db, "reviewer")
    with Session(bind=db.get_bind()) as writer:
        writer.query(ProjectMember).filter_by(id=member.id).update({"role_in_project": "viewer"})
        writer.commit()
    monkeypatch.setattr(review_service, "SessionLocal", lambda: Session(bind=db.get_bind()))
    monkeypatch.setattr(rbac_service, "check_permission", lambda *_a: True)
    with pytest.raises(review_service.ReviewAuthorizationRevokedError):
        review_service._assert_review_execution_authorized(task.id, actor.id, "local-token")


def test_team_formal_actor_denies_viewer_even_with_global_permission(db, monkeypatch):
    actor, project, *_ = seed(db)
    monkeypatch.setattr(rbac_service, "check_permission", lambda *_a: True)
    with pytest.raises(ForbiddenError):
        agent_team_review._require_actor(db, actor.id, project.id)


@pytest.mark.parametrize("scope", ["project_id", "file_id", "task_id"])
def test_team_create_and_claim_derive_real_project_for_viewer(db, monkeypatch, scope):
    actor, project, _, source, task, _ = seed(db)
    payload = {scope: {"project_id": project.id, "file_id": source.id, "task_id": task.id}[scope]}
    monkeypatch.setattr(rbac_service, "check_permission", lambda *_a: True)
    item = SimpleNamespace(input=payload, task_key="local", instructions="read local", depends_on=[])
    with pytest.raises(agent_team_service.AgentTeamValidationError):
        agent_team_service._validate_task_scope(db, actor, item, "agent:code_reviewer")
    error = agent_team_dispatcher._owner_access_error(db, actor.id, payload)
    assert error and "项目" in error


def test_team_scope_rejects_declared_project_disagreeing_with_file(db, monkeypatch):
    actor, project, _, source, _, _ = seed(db, "reviewer")
    other = Project(id=7302, user_id=actor.id, project_name="other local", status="active")
    db.add(other); db.commit()
    monkeypatch.setattr(rbac_service, "check_permission", lambda *_a: True)
    item = SimpleNamespace(input={"project_id": other.id, "file_id": source.id}, task_key="local",
                           instructions="read local", depends_on=[])
    with pytest.raises(agent_team_service.AgentTeamValidationError):
        agent_team_service._validate_task_scope(db, actor, item, "agent:code_reviewer")


def test_temporary_preparation_denies_viewer_before_source_analysis(db, monkeypatch):
    actor, project, _, source, _, _ = seed(db)
    monkeypatch.setattr(rbac_service, "check_permission", lambda *_a: True)
    with pytest.raises(ForbiddenError):
        temporary_agent_runtime._prepare_context(db, actor, {"payload": {"project_id": project.id, "file_id": source.id}})


@pytest.mark.parametrize("scope", ["issue", "task", "project"])
def test_viewer_ai_prompt_template_reads_but_llm_does_not_execute(db, monkeypatch, scope):
    actor, project, _, _, task, issue = seed(db)
    monkeypatch.setattr(rbac_service, "check_permission", lambda *_a: True)
    agent = AiPromptAgent(); agent.inject(db, user=actor)
    monkeypatch.setattr(agent, "_polish_with_llm", lambda *_a, **_k: pytest.fail("model reached"))
    execute = getattr(agent, "execute_for_" + scope)
    identifier = {"issue": issue.id, "task": task.id, "project": project.id}[scope]
    assert execute(identifier, use_llm=False).success is True
    blocked = execute(identifier, use_llm=True)
    assert blocked.success is False


@pytest.mark.parametrize("scope", ["issue", "task", "project"])
def test_ai_prompt_source_still_requires_actual_global_read_permission(db, monkeypatch, scope):
    actor, project, _, _, task, issue = seed(db, "reviewer")
    monkeypatch.setattr(rbac_service, "check_permission", lambda *_a: False)
    agent = AiPromptAgent(); agent.inject(db, user=actor)
    execute = getattr(agent, "execute_for_" + scope)
    identifier = {"issue": issue.id, "task": task.id, "project": project.id}[scope]
    assert execute(identifier, use_llm=False).success is False


def test_roundtable_model_boundary_reloads_viewer_before_call(db, monkeypatch):
    from app.ai import discussion_orchestrator as discussion
    actor, _, _, _, task, _ = seed(db)
    monkeypatch.setattr(discussion, "SessionLocal", lambda: Session(bind=db.get_bind()))
    monkeypatch.setattr(rbac_service, "check_permission", lambda *_a: True)
    model = SimpleNamespace(call_raw=lambda **_k: pytest.fail("model reached"))
    with pytest.raises(ForbiddenError):
        discussion._call_raw_for_task(model, task.id, actor.id, user_prompt="local")


def test_roundtable_direct_start_requires_execution_before_registration(db, monkeypatch):
    from app.api.v1 import discussion
    actor, project, _, source, _, _ = seed(db)
    monkeypatch.setattr(rbac_service, "check_permission", lambda *_a: True)
    monkeypatch.setattr(discussion.rule_service, "get_enabled_rules", lambda *_a, **_k: pytest.fail("rules reached"))
    with pytest.raises(ForbiddenError):
        discussion.start_discussion(project.id, source.id, db=db, user=actor)


def test_roundtable_pending_role_gate_reloads_current_member(db, monkeypatch):
    from app.api.v1 import ws_discussion
    actor, project, _, _, _, _ = seed(db)
    monkeypatch.setattr(ws_discussion, "SessionLocal", lambda: Session(bind=db.get_bind()))
    monkeypatch.setattr(rbac_service, "check_permission", lambda *_a: True)
    gate = getattr(ws_discussion, "_is_discussion_execution_active", None)
    assert callable(gate)
    assert gate(actor.id, project.id) is False
    assert gate(actor.id, 0) is False


def test_ai_prompt_role_downgrade_during_model_call_discards_result(db, monkeypatch):
    actor, _, member, _, _, issue = seed(db, "reviewer")
    monkeypatch.setattr(rbac_service, "check_permission", lambda *_a: True)
    agent = AiPromptAgent(); agent.inject(db, user=actor)
    def benign_model(*_a, **_k):
        with Session(bind=db.get_bind()) as writer:
            writer.query(ProjectMember).filter_by(id=member.id).update({"role_in_project": "viewer"})
            writer.commit()
        return "local", SimpleNamespace(tokens={})
    monkeypatch.setattr(agent, "_polish_with_llm", benign_model)
    result = agent.execute_for_issue(issue.id, use_llm=True)
    assert result.success is False


def test_roundtable_role_downgrade_during_model_call_discards_result(db, monkeypatch):
    from app.ai import discussion_orchestrator as discussion
    actor, _, member, _, task, _ = seed(db, "reviewer")
    monkeypatch.setattr(discussion, "SessionLocal", lambda: Session(bind=db.get_bind()))
    monkeypatch.setattr(rbac_service, "check_permission", lambda *_a: True)
    def benign_model(**_k):
        with Session(bind=db.get_bind()) as writer:
            writer.query(ProjectMember).filter_by(id=member.id).update({"role_in_project": "viewer"})
            writer.commit()
        return "local", {}
    with pytest.raises(ForbiddenError):
        discussion._call_raw_for_task(SimpleNamespace(call_raw=benign_model), task.id, actor.id)


@pytest.mark.parametrize("role", ["reviewer", "owner"])
def test_existing_roles_keep_ai_prompt_llm_execution(db, monkeypatch, role):
    actor, _, _, _, _, issue = seed(db, role)
    monkeypatch.setattr(rbac_service, "check_permission", lambda *_a: True)
    agent = AiPromptAgent(); agent.inject(db, user=actor)
    calls = []
    def benign_model(*_a, **_k):
        calls.append(True)
        return "local complete answer", SimpleNamespace(tokens={"total": 0})
    monkeypatch.setattr(agent, "_polish_with_llm", benign_model)
    assert agent.execute_for_issue(issue.id, use_llm=True).success is True
    assert calls == [True]


def test_temporary_post_call_role_recheck_does_not_return_result(db, monkeypatch):
    actor, project, member, source, _, _ = seed(db, "reviewer")
    monkeypatch.setattr(rbac_service, "check_permission", lambda *_a: True)
    member.role_in_project = "viewer"; db.commit()
    with pytest.raises(ForbiddenError):
        temporary_agent_runtime._recheck_access(db, actor.id, project.id, [source.id], {})


@pytest.mark.parametrize("payload", [{"project_id": True}, {"file_ids": [True]},
    {"file_ids": "7301"}, {"task_id": -1}, {"public_id": ""}])
def test_structured_execution_scope_rejects_invalid_resource_identifiers(db, payload):
    from app.core.exceptions import BadRequestError
    actor, *_ = seed(db, "reviewer")
    with pytest.raises(BadRequestError):
        members.require_scoped_project_execution(db, actor, payload)


def test_execution_scope_rejects_viewer_source_revision_and_environment(db):
    from datetime import datetime
    from app.models.project_source_revision import ProjectSourceRevision
    from app.models.agent_capability import SandboxEnvironment
    actor, project, *_ = seed(db)
    revision = ProjectSourceRevision(project_id=project.id, owner_id=actor.id, revision_no=1,
        source_sha256="0" * 64, repaired_files_json="[]", archive_blob=b"local unused",
        create_time=datetime.utcnow(), update_time=datetime.utcnow())
    env = SandboxEnvironment(public_id="viewer-unused-local", project_id=project.id,
        owner_id=actor.id, agent_code="test_verifier", purpose="test", language="python",
        image_ref="unused", source_sha256="0" * 64, resource_policy_json="{}", agent_config_json="{}",
        expires_at=datetime.utcnow())
    db.add_all([revision, env]); db.commit()
    for payload in ({"source_revision_id": revision.id}, {"public_id": env.public_id}):
        with pytest.raises(ForbiddenError):
            members.require_scoped_project_execution(db, actor, payload)


def test_draft_engagement_creation_is_denied_without_external_execution(db, monkeypatch):
    from app.services import pentest_service
    from app.models.pentest import PentestEngagement
    actor, project, *_ = seed(db)
    monkeypatch.setattr(pentest_service.settings, "pentest_enabled", True)
    with pytest.raises(ForbiddenError):
        pentest_service.create_engagement(db, actor, {"project_id": project.id, "target_type": "web"})
    assert db.query(PentestEngagement).count() == 0


def test_retry_child_inherits_project_execution_scope_from_dependency(db, monkeypatch):
    from app.models.agent_team import AgentTeam, AgentTeamTask
    from app.schemas.agent_team import AgentTeamCreateIn
    actor, project, member, *_ = seed(db, "reviewer")
    monkeypatch.setattr(rbac_service, "check_permission", lambda *_a: True)
    payload = AgentTeamCreateIn.model_validate({"surface": "user", "session_id": "local-scope-01", "title": "local dependency scope", "objective": "read local",
        "members": [{"member_key": "reader", "address": "agent:project_analyzer", "display_name": "local reader"}],
        "tasks": [{"task_key": "source", "member_key": "reader", "title": "source", "instructions": "read metadata",
                   "input": {"project_id": project.id}},
                  {"task_key": "summary", "member_key": "reader", "title": "summary", "instructions": "read summary",
                   "depends_on": ["source"]}]})
    from app.services import agent_supervisor_service
    plan = agent_supervisor_service.review_agent_team_plan(payload.model_dump(mode="json"))
    created = agent_team_service.create_team_from_xiaoling(db, actor, payload,
        supervisor_plan_sha256=plan["plan_sha256"], supervisor_confirmed_by=actor.id)
    team = db.get(AgentTeam, created["team_id"])
    tasks = {row.task_key: row for row in db.query(AgentTeamTask).filter_by(team_id=team.id)}
    tasks["source"].status = "completed"; tasks["summary"].status = "failed"; team.status = "failed"
    member.role_in_project = "viewer"; db.commit()
    strategy = {"summary": "重新逐项核对已有元数据依据"}
    preview = agent_team_service.preview_retry_team(db, actor, team.id,
        task_keys=["summary"], strategy_changes=strategy)
    with pytest.raises(ForbiddenError):
        agent_team_service.retry_team(db, actor, team.id, task_keys=["summary"],
            strategy_changes=strategy, supervisor_plan_sha256=preview["plan_sha256"])
    db.refresh(tasks["summary"])
    assert tasks["summary"].status == "failed"
