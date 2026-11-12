"""Current-read tests for the non-authorizing billing preview on disposable MySQL."""

import pytest
import test_fresh_database_chain as chain
from sqlalchemy.orm import sessionmaker
from test_mysql_billing_group_version import _seed

from app.api.v1.enterprise_chat import assistant_billing_context
from app.core.exceptions import ForbiddenError
from app.models.enterprise_org import EnterpriseGroup, EnterpriseMember
from app.models.user import User
from app.services import enterprise_assistant_billing_group as billing_group
from app.services import enterprise_assistant_runtime_service as runtime
from app.services import enterprise_chat_service as chat
from app.services import enterprise_org_service as org

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


def test_preview_refreshes_old_mysql_snapshot_after_default_group_change_and_removal(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, owner, user, group_a, group_b = _seed(factory)
    with factory.begin() as db:
        org.set_group_member(db, company, group_a, owner, user, "member")
        org.set_group_member(db, company, group_b, owner, user, "member")
        org.set_default_group(db, company, owner, user, group_a)
        room_id = chat.open_conversation(db, company, owner, "direct", other_user_id=user).id
    with factory() as stale:
        actor = stale.get(User, user)
        actor._authenticated_token_version = actor.token_version
        member = stale.query(EnterpriseMember).filter_by(enterprise_id=company, user_id=user).one()
        assert member.default_group_id == group_a
        with factory.begin() as writer:
            org.set_default_group(writer, company, owner, user, group_b)
            writer.get(EnterpriseGroup, group_b).name = "归属新名称"
        data = assistant_billing_context(company, room_id, db=stale, user=actor).data
        assert data["group_id"] == group_b and data["group_name"] == "归属新名称" and data["enabled"] is False
        stale.rollback()
    with factory() as stale:
        actor = stale.get(User, user)
        actor._authenticated_token_version = actor.token_version
        stale.query(EnterpriseMember).filter_by(enterprise_id=company, user_id=user).one()
        with factory.begin() as writer:
            org.remove_enterprise_member(writer, company, owner, user)
        with pytest.raises(ForbiddenError):
            assistant_billing_context(company, room_id, db=stale, user=actor)


def test_preview_rechecks_direct_room_and_token_in_old_mysql_snapshot(fresh_chain):
    from app.models.enterprise_chat import EnterpriseConversation

    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, owner, user, _, _ = _seed(factory)
    with factory.begin() as db:
        room_id = chat.open_conversation(db, company, owner, "direct", other_user_id=user).id
    with factory.begin() as db:
        other = User(username=f"preview-alternate-{user}", password="x", role="user", status=1)
        db.add(other)
        db.flush()
        other_id = other.id
        db.add(EnterpriseMember(enterprise_id=company, user_id=other_id, role="member", status="active"))
    with factory() as stale:
        actor = stale.get(User, user)
        actor._authenticated_token_version = actor.token_version
        assert chat.get_conversation(stale, company, user, room_id).participant_high_id == user
        # Synthetic direct SQL simulates a persisted room mutation, not a claimed product feature.
        with factory.begin() as writer:
            writer.get(EnterpriseConversation, room_id).participant_high_id = other_id
        with pytest.raises(ForbiddenError):
            assistant_billing_context(company, room_id, db=stale, user=actor)
        stale.rollback()
    with factory.begin() as writer:
        writer.get(EnterpriseConversation, room_id).participant_high_id = user
    with factory() as stale:
        actor = stale.get(User, user)
        actor._authenticated_token_version = actor.token_version
        with factory.begin() as writer:
            writer.get(User, user).token_version += 1
        with pytest.raises(ForbiddenError, match="登录状态"):
            assistant_billing_context(company, room_id, db=stale, user=actor)


def test_closed_gate_blocks_persisted_v1_but_keeps_legacy_attribution(fresh_chain, monkeypatch):
    from uuid import uuid4

    from app.models.enterprise_chat import EnterpriseAssistantRun

    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, owner, user, group_a, _ = _seed(factory)
    suffix = uuid4().hex
    with factory.begin() as db:
        org.set_group_member(db, company, group_a, owner, user, "member")
        org.set_default_group(db, company, owner, user, group_a)
    with factory() as db:
        room = chat.open_conversation(db, company, user, "company")
        message = chat.send_message(db, company, user, room.id, f"gate-{suffix}", "@小菱 请检查")
        room_id, message_id = room.id, message.id
    with factory.begin() as db:
        snapshot = billing_group.capture(db, enterprise_id=company, user_id=user, conversation_id=room_id)
        run = EnterpriseAssistantRun(
            public_id=f"gate-{suffix}", request_key=f"gate-{suffix}", enterprise_id=company,
            conversation_id=room_id, initiator_user_id=user, source_message_id=message_id,
            billing_attribution_version=1, billing_group_id=group_a,
            billing_membership_version=snapshot.membership_version,
            billing_user_token_version=snapshot.user_token_version, prompt_sha256="0" * 64,
        )
        db.add(run)
        db.flush()
        run_id = run.id
    with factory.begin() as db:
        run = db.get(EnterpriseAssistantRun, run_id)
        monkeypatch.setattr(runtime, "_CROSS_SCOPE_ATTRIBUTION_VERIFIED", True)
        assert billing_group.validate(db, run) == group_a
        monkeypatch.setattr(runtime, "_CROSS_SCOPE_ATTRIBUTION_VERIFIED", False)
        with pytest.raises(ForbiddenError, match="暂不能继续执行"):
            billing_group.validate(db, run)
        assert billing_group.frozen(run) == group_a
        run.billing_attribution_version = 0
        assert billing_group.validate(db, run) is None
