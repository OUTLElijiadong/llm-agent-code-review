"""企业小菱必须按企业范围取上下文、预占企业额度并可幂等读取回答。"""

import asyncio
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.database import Base
from app.core.exceptions import ForbiddenError, PaymentRequiredError, ServiceUnavailableError
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
from app.models.enterprise_chat import EnterpriseAssistantRun, EnterpriseChatMessage, EnterpriseConversation
from app.models.enterprise_knowledge import EnterpriseKnowledgeEntry
from app.models.enterprise_org import Enterprise, EnterpriseGroup, EnterpriseGroupMember, EnterpriseMember
from app.models.user import User
from app.services import billing_ledger_service
from app.services import enterprise_assistant_service as assistant


def test_enterprise_group_answer_uses_company_wallet_and_duplicate_is_idempotent(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    tables = [
        User.__table__, Enterprise.__table__, EnterpriseMember.__table__, EnterpriseGroup.__table__,
        EnterpriseGroupMember.__table__, EnterpriseConversation.__table__,
        EnterpriseAssistantRun.__table__, EnterpriseChatMessage.__table__, EnterpriseKnowledgeEntry.__table__,
        BillingWallet.__table__, BillingCreditBatch.__table__, BillingLimit.__table__,
        BillingReservation.__table__, BillingReservationPart.__table__, BillingReservationLimit.__table__,
        BillingOrder.__table__, BillingPaymentEvent.__table__, BillingSubscription.__table__,
        BillingRefund.__table__,
        BillingSubscriptionPeriod.__table__,
        BillingRateCatalog.__table__, BillingRateCatalogItem.__table__, BillingRateCatalogEvent.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with factory.begin() as db:
        from app.services.billing_rate_catalog_service import seed_test_baseline

        seed_test_baseline(db)
        owner = User(id=1, username="owner", password="x", email="owner@example.com", role="user", status=1)
        employee = User(id=2, username="employee", password="x", email="employee@example.com", role="user", status=1)
        db.add_all([owner, employee])
        db.add(Enterprise(
            id=10, name="甲企业", owner_id=1, seat_limit=5, status="active", paid_order_id="paid-enterprise",
        ))
        db.add_all([
            EnterpriseMember(enterprise_id=10, user_id=1, role="owner", status="active"),
            EnterpriseMember(enterprise_id=10, user_id=2, role="member", status="active"),
            EnterpriseGroup(id=100, enterprise_id=10, name="研发", status="active"),
            EnterpriseGroupMember(enterprise_id=10, group_id=100, user_id=2, role="member", status="active"),
        ])
        order = BillingOrder(order_no="enterprise-paid", user_id=1, enterprise_id=10, product="enterprise",
                             amount_fen=6900, quantity=1, channel="alipay", status="paid")
        db.add(order)
        db.flush()
        db.add(BillingPaymentEvent(channel="alipay", trade_id="trade-enterprise", order_no=order.order_no,
                                   amount_fen=order.amount_fen))
        subscription = BillingSubscription(owner_type="enterprise", owner_id=10, product="enterprise",
                                           anchor_at=now, last_end_at=now + timedelta(days=30), next_offset=1)
        db.add(subscription)
        db.flush()
        db.add(BillingSubscriptionPeriod(subscription_id=subscription.id, order_no=order.order_no,
                                         starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=30),
                                         credit_milli=1_000_000, seats=5, granted_at=now))
        room = EnterpriseConversation(enterprise_id=10, kind="group", conversation_key="group:100",
                                      group_id=100, created_by=1)
        db.add(room)
        wallet = BillingWallet(owner_type="enterprise", owner_id=10)
        db.add(wallet)
        db.flush()
        db.add(BillingCreditBatch(wallet_id=wallet.id, grant_key="company-credit", kind="purchase",
                                  issued_milli=1_000_000, remaining_milli=1_000_000, reserved_milli=0,
                                  refund_reserved_milli=0, expires_at=None))
        db.add(BillingLimit(limit_key="normal:10:test", enterprise_id=10, category="normal", group_id=None,
                            user_id=None, period_start=now - timedelta(days=1),
                            period_end=now + timedelta(days=30), limit_milli=1_000_000,
                            used_milli=0, reserved_milli=0))
        message = EnterpriseChatMessage(enterprise_id=10, conversation_id=room.id, sender_id=2,
                                        sender_type="human", client_message_id="ask-1", content="@小菱 请总结会议")
        db.add(message)
        db.flush()
        message_id, room_id, wallet_id = message.id, room.id, wallet.id

    monkeypatch.setattr(settings, "billing_commercial_metering_ready", True)
    monkeypatch.setattr(assistant, "_model_config", lambda _db: (SimpleNamespace(
        model="deepseek-v4-flash", provider="deepseek", source="system",
        base_url="https://api.deepseek.com/v1", api_key="platform-test-key",
    ), "deepseek-v4-flash"))
    emitted = {"count": 0}
    revoke_on_terminal = {"enabled": False, "target": "member"}
    usage_override = {"value": None}

    async def fake_stream_without_billing(self, _payload):
        emitted["count"] += 1
        event = {
            "type": "response.completed",
            "response": {
                "status": "completed", "model": "deepseek-v4-flash",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "会议结论：先完成验证。"}]}],
                "usage": usage_override["value"] or {"input_tokens": 20, "output_tokens": 12},
            },
        }
        if self._event_sink:
            await self._event_sink(event)
        if revoke_on_terminal["enabled"]:
            with factory.begin() as scope_db:
                if revoke_on_terminal["target"] == "enterprise":
                    scope_db.query(Enterprise).filter_by(id=10).one().status = "suspended"
                else:
                    scope_db.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=2).one().status = "removed"
            revoke_on_terminal["enabled"] = False
        yield event

    monkeypatch.setattr("app.services.agent_responses_service.NativeResponsesTransport._stream_without_billing",
                        fake_stream_without_billing)
    request_db = factory()
    try:
        with factory.begin() as db:
            db.query(BillingLimit).filter_by(enterprise_id=10).one().limit_milli = 0
        try:
            asyncio.run(assistant.run_for_message(
                request_db, enterprise_id=10, user_id=2, conversation_id=room_id, source_message_id=message_id,
            ))
            raise AssertionError("额度为零时应在上游调用前拒绝")
        except PaymentRequiredError as exc:
            assert "额度" in str(exc) or "积分" in str(exc)
        with factory() as db:
            retryable = db.query(EnterpriseAssistantRun).one()
            assert retryable.status == "retryable" and retryable.attempt_count == 1
            assert db.query(BillingReservation).count() == 0
        assert emitted["count"] == 0
        with factory.begin() as db:
            db.query(BillingLimit).filter_by(enterprise_id=10).one().limit_milli = 1_000_000

        original_finalize = assistant._finalize_output
        def fail_finalize(*_args, **_kwargs):
            raise RuntimeError("simulated final write failure")

        monkeypatch.setattr(assistant, "_finalize_output", fail_finalize)
        try:
            asyncio.run(assistant.run_for_message(
                request_db, enterprise_id=10, user_id=2, conversation_id=room_id, source_message_id=message_id,
            ))
            raise AssertionError("应复现已结算后消息写入失败")
        except RuntimeError as exc:
            assert "simulated final write failure" in str(exc)
        monkeypatch.setattr(assistant, "_finalize_output", original_finalize)
        with factory() as db:
            recovered = db.query(EnterpriseAssistantRun).one()
            reservation = db.query(BillingReservation).one()
            assert recovered.status == "output_ready" and recovered.response_text
            assert recovered.attempt_count == 2 and reservation.status == "settled"

        result = asyncio.run(assistant.run_for_message(
            request_db, enterprise_id=10, user_id=2, conversation_id=room_id, source_message_id=message_id,
        ))
        assert (result["status"], result["message"].sender_type, result["message"].sender_id) == (
            "completed", "assistant", None,
        )
        assert result["message"].content == "会议结论：先完成验证。"
        duplicate = asyncio.run(assistant.run_for_message(
            request_db, enterprise_id=10, user_id=2, conversation_id=room_id, source_message_id=message_id,
        ))
        assert duplicate["run_id"] == result["run_id"]
        assert duplicate["message"].id == result["message"].id
        assert emitted["count"] == 1
        with factory.begin() as db:
            run = db.query(EnterpriseAssistantRun).filter_by(public_id=result["run_id"]).one()
            run.status = "running"
            run.attempt_count = 3
            run.response_text = None
        asyncio.run(assistant._capture_completed_response(factory, result["run_id"], 2, {
            "type": "response.completed",
            "response": {
                "status": "completed", "output": [{"type": "message", "content":[
                    {"type": "output_text", "text": "过期 attempt 的答复"}]}],
                "usage": {"input_tokens": 99, "output_tokens": 99},
            },
        }, {"model": "deepseek-v4-flash", "input": "question", "max_output_tokens": 512, "tools": []}))
        with factory.begin() as db:
            run = db.query(EnterpriseAssistantRun).filter_by(public_id=result["run_id"]).one()
            assert run.status == "running" and run.attempt_count == 3 and run.response_text is None
            run.status = "completed"
            run.attempt_count = 2
            run.response_text = result["message"].content
        with factory() as db:
            reservation = db.query(BillingReservation).one()
            assert reservation.enterprise_id == 10 and reservation.user_id == 2
            assert reservation.group_id == 100 and reservation.personal_wallet_id is None
            assert reservation.status == "settled"
            assert reservation.input_tokens == 20 and reservation.output_tokens == 12
            assert reservation.actual_milli == billing_ledger_service.quote_milli("deepseek-v4-flash", 20, 12)
            assert db.query(BillingWallet).filter_by(id=wallet_id).one().owner_type == "enterprise"
            assert db.query(EnterpriseAssistantRun).one().prompt_sha256

        with factory.begin() as db:
            interrupted_message = EnterpriseChatMessage(
                enterprise_id=10, conversation_id=room_id, sender_id=2, sender_type="human",
                client_message_id="ask-settlement-recovery", content="@小菱 请再总结一次",
            )
            db.add(interrupted_message)
            db.flush()
            interrupted_message_id = interrupted_message.id
        from app.services import billing_runtime_gate

        original_finish = billing_runtime_gate.finish_attempt

        def fail_finish(_ticket, _usage):
            raise RuntimeError("simulated crash before ledger settlement")

        monkeypatch.setattr(billing_runtime_gate, "finish_attempt", fail_finish)
        try:
            asyncio.run(assistant.run_for_message(
                request_db, enterprise_id=10, user_id=2, conversation_id=room_id,
                source_message_id=interrupted_message_id,
            ))
            raise AssertionError("应复现答复已保存但费用未结算")
        except ServiceUnavailableError:
            pass
        monkeypatch.setattr(billing_runtime_gate, "finish_attempt", original_finish)
        with factory() as db:
            interrupted = db.query(EnterpriseAssistantRun).filter_by(
                source_message_id=interrupted_message_id,
            ).one()
            reservation = db.query(BillingReservation).filter(
                BillingReservation.request_key.like(f"enterprise-assistant:{interrupted.public_id}:%")
            ).one()
            assert interrupted.status == "output_ready" and interrupted.response_text
            assert reservation.status == "unknown"
        with factory.begin() as db:
            # Simulate process termination before the stream's finally block could mark the hold unknown.
            db.query(BillingReservation).filter(
                BillingReservation.request_key.like(f"enterprise-assistant:{interrupted.public_id}:%")
            ).one().status = "reserved"
        monkeypatch.setitem(
            billing_ledger_service._RATES, "deepseek-v4-flash", (Fraction(9), Fraction(27)),
        )
        recovered_again = asyncio.run(assistant.run_for_message(
            request_db, enterprise_id=10, user_id=2, conversation_id=room_id,
            source_message_id=interrupted_message_id,
        ))
        monkeypatch.setitem(
            billing_ledger_service._RATES, "deepseek-v4-flash", (Fraction(1), Fraction(4)),
        )
        assert recovered_again["status"] == "completed"
        assert recovered_again["message"].content == "会议结论：先完成验证。"
        assert emitted["count"] == 2
        with factory() as db:
            recovered_run = db.query(EnterpriseAssistantRun).filter_by(
                source_message_id=interrupted_message_id,
            ).one()
            reservation = db.query(BillingReservation).filter(
                BillingReservation.request_key.like(f"enterprise-assistant:{recovered_run.public_id}:%")
            ).one()
            assert reservation.status == "settled"
            assert reservation.actual_milli == billing_ledger_service.quote_reservation_milli(
                db, request_key=reservation.request_key, input_tokens=reservation.input_tokens,
                output_tokens=reservation.output_tokens,
            )

        with factory.begin() as db:
            revoked_message = EnterpriseChatMessage(
                enterprise_id=10, conversation_id=room_id, sender_id=2, sender_type="human",
                client_message_id="ask-revoked", content="@小菱 再总结一次",
            )
            db.add(revoked_message)
            db.flush()
            revoked_message_id = revoked_message.id
        revoke_on_terminal["enabled"] = True
        try:
            asyncio.run(assistant.run_for_message(
                request_db, enterprise_id=10, user_id=2, conversation_id=room_id,
                source_message_id=revoked_message_id,
            ))
            raise AssertionError("权限撤销后不应向会话发布回答")
        except ForbiddenError as exc:
            assert "权限" in str(exc) or "成员资格" in str(exc)
        with factory() as db:
            suppressed = db.query(EnterpriseAssistantRun).filter_by(source_message_id=revoked_message_id).one()
            reservation = db.query(BillingReservation).filter(
                BillingReservation.request_key.like(f"enterprise-assistant:{suppressed.public_id}:%")
            ).one()
            assert suppressed.status == "suppressed"
            assert reservation.status == "settled"
            assert db.query(EnterpriseChatMessage).filter_by(assistant_run_id=suppressed.id).count() == 0

        with factory.begin() as db:
            db.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=2).one().status = "active"
            unpriced_message = EnterpriseChatMessage(
                enterprise_id=10, conversation_id=room_id, sender_id=2, sender_type="human",
                client_message_id="ask-unpriced-usage", content="@小菱 查一下外部资料",
            )
            db.add(unpriced_message)
            db.flush()
            unpriced_message_id = unpriced_message.id
        usage_override["value"] = {
            "input_tokens": 20, "output_tokens": 12,
            "server_tool_use": {"tool_search": 1, "tool_fetch": 0},
        }
        try:
            asyncio.run(assistant.run_for_message(
                request_db, enterprise_id=10, user_id=2, conversation_id=room_id,
                source_message_id=unpriced_message_id,
            ))
            raise AssertionError("未计价的工具用量不得自动补结算")
        except ServiceUnavailableError as exc:
            assert "失败" in str(exc) or "费用" in str(exc)
        usage_override["value"] = None
        with factory() as db:
            unresolved = db.query(EnterpriseAssistantRun).filter_by(source_message_id=unpriced_message_id).one()
            reservation = db.query(BillingReservation).filter(
                BillingReservation.request_key.like(f"enterprise-assistant:{unresolved.public_id}:%")
            ).one()
            assert unresolved.status == "awaiting_reconciliation"
            assert unresolved.response_text is None
            assert unresolved.input_tokens is None and unresolved.output_tokens is None
            assert reservation.status == "unknown"
            assert db.query(EnterpriseChatMessage).filter_by(assistant_run_id=unresolved.id).count() == 0

        with factory.begin() as db:
            over_limit_message = EnterpriseChatMessage(
                enterprise_id=10, conversation_id=room_id, sender_id=2, sender_type="human",
                client_message_id="ask-over-limit-usage", content="@小菱 再总结一次",
            )
            db.add(over_limit_message)
            db.flush()
            over_limit_message_id = over_limit_message.id
        usage_override["value"] = {"input_tokens": 50_001, "output_tokens": 12}
        try:
            asyncio.run(assistant.run_for_message(
                request_db, enterprise_id=10, user_id=2, conversation_id=room_id,
                source_message_id=over_limit_message_id,
            ))
            raise AssertionError("超出预占上限的用量不得自动补结算")
        except ServiceUnavailableError as exc:
            assert "失败" in str(exc) or "费用" in str(exc)
        usage_override["value"] = None
        with factory() as db:
            unresolved = db.query(EnterpriseAssistantRun).filter_by(source_message_id=over_limit_message_id).one()
            reservation = db.query(BillingReservation).filter(
                BillingReservation.request_key.like(f"enterprise-assistant:{unresolved.public_id}:%")
            ).one()
            assert unresolved.status == "awaiting_reconciliation"
            assert unresolved.response_text is None
            assert reservation.status == "unknown"
            assert db.query(EnterpriseChatMessage).filter_by(assistant_run_id=unresolved.id).count() == 0

        with factory.begin() as db:
            over_output_message = EnterpriseChatMessage(
                enterprise_id=10, conversation_id=room_id, sender_id=2, sender_type="human",
                client_message_id="ask-over-output-usage", content="@小菱 再总结一次",
            )
            db.add(over_output_message)
            db.flush()
            over_output_message_id = over_output_message.id
        usage_override["value"] = {"input_tokens": 20, "output_tokens": 513}
        try:
            asyncio.run(assistant.run_for_message(
                request_db, enterprise_id=10, user_id=2, conversation_id=room_id,
                source_message_id=over_output_message_id,
            ))
            raise AssertionError("超过输出预占上限的用量不得自动补结算")
        except ServiceUnavailableError as exc:
            assert "失败" in str(exc) or "费用" in str(exc)
        usage_override["value"] = None
        with factory() as db:
            unresolved = db.query(EnterpriseAssistantRun).filter_by(source_message_id=over_output_message_id).one()
            reservation = db.query(BillingReservation).filter(
                BillingReservation.request_key.like(f"enterprise-assistant:{unresolved.public_id}:%")
            ).one()
            assert unresolved.status == "awaiting_reconciliation"
            assert unresolved.response_text is None
            assert reservation.status == "unknown"
            assert db.query(EnterpriseChatMessage).filter_by(assistant_run_id=unresolved.id).count() == 0

        with factory.begin() as db:
            suspended_message = EnterpriseChatMessage(
                enterprise_id=10, conversation_id=room_id, sender_id=2, sender_type="human",
                client_message_id="ask-enterprise-suspended", content="@小菱 再总结一次",
            )
            db.add(suspended_message)
            db.flush()
            suspended_message_id = suspended_message.id
        revoke_on_terminal.update(enabled=True, target="enterprise")
        try:
            asyncio.run(assistant.run_for_message(
                request_db, enterprise_id=10, user_id=2, conversation_id=room_id,
                source_message_id=suspended_message_id,
            ))
            raise AssertionError("企业挂起后不应向会话发布回答")
        except ForbiddenError as exc:
            assert "企业" in str(exc)
        with factory() as db:
            suspended = db.query(EnterpriseAssistantRun).filter_by(source_message_id=suspended_message_id).one()
            reservation = db.query(BillingReservation).filter(
                BillingReservation.request_key.like(f"enterprise-assistant:{suspended.public_id}:%")
            ).one()
            assert suspended.status == "suppressed"
            assert reservation.status == "settled"
            assert db.query(EnterpriseChatMessage).filter_by(assistant_run_id=suspended.id).count() == 0
    finally:
        request_db.close()
        engine.dispose()
