"""企业远测审批进入同一只读远测执行器，且启动前重新验证企业授权。"""
import json

import pytest
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from test_enterprise_chat_service import orgdb as base_orgdb

from app.api.v1.enterprise_remote_approval import router
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import AppError, ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.models.enterprise_org import EnterpriseAuditEvent, EnterpriseGroup, EnterpriseGroupMember, EnterpriseMember
from app.models.enterprise_project import EnterpriseProject, EnterpriseProjectGrant
from app.models.enterprise_remote_approval import EnterpriseRemoteApproval
from app.models.project import Project
from app.models.remote_pentest import (
    RemotePentestEngagement,
    RemotePentestFinding,
    RemotePentestPhaseRun,
    RemotePentestReport,
)
from app.models.user import User
from app.services import enterprise_remote_approval_service as service
from app.services import remote_pentest_service

orgdb = base_orgdb


@pytest.fixture
def remote_db(orgdb):
    EnterpriseRemoteApproval.__table__.create(bind=orgdb.get_bind())
    RemotePentestEngagement.__table__.create(bind=orgdb.get_bind())
    RemotePentestFinding.__table__.create(bind=orgdb.get_bind())
    RemotePentestPhaseRun.__table__.create(bind=orgdb.get_bind())
    RemotePentestReport.__table__.create(bind=orgdb.get_bind())
    Project.__table__.create(bind=orgdb.get_bind())
    EnterpriseProject.__table__.create(bind=orgdb.get_bind())
    EnterpriseProjectGrant.__table__.create(bind=orgdb.get_bind())
    orgdb.add(Project(id=500, user_id=2, project_name="企业远测项目", status="active"))
    orgdb.add(EnterpriseProject(
        enterprise_id=10, project_id=500, group_id=100, status="active",
    ))
    orgdb.add(EnterpriseGroupMember(enterprise_id=10, group_id=100, user_id=3, role="leader", status="active"))
    orgdb.commit()
    yield orgdb
    remote_pentest_service._PIPELINES.clear()


def _submit(db, user_id=2, group_id=100, enterprise_id=10):
    return service.submit_request(db, enterprise_id, group_id, user_id, project_id=500,
                                  target_url="https://EXAMPLE.com/",
                                  authorization_basis="已取得目标资产的明确书面授权", scope_notes="仅检查根路径",
                                  requested_window_hours=4, ack_rules=True)


def test_group_leader_can_approve_member_but_not_self_or_other_group(remote_db):
    request = _submit(remote_db)
    assert request.target_url == "https://example.com/" and request.status == "pending"
    assert request.scope_version == "enterprise-remote-v1"
    approved = service.decide_request(remote_db, 10, request.id, 3, decision="approved", reason="已核验")
    assert approved.status == "approved" and approved.decided_by == 3
    assert approved.execution_task_public_id
    task = remote_db.query(RemotePentestEngagement).filter_by(public_id=approved.execution_task_public_id).one()
    assert task.status == "authorized" and task.owner_id == request.owner_id
    assert task.enterprise_approval_required is True
    assert task.authorized_by_user_id == approved.decided_by
    own = _submit(remote_db, user_id=3)
    with pytest.raises(ForbiddenError):
        service.decide_request(remote_db, 10, own.id, 3, decision="approved", reason="自批")
    assert own.status == "pending" and own.execution_task_public_id is None
    own_actions = [
        row.action
        for row in remote_db.query(EnterpriseAuditEvent).filter_by(subject_user_id=3).all()
    ]
    assert own_actions == ["remote_approval_submitted"]
    with pytest.raises(ForbiddenError):
        service.decide_request(remote_db, 10, own.id, 2, decision="approved", reason="无权")
    with pytest.raises(NotFoundError):
        service.decide_request(remote_db, 20, own.id, 4, decision="approved", reason="跨企业")
    remote_db.add_all([
        EnterpriseGroup(id=101, enterprise_id=10, name="其他组", status="active"),
        Project(id=501, user_id=2, project_name="第二小组远测项目", status="active"),
        EnterpriseProject(enterprise_id=10, project_id=501, group_id=101, status="active"),
        EnterpriseMember(enterprise_id=10, user_id=4, role="member", status="active"),
        EnterpriseGroupMember(enterprise_id=10, group_id=101, user_id=2, role="member", status="active"),
        EnterpriseGroupMember(enterprise_id=10, group_id=101, user_id=4, role="leader", status="active"),
    ])
    remote_db.commit()
    second_group_request = service.submit_request(
        remote_db,
        10,
        101,
        2,
        project_id=501,
        target_url="https://second.example.com/",
        authorization_basis="已取得第二测试域名的明确书面授权",
        scope_notes="仅检查根路径",
        requested_window_hours=4,
        ack_rules=True,
    )
    with pytest.raises(ForbiddenError):
        service.decide_request(remote_db, 10, second_group_request.id, 3,
                               decision="approved", reason="其他小组组长不得审批")
    remote_db.refresh(second_group_request)
    assert second_group_request.status == "pending"
    approved_by_second_leader = service.decide_request(
        remote_db, 10, second_group_request.id, 4,
        decision="approved", reason="本组项目范围已核验",
    )
    assert approved_by_second_leader.status == "approved"
    assert approved_by_second_leader.decided_by == 4


