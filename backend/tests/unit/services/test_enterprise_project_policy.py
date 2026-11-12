"""企业项目权限矩阵与项目绑定服务。"""
import pytest

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ServiceUnavailableError
from app.models import billing_ledger  # noqa: F401 - registers billing tables used by org membership checks
from app.models.agent_governance import AgentKnowledgeChunk, AgentKnowledgeDoc, AgentKnowledgeSource
from app.models.code_file import CodeFile
from app.models.enterprise_org import EnterpriseGroup
from app.models.enterprise_project import EnterpriseProject, EnterpriseProjectGrant
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_doc import KnowledgeDoc
from app.models.pentest import PentestEngagement
from app.models.project import Project
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.user import User
from app.schemas.project import ProjectUpdateIn
from app.services import enterprise_org_service as org
from app.services import enterprise_project_service as enterprise_projects
from app.services import pentest_service, project_service, report_service
from app.services.enterprise_project_policy import require_enterprise_project_access
from app.services.project_member_service import (
    add_member,
    get_visible_project_ids,
    is_project_member,
    require_project_access,
    require_project_execution,
    require_scoped_project_execution,
)


def _user(db, name, role="user"):
    user = User(username=name, password="x", role=role, status=1)
    db.add(user)
    db.flush()
    return user


@pytest.fixture
def context(db):
    owner, leader, worker, outsider, platform_admin = (
        _user(db, "owner"), _user(db, "leader"), _user(db, "worker"),
        _user(db, "outside"), _user(db, "platform", "admin"),
    )
    company = org.create_enterprise_from_paid_order(
        db, owner_id=owner.id, name="企业甲", seat_limit=4, paid_order_id="p"
    )
    for user in (leader, worker):
        org.accept_invitation(db, org.create_invitation(db, company.id, owner.id), user.id)
    group = org.create_group(db, company.id, owner.id, "研发")
    org.set_group_member(db, company.id, group.id, owner.id, leader.id, "leader")
    org.set_group_member(db, company.id, group.id, owner.id, worker.id, "member")
    project = Project(user_id=owner.id, project_name="受限项目")
    db.add(project)
    db.flush()
    link = EnterpriseProject(enterprise_id=company.id, project_id=project.id, group_id=group.id, status="active")
    db.add(link)
    db.flush()
    return company, project, owner, leader, worker, outsider, platform_admin


def test_enterprise_roles_scope_project_access_to_the_bound_group(db, context):
    company, project, owner, leader, worker, outsider, platform_admin = context
    assert require_enterprise_project_access(db, company.id, project.id, owner.id, "manage") == "admin"
    assert require_enterprise_project_access(db, company.id, project.id, leader.id, "manage") == "leader"
    assert require_enterprise_project_access(db, company.id, project.id, worker.id, "execute") == "reviewer"
    for user in (outsider, platform_admin):
        with pytest.raises(ForbiddenError):
            require_enterprise_project_access(db, company.id, project.id, user.id, "view")


@pytest.mark.parametrize("role,allowed,denied", [
    ("viewer", ("view",), ("execute", "write", "manage")),
    ("reviewer", ("view", "execute"), ("write", "manage")),
    ("editor", ("view", "execute", "write"), ("manage",)),
])
def test_project_explicit_grant_action_matrix(db, context, role, allowed, denied):
    company, project, _, _, worker, _, _ = context
    db.add(EnterpriseProjectGrant(enterprise_id=company.id, project_id=project.id, user_id=worker.id,
                                  role=role, status="active"))
    db.flush()
    for action in allowed:
        assert require_enterprise_project_access(db, company.id, project.id, worker.id, action) == role
    for action in denied:
        with pytest.raises(ForbiddenError):
            require_enterprise_project_access(db, company.id, project.id, worker.id, action)


