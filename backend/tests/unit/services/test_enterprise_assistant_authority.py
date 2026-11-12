import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base
from app.core.exceptions import ConflictError, ForbiddenError
from app.models.billing_ledger import BillingReservation
from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun, EnterpriseChatMessage, EnterpriseConversation
from app.models.enterprise_org import Enterprise, EnterpriseGroup, EnterpriseGroupMember, EnterpriseMember
from app.models.user import User
from app.services.enterprise_assistant_runtime_service import (
    EnterpriseAssistantToolExecutor,
    RunCheckpoint,
    SupersededByAuthorityDirectiveError,
    _assert_no_newer_authority_directive,
    _digest,
    _finalize_runtime,
    _rank,
    _role_snapshot,
)


@pytest.mark.parametrize(
    ("member_role", "group_role", "room_kind", "expected"),
    [
        ("owner", None, "company", 4),
        ("admin", None, "company", 3),
        ("member", "leader", "group", 2),
        ("member", "leader", "direct", 1),
        ("member", "member", "group", 1),
        (None, "leader", "group", 2),
        ("unknown", "member", "group", 0),
    ],
)
def test_enterprise_role_priority_is_scoped_to_room(member_role, group_role, room_kind, expected):
    assert _rank(member_role, group_role, room_kind) == expected


def _authority_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'authority.db'}")
    Base.metadata.create_all(engine, tables=[
        User.__table__, EnterpriseMember.__table__, EnterpriseGroupMember.__table__,
        Enterprise.__table__, EnterpriseGroup.__table__, EnterpriseConversation.__table__,
        EnterpriseAssistantRun.__table__, EnterpriseAssistantRuntimeState.__table__,
        EnterpriseChatMessage.__table__, BillingReservation.__table__,
    ])
    db = Session(engine)
    db.add_all([
        User(id=1, username="owner", password="x", email="owner@example.invalid", role="user", status=1),
        User(id=2, username="member-a", password="x", email="a@example.invalid", role="user", status=1),
        User(id=3, username="member-b", password="x", email="b@example.invalid", role="user", status=1),
        User(id=4, username="member-c", password="x", email="c@example.invalid", role="user", status=1),
        User(id=5, username="member-d", password="x", email="d@example.invalid", role="user", status=1),
    ])
    db.add_all([
        EnterpriseMember(enterprise_id=10, user_id=1, role="owner", status="active"),
        EnterpriseMember(enterprise_id=10, user_id=2, role="admin", status="active"),
        EnterpriseMember(enterprise_id=10, user_id=3, role="member", status="active"),
        EnterpriseMember(enterprise_id=10, user_id=4, role="member", status="active"),
        EnterpriseMember(enterprise_id=10, user_id=5, role="member", status="active"),
        EnterpriseGroupMember(enterprise_id=10, group_id=20, user_id=2, role="member", status="active"),
        EnterpriseGroupMember(enterprise_id=10, group_id=20, user_id=3, role="member", status="active"),
        EnterpriseGroupMember(enterprise_id=10, group_id=20, user_id=4, role="leader", status="active"),
        EnterpriseGroupMember(enterprise_id=10, group_id=20, user_id=5, role="member", status="active"),
        EnterpriseGroup(id=20, enterprise_id=10, name="小组", status="active"),
        EnterpriseChatMessage(
            id=100, enterprise_id=10, conversation_id=30, sender_id=3,
            sender_type="human", client_message_id="old", content="@小菱 请处理当前任务",
        ),
    ])
    db.commit()
    return engine, db


@pytest.mark.parametrize("sender_id", [2, 4])
def test_newer_admin_or_group_leader_directive_blocks_member_run(tmp_path, sender_id):
    engine, db = _authority_db(tmp_path)
    try:
        db.add(EnterpriseChatMessage(
            id=101, enterprise_id=10, conversation_id=30, sender_id=sender_id,
            sender_type="human", client_message_id="new", content="@小菱 暂停并先复核",
            sender_authority_rank=3 if sender_id == 2 else 2,
        ))
        db.commit()
        with pytest.raises(ConflictError, match="新的同级或更高权限"):
            _assert_no_newer_authority_directive(
                db, enterprise_id=10,
                room=SimpleNamespace(id=30, kind="group", group_id=20),
                source_message_id=100, initiator_id=3, initiator_rank=1,
            )
    finally:
        db.close()
        engine.dispose()