@pytest.mark.parametrize("enterprise_role", ["owner", "admin"])
def test_enterprise_owner_and_admin_can_self_approve_with_live_role_recheck(remote_db, enterprise_role):
    member = remote_db.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=1).one()
    member.role = enterprise_role
    # Keep the former owner/admin inside the applicant's group after demotion,
    # so the execution denial must come from the live approver-role check.
    remote_db.add(EnterpriseGroupMember(
        enterprise_id=10, group_id=100, user_id=1, role="member", status="active",
    ))
    remote_db.commit()

    own = _submit(remote_db, user_id=1)
    approved = service.decide_request(
        remote_db, 10, own.id, 1, decision="approved", reason="企业管理角色已核验范围"
    )
    assert approved.status == "approved" and approved.decided_by == 1
    assert approved.execution_task_public_id
    task = remote_db.query(RemotePentestEngagement).filter_by(public_id=approved.execution_task_public_id).one()
    assert task.status == "authorized" and task.owner_id == task.authorized_by_user_id == 1
    assert service.require_execution_authorization(remote_db, task, 1) is True
    active = remote_pentest_service.find_active_authorization(remote_db, 1, "https://example.com/")
    assert active is not None and active.public_id == task.public_id

    # Approval is not a permanent role grant: a demoted owner/admin loses the
    # ability to launch the still-pending authorized execution.
    member.role = "member"
    remote_db.commit()
    with pytest.raises(ForbiddenError):
        service.require_execution_authorization(remote_db, task, 1)
    assert remote_pentest_service.find_active_authorization(remote_db, 1, "https://example.com/") is None

    actions = [row.action for row in remote_db.query(EnterpriseAuditEvent).filter_by(subject_user_id=1).all()]
    assert actions == ["remote_approval_submitted", "remote_approval_approved"]


def test_personal_reviewer_role_does_not_grant_enterprise_approval(remote_db):
    reviewer = User(id=5, username="personal-reviewer", password="x", role="reviewer", status=1)
    remote_db.add(reviewer)
    remote_db.commit()
    request = _submit(remote_db)

    with pytest.raises(ForbiddenError):
        service.decide_request(remote_db, 10, request.id, reviewer.id, decision="approved", reason="个人审查员")

    remote_db.refresh(request)
    assert request.status == "pending" and request.execution_task_public_id is None


def test_identical_pending_submission_is_idempotent(remote_db):
    first = _submit(remote_db)
    replay = _submit(remote_db)
    assert replay.id == first.id
    assert remote_db.query(EnterpriseRemoteApproval).filter_by(owner_id=2, status="pending").count() == 1


