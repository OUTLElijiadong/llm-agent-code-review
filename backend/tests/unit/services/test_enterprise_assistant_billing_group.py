from types import SimpleNamespace

import pytest

from app.core.exceptions import ForbiddenError
from app.models.enterprise_chat import EnterpriseAssistantRun, EnterpriseChatMessage, EnterpriseConversation
from app.models.enterprise_org import Enterprise, EnterpriseGroup, EnterpriseGroupMember, EnterpriseMember
from app.models.user import User
from app.services import enterprise_assistant_billing_group as billing_group
from app.services import enterprise_assistant_service as legacy_assistant
from app.models.billing_ledger import BillingReservation


@pytest.fixture
def scope(db, monkeypatch):
    from app.services import enterprise_assistant_runtime_service as runtime

    # Attribution tests exercise version checking, not the feature gate.
    monkeypatch.setattr(runtime, "_CROSS_SCOPE_ATTRIBUTION_VERIFIED", True)
    db.add_all([
        User(id=101, username="billing-owner", password="x", role="user", status=1),
        User(id=102, username="billing-member", password="x", role="user", status=1),
        Enterprise(id=101, name="甲企业", owner_id=101, seat_limit=5, status="active", paid_order_id="billing-a"),
        Enterprise(id=102, name="乙企业", owner_id=101, seat_limit=5, status="active", paid_order_id="billing-b"),
        EnterpriseMember(enterprise_id=101, user_id=101, role="owner", status="active"),
        EnterpriseMember(enterprise_id=101, user_id=102, role="member", status="active", billing_group_version=4),
        EnterpriseGroup(id=201, enterprise_id=101, name="甲组", status="active"),
        EnterpriseGroup(id=202, enterprise_id=101, name="乙组", status="active"),
        EnterpriseGroup(id=203, enterprise_id=102, name="其他企业组", status="active"),
        EnterpriseConversation(id=301, enterprise_id=101, kind="company", conversation_key="company", created_by=101),
        EnterpriseConversation(id=302, enterprise_id=101, kind="direct", conversation_key="direct:101:102",
                               participant_low_id=101, participant_high_id=102, created_by=101),
        EnterpriseConversation(id=303, enterprise_id=101, kind="group", conversation_key="group:202",
                               group_id=202, created_by=101),
    ])
    db.flush()
    return SimpleNamespace(db=db, member=db.query(EnterpriseMember).filter_by(enterprise_id=101, user_id=102).one())


@pytest.mark.parametrize("room_id", [301, 302])
def test_company_and_direct_snapshot_valid_default_group(scope, room_id):
    db, member = scope.db, scope.member
    db.add(EnterpriseGroupMember(enterprise_id=101, group_id=201, user_id=102, role="member", status="active"))
    member.default_group_id = 201
    db.flush()
    snapshot = billing_group.capture(db, enterprise_id=101, user_id=102, conversation_id=room_id)
    assert snapshot.group_id == 201
    assert snapshot.membership_version == 4


def test_ungrouped_is_distinct_from_missing_or_cross_enterprise_default(scope):
    db, member = scope.db, scope.member
    assert billing_group.capture(db, enterprise_id=101, user_id=102, conversation_id=301).group_id is None
    db.add(EnterpriseGroupMember(enterprise_id=101, group_id=201, user_id=102, role="member", status="active"))
    db.flush()
    with pytest.raises(ForbiddenError, match="默认小组"):
        billing_group.capture(db, enterprise_id=101, user_id=102, conversation_id=301)
    member.default_group_id = 203
    db.flush()
    with pytest.raises(ForbiddenError, match="默认小组"):
        billing_group.capture(db, enterprise_id=101, user_id=102, conversation_id=301)


def test_group_room_keeps_actual_group_not_default(scope):
    db, member = scope.db, scope.member
    db.add_all([
        EnterpriseGroupMember(enterprise_id=101, group_id=201, user_id=102, role="member", status="active"),
        EnterpriseGroupMember(enterprise_id=101, group_id=202, user_id=102, role="member", status="active"),
    ])
    member.default_group_id = 201
    db.flush()
    snapshot = billing_group.capture(db, enterprise_id=101, user_id=102, conversation_id=303)
    assert snapshot.group_id == 202
    assert snapshot.membership_version is None


