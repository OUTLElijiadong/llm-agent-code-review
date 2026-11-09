from datetime import timedelta
import runpy
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.models.enterprise_assistant_clarification import (
    EnterpriseAssistantClarificationDecision,
)
from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun, EnterpriseChatMessage, EnterpriseConversation
from app.models.enterprise_org import Enterprise, EnterpriseMember
from app.models.user import User
from app.services import enterprise_assistant_clarification_service as clarification


@pytest.fixture
def records_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    migration = Path(__file__).resolve().parents[3] / "alembic/versions/118_clarification_authority.py"
    with engine.begin() as connection:
        runpy.run_path(str(migration))["install_version_triggers"](connection)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    db.add_all([
        User(id=i, username=f"user-{i}", password="x", email=f"{i}@example.test", role="user", status=1)
        for i in (1, 2, 3, 4)
    ])
    db.add_all([
        Enterprise(id=10, name="公司", owner_id=1, seat_limit=10, status="active", paid_order_id="paid"),
        EnterpriseMember(enterprise_id=10, user_id=1, role="owner", status="active"),
        EnterpriseMember(enterprise_id=10, user_id=2, role="admin", status="active"),
        EnterpriseMember(enterprise_id=10, user_id=3, role="member", status="active"),
        EnterpriseMember(enterprise_id=10, user_id=4, role="member", status="active"),
        EnterpriseConversation(id=30, enterprise_id=10, kind="company", conversation_key="company", created_by=1),
        EnterpriseConversation(id=31, enterprise_id=10, kind="direct", conversation_key="direct:3:4",
                               participant_low_id=3, participant_high_id=4, created_by=3),
        EnterpriseChatMessage(id=100, enterprise_id=10, conversation_id=30, sender_id=2, sender_type="human",
                              client_message_id="one", content="先不要创建团队"),
        EnterpriseChatMessage(id=101, enterprise_id=10, conversation_id=30, sender_id=3, sender_type="human",
                              client_message_id="two", content="@小菱 创建团队"),
        EnterpriseChatMessage(id=102, enterprise_id=10, conversation_id=31, sender_id=4, sender_type="human",
                              client_message_id="private", content="私聊不能泄露"),
        EnterpriseAssistantRun(id=200, public_id="clarify-run", request_key="clarify-run", enterprise_id=10,
                               conversation_id=30, initiator_user_id=3, source_message_id=101,
                               prompt_sha256="a" * 64, status="running"),
    ])
    db.flush()
    db.add(EnterpriseAssistantRuntimeState(id=300, assistant_run_id=200, enterprise_id=10, conversation_id=30,
                                          initiator_user_id=3, source_message_id=101, status="running",
                                          permission_snapshot_json="{}", permission_snapshot_sha256="b" * 64))
    db.commit()
    yield db
    db.close()
    engine.dispose()


def _request(db, **overrides):
    options = dict(enterprise_id=10, conversation_id=30, run_id=200, phase="tool", call_id="call-1",
                   action="create_registered_enterprise_agent_team", target={"team": "new"},
                   arguments={"title": "团队"}, expires_at=clarification._now() + timedelta(hours=1))
    options.update(overrides)
    return clarification.create_request(db, **options)


def test_request_binds_exact_action_and_is_idempotent(records_db):
    first = _request(records_db)
    records_db.commit()
    replay = _request(records_db)
    assert replay.id == first.id
    with pytest.raises(ConflictError):
        _request(records_db, arguments={"title": "其他团队"})


def test_request_uses_current_real_message_authors_not_other_rooms(records_db):
    request = _request(records_db)
    records_db.commit()
    decisions = records_db.query(EnterpriseAssistantClarificationDecision).filter_by(request_id=request.id).all()
    assert [(item.source_message_id, item.author_user_id) for item in decisions] == [(100, 2)]