def test_newer_lower_rank_or_non_xiaoling_message_does_not_override_owner(tmp_path):
    engine, db = _authority_db(tmp_path)
    try:
        source = db.query(EnterpriseChatMessage).filter_by(id=100).one()
        source.sender_id = 1
        db.add_all([
            EnterpriseChatMessage(
                id=101, enterprise_id=10, conversation_id=30, sender_id=3,
                sender_type="human", client_message_id="lower", content="@小菱 继续处理",
                sender_authority_rank=1,
            ),
            EnterpriseChatMessage(
                id=102, enterprise_id=10, conversation_id=30, sender_id=2,
                sender_type="human", client_message_id="not-addressed", content="我觉得先看看情况",
            ),
        ])
        db.commit()
        _assert_no_newer_authority_directive(
            db, enterprise_id=10,
            room=SimpleNamespace(id=30, kind="group", group_id=20),
            source_message_id=100, initiator_id=1, initiator_rank=4,
        )
    finally:
        db.close()
        engine.dispose()


def test_same_rank_peer_directive_blocks_member_run(tmp_path):
    engine, db = _authority_db(tmp_path)
    try:
        db.add(EnterpriseChatMessage(
            id=101, enterprise_id=10, conversation_id=30, sender_id=5,
            sender_type="human", client_message_id="peer", content="@小菱 先停止",
            sender_authority_rank=1,
        ))
        db.commit()
        with pytest.raises(ConflictError, match="新的同级或更高权限"):
            _assert_no_newer_authority_directive(
                db, enterprise_id=10,
                room=SimpleNamespace(id=30, kind="group", group_id=20),
                source_message_id=100, initiator_id=3, initiator_rank=1,
            )
    finally:
        db.close()
        engine.dispose()


@pytest.mark.parametrize("room_kind", ["company", "group", "direct"])
def test_disabled_account_has_no_current_message_authority(tmp_path, room_kind):
    engine, db = _authority_db(tmp_path)
    try:
        db.query(User).filter_by(id=2).one().status = 0
        db.commit()
        message = EnterpriseChatMessage(
            id=101, enterprise_id=10, conversation_id=30, sender_id=2,
            sender_type="human", client_message_id="disabled", content="@小菱 旧指令",
        )
        room = SimpleNamespace(id=30, kind=room_kind, group_id=20 if room_kind == "group" else None)
        snapshot = _role_snapshot(db, enterprise_id=10, room=room, messages=[message], initiator_id=3)
        assert snapshot["messages"][0]["rank"] == 0
        assert snapshot["highest_authority_user_ids"] == []
    finally:
        db.close()
        engine.dispose()


def test_removed_group_member_no_longer_has_authority_in_that_group(tmp_path):
    engine, db = _authority_db(tmp_path)
    try:
        message = EnterpriseChatMessage(
            id=101, enterprise_id=10, conversation_id=30, sender_id=2,
            sender_type="human", client_message_id="removed", content="@小菱 旧指令",
        )
        room = SimpleNamespace(id=30, kind="group", group_id=20)
        before = _role_snapshot(db, enterprise_id=10, room=room, messages=[message], initiator_id=3)
        assert before["messages"][0]["rank"] == 3
        db.query(EnterpriseGroupMember).filter_by(enterprise_id=10, group_id=20, user_id=2).one().status = "removed"
        db.commit()
        after = _role_snapshot(db, enterprise_id=10, room=room, messages=[message], initiator_id=3)
        assert after["messages"][0]["rank"] == 0
        assert after["highest_authority_user_ids"] == []
        company = _role_snapshot(db, enterprise_id=10, room=SimpleNamespace(id=31, kind="company", group_id=None),
                                 messages=[message], initiator_id=3)
        assert company["messages"][0]["rank"] == 3
    finally:
        db.close()
        engine.dispose()


