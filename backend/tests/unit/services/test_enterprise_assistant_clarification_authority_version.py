"""Clarification evidence is invalid after authorization ABA and reviews remain append-only."""
import pytest
from test_enterprise_assistant_clarification_records import _request

from app.core.exceptions import ConflictError, ForbiddenError
from app.models.enterprise_assistant_clarification import EnterpriseAssistantClarificationReview
from app.models.enterprise_chat import EnterpriseChatMessage
from app.models.enterprise_org import EnterpriseMember
from app.models.user import User
from app.services import enterprise_assistant_clarification_service as clarification

pytest_plugins = ("test_enterprise_assistant_clarification_records",)


def test_member_role_aba_cannot_reactivate_evidence(records_db):
    request = _request(records_db)
    clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                         source_message_id=100, user_id=2, decision="allow")
    records_db.commit()
    member = records_db.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=2).one()
    member.role = "member"
    records_db.commit()
    member.role = "admin"
    records_db.commit()
    assert clarification.status(records_db, enterprise_id=10, conversation_id=30, request_id=request.id) == "stale"


def test_higher_authority_review_preserves_deny_and_is_idempotent(records_db):
    request = _request(records_db)
    clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                         source_message_id=100, user_id=2, decision="deny")
    records_db.commit()
    for _ in range(2):
        clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                             source_message_id=100, user_id=1, decision="allow")
        records_db.commit()
    assert clarification.status(records_db, enterprise_id=10, conversation_id=30, request_id=request.id) == "recorded"
    assert clarification.decision_status(records_db, enterprise_id=10, conversation_id=30, request_id=request.id) == "complete"
    from app.models.enterprise_assistant_clarification import EnterpriseAssistantClarificationDecision, EnterpriseAssistantClarificationReview
    assert records_db.query(EnterpriseAssistantClarificationDecision).filter_by(request_id=request.id).one().decision == "deny"
    assert records_db.query(EnterpriseAssistantClarificationReview).filter_by(request_id=request.id).count() == 1


@pytest.mark.parametrize("initial_decision,review_decision,expected_status", [
    ("allow", "deny", "denied"),
    ("unrelated", "deny", "denied"),
    ("allow", "unrelated", "complete"),
])
def test_higher_authority_can_supersede_any_lower_decision(records_db, initial_decision, review_decision,
                                                           expected_status):
    from app.models.enterprise_assistant_clarification import (
        EnterpriseAssistantClarificationDecision,
        EnterpriseAssistantClarificationReview,
    )

    request = _request(records_db)
    clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                         source_message_id=100, user_id=2, decision=initial_decision)
    records_db.commit()

    clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                         source_message_id=100, user_id=1, decision=review_decision)
    records_db.commit()

    assert clarification.decision_status(records_db, enterprise_id=10, conversation_id=30,
                                         request_id=request.id) == expected_status
    original = records_db.query(EnterpriseAssistantClarificationDecision).filter_by(
        request_id=request.id, source_message_id=100,
    ).one()
    assert original.decision == initial_decision and original.decided_by == 2
    reviews = records_db.query(EnterpriseAssistantClarificationReview).filter_by(
        request_id=request.id, source_message_id=100,
    ).all()
    assert len(reviews) == 1
    assert reviews[0].review_kind == "higher_review"
    assert reviews[0].decision == review_decision and reviews[0].decided_by == 1