def test_only_author_or_current_higher_authority_can_decide(records_db):
    request = _request(records_db)
    records_db.commit()
    with pytest.raises(ForbiddenError):
        clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                             source_message_id=100, user_id=4, decision="unrelated")
    clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                         source_message_id=100, user_id=1, decision="allow")
    records_db.commit()
    assert clarification.status(records_db, enterprise_id=10, conversation_id=30, request_id=request.id) == "recorded"


def test_expiry_or_new_higher_message_invalidates_prior_decision(records_db):
    request = _request(records_db)
    records_db.commit()
    clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                         source_message_id=100, user_id=2, decision="allow")
    records_db.commit()
    records_db.add(EnterpriseChatMessage(id=103, enterprise_id=10, conversation_id=30, sender_id=1,
                                        sender_type="human", client_message_id="new", content="先暂停"))
    records_db.commit()
    assert clarification.status(records_db, enterprise_id=10, conversation_id=30, request_id=request.id) == "stale"
    request.expires_at = clarification._now() - timedelta(seconds=1)
    records_db.commit()
    assert clarification.status(records_db, enterprise_id=10, conversation_id=30, request_id=request.id) == "expired"


def test_changed_authority_creates_new_version_and_preserves_old_decisions(records_db):
    first = _request(records_db)
    records_db.commit()
    clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=first.id,
                         source_message_id=100, user_id=2, decision="allow")
    records_db.commit()
    records_db.add(EnterpriseChatMessage(id=103, enterprise_id=10, conversation_id=30, sender_id=1,
                                        sender_type="human", client_message_id="new", content="先暂停"))
    records_db.commit()
    second = _request(records_db)
    records_db.commit()
    assert first.status == "superseded"
    assert second.version == 2 and second.id != first.id
    assert clarification.status(records_db, enterprise_id=10, conversation_id=30, request_id=second.id) == "waiting"
    assert records_db.query(EnterpriseAssistantClarificationDecision).filter_by(
        request_id=first.id, source_message_id=100,
    ).one().decision == "allow"


def test_repeating_same_author_decision_is_idempotent(records_db):
    request = _request(records_db)
    records_db.commit()
    for _ in range(2):
        clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                             source_message_id=100, user_id=2, decision="allow")
        records_db.commit()
    assert records_db.query(EnterpriseAssistantClarificationDecision).filter_by(request_id=request.id).count() == 1


def test_decider_losing_authority_invalidates_decision(records_db):
    request = _request(records_db)
    records_db.commit()
    clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                         source_message_id=100, user_id=1, decision="allow")
    records_db.commit()
    records_db.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=1).one().role = "member"
    records_db.commit()
    assert clarification.status(records_db, enterprise_id=10, conversation_id=30, request_id=request.id) == "stale"


def test_missing_source_decision_never_becomes_ready(records_db):
    request = _request(records_db)
    records_db.commit()
    records_db.query(EnterpriseAssistantClarificationDecision).filter_by(request_id=request.id).delete()
    records_db.commit()
    assert clarification.status(records_db, enterprise_id=10, conversation_id=30, request_id=request.id) == "stale"


def test_modified_action_payload_never_becomes_ready(records_db):
    request = _request(records_db)
    records_db.commit()
    clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                         source_message_id=100, user_id=2, decision="allow")
    request.arguments_json = '{"title":"不同内容"}'
    records_db.commit()
    assert clarification.status(records_db, enterprise_id=10, conversation_id=30, request_id=request.id) == "stale"


def test_changed_initiating_message_invalidates_decision(records_db):
    request = _request(records_db)
    records_db.commit()
    clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=request.id,
                         source_message_id=100, user_id=2, decision="allow")
    records_db.commit()
    records_db.get(EnterpriseChatMessage, 101).content = "@小菱 执行另一个动作"
    records_db.commit()
    assert clarification.status(records_db, enterprise_id=10, conversation_id=30, request_id=request.id) == "stale"


def test_cross_conversation_request_does_not_read_or_authorize(records_db):
    with pytest.raises((ForbiddenError, ConflictError, NotFoundError)):
        _request(records_db, conversation_id=31)