def test_removed_member_immediately_loses_access_even_with_grant(db, context):
    company, project, owner, _, worker, _, _ = context
    db.add(EnterpriseProjectGrant(enterprise_id=company.id, project_id=project.id, user_id=worker.id,
                                  role="editor", status="active"))
    db.flush()
    org.remove_enterprise_member(db, company.id, owner.id, worker.id)
    grant = db.query(EnterpriseProjectGrant).filter_by(
        enterprise_id=company.id, project_id=project.id, user_id=worker.id,
    ).one()
    assert grant.status == "inactive"
    with pytest.raises(ForbiddenError):
        require_enterprise_project_access(db, company.id, project.id, worker.id, "view")


def test_removed_and_reinvited_member_does_not_recover_old_project_grants(db, context):
    company, project, owner, _, worker, *_ = context
    enterprise_projects.set_project_grant(
        db, company.id, project.id, worker.id, actor_id=owner.id, role="editor",
    )

    org.remove_enterprise_member(db, company.id, owner.id, worker.id)
    org.accept_invitation(db, org.create_invitation(db, company.id, owner.id), worker.id)
    group_id = db.query(EnterpriseGroup.id).filter_by(enterprise_id=company.id).scalar()
    org.set_group_member(db, company.id, group_id, owner.id, worker.id, "member")

    grant = db.query(EnterpriseProjectGrant).filter_by(
        enterprise_id=company.id, project_id=project.id, user_id=worker.id,
    ).one()
    assert grant.status == "inactive"
    assert require_enterprise_project_access(db, company.id, project.id, worker.id, "execute") == "reviewer"
    with pytest.raises(ForbiddenError):
        require_enterprise_project_access(db, company.id, project.id, worker.id, "write")


def test_project_in_wrong_company_denied_and_management_routes_are_exposed(db, context):
    company, project, owner, *_ = context
    with pytest.raises(NotFoundError):
        require_enterprise_project_access(db, company.id + 999, project.id, owner.id, "view")
    from app.api.v1.enterprise_projects import router

    assert any(
        route.path == "/enterprises/{enterprise_id}/projects" and "POST" in route.methods
        for route in router.routes
    )


@pytest.mark.parametrize("state", ["authorized", "running"])
def test_binding_rejects_active_personal_pentest_until_cancelled(db, context, state):
    company, _, owner, *_ = context
    group_id = db.query(EnterpriseGroup.id).filter_by(enterprise_id=company.id).scalar()
    project = Project(user_id=owner.id, project_name=f"待绑定-{state}", status="active")
    db.add(project)
    db.flush()
    engagement = PentestEngagement(
        public_id=f"test-bind-{state}", project_id=project.id, user_id=owner.id,
        target_type="web", scope_json="{}", status=state, rules_ack=1,
    )
    db.add(engagement)
    db.flush()
    with pytest.raises(ConflictError, match="个人渗透测试"):
        enterprise_projects.bind_owned_project(db, company.id, project.id, group_id=group_id, actor_id=owner.id)
    assert db.query(EnterpriseProject).filter_by(project_id=project.id).first() is None
    engagement.status = "cancelled"
    db.flush()
    result = enterprise_projects.bind_owned_project(db, company.id, project.id, group_id=group_id, actor_id=owner.id)
    assert result["project_id"] == project.id