def test_removed_enterprise_member_cannot_keep_group_leader_authority(tmp_path):
    engine, db = _authority_db(tmp_path)
    try:
        db.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=4).one().status = "removed"
        db.commit()
        message = EnterpriseChatMessage(
            id=101, enterprise_id=10, conversation_id=30, sender_id=4,
            sender_type="human", client_message_id="removed-company", content="@小菱 旧指令",
        )
        snapshot = _role_snapshot(db, enterprise_id=10, room=SimpleNamespace(id=30, kind="group", group_id=20),
                                  messages=[message], initiator_id=3)
        assert snapshot["messages"][0]["rank"] == 0
    finally:
        db.close()
        engine.dispose()


@pytest.mark.parametrize("revocation", ["group", "account"])
def test_send_time_authority_survives_later_author_revocation(tmp_path, revocation):
    engine, db = _authority_db(tmp_path)
    try:
        if revocation == "group":
            db.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=2).one().role = "member"
            db.query(EnterpriseGroupMember).filter_by(
                enterprise_id=10, group_id=20, user_id=2,
            ).one().role = "leader"
        sender_authority_rank = 2 if revocation == "group" else 3
        db.add(EnterpriseChatMessage(
            id=101, enterprise_id=10, conversation_id=30, sender_id=2,
            sender_type="human", client_message_id="revoked-newer", content="@小菱 旧指令",
            sender_authority_rank=sender_authority_rank,
        ))
        if revocation == "group":
            db.query(EnterpriseGroupMember).filter_by(enterprise_id=10, group_id=20, user_id=2).one().status = "removed"
        else:
            db.query(User).filter_by(id=2).one().status = 0
        db.commit()
        current = _role_snapshot(
            db, enterprise_id=10, room=SimpleNamespace(id=30, kind="group", group_id=20),
            messages=[db.query(EnterpriseChatMessage).filter_by(id=101).one()], initiator_id=3,
        )
        assert current["messages"][0]["rank"] == 0
        with pytest.raises(ConflictError, match="新的同级或更高权限"):
            _assert_no_newer_authority_directive(
                db, enterprise_id=10, room=SimpleNamespace(id=30, kind="group", group_id=20),
                source_message_id=100, initiator_id=3, initiator_rank=1,
            )
    finally:
        db.close()
        engine.dispose()


def test_current_role_change_updates_group_message_authority(tmp_path):
    engine, db = _authority_db(tmp_path)
    try:
        message = EnterpriseChatMessage(
            id=101, enterprise_id=10, conversation_id=30, sender_id=2,
            sender_type="human", client_message_id="role-change", content="@小菱 指令",
        )
        room = SimpleNamespace(id=30, kind="group", group_id=20)
        before = _role_snapshot(db, enterprise_id=10, room=room, messages=[message], initiator_id=3)
        db.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=2).one().role = "member"
        db.query(EnterpriseGroupMember).filter_by(enterprise_id=10, group_id=20, user_id=2).one().role = "leader"
        db.commit()
        after = _role_snapshot(db, enterprise_id=10, room=room, messages=[message], initiator_id=3)
        assert before["messages"][0]["rank"] == 3
        assert after["messages"][0]["rank"] == 2
        assert _digest(before) != _digest(after)
    finally:
        db.close()
        engine.dispose()


def test_tool_executor_does_not_swallow_superseding_directive(tmp_path, monkeypatch):
    engine, db = _authority_db(tmp_path)
    try:
        executor = EnterpriseAssistantToolExecutor(
            db, enterprise_id=10, conversation_id=30, run_public_id="ear-test",
            lease_generation=1, actor_user_id=3,
        )

        def superseded(**_kwargs):
            raise SupersededByAuthorityDirectiveError("新同级指令")

        monkeypatch.setattr(executor, "_current", superseded)
        call = SimpleNamespace(name="search_enterprise_knowledge", arguments={}, call_id="call-1")
        with pytest.raises(SupersededByAuthorityDirectiveError):
            asyncio.run(executor.execute(call))
    finally:
        db.close()
        engine.dispose()


