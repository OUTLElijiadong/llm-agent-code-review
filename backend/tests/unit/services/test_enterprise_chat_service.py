"""企业会话隔离与消息幂等：不得让平台身份绕过企业或私聊边界。"""
import asyncio
import json

import pytest
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.enterprise_chat import AssistantRunIn, request_assistant
from app.api.v1.enterprise_chat import router as chat_router
from app.api.v1.enterprise_content import router as content_router
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import (
    AppError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    PaymentRequiredError,
    ValidationError,
)
from app.models.billing_ledger import (
    BillingOrder,
    BillingPaymentEvent,
    BillingRefund,
    BillingSubscription,
    BillingSubscriptionPeriod,
    BillingWallet,
)
from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun, EnterpriseChatMessage, EnterpriseConversation
from app.models.enterprise_content import EnterpriseAnnouncement, EnterpriseForumPost, EnterpriseForumReply
from app.models.enterprise_knowledge import EnterpriseKnowledgeAttachment, EnterpriseKnowledgeAttachmentChunk
from app.models.enterprise_org import (
    Enterprise,
    EnterpriseAuditEvent,
    EnterpriseGroup,
    EnterpriseGroupMember,
    EnterpriseMember,
)
from app.models.user import User
from app.services import enterprise_assistant_runtime_service as assistant_runtime
from app.services import enterprise_chat_service as chat
from app.services import enterprise_content_service as content
from app.services.deepseek_responses_runtime import RunCheckpoint


@pytest.fixture
def orgdb():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[
        User.__table__, Enterprise.__table__, EnterpriseMember.__table__, EnterpriseGroup.__table__,
        EnterpriseGroupMember.__table__, EnterpriseConversation.__table__, EnterpriseAssistantRun.__table__,
        EnterpriseAssistantRuntimeState.__table__,
        EnterpriseChatMessage.__table__,
        EnterpriseKnowledgeAttachment.__table__, EnterpriseKnowledgeAttachmentChunk.__table__,
        EnterpriseForumPost.__table__, EnterpriseForumReply.__table__, EnterpriseAnnouncement.__table__,
        EnterpriseAuditEvent.__table__, BillingOrder.__table__, BillingPaymentEvent.__table__,
        BillingRefund.__table__,
        BillingSubscription.__table__, BillingSubscriptionPeriod.__table__, BillingWallet.__table__,
    ])
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    for n, role in [(1, "admin"), (2, "user"), (3, "user"), (4, "user")]:
        session.add(User(id=n, username=f"u{n}", password="x", email=f"u{n}@example.com", role=role, status=1))
    session.add_all([
        Enterprise(id=10, name="甲", owner_id=1, seat_limit=5, status="active", paid_order_id="order-a"),
        Enterprise(id=20, name="乙", owner_id=4, seat_limit=5, status="active", paid_order_id="order-b"),
        EnterpriseMember(enterprise_id=10, user_id=1, role="owner", status="active"),
        EnterpriseMember(enterprise_id=10, user_id=2, role="member", status="active"),
        EnterpriseMember(enterprise_id=10, user_id=3, role="member", status="active"),
        EnterpriseMember(enterprise_id=20, user_id=4, role="owner", status="active"),
        EnterpriseGroup(id=100, enterprise_id=10, name="甲组", status="active"),
        EnterpriseGroup(id=200, enterprise_id=20, name="乙组", status="active"),
        EnterpriseGroupMember(enterprise_id=10, group_id=100, user_id=2, role="member", status="active"),
    ])
    session.commit()
    yield session
    session.close()
    engine.dispose()


def test_company_history_and_cross_company_denial(orgdb):
    room = chat.open_conversation(orgdb, 10, 1, "company")
    chat.send_message(orgdb, 10, 1, room.id, "msg-1", "欢迎加入")
    # 后加入成员可见公司群历史，其他企业不可通过全局 ID 读取。
    assert [m.content for m in chat.list_messages(orgdb, 10, 2, room.id)] == ["欢迎加入"]
    with pytest.raises(ForbiddenError):
        chat.list_messages(orgdb, 10, 4, room.id)
    with pytest.raises(NotFoundError):
        chat.list_messages(orgdb, 20, 4, room.id)