def test_binding_project_retires_existing_personal_knowledge_copies(db, context):
    """项目归入企业时，同一事务移除旧个人知识库中的源码和项目问题副本。"""
    company, _, owner, _, worker, *_ = context
    group_id = db.query(EnterpriseProject.group_id).filter_by(
        enterprise_id=company.id,
    ).scalar()
    project = Project(user_id=owner.id, project_name="待绑定项目", status="active")
    db.add(project)
    db.flush()
    code_file = CodeFile(
        project_id=project.id,
        file_name="secret.py",
        file_path="src/secret.py",
        language="python",
        size_bytes=12,
        line_count=1,
        content="company source",
        status="active",
    )
    task = ReviewTask(
        user_id=worker.id,
        project_id=project.id,
        task_name="已完成审查",
        review_type="standard",
        status="success",
    )
    db.add_all([code_file, task])
    db.flush()
    issue = ReviewIssue(
        task_id=task.id,
        issue_type="security",
        severity="high",
        title="公司漏洞",
        description="公司问题正文",
        status="fixed",
    )
    db.add(issue)
    db.flush()
    docs = [
        KnowledgeDoc(user_id=owner.id, source_type="code", source_ref=f"file:{code_file.id}",
                     title="源码副本", char_count=13, chunk_count=1, status="active"),
        KnowledgeDoc(user_id=worker.id, source_type="issue", source_ref=f"issue:{issue.id}",
                     title="问题副本", char_count=13, chunk_count=1, status="active"),
    ]
    db.add_all(docs)
    db.flush()
    db.add_all([
        KnowledgeChunk(doc_id=doc.id, user_id=doc.user_id, seq=0, content="company secret",
                       embedding="[1.0]", embed_model="test:model")
        for doc in docs
    ])
    agent_source = AgentKnowledgeSource(
        agent_code="manager",
        source_type="project",
        source_uri=str(project.id),
        whitelist=1,
        enabled=1,
        config_json=f'{{"project_id": {project.id}}}',
    )
    agent_doc = AgentKnowledgeDoc(
        agent_code="manager",
        source_type="project",
        source_ref=f"code_file:{code_file.id}",
        title="旧全局 Agent 源码副本",
        status="active",
        risk_level="low",
        confidence=1.0,
        char_count=13,
        chunk_count=1,
    )
    db.add(agent_source)
    db.flush()
    agent_doc.source_id = agent_source.id
    db.add(agent_doc)
    db.flush()
    agent_chunk = AgentKnowledgeChunk(
        doc_id=agent_doc.id,
        agent_code="manager",
        seq=0,
        content="company source secret",
        embedding="[1.0]",
        embed_model="test:model",
    )
    db.add(agent_chunk)
    manual_agent_doc = AgentKnowledgeDoc(
        agent_code="manager",
        source_type="manual",
        source_ref=f"project:{project.id}",
        title="手动知识与项目引用同名",
        status="active",
        risk_level="low",
        confidence=1.0,
        char_count=14,
        chunk_count=1,
    )
    db.add(manual_agent_doc)
    db.flush()
    manual_agent_chunk = AgentKnowledgeChunk(
        doc_id=manual_agent_doc.id,
        agent_code="manager",
        seq=0,
        content="must remain manual knowledge",
        embedding="[1.0]",
        embed_model="test:model",
    )
    db.add(manual_agent_chunk)
    db.commit()

    enterprise_projects.bind_owned_project(
        db, company.id, project.id, group_id=group_id, actor_id=owner.id,
    )

    for doc in docs:
        db.refresh(doc)
    assert all(doc.status == "deleted" for doc in docs)
    assert db.query(KnowledgeChunk).filter(KnowledgeChunk.doc_id.in_([doc.id for doc in docs])).count() == 0
    db.refresh(agent_doc)
    db.refresh(agent_source)
    assert agent_doc.status == "deleted"
    assert db.query(AgentKnowledgeChunk).filter_by(doc_id=agent_doc.id).count() == 0
    assert agent_source.enabled == 0
    assert agent_source.whitelist == 0
    db.refresh(manual_agent_doc)
    assert manual_agent_doc.status == "active"
    assert db.query(AgentKnowledgeChunk).filter_by(doc_id=manual_agent_doc.id).count() == 1


def test_suspended_company_keeps_read_and_edit_but_denies_execution(db, context):
    company, project, owner, *_ = context
    company.status = "suspended"
    db.flush()
    assert require_enterprise_project_access(db, company.id, project.id, owner.id, "view") == "admin"
    assert require_enterprise_project_access(db, company.id, project.id, owner.id, "write") == "admin"
    with pytest.raises(ForbiddenError):
        require_enterprise_project_access(db, company.id, project.id, owner.id, "execute")


