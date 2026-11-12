"""企业小菱公告审批的持久队列、假上游和幂等回放闭环。"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.core.exceptions import ForbiddenError
from app.models.billing_ledger import (
    BillingCreditBatch,
    BillingLimit,
    BillingOrder,
    BillingPaymentEvent,
    BillingRateCatalog,
    BillingRateCatalogEvent,
    BillingRateCatalogItem,
    BillingRefund,
    BillingReservation,
    BillingReservationLimit,
    BillingReservationPart,
    BillingSubscription,
    BillingSubscriptionPeriod,
    BillingWallet,
)
from app.models.enterprise_asset import EnterpriseAssistantAssetInvocation
from app.models.enterprise_assistant_clarification import (
    EnterpriseAssistantClarification,
    EnterpriseAssistantClarificationDecision,
    EnterpriseAssistantClarificationReview,
)
from app.models.enterprise_assistant_runtime import EnterpriseAssistantApproval, EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun, EnterpriseChatMessage, EnterpriseConversation
from app.models.enterprise_content import EnterpriseAnnouncement
from app.models.enterprise_org import (
    Enterprise,
    EnterpriseAuditEvent,
    EnterpriseGroup,
    EnterpriseGroupMember,
    EnterpriseMember,
)
from app.models.user import User
from app.services import enterprise_assistant_runtime_service as runtime


def test_enterprise_admin_cannot_self_approve_announcement(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[
        User.__table__, Enterprise.__table__, EnterpriseMember.__table__, EnterpriseConversation.__table__,
        EnterpriseChatMessage.__table__, EnterpriseAssistantRun.__table__,
        EnterpriseAssistantRuntimeState.__table__, EnterpriseAssistantApproval.__table__,
        EnterpriseAnnouncement.__table__,
        BillingOrder.__table__, BillingPaymentEvent.__table__, BillingRefund.__table__,
        BillingSubscription.__table__, BillingSubscriptionPeriod.__table__,
    ])
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    payload = {"title": "管理员自发公告", "content": "不能自批"}
    with factory.begin() as db:
        db.add(User(id=1, username="公告管理员", password="x", role="user", status=1))
        db.add(Enterprise(
            id=10, name="自批测试企业", owner_id=1, seat_limit=3, status="active", paid_order_id="self-approval-order",
        ))
        db.add(EnterpriseMember(enterprise_id=10, user_id=1, role="owner", status="active"))
        room = EnterpriseConversation(
            id=20, enterprise_id=10, kind="company", conversation_key="company", created_by=1,
        )
        db.add(room)
        db.add(EnterpriseChatMessage(
            id=30, enterprise_id=10, conversation_id=20, sender_id=1, sender_type="human",
            client_message_id="self-approval", content="@小菱 请发布一条公告。",
        ))
        run = EnterpriseAssistantRun(
            id=40, public_id="ear_self_approval", request_key="enterprise-assistant:self-approval",
            enterprise_id=10, conversation_id=20, group_id=None, initiator_user_id=1,
            source_message_id=30, status="awaiting_approval", prompt_sha256="a" * 64,
        )
        db.add(run)
        db.add(EnterpriseAssistantRuntimeState(
            assistant_run_id=40, enterprise_id=10, conversation_id=20, group_id=None,
            initiator_user_id=1, source_message_id=30, status=runtime.WAITING_APPROVAL,
            checkpoint_json="{}", permission_snapshot_json="{}", permission_snapshot_sha256="b" * 64,
            lease_generation=1, billing_invocation_count=1, version=1,
        ))
        db.add(EnterpriseAssistantApproval(
            assistant_run_id=40, enterprise_id=10, conversation_id=20, group_id=None,
            initiator_user_id=1, source_message_id=30, run_public_id="ear_self_approval",
            call_id="call_self_approval", tool_name="propose_enterprise_announcement",
            required_role="enterprise_admin", status="pending", arguments_json=json.dumps(payload),
            arguments_sha256=runtime._digest(payload), requested_message_id=30,
        ))

    monkeypatch.setattr(runtime.assistant_service, "_require_subscription", lambda *_args: None)
    monkeypatch.setattr(runtime, "_live_role_snapshot", lambda *_args: {})
    with factory() as db:
        with pytest.raises(ForbiddenError, match="公告发起人不能批准或拒绝自己的审批"):
            asyncio.run(runtime.decide_approval(
                db, enterprise_id=10, approval_id=1, user_id=1, decision="approve",
            ))
    with factory() as db:
        approval = db.query(EnterpriseAssistantApproval).one()
        assert approval.status == "pending" and approval.decided_by is None
        assert db.query(EnterpriseAnnouncement).count() == 0
        assert db.query(EnterpriseAssistantRun).one().status == "awaiting_approval"
        state = db.query(EnterpriseAssistantRuntimeState).one()
        assert state.status == runtime.WAITING_APPROVAL and state.lease_generation == 1
    engine.dispose()


def test_announcement_tool_runs_through_admin_approval_and_replays_source_message(monkeypatch):
    monkeypatch.setattr(runtime, "_CROSS_SCOPE_ATTRIBUTION_VERIFIED", True)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[
        User.__table__, Enterprise.__table__, EnterpriseMember.__table__, EnterpriseAuditEvent.__table__,
        EnterpriseGroup.__table__, EnterpriseGroupMember.__table__,
        EnterpriseConversation.__table__, EnterpriseChatMessage.__table__, EnterpriseAssistantRun.__table__,
        EnterpriseAssistantRuntimeState.__table__, EnterpriseAssistantApproval.__table__,
        EnterpriseAssistantClarification.__table__, EnterpriseAssistantClarificationDecision.__table__,
        EnterpriseAssistantClarificationReview.__table__,
        EnterpriseAssistantAssetInvocation.__table__, EnterpriseAnnouncement.__table__,
        BillingWallet.__table__, BillingCreditBatch.__table__,
        BillingLimit.__table__, BillingReservation.__table__, BillingReservationPart.__table__,
        BillingReservationLimit.__table__,
        BillingRateCatalog.__table__, BillingRateCatalogItem.__table__, BillingRateCatalogEvent.__table__,
    ])
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with factory.begin() as db:
        from app.services.billing_rate_catalog_service import seed_test_baseline

        seed_test_baseline(db)
        db.add_all([
            User(id=1, username="公告所有者", password="x", role="user", status=1),
            User(id=2, username="公告发起人", password="x", role="user", status=1),
            User(id=3, username="无审批权成员", password="x", role="user", status=1),
        ])
        db.add(Enterprise(
            id=10, name="公告审批企业", owner_id=1, seat_limit=5, status="active", paid_order_id="announcement-order",
        ))
        db.add_all([
            EnterpriseMember(enterprise_id=10, user_id=1, role="owner", status="active"),
            EnterpriseMember(enterprise_id=10, user_id=2, role="member", status="active"),
            EnterpriseMember(enterprise_id=10, user_id=3, role="member", status="active"),
        ])
        db.add(EnterpriseConversation(
            id=20, enterprise_id=10, kind="company", conversation_key="company", created_by=1,
        ))
        db.add(EnterpriseChatMessage(
            id=30, enterprise_id=10, conversation_id=20, sender_id=2, sender_type="human",
            client_message_id="announcement-request", content="@小菱 请发布今晚维护公告。",
        ))
        wallet = BillingWallet(owner_type="enterprise", owner_id=10)
        db.add(wallet)
        db.flush()
        db.add(BillingCreditBatch(
            wallet_id=wallet.id, grant_key="announcement-credits", kind="purchase",
            issued_milli=1_000_000, remaining_milli=1_000_000, reserved_milli=0,
            refund_reserved_milli=0, expires_at=None,
        ))
        db.add(BillingLimit(
            limit_key="normal:10:announcement", enterprise_id=10, category="normal",
            group_id=None, user_id=None, period_start=now - timedelta(days=1),
            period_end=now + timedelta(days=30), limit_milli=1_000_000, used_milli=0, reserved_milli=0,
        ))

    monkeypatch.setattr(runtime.assistant_service, "_model_config", lambda _db: (
        SimpleNamespace(
            api_key="fake-enterprise-key", base_url="https://api.deepseek.com/v1",
            model="deepseek-v4-flash", provider="deepseek", source="system", timeout_seconds=5,
        ),
        "deepseek-v4-flash",
    ))
    monkeypatch.setattr(runtime.assistant_service, "_require_active_limit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime.enterprise_org_service, "require_enterprise_execution", lambda *_args: None)
    monkeypatch.setattr(runtime.enterprise_org_service, "_billing_access_suspended", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        runtime.billing_checkout_service, "enterprise_subscription_status", lambda *_args, **_kwargs: "active",
    )
    monkeypatch.setattr(runtime.enterprise_knowledge_service, "search_entries", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        runtime.enterprise_knowledge_attachment_service, "search_attachments", lambda *_args, **_kwargs: [],
    )

    announcement_payload = {"title": "今晚维护公告", "content": "今晚 23:00 至 23:30 维护服务。"}
    responses = [
        {
            "status": "completed",
            "model": "deepseek-v4-flash",
            "output": [{
                "type": "function_call", "id": "item_announcement", "call_id": "call_announcement",
                "name": "propose_enterprise_announcement",
                "arguments": json.dumps(announcement_payload, ensure_ascii=False),
            }],
            "usage": {"input_tokens": 80, "output_tokens": 20},
        },
        {
            "status": "completed",
            "model": "deepseek-v4-flash",
            "output": [{
                "type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "公告已由企业所有者批准并发布。"},
                ],
            }],
            "usage": {"input_tokens": 110, "output_tokens": 18},
        },
    ]
    provider_payloads = []

    async def fake_stream(_transport, payload):
        provider_payloads.append(payload)
        response = responses[len(provider_payloads) - 1]
        yield {"type": "response.completed", "response": response}

    monkeypatch.setattr(runtime.NativeResponsesTransport, "_stream_without_billing", fake_stream)

    with factory() as db:
        queued = asyncio.run(runtime.run_for_message(
            db, enterprise_id=10, user_id=2, conversation_id=20, source_message_id=30,
        ))
        run_id = queued["run_id"]
    assert queued["status"] == "queued"

    asyncio.run(runtime.execute_queued_run(
        engine, enterprise_id=10, conversation_id=20, run_public_id=run_id,
    ))

    with factory() as db:
        run = db.query(EnterpriseAssistantRun).filter_by(public_id=run_id).one()
        state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).one()
        approvals = db.query(EnterpriseAssistantApproval).filter_by(assistant_run_id=run.id).all()
        assert len(approvals) == 1, {
            "run_status": run.status,
            "run_error": run.error_code,
            "state_status": state.status,
            "state_error": state.error_code,
            "checkpoint_tail": state.checkpoint_json[-1200:],
            "provider_calls": len(provider_payloads),
        }
        approval = approvals[0]
        assert run.status == "awaiting_approval"
        assert state.status == runtime.WAITING_APPROVAL
        assert state.lease_expires_at is None
        assert approval.status == "pending"
        assert approval.initiator_user_id == 2
        assert approval.required_role == "enterprise_admin"
        assert json.loads(approval.arguments_json) == announcement_payload
        assert db.query(EnterpriseAnnouncement).count() == 0
        assert len(provider_payloads) == 1
        assert any(tool.get("name") == "propose_enterprise_announcement" for tool in provider_payloads[0]["tools"])
        approval_id = approval.id

    with factory() as db:
        with pytest.raises(ForbiddenError):
            asyncio.run(runtime.decide_approval(
                db,
                enterprise_id=10,
                approval_id=approval_id,
                user_id=3,
                decision="approve",
                reason="普通成员无审批权",
            ))
    with factory() as db:
        approval = db.query(EnterpriseAssistantApproval).filter_by(id=approval_id).one()
        assert approval.status == "pending"
        assert db.query(EnterpriseAnnouncement).count() == 0
    assert len(provider_payloads) == 1

    with factory() as db:
        approved = asyncio.run(runtime.decide_approval(
            db,
            enterprise_id=10,
            approval_id=approval_id,
            user_id=1,
            decision="approve",
            reason="内容符合本企业维护通知规则",
        ))

    assert approved["status"] == "completed"
    assert len(provider_payloads) == 2
    assert provider_payloads[1]["input"][-1]["type"] == "function_call_output"
    assert provider_payloads[1]["input"][-1]["call_id"] == "call_announcement"
    tool_result = json.loads(provider_payloads[1]["input"][-1]["output"])
    assert tool_result["status"] == "success"
    with factory() as db:
        run = db.query(EnterpriseAssistantRun).filter_by(public_id=run_id).one()
        approval = db.query(EnterpriseAssistantApproval).filter_by(id=approval_id).one()
        announcement = db.query(EnterpriseAnnouncement).one()
        reservations = db.query(BillingReservation).order_by(BillingReservation.id).all()
        assistant_message = db.query(EnterpriseChatMessage).filter_by(
            assistant_run_id=run.id, sender_type="assistant",
        ).one()
        assert run.status == "completed"
        assert assistant_message.content == "公告已由企业所有者批准并发布。"
        assert approval.status == "executed" and approval.decided_by == 1
        assert approval.result_object_id == announcement.id
        assert tool_result["output"]["announcement_id"] == announcement.id
        assert (announcement.enterprise_id, announcement.author_id) == (10, 1)
        assert (announcement.title, announcement.content) == (
            announcement_payload["title"], announcement_payload["content"],
        )
        assert len(reservations) == 2 and all(row.status == "settled" for row in reservations)

    with factory() as db:
        replay = asyncio.run(runtime.run_for_message(
            db, enterprise_id=10, user_id=2, conversation_id=20, source_message_id=30,
        ))
        assert replay["run_id"] == run_id and replay["status"] == "completed"
        assert replay["message"].content == "公告已由企业所有者批准并发布。"
    assert len(provider_payloads) == 2
    with factory() as db:
        assert db.query(EnterpriseAnnouncement).count() == 1
        assert db.query(BillingReservation).count() == 2
    engine.dispose()
