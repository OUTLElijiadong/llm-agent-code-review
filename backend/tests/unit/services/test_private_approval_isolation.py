"""私人会话的审批不得通过超级管理员入口跨账号读取或执行。"""

import json

import pytest

from app.core.exceptions import ForbiddenError, NotFoundError
from app.models.agent_governance import AgentKnowledgeDoc, ApprovalItem
from app.models.agent_response_run import AgentResponseRun
from app.models.audit_log import AuditLog
from app.models.user import User
from app.services import approval_service as service


@pytest.fixture
def normal_user(db):
    user = User(username="private-approval-owner", password="unused", role="admin", status=1)
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def approvals(db, admin_user, normal_user):
    db.add(AgentResponseRun(
        run_id="private-approval-run", user_id=normal_user.id, surface="user",
        session_key="private-approval-session", checkpoint_json="{}", status="completed",
    ))
    db.flush()
    doc = AgentKnowledgeDoc(
        agent_code="manager", title="PRIVATE_KNOWLEDGE_TITLE", source_type="manual",
        source_ref="response_run:private-approval-run", status="pending_approval",
    )
    db.add(doc)
    db.flush()
    cases = {
        "responses": ("responses.save_knowledge_note", {"owner_user_id": normal_user.id}),
        "malformed": ("responses.save_knowledge_note", {"owner_user_id": True}),
        "knowledge": ("knowledge.activate", {"doc_id": doc.id}),
        "missing_doc": ("knowledge.activate", {"doc_id": 99999}),
        "shared": ("agent.toggle", {"context": {"agent_code": "fixture", "enable": True}}),
    }
    rows = {}
    for key, (action, payload) in cases.items():
        row = ApprovalItem(title="PRIVATE_APPROVAL_TITLE" if key != "shared" else "shared",
                           action=action, resource="fixture", risk_level="high", status="pending",
                           request_json=json.dumps(payload))
        db.add(row)
        rows[key] = row
    db.commit()
    return rows


@pytest.mark.parametrize("role", ["admin", "super_admin"])
def test_private_approvals_hidden_even_from_super_admin(db, approvals, admin_user, super_admin_user, role):
    actor = admin_user if role == "admin" else super_admin_user
    visible = service.list_items(db, actor=actor)
    assert [row.id for row in visible] == [approvals["shared"].id]


@pytest.mark.parametrize("kind", ["responses", "malformed", "knowledge", "missing_doc"])
@pytest.mark.parametrize("approve", [True, False])
def test_super_admin_cannot_decide_foreign_private_approval(db, approvals, super_admin_user, kind, approve):
    item = approvals[kind]
    with pytest.raises((ForbiddenError, NotFoundError)):
        service.decide_item(db, super_admin_user, item.id, approve)
    assert db.get(ApprovalItem, item.id).status == "pending"


def test_owner_retains_private_approval_list_and_activation(db, approvals, normal_user):
    visible = service.list_items(db, actor=normal_user)
    assert {row.id for row in visible} == {approvals[key].id for key in ("responses", "knowledge", "shared")}
    item = service.decide_item(db, normal_user, approvals["knowledge"].id, True)
    doc_id = json.loads(item.request_json)["doc_id"]
    assert db.get(AgentKnowledgeDoc, doc_id).status == "active"
    assert "PRIVATE_" not in db.query(AuditLog).filter_by(target_type="approval").one().detail


def test_new_approval_audit_does_not_copy_private_title(db, normal_user):
    item = service.create_or_auto_decide(db, title="PRIVATE_APPROVAL_TITLE", action="responses.save_knowledge_note",
        resource="fixture", risk_level="high", decision="escalate", reason="fixture", actor=normal_user,
        request={"owner_user_id": normal_user.id})
    entry = db.query(AuditLog).filter_by(target_type="approval", target_id=str(item.id)).one()
    assert "PRIVATE_" not in entry.detail
