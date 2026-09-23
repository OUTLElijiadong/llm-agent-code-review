"""Agent 发布审批专用接口只能处理与发布包一致的审批单。"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1 import admin_agent_releases, agent_governance
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.core.error_handlers import register_handlers
from app.models.agent_governance import ApprovalItem
from app.models.custom_agent import CustomAgent, CustomAgentRelease, CustomAgentVersion
from app.models.user import User
from app.services import agent_studio_service, approval_service


@pytest.fixture
def release_api():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    admin = User(username="release-admin", password="x", role="admin", status=1)
    reviewer = User(username="release-reviewer", password="x", role="reviewer", status=1)
    db.add_all([admin, reviewer])
    db.commit()

    app = FastAPI()
    register_handlers(app)
    app.include_router(admin_agent_releases.router, prefix="/api/admin/agent-releases")
    app.include_router(agent_governance.router, prefix="/api/agent-governance")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: admin
    with TestClient(app) as client:
        yield client, db, reviewer
    db.close()
    engine.dispose()


def _submitted_package(db, reviewer, code: str) -> ApprovalItem:
    _, version = agent_studio_service.create_agent(
        db,
        reviewer,
        code=code,
        name="权限边界审查员",
        description="检查资源归属与审批边界",
        prompt="仅根据提供的代码核对鉴权与资源归属，并输出有证据的问题。",
        review_focus="鉴权、资源归属与账号隔离",
        model_config={},
    )
    agent_studio_service.test_agent_version(db, reviewer, version.id, {"issues": []})
    return agent_studio_service.submit_agent_version(db, reviewer, version.id, "申请发布")


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_agent_release_endpoint_rejects_other_approval_types(release_api, decision):
    client, db, _ = release_api
    unrelated = ApprovalItem(
        title="其他业务审批",
        action="other.noop",
        resource="other:1",
        risk_level="high",
        status="pending",
        request_json="{}",
    )
    db.add(unrelated)
    db.commit()

    response = client.post(f"/api/admin/agent-releases/{unrelated.id}/{decision}", json={"note": "决定"})

    assert response.status_code == 403
    db.refresh(unrelated)
    assert unrelated.status == "pending"
    assert unrelated.decision is None
    assert db.query(CustomAgentRelease).count() == 0


@pytest.mark.parametrize("corruption", ["resource", "owner", "agent_code", "version"])
@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_agent_release_endpoint_rejects_mismatched_package(release_api, corruption, decision):
    client, db, reviewer = release_api
    approval = _submitted_package(db, reviewer, f"scope_{corruption}_{decision}")
    original_version_id = json.loads(approval.request_json)["agent_version_id"]
    if corruption == "resource":
        approval.resource = "custom_agent_version:999999"
    elif corruption == "owner":
        approval.request_json = json.dumps({"agent_version_id": original_version_id, "owner_id": 999999})
    elif corruption == "agent_code":
        approval.agent_code = "wrong_agent"
    else:
        approval.request_json = json.dumps({"agent_version_id": 999999, "owner_id": reviewer.id})
    db.commit()

    response = client.post(f"/api/admin/agent-releases/{approval.id}/{decision}", json={"note": "决定"})

    assert response.status_code == 409
    db.refresh(approval)
    assert approval.status == "pending"
    assert db.get(CustomAgentVersion, original_version_id).status == "pending_approval"
    assert db.query(CustomAgentRelease).count() == 0


def test_matching_agent_release_can_publish_and_repeat_approval(release_api):
    client, db, reviewer = release_api
    approval = _submitted_package(db, reviewer, "scope_happy_publish")

    first = client.post(f"/api/admin/agent-releases/{approval.id}/approve", json={"note": "内容通过"})
    repeated = client.post(f"/api/admin/agent-releases/{approval.id}/approve", json={"note": "重复请求"})

    assert first.status_code == 200
    assert repeated.status_code == 200
    assert first.json()["data"]["id"] == repeated.json()["data"]["id"]
    assert db.query(CustomAgentRelease).count() == 1
    assert db.query(CustomAgent).filter(CustomAgent.is_enabled == 1).count() == 1


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_generic_governance_decision_cannot_bypass_release_target_check(release_api, decision):
    client, db, reviewer = release_api
    approval = _submitted_package(db, reviewer, f"generic_bypass_{decision}")
    version_id = json.loads(approval.request_json)["agent_version_id"]
    approval.resource = "custom_agent_version:999999"
    db.commit()

    response = client.post(f"/api/agent-governance/approvals/{approval.id}/{decision}", json={"note": "决定"})

    assert response.status_code == 409
    db.refresh(approval)
    assert approval.status == "pending"
    assert db.get(CustomAgentVersion, version_id).status == "pending_approval"
    assert db.query(CustomAgentRelease).count() == 0


def test_publish_service_rejects_mismatched_approval_even_when_called_directly(release_api):
    _, db, reviewer = release_api
    approval = _submitted_package(db, reviewer, "direct_publish_target")
    approval.agent_code = "wrong_agent"
    db.commit()

    from app.core.exceptions import ConflictError

    with pytest.raises(ConflictError):
        agent_studio_service.publish_for_approval(db, approval)
    assert db.query(CustomAgentRelease).count() == 0


def test_generic_governance_allows_matching_release_for_admin(release_api):
    client, db, reviewer = release_api
    approval = _submitted_package(db, reviewer, "generic_valid_publish")

    response = client.post(f"/api/agent-governance/approvals/{approval.id}/approve", json={"note": "内容通过"})

    assert response.status_code == 200
    assert db.get(ApprovalItem, approval.id).status == "approved"
    assert db.query(CustomAgentRelease).filter(CustomAgentRelease.approval_id == approval.id).count() == 1


def test_generic_governance_rejects_reviewer_release_decision(release_api):
    client, db, reviewer = release_api
    approval = _submitted_package(db, reviewer, "generic_reviewer_denied")
    client.app.dependency_overrides[get_current_user] = lambda: reviewer

    response = client.post(f"/api/agent-governance/approvals/{approval.id}/approve", json={"note": "越权"})

    assert response.status_code == 403
    assert db.get(ApprovalItem, approval.id).status == "pending"
    assert db.query(CustomAgentRelease).count() == 0


def test_release_service_rejects_reviewer_even_without_route_guard(release_api):
    _, db, reviewer = release_api
    approval = _submitted_package(db, reviewer, "service_reviewer_denied")

    from app.core.exceptions import ForbiddenError

    with pytest.raises(ForbiddenError):
        approval_service.decide_item(db, reviewer, approval.id, approve=True)
    assert db.get(ApprovalItem, approval.id).status == "pending"
    assert db.query(CustomAgentRelease).count() == 0