def test_remote_approval_and_execution_require_same_active_enterprise_project(remote_db):
    request = _submit(remote_db)
    binding = remote_db.query(EnterpriseProject).filter_by(project_id=500).one()
    binding.status = "inactive"
    remote_db.commit()
    with pytest.raises(ForbiddenError, match="项目"):
        service.decide_request(remote_db, 10, request.id, 3, decision="approved", reason="项目已停用")
    assert request.status == "pending"
    assert request.execution_task_public_id is None

    remote_db.rollback()
    binding.status = "active"
    remote_db.commit()
    approved = service.decide_request(remote_db, 10, request.id, 3, decision="approved", reason="范围已核验")
    task = remote_db.query(RemotePentestEngagement).filter_by(public_id=approved.execution_task_public_id).one()
    assert request.project_id == task.project_id == 500

    binding.status = "inactive"
    remote_db.commit()
    with pytest.raises(ForbiddenError):
        service.require_execution_authorization(remote_db, task, 2)
    with pytest.raises(NotFoundError):
        remote_pentest_service.get_task(remote_db, remote_db.get(User, 2), task.public_id)


def test_enterprise_project_report_is_visible_to_current_project_members_only(remote_db):
    request = _submit(remote_db)
    approved = service.decide_request(remote_db, 10, request.id, 3, decision="approved", reason="范围已核验")
    task = remote_db.query(RemotePentestEngagement).filter_by(public_id=approved.execution_task_public_id).one()
    remote_db.add(RemotePentestReport(
        public_id="enterprise-report",
        engagement_id=task.id,
        owner_id=task.owner_id,
        scope_digest=task.scope_digest,
        status="completed",
        content_json=json.dumps({"score": {"score": 90}}),
        generated_at=service._now(),
    ))
    remote_db.commit()

    assert remote_pentest_service.get_report(remote_db, remote_db.get(User, 3), task.public_id)["public_id"] == (
        "enterprise-report"
    )
    with pytest.raises(NotFoundError):
        remote_pentest_service.get_report(remote_db, remote_db.get(User, 4), task.public_id)


def test_reject_and_invalid_requests(remote_db, monkeypatch):
    import socket

    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: pytest.fail("不得解析或探测目标"))
    request = _submit(remote_db)
    rejected = service.decide_request(remote_db, 10, request.id, 3, decision="rejected", reason="资产证明不足")
    assert rejected.status == "rejected" and rejected.decision_reason == "资产证明不足"
    with pytest.raises(ConflictError):
        service.decide_request(remote_db, 10, request.id, 1, decision="approved", reason="重复")
    with pytest.raises(ValidationError):
        service.submit_request(remote_db, 10, 100, 2, project_id=500, target_url="https://example.com/path",
                               authorization_basis="已取得目标资产的明确书面授权", scope_notes="",
                               requested_window_hours=4, ack_rules=True)
    with pytest.raises(ValidationError):
        service.submit_request(remote_db, 10, 100, 2, project_id=500, target_url="https://example.com/",
                               authorization_basis="已取得目标资产的明确书面授权", scope_notes="",
                               requested_window_hours=4, ack_rules=False)


def test_approval_requires_current_applicant_membership_and_unchanged_scope(remote_db):
    request = _submit(remote_db)
    member = remote_db.query(EnterpriseGroupMember).filter_by(enterprise_id=10, group_id=100, user_id=2).one()
    member.status = "removed"
    remote_db.commit()
    with pytest.raises(ForbiddenError):
        service.decide_request(remote_db, 10, request.id, 1, decision="approved", reason="成员已移除")
    remote_db.rollback()
    member.status = "active"
    request.scope_notes = "未经授权的变更"
    remote_db.commit()
    with pytest.raises(ConflictError):
        service.decide_request(remote_db, 10, request.id, 1, decision="approved", reason="范围变化")