def test_lower_allow_does_not_resolve_another_higher_authority_candidate(records_db):
    from app.models.enterprise_assistant_clarification import EnterpriseAssistantClarificationDecision
    from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
    from app.models.enterprise_chat import EnterpriseChatMessage, EnterpriseConversation
    from app.services import enterprise_assistant_runtime_service as runtime

    records_db.get(EnterpriseChatMessage, 100).sender_id = 1
    records_db.get(EnterpriseChatMessage, 100).content = "@小菱 不要创建团队"
    records_db.add(EnterpriseChatMessage(
        id=99, enterprise_id=10, conversation_id=30, sender_id=4, sender_type="human",
        client_message_id="peer-directive", content="@小菱 可以创建团队",
    ))
    state = records_db.get(EnterpriseAssistantRuntimeState, 300)
    room = records_db.get(EnterpriseConversation, 30)
    messages = records_db.query(EnterpriseChatMessage).filter_by(
        enterprise_id=10, conversation_id=30, sender_type="human",
    ).order_by(EnterpriseChatMessage.id).all()
    snapshot = runtime._role_snapshot(records_db, enterprise_id=10, room=room, messages=messages, initiator_id=3)
    state.permission_snapshot_json = runtime._json(snapshot)
    state.permission_snapshot_sha256 = runtime._digest(snapshot)
    records_db.commit()

    request = _request(records_db)
    decisions = records_db.query(EnterpriseAssistantClarificationDecision).filter_by(
        request_id=request.id,
    ).order_by(EnterpriseAssistantClarificationDecision.source_message_id).all()
    assert [(row.source_message_id, row.author_user_id) for row in decisions] == [(99, 4), (100, 1)]

    clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                         source_message_id=99, user_id=4, decision="allow")
    records_db.commit()
    assert clarification.decision_status(records_db, enterprise_id=10, conversation_id=30,
                                         request_id=request.id) == "waiting"

    with pytest.raises(ForbiddenError):
        clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                             source_message_id=100, user_id=3, decision="allow")
    records_db.rollback()
    assert clarification.decision_status(records_db, enterprise_id=10, conversation_id=30,
                                         request_id=request.id) == "waiting"

    clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                         source_message_id=100, user_id=1, decision="deny")
    records_db.commit()
    assert clarification.decision_status(records_db, enterprise_id=10, conversation_id=30,
                                         request_id=request.id) == "denied"


def test_account_status_aba_invalidates_decision(records_db):
    request = _request(records_db)
    clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                         source_message_id=100, user_id=2, decision="allow")
    records_db.commit()
    actor = records_db.get(User, 2)
    actor.status = 0
    records_db.commit()
    actor.status = 1
    records_db.commit()
    assert clarification.status(records_db, enterprise_id=10, conversation_id=30, request_id=request.id) == "stale"


@pytest.mark.parametrize("entity,changes", [
    ("member_role", ("member", "admin")),
    ("member_status", ("removed", "active")),
    ("account_status", (0, 1)),
    ("owner", (2, 1)),
])
def test_current_authority_aba_always_invalidates(records_db, entity, changes):
    from app.models.enterprise_org import Enterprise
    request = _request(records_db)
    clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                         source_message_id=100, user_id=2, decision="allow")
    records_db.commit()
    if entity == "member_role":
        obj, field = records_db.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=2).one(), "role"
    elif entity == "member_status":
        obj, field = records_db.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=2).one(), "status"
    elif entity == "account_status":
        obj, field = records_db.get(User, 2), "status"
    else:
        obj, field = records_db.get(Enterprise, 10), "owner_id"
    for value in changes:
        setattr(obj, field, value)
        records_db.commit()
    assert clarification.status(records_db, enterprise_id=10, conversation_id=30, request_id=request.id) == "stale"


@pytest.mark.parametrize("field,other,original", [("role", "member", "leader"), ("status", "removed", "active")])
def test_group_member_aba_invalidates(records_db, field, other, original):
    from app.models.enterprise_org import EnterpriseGroup, EnterpriseGroupMember
    from app.models.enterprise_chat import EnterpriseConversation
    room = records_db.get(EnterpriseConversation, 30)
    room.kind, room.group_id = "group", 20
    records_db.add(EnterpriseGroup(id=20, enterprise_id=10, name="组", status="active"))
    records_db.add_all([EnterpriseGroupMember(enterprise_id=10, group_id=20, user_id=i,
                                             role="leader" if i == 2 else "member", status="active")
                        for i in (1, 2, 3, 4)])
    records_db.commit()
    request = _request(records_db)
    clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                         source_message_id=100, user_id=2, decision="allow")
    records_db.commit()
    row = records_db.query(EnterpriseGroupMember).filter_by(enterprise_id=10, group_id=20, user_id=2).one()
    for value in (other, original):
        setattr(row, field, value)
        records_db.commit()
    assert clarification.status(records_db, enterprise_id=10, conversation_id=30, request_id=request.id) == "stale"


def test_old_115_record_without_versions_is_stale(records_db):
    request = _request(records_db)
    records_db.commit()
    request.authority_version_sha256 = None
    records_db.commit()
    assert clarification.status(records_db, enterprise_id=10, conversation_id=30, request_id=request.id) == "stale"
    new_request = _request(records_db)
    assert new_request.version == 2 and new_request.id != request.id