def test_billing_suspension_or_expiry_blocks_execution_for_owner_and_admin(db, context, monkeypatch):
    company, project, owner, *_ = context
    admin = _user(db, "company_admin")
    org.accept_invitation(db, org.create_invitation(db, company.id, owner.id), admin.id)
    org.set_enterprise_role(db, company.id, owner.id, admin.id, "admin")

    company.billing_suspended = True
    db.flush()
    for actor in (owner, admin):
        assert require_enterprise_project_access(db, company.id, project.id, actor.id, "view") == "admin"
        with pytest.raises(ForbiddenError, match="计费"):
            require_enterprise_project_access(db, company.id, project.id, actor.id, "execute")

    company.billing_suspended = False
    db.flush()
    from app.services import billing_checkout_service

    monkeypatch.setattr(billing_checkout_service, "enterprise_subscription_status", lambda *_args, **_kwargs: "expired")
    for actor in (owner, admin):
        assert require_enterprise_project_access(db, company.id, project.id, actor.id, "view") == "admin"
        with pytest.raises(ForbiddenError, match="订阅已过期"):
            require_enterprise_project_access(db, company.id, project.id, actor.id, "execute")

    monkeypatch.setattr(billing_checkout_service, "enterprise_subscription_status", lambda *_args, **_kwargs: "none")
    monkeypatch.setattr(billing_checkout_service, "enterprise_subscription_refund_pending", lambda *_args: True)
    for actor in (owner, admin):
        with pytest.raises(ForbiddenError, match="计费已暂停"):
            require_enterprise_project_access(db, company.id, project.id, actor.id, "execute")


def test_legacy_project_gateway_rejects_platform_admin_bypass_and_lists_company_members_only(db, context):
    company, project, owner, leader, worker, outsider, platform_admin = context
    assert is_project_member(db, project.id, platform_admin) == (False, "")
    assert project.id not in get_visible_project_ids(db, platform_admin)[0]
    assert project.id in get_visible_project_ids(db, owner)[0]
    assert project.id in get_visible_project_ids(db, leader)[0]
    assert project.id in get_visible_project_ids(db, worker)[0]
    assert project.id not in get_visible_project_ids(db, outsider)[0]
    db.add(EnterpriseProjectGrant(enterprise_id=company.id, project_id=project.id, user_id=worker.id,
                                  role="viewer", status="active"))
    db.flush()
    assert project.id in get_visible_project_ids(db, worker)[0]
    assert require_project_access(db, project.id, worker) == "enterprise_viewer"


def test_project_owner_can_bind_owned_project_to_enterprise_group_and_grant_editor(db, context):
    company, _, owner, leader, worker, *_ = context
    project = Project(user_id=worker.id, project_name="新绑定项目", language="python")
    db.add(project)
    db.flush()
    group_id = db.query(EnterpriseGroup.id).filter_by(enterprise_id=company.id).scalar()

    result = enterprise_projects.bind_owned_project(
        db, company.id, project.id, group_id=group_id, actor_id=worker.id,
    )

    assert result["project_id"] == project.id
    assert require_enterprise_project_access(db, company.id, project.id, worker.id, "write") == "editor"
    assert require_enterprise_project_access(db, company.id, project.id, owner.id, "manage") == "admin"
    assert require_enterprise_project_access(db, company.id, project.id, leader.id, "manage") == "leader"


def test_project_binding_rejects_non_owner_duplicate_and_out_of_group_actor(db, context):
    company, project, owner, leader, worker, outsider, *_ = context
    group_id = db.query(EnterpriseGroup.id).filter_by(enterprise_id=company.id).scalar()
    another = Project(user_id=owner.id, project_name="另一项目")
    db.add(another)
    db.flush()
    with pytest.raises(ForbiddenError):
        enterprise_projects.bind_owned_project(db, company.id, another.id, group_id=group_id, actor_id=leader.id)
    with pytest.raises(ForbiddenError):
        enterprise_projects.bind_owned_project(db, company.id, another.id, group_id=group_id, actor_id=outsider.id)
    with pytest.raises(ConflictError):
        enterprise_projects.bind_owned_project(db, company.id, project.id, group_id=group_id, actor_id=owner.id)