def test_revoked_group_lead_and_disabled_account_cannot_approve(remote_db):
    request = _submit(remote_db)
    leader = remote_db.query(EnterpriseGroupMember).filter_by(enterprise_id=10, group_id=100, user_id=3).one()
    leader.status = "removed"
    remote_db.commit()
    with pytest.raises(ForbiddenError):
        service.decide_request(remote_db, 10, request.id, 3, decision="approved", reason="已移除")
    remote_db.rollback()
    leader.status = "active"
    remote_db.get(User, 3).status = 0
    remote_db.commit()
    with pytest.raises(ForbiddenError):
        service.decide_request(remote_db, 10, request.id, 3, decision="approved", reason="账号禁用")


def test_visibility_is_enterprise_and_group_scoped_and_cancel_is_owner_only(remote_db):
    mine = _submit(remote_db, user_id=2)
    own_lead = _submit(remote_db, user_id=3)
    assert {r.id for r in service.list_requests(remote_db, 10, 2)[0]} == {mine.id}
    assert {r.id for r in service.list_requests(remote_db, 10, 3)[0]} == {mine.id, own_lead.id}
    with pytest.raises(NotFoundError):
        service.get_request(remote_db, 20, mine.id, 4)
    with pytest.raises(ForbiddenError):
        service.cancel_request(remote_db, 10, mine.id, 3)
    assert service.cancel_request(remote_db, 10, mine.id, 2).status == "cancelled"
    assert {r.id for r in service.list_requests(remote_db, 10, 3)[0]} == {mine.id, own_lead.id}
    member = remote_db.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=2).one()
    member.status = "removed"
    remote_db.commit()
    with pytest.raises(ForbiddenError):
        service.get_request(remote_db, 10, mine.id, 2)


def test_http_starts_approved_scope_only_through_existing_executor(remote_db, monkeypatch):
    app = FastAPI()
    app.include_router(router)

    class ThreadStub:
        def __init__(self, *args, **kwargs):
            self.started = False

        def is_alive(self):
            return False

        def start(self):
            self.started = True

    monkeypatch.setattr(remote_pentest_service.threading, "Thread", ThreadStub)

    @app.exception_handler(AppError)
    async def on_error(_, exc):
        return JSONResponse(status_code=exc.http_status, content={"message": exc.message})

    app.dependency_overrides[get_db] = lambda: remote_db
    app.dependency_overrides[get_current_user] = lambda x_test_user=Header(): remote_db.get(User, int(x_test_user))
    payload = {"group_id": 100, "project_id": 500, "target_url": "https://example.com/",
               "authorization_basis": "已取得目标资产的明确书面授权",
               "scope_notes": "仅检查根路径", "requested_window_hours": 4, "ack_rules": True}
    with TestClient(app) as client:
        missing_project = {key: value for key, value in payload.items() if key != "project_id"}
        assert client.post("/enterprises/10/remote-test-requests", json=missing_project,
                           headers={"x-test-user": "2"}).status_code == 422
        response = client.post("/enterprises/10/remote-test-requests", json=payload, headers={"x-test-user": "2"})
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["execution_available"] is False
        request_id = data["id"]
        assert client.get(f"/enterprises/20/remote-test-requests/{request_id}",
                          headers={"x-test-user": "4"}).status_code == 404
        approve = client.post(f"/enterprises/10/remote-test-requests/{request_id}/decision",
                              json={"decision": "approved", "reason": "审核通过"}, headers={"x-test-user": "3"})
        assert approve.status_code == 200 and approve.json()["data"]["execution_available"] is True
        assert approve.json()["data"]["execution_task_public_id"]
        start = client.post(f"/enterprises/10/remote-test-requests/{request_id}/start",
                            headers={"x-test-user": "2"})
        assert start.status_code == 200
        assert start.json()["data"]["execution"]["status"] == "running"

        owner_submission = client.post(
            "/enterprises/10/remote-test-requests", json=payload, headers={"x-test-user": "1"}
        )
        assert owner_submission.status_code == 200
        owner_request_id = owner_submission.json()["data"]["id"]
        owner_approval = client.post(
            f"/enterprises/10/remote-test-requests/{owner_request_id}/decision",
            json={"decision": "approved", "reason": "owner 已核验固定范围"},
            headers={"x-test-user": "1"},
        )
        assert owner_approval.status_code == 200
        assert owner_approval.json()["data"]["decided_by"] == 1
        owner_start = client.post(
            f"/enterprises/10/remote-test-requests/{owner_request_id}/start",
            headers={"x-test-user": "1"},
        )
        assert owner_start.status_code == 200
        assert owner_start.json()["data"]["execution"]["status"] == "running"