def test_same_value_does_not_advance_version(records_db):
    from sqlalchemy import text
    before = records_db.execute(text('SELECT clarification_authority_version FROM user WHERE id=2')).scalar_one()
    records_db.execute(text('UPDATE user SET status=status WHERE id=2'))
    records_db.commit()
    after = records_db.execute(text('SELECT clarification_authority_version FROM user WHERE id=2')).scalar_one()
    assert before == after


@pytest.mark.parametrize("initial_decision", ["allow", "deny", "unrelated"])
def test_same_or_lower_rank_cannot_review_any_decision(records_db, initial_decision):
    records_db.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=4).one().role = "admin"
    records_db.commit()
    request = _request(records_db)
    clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                         source_message_id=100, user_id=2, decision=initial_decision)
    records_db.commit()
    for user_id in (3, 4):
        with pytest.raises((ForbiddenError, ConflictError)):
            clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                                 source_message_id=100, user_id=user_id, decision="allow")
        records_db.rollback()
    expected = "denied" if initial_decision == "deny" else "complete"
    assert clarification.decision_status(records_db, enterprise_id=10, conversation_id=30,
                                         request_id=request.id) == expected


def test_equal_rank_cannot_supersede_a_higher_review(records_db):
    records_db.get(EnterpriseChatMessage, 100).sender_id = 4
    records_db.add(User(id=5, username="user-5", password="x", email="5@example.test", role="user", status=1))
    records_db.add(EnterpriseMember(enterprise_id=10, user_id=5, role="admin", status="active"))
    records_db.commit()
    request = _request(records_db)
    clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                         source_message_id=100, user_id=4, decision="allow")
    clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                         source_message_id=100, user_id=2, decision="deny")
    records_db.commit()

    with pytest.raises(ConflictError):
        clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                             source_message_id=100, user_id=5, decision="allow")
    records_db.rollback()

    assert clarification.decision_status(records_db, enterprise_id=10, conversation_id=30,
                                         request_id=request.id) == "denied"
    assert records_db.query(EnterpriseAssistantClarificationReview).filter_by(request_id=request.id).count() == 1


def test_reviewer_member_role_aba_invalidates_higher_review(records_db):
    records_db.get(EnterpriseChatMessage, 100).sender_id = 4
    records_db.commit()
    request = _request(records_db)
    clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                         source_message_id=100, user_id=4, decision="allow")
    clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                         source_message_id=100, user_id=2, decision="deny")
    records_db.commit()

    reviewer = records_db.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=2).one()
    reviewer.role = "member"
    records_db.commit()
    reviewer.role = "admin"
    records_db.commit()

    assert clarification.decision_status(records_db, enterprise_id=10, conversation_id=30,
                                         request_id=request.id) == "stale"


def test_higher_decider_account_aba_invalidates_review(records_db):
    request = _request(records_db)
    clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                         source_message_id=100, user_id=2, decision="deny")
    clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                         source_message_id=100, user_id=1, decision="allow")
    records_db.commit()
    owner = records_db.get(User, 1)
    owner.status = 0
    records_db.commit()
    owner.status = 1
    records_db.commit()
    assert clarification.decision_status(records_db, enterprise_id=10, conversation_id=30, request_id=request.id) == "stale"


def test_review_audit_cannot_be_updated_or_deleted(records_db):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    request = _request(records_db)
    clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                         source_message_id=100, user_id=2, decision="deny")
    clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                         source_message_id=100, user_id=1, decision="allow")
    records_db.commit()
    for statement in ("UPDATE enterprise_assistant_clarification_review SET decision='deny'",
                      "DELETE FROM enterprise_assistant_clarification_review"):
        with pytest.raises(IntegrityError):
            records_db.execute(text(statement))
        records_db.rollback()
    assert clarification.decision_status(records_db, enterprise_id=10, conversation_id=30, request_id=request.id) == "complete"