def test_project_grant_is_audited_revocable_and_cannot_cross_company(db, context):
    company, project, owner, _, worker, outsider, _ = context

    row = enterprise_projects.set_project_grant(
        db, company.id, project.id, worker.id, actor_id=owner.id, role="viewer",
    )
    assert row.role == "viewer"
    assert require_enterprise_project_access(db, company.id, project.id, worker.id, "view") == "viewer"
    with pytest.raises((ForbiddenError, NotFoundError)):
        enterprise_projects.set_project_grant(
            db, company.id, project.id, outsider.id, actor_id=owner.id, role="viewer",
        )
    with pytest.raises((ForbiddenError, NotFoundError)):
        enterprise_projects.set_project_grant(
            db, company.id + 100, project.id, worker.id, actor_id=owner.id, role="editor",
        )

    enterprise_projects.revoke_project_grant(db, company.id, project.id, worker.id, actor_id=owner.id)
    assert require_enterprise_project_access(db, company.id, project.id, worker.id, "execute") == "reviewer"


def test_enterprise_project_members_can_read_group_reports_but_personal_reports_stay_author_only(
    db, context, monkeypatch,
):
    company, project, owner, _, worker, outsider, _ = context
    task = ReviewTask(
        user_id=owner.id,
        project_id=project.id,
        review_type="standard",
        status="success",
        total_files=1,
        processed_files=1,
        score=90,
    )
    db.add(task)
    db.flush()
    monkeypatch.setattr(report_service, "is_report_available", lambda row: row is not None)
    monkeypatch.setattr(report_service, "has_source_report_permission", lambda *_: True)
    monkeypatch.setattr(report_service, "_standard_report_permission_filter", lambda *_: True)
    monkeypatch.setattr(report_service.rbac_service, "check_permission", lambda *_: True)
    monkeypatch.setattr(report_service.rbac_service, "is_admin_user", lambda *_: False)

    assert report_service.get_readable_report_task(db, worker, task.id).id == task.id
    assert report_service.list_reports(db, worker)["items"][0]["can_view_report"] is True
    with pytest.raises(NotFoundError):
        report_service.get_readable_report_task(db, outsider, task.id)


def test_report_list_shared_enterprise_report_does_not_offer_delete_to_other_authors(db, context, monkeypatch):
    company, project, owner, _, worker, *_ = context
    task = ReviewTask(
        user_id=owner.id,
        project_id=project.id,
        review_type="standard",
        status="success",
        total_files=1,
        processed_files=1,
        score=90,
    )
    db.add(task)
    db.flush()
    monkeypatch.setattr(report_service, "has_source_report_permission", lambda *_: True)
    monkeypatch.setattr(report_service, "_standard_report_permission_filter", lambda *_: True)
    monkeypatch.setattr(report_service.rbac_service, "check_permission", lambda *_: True)
    monkeypatch.setattr(report_service.rbac_service, "is_admin_user", lambda *_: False)

    item = report_service.list_reports(db, worker)["items"][0]

    assert item["can_view_report"] is True
    assert item["can_delete"] is False
    assert require_project_execution(db, project.id, worker) == "enterprise_reviewer"
    with pytest.raises(ForbiddenError):
        require_project_access(db, project.id, worker, need_write=True)


def test_legacy_project_membership_mutations_cannot_bypass_company_grant_policy(db, context):
    _, project, owner, _, _, outsider, _ = context
    with pytest.raises(ServiceUnavailableError):
        add_member(db, project.id, outsider.id, operator=owner)


def test_personal_project_legacy_access_remains_unchanged(db, context):
    _, _, owner, _, _, _, platform_admin = context
    personal = Project(user_id=owner.id, project_name="个人项目")
    db.add(personal)
    db.flush()
    assert is_project_member(db, personal.id, owner) == (True, "owner")
    assert is_project_member(db, personal.id, platform_admin) == (True, "admin")
    assert personal.id in get_visible_project_ids(db, owner)[0]