def test_start_rechecks_current_approver_membership_and_scope(remote_db):
    request = _submit(remote_db)
    service.decide_request(remote_db, 10, request.id, 3, decision="approved", reason="已核验")
    task = remote_db.query(RemotePentestEngagement).filter_by(public_id=request.execution_task_public_id).one()
    leader = remote_db.query(EnterpriseGroupMember).filter_by(enterprise_id=10, group_id=100, user_id=3).one()
    leader.status = "removed"
    remote_db.commit()
    with pytest.raises(ForbiddenError):
        service.require_execution_authorization(remote_db, task, 2)
    remote_db.rollback()
    leader.status = "active"
    task.target_url = "https://attacker.example/"
    remote_db.commit()
    with pytest.raises(ForbiddenError, match="快照不一致"):
        service.require_execution_authorization(remote_db, task, 2)


def test_pipeline_rechecks_enterprise_membership_between_remote_phases(remote_db, monkeypatch):
    request = _submit(remote_db)
    service.decide_request(remote_db, 10, request.id, 3, decision="approved", reason="已核验")
    task = remote_db.query(RemotePentestEngagement).filter_by(public_id=request.execution_task_public_id).one()

    class ThreadStub:
        def __init__(self, *args, **kwargs):
            pass

        def is_alive(self):
            return False

        def start(self):
            pass

    monkeypatch.setattr(remote_pentest_service.threading, "Thread", ThreadStub)
    monkeypatch.setattr(remote_pentest_service.settings, "sandbox_remote_targets_enabled", True)
    monkeypatch.setattr(remote_pentest_service, "SessionLocal", sessionmaker(
        bind=remote_db.get_bind(), expire_on_commit=False,
    ))
    service.start_request(remote_db, 10, request.id, 2)
    remote_db.commit()

    calls = 0

    def remove_applicant_group_access(_url, *, expected_ip=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            applicant_group = remote_db.query(EnterpriseGroupMember).filter_by(
                enterprise_id=10, group_id=100, user_id=2,
            ).one()
            applicant_group.status = "removed"
            remote_db.commit()
        return {
            "status_code": 200,
            "latency_ms": 2,
            "content_type": "text/html",
            "server_header_present": False,
            "security_headers": {},
            "response_body_saved": False,
            "redirect_followed": False,
            "resolved_public_target": True,
            "resolved_ip": expected_ip or "203.0.113.42",
        }

    monkeypatch.setattr(remote_pentest_service, "_safe_probe", remove_applicant_group_access)
    remote_pentest_service._run_task(task.id)

    remote_db.refresh(task)
    phases = {phase.phase_key: phase for phase in remote_db.query(RemotePentestPhaseRun)
              .filter_by(engagement_id=task.id).all()}
    assert calls == 1
    assert task.status == "failed"
    assert phases["recon"].status == "success"
    assert phases["threat_model"].status == "skipped"
    assert phases["detect"].status == "skipped"


def test_enterprise_task_cannot_fall_back_to_platform_authorization_if_link_is_missing(remote_db):
    request = _submit(remote_db)
    service.decide_request(remote_db, 10, request.id, 3, decision="approved", reason="已核验")
    task = remote_db.query(RemotePentestEngagement).filter_by(public_id=request.execution_task_public_id).one()
    request.execution_task_public_id = None
    remote_db.commit()

    with pytest.raises(ForbiddenError, match="关联缺失"):
        service.require_execution_authorization(remote_db, task, 2)
