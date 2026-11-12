"""Clarification contracts; records are not authorization.

These tests document local synthetic cases only. No clarification route or
execution/model-send gate is enabled by their presence.
"""
import pytest
from test_enterprise_assistant_clarification_records import _request

from app.core.exceptions import ConflictError
from app.models.enterprise_assistant_clarification import EnterpriseAssistantClarificationDecision
from app.models.enterprise_org import EnterpriseMember
from app.services import enterprise_assistant_clarification_service as clarification

pytest_plugins = ("test_enterprise_assistant_clarification_records",)


class AuthorityVersionGap(AssertionError):
    """A revoked and restored role reused an earlier recorded decision."""


class HigherAuthorityResolutionGap(AssertionError):
    """A higher authority cannot resolve an already denied clarification."""


def test_restored_role_does_not_reactivate_prior_clarification(records_db):
    request = _request(records_db)
    clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                         source_message_id=100, user_id=2, decision="allow")
    records_db.commit()
    author = records_db.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=2).one()
    author.role = "member"
    records_db.commit()
    author.role = "admin"
    records_db.commit()
    actual = clarification.status(records_db, enterprise_id=10, conversation_id=30, request_id=request.id)
    if actual != "stale":
        raise AuthorityVersionGap(f"角色经过降权和恢复后，旧决议状态仍为 {actual}")


def test_owner_can_review_a_previous_admin_denial(records_db):
    request = _request(records_db)
    clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                         source_message_id=100, user_id=2, decision="deny")
    records_db.commit()
    original_id = records_db.query(EnterpriseAssistantClarificationDecision.id).filter_by(
        request_id=request.id, source_message_id=100,
    ).scalar()
    try:
        clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                             source_message_id=100, user_id=1, decision="allow")
    except ConflictError as exc:
        raise HigherAuthorityResolutionGap("企业所有者无法复核管理员之前的拒绝") from exc
    records_db.commit()
    original = records_db.get(EnterpriseAssistantClarificationDecision, original_id)
    assert original is not None and original.decision == "deny" and original.decided_by == 2
    # Any future implementation must retain the prior denial as audit evidence;
    # the resolution may use a new request version instead of changing this API.
    assert clarification.status(records_db, enterprise_id=10, conversation_id=30, request_id=request.id) == "recorded"