def test_message_keyset_pagination_latest_older_and_newer(orgdb):
    room = chat.open_conversation(orgdb, 10, 1, "company")
    orgdb.add_all([
        EnterpriseChatMessage(enterprise_id=10, conversation_id=room.id, sender_id=1,
                              client_message_id=f"msg-{i}", content=str(i))
        for i in range(105)
    ])
    orgdb.commit()
    latest = chat.list_messages(orgdb, 10, 2, room.id, limit=100)
    assert [m.content for m in latest] == [str(i) for i in range(5, 105)]
    # 0 明确表示从头增量拉取，不能混同于初次加载最新一页。
    assert [m.content for m in chat.list_messages(orgdb, 10, 2, room.id, after_id=0, limit=100)] == [
        str(i) for i in range(100)
    ]
    older = chat.list_messages(orgdb, 10, 2, room.id, before_id=latest[0].id, limit=100)
    assert [m.content for m in older] == [str(i) for i in range(5)]
    chat.send_message(orgdb, 10, 1, room.id, "new", "new")
    assert [m.content for m in chat.list_messages(orgdb, 10, 2, room.id, after_id=latest[-1].id)] == ["new"]
    with pytest.raises(ValidationError):
        chat.list_messages(orgdb, 10, 2, room.id, after_id=latest[-1].id, before_id=latest[0].id)
    with pytest.raises(ValidationError):
        chat.list_messages(orgdb, 10, 2, room.id, after_id=0, before_id=latest[0].id)
    with pytest.raises(ForbiddenError):
        chat.list_messages(orgdb, 10, 4, room.id, before_id=latest[0].id)


def test_group_membership_and_revocation(orgdb):
    room = chat.open_conversation(orgdb, 10, 2, "group", group_id=100)
    chat.send_message(orgdb, 10, 2, room.id, "m1", "组内内容")
    with pytest.raises(ForbiddenError):
        chat.list_messages(orgdb, 10, 3, room.id)
    # 企业所有者也不因管理身份自动成为每个小组群的成员。
    with pytest.raises(ForbiddenError):
        chat.list_messages(orgdb, 10, 1, room.id)
    member = orgdb.query(EnterpriseGroupMember).filter_by(enterprise_id=10, group_id=100, user_id=2).one()
    member.status = "removed"
    orgdb.commit()
    with pytest.raises(ForbiddenError):
        chat.list_messages(orgdb, 10, 2, room.id)
    assert room not in chat.list_conversations(orgdb, 10, 2)


def test_direct_private_even_against_company_owner_and_stable_pair(orgdb):
    room = chat.open_conversation(orgdb, 10, 2, "direct", other_user_id=3)
    assert chat.open_conversation(orgdb, 10, 3, "direct", other_user_id=2).id == room.id
    chat.send_message(orgdb, 10, 2, room.id, "m2", "私聊")
    with pytest.raises(ForbiddenError):
        chat.list_messages(orgdb, 10, 1, room.id)
    assert room not in chat.list_conversations(orgdb, 10, 1)


def test_message_idempotence_requires_same_content(orgdb):
    room = chat.open_conversation(orgdb, 10, 2, "company")
    first = chat.send_message(orgdb, 10, 2, room.id, "request-1", "原消息")
    assert chat.send_message(orgdb, 10, 2, room.id, "request-1", "原消息").id == first.id
    with pytest.raises(ConflictError):
        chat.send_message(orgdb, 10, 2, room.id, "request-1", "不同消息")
    assert len(chat.list_messages(orgdb, 10, 2, room.id)) == 1


def test_queued_run_is_durable_listable_and_claimed_with_a_new_fencing_lease(orgdb):
    room = chat.open_conversation(orgdb, 10, 2, "company")
    source = chat.send_message(orgdb, 10, 2, room.id, "source-queue", "@小菱 检查当前企业共享知识")
    snapshot = assistant_runtime._role_snapshot(
        orgdb, enterprise_id=10, room=room, messages=[source], initiator_id=2,
    )
    run = EnterpriseAssistantRun(
        public_id="ear_queued_test", request_key="queue-test", enterprise_id=10,
        conversation_id=room.id, initiator_user_id=2, source_message_id=source.id,
        status="queued", attempt_count=0, model_name="test-model", prompt_sha256="a" * 64,
    )
    orgdb.add(run)
    orgdb.flush()
    checkpoint = RunCheckpoint(
        run_id=run.public_id, model="test-model", transcript=[{"role": "user", "content": "prompt"}],
        tools=[], status="queued",
    )
    state = EnterpriseAssistantRuntimeState(
        assistant_run_id=run.id, enterprise_id=10, conversation_id=room.id,
        initiator_user_id=2, source_message_id=source.id, status="queued",
        checkpoint_json=assistant_runtime._json(checkpoint.to_dict()),
        permission_snapshot_json=assistant_runtime._json(snapshot),
        permission_snapshot_sha256=assistant_runtime._digest(snapshot),
        lease_generation=0, billing_invocation_count=0,
    )
    orgdb.add(state)
    orgdb.commit()

    visible = assistant_runtime.list_active_runs(orgdb, enterprise_id=10, user_id=2, conversation_id=room.id)
    assert [(item["run_id"], item["status"], item["source_message_id"]) for item in visible] == [
        ("ear_queued_test", "queued", source.id),
    ]
    claimed_run, claimed_state = assistant_runtime._claim_new_invocation(
        orgdb, enterprise_id=10, conversation_id=room.id, run_public_id=run.public_id,
        allow_statuses={"queued"}, actor_user_id=2,
    )
    claimed_checkpoint = RunCheckpoint.from_dict(json.loads(claimed_state.checkpoint_json))
    assert claimed_run.status == "running"
    assert claimed_state.status == "running"
    assert claimed_state.lease_generation == 1
    assert claimed_state.billing_invocation_count == 1
    assert claimed_checkpoint.status == "running"