def test_completed_answer_is_suppressed_if_authority_directive_arrives_before_publish(tmp_path, monkeypatch):
    engine, db = _authority_db(tmp_path)
    try:
        db.add(Enterprise(id=10, name="甲企业", owner_id=1, seat_limit=5,
                          status="active", paid_order_id="paid-10"))
        room = EnterpriseConversation(
            id=30, enterprise_id=10, kind="group", conversation_key="group:20",
            group_id=20, created_by=1,
        )
        run = EnterpriseAssistantRun(
            public_id="authority-race", request_key="assistant:authority-race",
            enterprise_id=10, conversation_id=30, group_id=20,
            initiator_user_id=3, source_message_id=100, status="running",
            attempt_count=1, model_name="deepseek-flash", prompt_sha256="a" * 64,
        )
        db.add_all([room, run])
        db.flush()
        source = db.query(EnterpriseChatMessage).filter_by(id=100).one()
        snapshot = _role_snapshot(
            db, enterprise_id=10, room=room, messages=[source], initiator_id=3,
        )
        checkpoint = RunCheckpoint(
            run_id=run.public_id, model="deepseek-flash", transcript=[], tools=[],
            status="completed", output_text="过期回答，不应发布",
        )
        state = EnterpriseAssistantRuntimeState(
            assistant_run_id=run.id, enterprise_id=10, conversation_id=30,
            group_id=20, initiator_user_id=3, source_message_id=100,
            status="completed", checkpoint_json=json.dumps(checkpoint.to_dict()),
            permission_snapshot_json=json.dumps(snapshot), permission_snapshot_sha256=_digest(snapshot),
            lease_generation=1, billing_invocation_count=1, version=1,
        )
        placeholder = EnterpriseChatMessage(
            enterprise_id=10, conversation_id=30, sender_id=None, sender_type="assistant",
            assistant_run_id=run.id, client_message_id=run.public_id, content="小菱已收到请求，正在处理。",
        )
        db.add(EnterpriseChatMessage(
            id=101, enterprise_id=10, conversation_id=30, sender_id=2,
            sender_type="human", client_message_id="new-admin-directive",
            content="@小菱 请先暂停并处理新问题",
        ))
        reservation = BillingReservation(
            request_key="enterprise-assistant:authority-race:1:1", model_name="deepseek-flash",
            requested_milli=1000, enterprise_id=10, user_id=3, usage_kind="member",
            status="settled", actual_milli=10, input_tokens=20, output_tokens=10,
        )
        db.add_all([state, placeholder, reservation])
        db.commit()

        monkeypatch.setattr("app.services.enterprise_assistant_service._require_subscription",
                            lambda *_args: None)
        monkeypatch.setattr("app.services.billing_checkout_service.enterprise_subscription_refund_pending",
                            lambda *_args: False)
        monkeypatch.setattr("app.services.billing_checkout_service.enterprise_subscription_status",
                            lambda *_args, **_kwargs: "active")
        factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

        with pytest.raises(ForbiddenError, match="权限或订阅已变化"):
            _finalize_runtime(
                factory, enterprise_id=10, conversation_id=30, run_public_id="authority-race",
            )

        db.expire_all()
        saved_run = db.query(EnterpriseAssistantRun).filter_by(public_id="authority-race").one()
        saved_state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=saved_run.id).one()
        saved_message = db.query(EnterpriseChatMessage).filter_by(assistant_run_id=saved_run.id).one()
        assert saved_run.status == "suppressed"
        assert saved_run.error_code == "authority_directive_superseded"
        assert saved_run.response_text is None
        assert saved_state.status == "suppressed"
        assert saved_state.error_code == "authority_directive_superseded"
        assert "新的同级或更高权限" in saved_message.content
        assert "过期回答" not in saved_message.content
        assert db.query(BillingReservation).filter_by(request_key=reservation.request_key).one().status == "settled"
    finally:
        db.close()
        engine.dispose()