def test_project_listing_capabilities_follow_enterprise_grant_not_personal_owner(db, context):
    company, project, _, _, worker, *_ = context
    project.user_id = worker.id
    db.add(EnterpriseProjectGrant(enterprise_id=company.id, project_id=project.id, user_id=worker.id,
                                  role="viewer", status="active"))
    db.flush()
    items = project_service.list_projects(db, worker)["items"]
    item = next(row for row in items if row["id"] == project.id)
    assert not item["can_update"]
    assert not item["can_delete"]
    assert not item["can_execute"]


def test_enterprise_project_editor_cannot_delete_project(db, context):
    company, project, _, _, worker, *_ = context
    db.add(EnterpriseProjectGrant(enterprise_id=company.id, project_id=project.id, user_id=worker.id,
                                  role="editor", status="active"))
    db.flush()
    assert require_project_access(db, project.id, worker, need_write=True) == "enterprise_editor"
    with pytest.raises(ForbiddenError):
        project_service.delete_project(db, worker, project.id)


@pytest.mark.parametrize("status", ["archived", "active"])
def test_enterprise_project_editor_cannot_change_lifecycle_status(db, context, status):
    company, project, _, _, worker, *_ = context
    db.add(EnterpriseProjectGrant(enterprise_id=company.id, project_id=project.id, user_id=worker.id,
                                  role="editor", status="active"))
    db.flush()
    with pytest.raises(ForbiddenError):
        project_service.update_project(db, worker, project.id, ProjectUpdateIn(status=status))
    db.refresh(project)
    assert project.status == "active"


def test_report_detail_rechecks_enterprise_membership_for_platform_admin(db, context):
    _, project, owner, _, _, _, platform_admin = context
    task = ReviewTask(user_id=owner.id, project_id=project.id, task_name="项目审查",
                      review_type="standard", status="success", total_files=1, processed_files=1, score=88)
    db.add(task)
    db.flush()
    with pytest.raises(NotFoundError):
        report_service.get_readable_report_task(db, platform_admin, task.id)


def test_legacy_pentest_endpoint_cannot_list_read_or_approve_enterprise_engagement(db, context, monkeypatch):
    _, project, owner, _, _, _, platform_admin = context
    engagement = PentestEngagement(
        public_id="enterprise-fixture", user_id=owner.id, project_id=project.id,
        target_type="web", scope_json="{}", status="pending_authorization", rules_ack=1,
    )
    db.add(engagement)
    db.flush()
    monkeypatch.setattr(pentest_service.settings, "pentest_enabled", True)
    assert pentest_service.list_engagements(db, platform_admin, scope="all")["total"] == 0
    with pytest.raises(pentest_service.PentestNotFoundError):
        pentest_service._engagement_or_raise(db, platform_admin, engagement.public_id, allow_authorizer=True)
    with pytest.raises(pentest_service.PentestNotFoundError):
        pentest_service.authorize_engagement(db, platform_admin, engagement.public_id)
    with pytest.raises(pentest_service.PentestNotFoundError):
        pentest_service.start_engagement(db, platform_admin, engagement.public_id)
    with pytest.raises(pentest_service.PentestNotFoundError):
        pentest_service.create_engagement(db, owner, {"target_type": "web", "project_id": project.id})


def test_agent_team_project_scope_rechecks_enterprise_access(db, context):
    company, project, owner, _, worker, _, platform_admin = context
    assert require_scoped_project_execution(db, owner, {"project_id": project.id}) == {project.id}
    with pytest.raises(NotFoundError):
        require_scoped_project_execution(db, platform_admin, {"project_id": project.id})
    db.add(EnterpriseProjectGrant(enterprise_id=company.id, project_id=project.id, user_id=worker.id,
                                  role="reviewer", status="active"))
    db.flush()
    assert require_scoped_project_execution(db, worker, {"project_id": project.id}) == {project.id}
    org.remove_enterprise_member(db, company.id, owner.id, worker.id)
    with pytest.raises(NotFoundError):
        require_scoped_project_execution(db, worker, {"project_id": project.id})