def test_same_rank_author_can_revise_only_own_decision(records_db):
    from app.models.enterprise_chat import EnterpriseChatMessage
    from app.models.enterprise_assistant_clarification import EnterpriseAssistantClarificationReview
    from app.core.exceptions import ForbiddenError
    member = records_db.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=2).one()
    member.role = "member"
    records_db.add(EnterpriseChatMessage(id=103, enterprise_id=10, conversation_id=30, sender_id=4,
                                         sender_type="human", client_message_id="peer", content="可以创建"))
    records_db.commit()
    request = _request(records_db)
    for author, mid, choice in ((2, 100, "deny"), (4, 103, "allow")):
        clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                             source_message_id=mid, user_id=author, decision=choice)
    records_db.commit()
    assert clarification.decision_status(records_db, enterprise_id=10, conversation_id=30, request_id=request.id) == "denied"
    with pytest.raises(ForbiddenError):
        clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                             source_message_id=100, user_id=4, decision="allow")
    records_db.rollback()
    for _ in range(2):
        clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                             source_message_id=100, user_id=2, decision="allow")
        records_db.commit()
    reviews = records_db.query(EnterpriseAssistantClarificationReview).filter_by(request_id=request.id).all()
    assert len(reviews) == 1 and reviews[0].review_kind == "author_revision"
    assert clarification.decision_status(records_db, enterprise_id=10, conversation_id=30, request_id=request.id) == "complete"


def test_original_decided_evidence_cannot_be_mutated(records_db):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    request = _request(records_db)
    clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                         source_message_id=100, user_id=2, decision="deny")
    records_db.commit()
    for statement in ("UPDATE enterprise_assistant_clarification_decision SET decision='allow'",
                      "DELETE FROM enterprise_assistant_clarification_decision"):
        with pytest.raises(IntegrityError):
            records_db.execute(text(statement))
        records_db.rollback()
    assert clarification.decision_status(records_db, enterprise_id=10, conversation_id=30, request_id=request.id) == "denied"


def test_original_author_cannot_undo_higher_review(records_db):
    from app.core.exceptions import ConflictError
    request = _request(records_db)
    clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                         source_message_id=100, user_id=2, decision="deny")
    clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                         source_message_id=100, user_id=1, decision="allow")
    records_db.commit()
    with pytest.raises(ConflictError):
        clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                             source_message_id=100, user_id=2, decision="deny")
    records_db.rollback()
    assert clarification.decision_status(records_db, enterprise_id=10, conversation_id=30, request_id=request.id) == "complete"


def test_author_revision_can_return_to_earlier_decision(records_db):
    from app.models.enterprise_assistant_clarification import EnterpriseAssistantClarificationReview
    request = _request(records_db)
    for choice in ("allow", "deny", "allow", "allow"):
        clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                             source_message_id=100, user_id=2, decision=choice)
        records_db.commit()
    reviews = records_db.query(EnterpriseAssistantClarificationReview).filter_by(request_id=request.id).order_by(
        EnterpriseAssistantClarificationReview.id).all()
    assert [r.decision for r in reviews] == ["deny", "allow"]
    assert clarification.decision_status(records_db, enterprise_id=10, conversation_id=30, request_id=request.id) == "complete"


@pytest.mark.parametrize('recursive', [0, 1])
def test_sqlite_authority_version_cannot_be_rewound(records_db, recursive):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    records_db.execute(text(f'PRAGMA recursive_triggers={recursive}'))
    request = _request(records_db)
    clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                         source_message_id=100, user_id=2, decision='allow')
    records_db.commit()
    records_db.execute(text("UPDATE enterprise_member SET role='member' WHERE enterprise_id=10 AND user_id=2"))
    records_db.commit()
    records_db.execute(text("UPDATE enterprise_member SET role='admin' WHERE enterprise_id=10 AND user_id=2"))
    records_db.commit()
    assert records_db.execute(text('SELECT clarification_authority_version FROM enterprise_member WHERE enterprise_id=10 AND user_id=2')).scalar_one() == 2
    with pytest.raises(IntegrityError):
        records_db.execute(text('UPDATE enterprise_member SET clarification_authority_version=0 WHERE enterprise_id=10 AND user_id=2'))
    records_db.rollback()
    assert clarification.status(records_db, enterprise_id=10, conversation_id=30, request_id=request.id) == 'stale'


@pytest.mark.parametrize('recursive', [0, 1])
def test_sqlite_role_change_with_explicit_version_bumps_exactly_once(records_db, recursive):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    records_db.execute(text(f'PRAGMA recursive_triggers={recursive}'))
    records_db.execute(text("UPDATE enterprise_member SET role='member', clarification_authority_version=clarification_authority_version+1 WHERE enterprise_id=10 AND user_id=2"))
    records_db.commit()
    assert records_db.execute(text('SELECT clarification_authority_version FROM enterprise_member WHERE enterprise_id=10 AND user_id=2')).scalar_one() == 1
    with pytest.raises(IntegrityError):
        records_db.execute(text("UPDATE enterprise_member SET role='admin', clarification_authority_version=0 WHERE enterprise_id=10 AND user_id=2"))
    records_db.rollback()
    assert records_db.execute(text('SELECT clarification_authority_version FROM enterprise_member WHERE enterprise_id=10 AND user_id=2')).scalar_one() == 1