def test_old_snapshot_cannot_continue_after_default_group_aba(scope):
    db, member = scope.db, scope.member
    db.add(EnterpriseGroupMember(enterprise_id=101, group_id=201, user_id=102, role="member", status="active"))
    member.default_group_id = 201
    db.flush()
    snap = billing_group.capture(db, enterprise_id=101, user_id=102, conversation_id=301)
    run = SimpleNamespace(enterprise_id=101, conversation_id=301, initiator_user_id=102, group_id=None,
                          billing_attribution_version=1, billing_group_id=201,
                          billing_membership_version=snap.membership_version,
                          billing_user_token_version=snap.user_token_version)
    member.billing_group_version += 2
    db.flush()
    with pytest.raises(ForbiddenError, match="计费归属"):
        billing_group.validate(db, run)


def test_old_snapshot_cannot_continue_after_account_reactivation(scope):
    db = scope.db
    snap = billing_group.capture(db, enterprise_id=101, user_id=102, conversation_id=301)
    run = SimpleNamespace(enterprise_id=101, conversation_id=301, initiator_user_id=102, group_id=None,
                          billing_attribution_version=1, billing_group_id=None,
                          billing_membership_version=snap.membership_version,
                          billing_user_token_version=snap.user_token_version)
    db.get(User, 102).token_version += 1
    db.flush()
    with pytest.raises(ForbiddenError, match="计费归属"):
        billing_group.validate(db, run)


def test_legacy_run_keeps_historical_group(scope):
    run = SimpleNamespace(enterprise_id=101, conversation_id=301, initiator_user_id=102, group_id=None,
                          billing_attribution_version=0, billing_group_id=None, billing_membership_version=None)
    assert billing_group.validate(scope.db, run) is None


def test_closed_feature_blocks_new_v1_actions_but_preserves_frozen_accounting(scope, monkeypatch):
    from app.services import enterprise_assistant_runtime_service as runtime

    run = SimpleNamespace(enterprise_id=101, conversation_id=301, initiator_user_id=102, group_id=None,
                          billing_attribution_version=1, billing_group_id=None, billing_membership_version=4)
    monkeypatch.setattr(runtime, "_CROSS_SCOPE_ATTRIBUTION_VERIFIED", False)
    with pytest.raises(ForbiddenError, match="暂不能继续执行"):
        billing_group.validate(scope.db, run)
    assert billing_group.frozen(run) is None


@pytest.mark.parametrize("room_id", [301, 302])
def test_legacy_answer_path_cannot_start_new_company_or_direct_send(scope, monkeypatch, room_id):
    import asyncio

    db = scope.db
    db.add(EnterpriseGroupMember(
        enterprise_id=101, group_id=201, user_id=102, role="member", status="active",
    ))
    scope.member.default_group_id = 201
    db.flush()
    assert billing_group.capture(
        db, enterprise_id=101, user_id=102, conversation_id=room_id,
    ).group_id == 201
    message = EnterpriseChatMessage(
        enterprise_id=101, conversation_id=room_id, sender_id=102, sender_type="human",
        client_message_id=f"legacy-cross-scope-{room_id}", content="@小菱 请总结",
    )
    db.add(message)
    db.commit()
    monkeypatch.setattr(legacy_assistant, "_model_config",
                        lambda *_args: pytest.fail("legacy cross-scope path must stop before provider setup"))

    with pytest.raises(billing_group.BillingAttributionUnavailable, match="旧版问答入口"):
        asyncio.run(legacy_assistant.run_for_message(
            db, enterprise_id=101, user_id=102, conversation_id=room_id, source_message_id=message.id,
        ))

    assert db.query(EnterpriseAssistantRun).filter_by(source_message_id=message.id).count() == 0
    assert db.query(BillingReservation).count() == 0