def test_start_request_persists_queued_checkpoint_and_returns_without_model_call(orgdb, monkeypatch):
    # This exercises queue persistence with the unfinished feature gate opened
    # only inside the test; production keeps the gate closed.
    monkeypatch.setattr(assistant_runtime, "_CROSS_SCOPE_ATTRIBUTION_VERIFIED", True)
    orgdb.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=2).one().default_group_id = 100
    orgdb.commit()
    room = chat.open_conversation(orgdb, 10, 2, "company")
    source = chat.send_message(orgdb, 10, 2, room.id, "source-start", "@小菱 查企业政策")
    orgdb.add(BillingWallet(owner_type="enterprise", owner_id=10))
    orgdb.commit()
    monkeypatch.setattr(assistant_runtime.assistant_service, "_require_subscription", lambda *_: None)
    monkeypatch.setattr(assistant_runtime.assistant_service, "_require_active_limit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(assistant_runtime.assistant_service, "_model_config", lambda _db: (object(), "test-model"))
    monkeypatch.setattr(assistant_runtime.assistant_service.ledger, "available_milli", lambda *_: 1000)
    monkeypatch.setattr(assistant_runtime.enterprise_knowledge_service, "search_entries", lambda *_args, **_kwargs: [])

    first = asyncio.run(assistant_runtime.run_for_message(
        orgdb, enterprise_id=10, user_id=2, conversation_id=room.id, source_message_id=source.id,
    ))
    again = asyncio.run(assistant_runtime.run_for_message(
        orgdb, enterprise_id=10, user_id=2, conversation_id=room.id, source_message_id=source.id,
    ))
    state = orgdb.query(EnterpriseAssistantRuntimeState).filter_by(
        enterprise_id=10, conversation_id=room.id,
    ).one()
    checkpoint = RunCheckpoint.from_dict(json.loads(state.checkpoint_json))
    assert first["status"] == again["status"] == "queued"
    assert first["run_id"] == again["run_id"]
    assert state.status == checkpoint.status == "queued"
    assert checkpoint.context_metadata["initiator_user_id"] == 2
    assert state.lease_generation == state.billing_invocation_count == 0
    assert first["message"].content == "小菱已收到请求，正在处理。"
    assert orgdb.query(EnterpriseAssistantRun).filter_by(enterprise_id=10).count() == 1


def test_removed_member_cannot_read_even_their_own_private_messages(orgdb):
    room = chat.open_conversation(orgdb, 10, 2, "direct", other_user_id=3)
    chat.send_message(orgdb, 10, 2, room.id, "m", "历史")
    member = orgdb.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=2).one()
    member.status = "removed"
    orgdb.commit()
    with pytest.raises(ForbiddenError):
        chat.list_messages(orgdb, 10, 2, room.id)
    assert [m.content for m in chat.list_messages(orgdb, 10, 3, room.id)] == ["历史"]


def test_forum_is_organization_scoped_and_only_author_or_admin_can_remove(orgdb):
    post = content.create_post(orgdb, 10, 2, "企业帖子", "正文")
    reply = content.create_reply(orgdb, 10, 3, post.id, "回复")
    assert [row.id for row in content.replies(orgdb, 10, 2, post.id)] == [reply.id]
    with pytest.raises(ForbiddenError):
        content.posts(orgdb, 10, 4)
    with pytest.raises(NotFoundError):
        content.post_detail(orgdb, 20, 4, post.id)
    with pytest.raises(ForbiddenError):
        content.remove_post(orgdb, 10, 3, post.id)
    with pytest.raises(ForbiddenError):
        content.update_post(orgdb, 10, 3, post.id, "标题", "被篡改")
    assert content.update_post(orgdb, 10, 2, post.id, "新标题", "新正文").title == "新标题"
    with pytest.raises(ForbiddenError):
        content.remove_reply(orgdb, 10, 2, post.id, reply.id)
    content.remove_reply(orgdb, 10, 3, post.id, reply.id)
    assert content.replies(orgdb, 10, 2, post.id) == []
    content.remove_post(orgdb, 10, 1, post.id)
    with pytest.raises(NotFoundError):
        content.replies(orgdb, 10, 2, post.id)