@pytest.mark.parametrize('recursive', [0, 1])
@pytest.mark.parametrize('table,where,field,new_value', [
    ('user', 'id=2', 'status', '0'),
    ('enterprise', 'id=10', 'owner_id', '2'),
    ('enterprise_member', 'enterprise_id=10 AND user_id=2', 'status', "'removed'"),
    ('enterprise_group_member', 'enterprise_id=10 AND group_id=20 AND user_id=2', 'role', "'leader'"),
])
def test_sqlite_all_authority_rows_protect_version_and_increment_once(records_db, recursive, table, where, field, new_value):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    from app.models.enterprise_org import EnterpriseGroup, EnterpriseGroupMember
    records_db.execute(text(f'PRAGMA recursive_triggers={recursive}'))
    if table == 'enterprise_group_member':
        records_db.add(EnterpriseGroup(id=20, enterprise_id=10, name='组', status='active'))
        records_db.add(EnterpriseGroupMember(enterprise_id=10, group_id=20, user_id=2, role='member', status='active'))
        records_db.commit()
    records_db.execute(text(f'UPDATE {table} SET {field}={new_value}, clarification_authority_version=clarification_authority_version+1 WHERE {where}'))
    records_db.commit()
    query = text(f'SELECT clarification_authority_version FROM {table} WHERE {where}')
    assert records_db.execute(query).scalar_one() == 1
    for bad in ('0', '3', 'NULL'):
        with pytest.raises(IntegrityError):
            records_db.execute(text(f'UPDATE {table} SET clarification_authority_version={bad} WHERE {where}'))
        records_db.rollback()
        assert records_db.execute(query).scalar_one() == 1
    records_db.execute(text(f'UPDATE {table} SET clarification_authority_version=clarification_authority_version+1 WHERE {where}'))
    records_db.commit()
    assert records_db.execute(query).scalar_one() == 2


@pytest.mark.parametrize('recursive,blocked', [(0, False), (1, True)])
def test_sqlite_replace_requires_recursive_trigger_guard(records_db, recursive, blocked):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    records_db.execute(text(f'PRAGMA recursive_triggers={recursive}'))
    records_db.execute(text('PRAGMA foreign_keys=OFF'))
    request = _request(records_db)
    clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                         source_message_id=100, user_id=2, decision='allow')
    records_db.commit()
    for status in (0, 1):
        records_db.execute(text('UPDATE user SET status=:status WHERE id=2'), {'status': status})
        records_db.commit()
    assert clarification.status(records_db, enterprise_id=10, conversation_id=30, request_id=request.id) == 'stale'
    columns = [row[1] for row in records_db.execute(text('PRAGMA table_info(user)'))]
    quoted = ', '.join(f'"{column}"' for column in columns)
    values = ', '.join('0' if column == 'clarification_authority_version' else f'"{column}"' for column in columns)
    if blocked:
        with pytest.raises(IntegrityError):
            records_db.execute(text(f'INSERT OR REPLACE INTO user ({quoted}) SELECT {values} FROM user WHERE id=2'))
        records_db.rollback()
    else:
        records_db.execute(text(f'INSERT OR REPLACE INTO user ({quoted}) SELECT {values} FROM user WHERE id=2'))
        records_db.commit()
    expected = 'stale' if blocked else 'recorded'
    assert clarification.status(records_db, enterprise_id=10, conversation_id=30, request_id=request.id) == expected


def test_sqlite_native_upsert_uses_update_version_trigger(records_db):
    from sqlalchemy import text
    records_db.execute(text('PRAGMA recursive_triggers=ON'))
    records_db.execute(text("""
        INSERT INTO user (id, username, password, role, status, token_version,
                          create_time, update_time, clarification_authority_version)
        VALUES (2, 'user-2', 'x', 'user', 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)
        ON CONFLICT(id) DO UPDATE SET status=excluded.status
    """))
    records_db.commit()
    row = records_db.execute(text(
        'SELECT status, clarification_authority_version FROM user WHERE id=2'
    )).one()
    assert row == (0, 1)
