"""Published enterprise asset sends must revalidate frozen billing groups on MySQL RR."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
import test_fresh_database_chain as chain
from sqlalchemy.orm import sessionmaker

import app.main  # noqa: F401 - register models for the fresh-chain fixture
from app.models.billing_ledger import BillingCreditBatch, BillingLimit, BillingReservation
from app.models.enterprise_asset import EnterpriseAssistantAssetInvocation
from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun, EnterpriseChatMessage, EnterpriseConversation
from app.models.enterprise_org import (
    Enterprise,
    EnterpriseMember,
)
from app.models.user import User
from app.services import billing_ledger_service as ledger
from app.services import billing_runtime_gate, enterprise_chat_service
from app.services import enterprise_asset_service as assets
from app.services import enterprise_assistant_runtime_service as runtime
from app.services import enterprise_org_service as org
from app.services.deepseek_responses_runtime import ToolCall

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url
verified_model_catalog = chain.verified_model_catalog


@pytest.mark.parametrize("room_kind", ["company", "direct"])
def test_mysql_asset_default_group_aba_before_stream_send_keeps_hold_for_review(
    fresh_chain, verified_model_catalog, monkeypatch, room_kind,
):
    suffix = uuid4().hex[:12]
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    now = datetime.now(timezone.utc).replace(microsecond=0).replace(tzinfo=None)
    snapshot = None

    with sessions.begin() as db:
        owner = User(username=f"mysql-asset-owner-{suffix}", password="x", role="user", status=1)
        member = User(username=f"mysql-asset-member-{suffix}", password="x", role="user", status=1)
        db.add_all([owner, member])
        db.flush()
        company = Enterprise(
            name=f"MySQL 资产归属 {suffix}", owner_id=owner.id, seat_limit=3,
            status="active", billing_suspended=False, paid_order_id=f"mysql-asset-{suffix}",
        )
        db.add(company)
        db.flush()
        enterprise_id, owner_id, member_id = int(company.id), int(owner.id), int(member.id)
        db.add_all([
            EnterpriseMember(enterprise_id=enterprise_id, user_id=owner_id, role="owner", status="active"),
            EnterpriseMember(
                enterprise_id=enterprise_id, user_id=member_id, role="member", status="active",
                default_group_id=None, billing_group_version=5,
            ),
        ])
        db.flush()
        group_a = org.create_group(db, enterprise_id, owner_id, f"资产 A-{suffix}")
        group_b = org.create_group(db, enterprise_id, owner_id, f"资产 B-{suffix}")
        group_a_id, group_b_id = int(group_a.id), int(group_b.id)
        org.set_group_member(db, enterprise_id, group_a_id, owner_id, member_id, "member")
        org.set_group_member(db, enterprise_id, group_b_id, owner_id, member_id, "member")
        org.set_default_group(db, enterprise_id, owner_id, member_id, group_a_id)
        member_row = db.query(EnterpriseMember).filter_by(
            enterprise_id=enterprise_id, user_id=member_id,
        ).one()
        # Keep an intentional starting version for an observable A→B→A monotonic change.
        member_row.billing_group_version = 5
        room = EnterpriseConversation(
            enterprise_id=enterprise_id,
            kind=room_kind,
            conversation_key=f"{room_kind}:{suffix}",
            group_id=None,
            participant_low_id=owner_id if room_kind == "direct" else None,
            participant_high_id=member_id if room_kind == "direct" else None,
            created_by=owner_id,
        )
        db.add(room)
        db.flush()
        conversation_id = int(room.id)
        message = EnterpriseChatMessage(
            enterprise_id=enterprise_id,
            conversation_id=conversation_id,
            sender_id=member_id,
            sender_type="human",
            client_message_id=f"mysql-asset-message-{suffix}",
            content="@小菱 调用已发布 Agent",
            sender_authority_rank=1,
        )
        db.add(message)
        db.flush()
        snapshot = runtime._role_snapshot(
            db, enterprise_id=enterprise_id, room=room, messages=[message], initiator_id=member_id,
        )
        public_id = f"mysql_asset_{suffix}"
        run = EnterpriseAssistantRun(
            public_id=public_id,
            request_key=f"mysql-asset-run:{suffix}",
            enterprise_id=enterprise_id,
            conversation_id=conversation_id,
            group_id=None,
            initiator_user_id=member_id,
            source_message_id=message.id,
            status="running",
            attempt_count=1,
            prompt_sha256="c" * 64,
            billing_attribution_version=1,
            billing_group_id=group_a_id,
            billing_membership_version=5,
            billing_user_token_version=int(member.token_version),
        )
        db.add(run)
        db.flush()
        db.add(EnterpriseAssistantRuntimeState(
            assistant_run_id=run.id,
            enterprise_id=enterprise_id,
            conversation_id=conversation_id,
            group_id=None,
            initiator_user_id=member_id,
            source_message_id=message.id,
            status="running",
            checkpoint_json="{}",
            permission_snapshot_json=runtime._json(snapshot),
            permission_snapshot_sha256=runtime._digest(snapshot),
            stop_requested=False,
            cancel_reason="",
            lease_generation=4,
            lease_expires_at=now + timedelta(minutes=5),
            billing_invocation_count=1,
            version=1,
        ))
        wallet = ledger.get_or_create_wallet(db, "enterprise", enterprise_id)
        ledger.grant(
            db, wallet_id=int(wallet.id), grant_key=f"mysql-asset-credit:{suffix}",
            kind="purchase", amount_milli=1_000_000,
        )
        db.add(BillingLimit(
            limit_key=f"mysql-asset-limit:{suffix}", enterprise_id=enterprise_id,
            category="normal", group_id=group_a_id, user_id=member_id,
            period_start=now - timedelta(days=1), period_end=now + timedelta(days=30),
            limit_milli=1_000_000, used_milli=0, reserved_milli=0,
        ))

    monkeypatch.setattr(runtime, "_CROSS_SCOPE_ATTRIBUTION_VERIFIED", True)
    monkeypatch.setattr(runtime, "_live_role_snapshot", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setattr(runtime, "_require_model_send_clearance", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime.assistant_service, "_require_active_limit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime.assistant_service, "_model_config", lambda _db: (
        SimpleNamespace(
            api_key="platform-test-key", base_url="https://api.deepseek.com/v1",
            model="deepseek-v4-flash", provider="deepseek", source="system", timeout_seconds=5,
        ),
        "deepseek-v4-flash",
    ))
    monkeypatch.setattr(
        runtime.billing_checkout_service, "enterprise_subscription_status_current",
        lambda *_args, **_kwargs: "active",
    )
    monkeypatch.setattr(enterprise_chat_service, "_require_locked_member", lambda *_args, **_kwargs: None)

    with sessions() as db:
        draft = assets.create_draft(
            db, enterprise_id, member_id, kind="agent", title=f"MySQL 归属测试-{suffix}",
            definition={"schema_version": 1, "runtime": "prompt", "system_prompt": "简短归纳。"},
            request_key=f"mysql-asset-draft:{suffix}",
        )
        publication = assets.request_publication(
            db, enterprise_id, member_id, draft.id, scope="enterprise", expected_version=1,
            request_key=f"mysql-asset-publication:{suffix}",
        )
        publication = assets.decide_enterprise_publication(
            db, enterprise_id, publication.id, owner_id, approve=True, reason="隔离 MySQL 测试批准",
        )
        executor = runtime.EnterpriseAssistantToolExecutor(
            db, enterprise_id=enterprise_id, conversation_id=conversation_id,
            run_public_id=public_id, lease_generation=4, actor_user_id=member_id,
        )

    original_before_send = billing_runtime_gate.before_stream_send
    changed = []

    def move_group_before_final_authorization(ticket):
        if ticket is not None and not changed:
            with sessions.begin() as mutation_db:
                org.set_default_group(mutation_db, enterprise_id, owner_id, member_id, group_b_id)
            with sessions.begin() as mutation_db:
                org.set_default_group(mutation_db, enterprise_id, owner_id, member_id, group_a_id)
            changed.append(True)
        return original_before_send(ticket)

    monkeypatch.setattr(billing_runtime_gate, "before_stream_send", move_group_before_final_authorization)
    provider_calls = []

    async def fake_stream(self, payload):
        provider_calls.append(payload)
        yield {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "model": payload["model"],
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "不得发送"}]}],
                "usage": {"input_tokens": 10, "output_tokens": 4},
            },
        }

    monkeypatch.setattr(runtime.NativeResponsesTransport, "_stream_without_billing", fake_stream)
    arguments = {"publication_id": int(publication.id), "task": "形成简短结论"}
    try:
        result = asyncio.run(executor.execute(ToolCall(
            call_id=f"mysql-asset-call-{suffix}",
            name="invoke_published_enterprise_asset",
            arguments=arguments,
            raw_arguments=runtime._json(arguments),
        )))
    finally:
        executor._db.close()

    assert changed == [True], (result.failure_kind, result.error)
    assert provider_calls == []
    assert result.status == "error"
    with sessions() as db:
        reservation = db.query(BillingReservation).filter(
            BillingReservation.enterprise_id == enterprise_id,
        ).one()
        invocation = db.query(EnterpriseAssistantAssetInvocation).filter_by(
            assistant_run_id=db.query(EnterpriseAssistantRun).filter_by(public_id=public_id).one().id,
        ).one()
        member_row = db.query(EnterpriseMember).filter_by(
            enterprise_id=enterprise_id, user_id=member_id,
        ).one()
        batch = db.query(BillingCreditBatch).filter_by(
            wallet_id=reservation.enterprise_wallet_id,
        ).one()
        limit = db.query(BillingLimit).filter_by(limit_key=f"mysql-asset-limit:{suffix}").one()
        assert (reservation.group_id, reservation.status, reservation.usage_state,
                reservation.provider_attempt_started_at) == (group_a_id, "unknown", "unknown", None)
        assert (invocation.status, invocation.error_code, invocation.output_text) == (
            "unknown", "ForbiddenError", None,
        )
        assert (member_row.default_group_id, member_row.billing_group_version) == (group_a_id, 7)
        assert batch.reserved_milli == reservation.requested_milli
        assert limit.reserved_milli == reservation.requested_milli