def test_announcement_write_admin_only_and_cross_org_hidden(orgdb):
    with pytest.raises(ForbiddenError):
        content.save_announcement(orgdb, 10, 2, "公告", "正文")
    row = content.save_announcement(orgdb, 10, 1, "公告", "正文")
    assert [item.id for item in content.announcements(orgdb, 10, 2)] == [row.id]
    assert content.announcements(orgdb, 20, 4) == []
    with pytest.raises(NotFoundError):
        content.remove_announcement(orgdb, 20, 4, row.id)
    content.save_announcement(orgdb, 10, 1, "新标题", "更新", announcement_id=row.id)
    content.remove_announcement(orgdb, 10, 1, row.id)
    assert content.announcements(orgdb, 10, 2) == []
    assert orgdb.query(EnterpriseAuditEvent).filter_by(enterprise_id=10).count() == 3


def test_assistant_request_rejects_wrong_sender_and_requires_paid_scope(orgdb):
    room = chat.open_conversation(orgdb, 10, 2, "direct", other_user_id=3)
    message = chat.send_message(orgdb, 10, 2, room.id, "m", "@小菱 请分析")
    payload = AssistantRunIn(source_message_id=message.id)
    with pytest.raises(ForbiddenError):
        asyncio.run(request_assistant(10, room.id, payload, db=orgdb, user=orgdb.get(User, 3)))
    with pytest.raises(PaymentRequiredError):
        asyncio.run(request_assistant(10, room.id, payload, db=orgdb, user=orgdb.get(User, 2)))
    plain = chat.send_message(orgdb, 10, 2, room.id, "m2", "普通消息")
    with pytest.raises(ForbiddenError):
        asyncio.run(request_assistant(10, room.id, AssistantRunIn(source_message_id=plain.id), db=orgdb,
                                      user=orgdb.get(User, 2)))


def test_http_tenant_private_chat_and_assistant_requires_paid_scope(orgdb):
    app = FastAPI()
    app.include_router(chat_router)
    app.include_router(content_router)

    @app.exception_handler(AppError)
    async def on_error(_, exc):
        return JSONResponse(status_code=exc.http_status, content={"message": exc.message})

    def current_user(x_test_user: int = Header()):
        user = orgdb.get(User, x_test_user)
        user._authenticated_token_version = int(user.token_version or 0)
        return user

    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[get_db] = lambda: orgdb
    with TestClient(app) as client:
        room = client.post("/enterprises/10/conversations", headers={"x-test-user": "2"},
                           json={"kind": "direct", "other_user_id": 3}).json()["data"]["id"]
        url = f"/enterprises/10/conversations/{room}/messages"
        message = client.post(url, headers={"x-test-user": "2"},
                              json={"client_message_id": "http-1", "content": "@小菱 你好"})
        assert message.status_code == 200
        assert message.json()["data"]["assistant_available"] is False
        assert client.get(url, headers={"x-test-user": "1"}).status_code == 403
        assert client.get(url, headers={"x-test-user": "4"}).status_code == 403
        message_id = message.json()["data"]["message"]["id"]
        next_message = client.post(url, headers={"x-test-user": "2"},
                                   json={"client_message_id": "http-2", "content": "下一条"})
        next_id = next_message.json()["data"]["message"]["id"]
        assert [row["id"] for row in client.get(url, headers={"x-test-user": "2"}, params={"limit": 1})
                .json()["data"]["items"]] == [next_id]
        assert [row["id"] for row in client.get(url, headers={"x-test-user": "2"},
                                               params={"before_id": next_id, "limit": 1})
                .json()["data"]["items"]] == [message_id]
        assert [row["id"] for row in client.get(url, headers={"x-test-user": "2"},
                                               params={"after_id": message_id, "limit": 1})
                .json()["data"]["items"]] == [next_id]
        assert (client.get(url, headers={"x-test-user": "2"},
                           params={"after_id": 0, "before_id": next_id}).status_code == 400)
        result = client.post(f"/enterprises/10/conversations/{room}/assistant-runs", headers={"x-test-user": "2"},
                             json={"source_message_id": message_id})
        assert result.status_code == 402
        announcement = client.post("/enterprises/10/announcements", headers={"x-test-user": "2"},
                                   json={"title": "越权", "content": "正文"})
        assert announcement.status_code == 403
