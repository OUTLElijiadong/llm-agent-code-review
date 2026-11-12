"""群聊主动建议必须显式启用、有企业预算且不能以人类身份执行工具。"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.core.exceptions import ForbiddenError, ValidationError
from app.models.enterprise_chat import EnterpriseAssistantProactiveEvent, EnterpriseAssistantRun
from app.models.enterprise_org import Enterprise, EnterpriseGroup, EnterpriseGroupMember, EnterpriseMember
from app.models.user import User
from app.services import enterprise_assistant_proactive_service as proactive
from app.services import enterprise_chat_service as chat
from app.services.deepseek_responses_runtime import ToolCall
from app.services.enterprise_assistant_runtime_service import EnterpriseAssistantToolExecutor


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(proactive.settings, "billing_commercial_metering_ready", True)
    # Import the app's model registry before creating the isolated database.
    import app.models  # noqa: F401

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    session.add_all([
        User(id=1, username="proactive-owner", password="x", role="user", status=1),
        User(id=2, username="proactive-member", password="x", role="user", status=1),
        User(id=3, username="proactive-outsider", password="x", role="user", status=1),
        Enterprise(id=10, name="甲", owner_id=1, seat_limit=5, status="active", paid_order_id="p-a"),
        Enterprise(id=20, name="乙", owner_id=3, seat_limit=5, status="active", paid_order_id="p-b"),
        EnterpriseMember(enterprise_id=10, user_id=1, role="owner", status="active"),
        EnterpriseMember(enterprise_id=10, user_id=2, role="member", status="active"),
        EnterpriseMember(enterprise_id=20, user_id=3, role="owner", status="active"),
        EnterpriseGroup(id=100, enterprise_id=10, name="研发", status="active"),
        EnterpriseGroupMember(enterprise_id=10, group_id=100, user_id=2, role="member", status="active"),
    ])
    session.commit()
    yield session
    session.close()
    engine.dispose()


def test_policy_is_default_off_and_only_admin_can_enable(db):
    assert proactive.get_policy(db, 10, 1)["enabled"] is False
    with pytest.raises(ForbiddenError):
        proactive.set_policy(db, 10, 2, enabled=True, monthly_credits=10, expected_version=0, expected_token_version=0)
    db.rollback()
    with pytest.raises(ValidationError):
        proactive.set_policy(db, 10, 1, enabled=True, monthly_credits=0, expected_version=0, expected_token_version=0)
    db.rollback()
    policy = proactive.set_policy(db, 10, 1, enabled=True, monthly_credits=10, expected_version=0, expected_token_version=0)
    db.commit()
    assert policy["enabled"] is True and policy["monthly_credits"] == 10
    with pytest.raises(ForbiddenError):
        proactive.get_policy(db, 10, 3)


def test_real_group_message_event_is_atomic_idempotent_and_scoped(db, monkeypatch):
    monkeypatch.setattr(proactive.settings, "billing_commercial_metering_ready", True)
    room = chat.open_conversation(db, 10, 2, "group", group_id=100)
    chat.send_message(db, 10, 2, room.id, "before", "这个问题如何处理？")
    assert db.query(EnterpriseAssistantProactiveEvent).count() == 0
    proactive.set_policy(db, 10, 1, enabled=True, monthly_credits=10, expected_version=0, expected_token_version=0)
    db.commit()
    source = chat.send_message(db, 10, 2, room.id, "one", "这个问题如何处理？")
    event = db.query(EnterpriseAssistantProactiveEvent).one()
    assert (event.enterprise_id, event.conversation_id, event.source_message_id, event.actor_user_id) == (
        10, room.id, source.id, 2,
    )
    chat.send_message(db, 10, 2, room.id, "one", "这个问题如何处理？")
    assert db.query(EnterpriseAssistantProactiveEvent).count() == 1
    chat.send_message(db, 10, 2, room.id, "mention", "@小菱 请检查")
    assert db.query(EnterpriseAssistantProactiveEvent).count() == 1


def test_private_chat_is_never_automatically_triggered(db, monkeypatch):
    monkeypatch.setattr(proactive.settings, "billing_commercial_metering_ready", True)
    proactive.set_policy(db, 10, 1, enabled=True, monthly_credits=10, expected_version=0, expected_token_version=0)
    db.commit()
    room = chat.open_conversation(db, 10, 1, "direct", other_user_id=2)
    chat.send_message(db, 10, 1, room.id, "private", "这个问题如何处理？")
    assert db.query(EnterpriseAssistantProactiveEvent).count() == 0


@pytest.mark.parametrize("kind", ["company", "direct"])
def test_new_manual_company_and_direct_billing_stays_closed_until_verified(db, monkeypatch, kind):
    from app.services import enterprise_assistant_runtime_service as runtime

    monkeypatch.setattr(runtime.assistant_service, "_require_subscription", lambda *_args: None)
    options = {"other_user_id": 1} if kind == "direct" else {}
    room = chat.open_conversation(db, 10, 2, kind, **options)
    source = chat.send_message(db, 10, 2, room.id, "manual-gate", "@小菱 请检查")
    monkeypatch.setattr(runtime.assistant_service, "_model_config",
                        lambda *_a: pytest.fail("closed billing path must not inspect provider configuration"))
    with pytest.raises(ForbiddenError, match="计费归属正在验证"):
        asyncio.run(runtime.run_for_message(db, enterprise_id=10, user_id=2, conversation_id=room.id,
                                           source_message_id=source.id))
    assert db.query(EnterpriseAssistantRun).count() == 0


@pytest.mark.parametrize("kind,group_limit,change_default", [
    ("company", 0, "none"), ("direct", 0, "none"), ("company", 1_000_000, "none"),
    ("direct", 1_000_000, "before_claim"), ("company", 1_000_000, "after_hold"),
    ("company", 1_000_000, "gate_before_claim"), ("direct", 1_000_000, "gate_after_hold"),
    ("company", 1_000_000, "gate_after_send"),
    ("company", 1_000_000, "after_last_check"), ("direct", 1_000_000, "after_last_check"),
])
def test_closed_feature_test_mode_parent_model_uses_default_group_limit(db, monkeypatch, kind, group_limit, change_default):
    from types import SimpleNamespace

    from app.models.billing_ledger import BillingLimit, BillingReservation
    from app.services import billing_ledger_service as ledger
    from app.services import enterprise_assistant_runtime_service as runtime
    from app.services.billing_rate_catalog_service import seed_test_baseline

    seed_test_baseline(db)
    member = db.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=2).one()
    member.default_group_id = 100
    member.billing_group_version = 7
    wallet = ledger.get_or_create_wallet(db, "enterprise", 10)
    ledger.grant(db, wallet_id=wallet.id, grant_key="cross-scope-test", kind="purchase", amount_milli=2_000_000)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.add_all([
        BillingLimit(limit_key="parent-ent", enterprise_id=10, category="normal", period_start=now - timedelta(days=1),
                     period_end=now + timedelta(days=30), limit_milli=1_000_000, accounting_version=1),
        BillingLimit(limit_key="parent-member", enterprise_id=10, category="normal", group_id=100, user_id=2,
                     period_start=now - timedelta(days=1), period_end=now + timedelta(days=30),
                     limit_milli=group_limit, accounting_version=1),
    ])
    db.commit()
    monkeypatch.setattr(runtime, "_CROSS_SCOPE_ATTRIBUTION_VERIFIED", True)
    monkeypatch.setattr(proactive.settings, "billing_commercial_metering_ready", True)
    monkeypatch.setattr(runtime.assistant_service, "_model_config", lambda _db: (SimpleNamespace(
        api_key="fake", base_url="https://api.deepseek.com/v1", model="deepseek-v4-flash",
        provider="deepseek", source="system", timeout_seconds=5), "deepseek-v4-flash"))
    monkeypatch.setattr(runtime.enterprise_org_service, "require_enterprise_execution", lambda *_args: None)
    monkeypatch.setattr(runtime.enterprise_org_service, "_billing_access_suspended", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(runtime.billing_checkout_service, "enterprise_subscription_status", lambda *_a, **_kw: "active")
    monkeypatch.setattr(runtime.enterprise_knowledge_service, "search_entries", lambda *_a, **_kw: [])
    monkeypatch.setattr(runtime.enterprise_knowledge_attachment_service, "search_attachments", lambda *_a, **_kw: [])
    sends = []

    async def fake_stream(_transport, _payload):
        sends.append(True)
        if change_default == "gate_after_send":
            monkeypatch.setattr(runtime, "_CROSS_SCOPE_ATTRIBUTION_VERIFIED", False)
        yield {"type": "response.completed", "response": {"status": "completed", "model": "deepseek-v4-flash",
               "output": [{"type": "message", "role": "assistant", "content": [
                   {"type": "output_text", "text": "已完成"}]}], "usage": {"input_tokens": 80, "output_tokens": 20}}}

    monkeypatch.setattr(runtime.NativeResponsesTransport, "_stream_without_billing", fake_stream)
    room = chat.open_conversation(db, 10, 2, kind, **({"other_user_id": 1} if kind == "direct" else {}))
    source = chat.send_message(db, 10, 2, room.id, "manual-test", "@小菱 请检查")
    result = asyncio.run(runtime.run_for_message(db, enterprise_id=10, user_id=2, conversation_id=room.id,
                                                source_message_id=source.id))
    if change_default == "gate_before_claim":
        monkeypatch.setattr(runtime, "_CROSS_SCOPE_ATTRIBUTION_VERIFIED", False)
    elif change_default == "gate_after_hold":
        from app.services import billing_runtime_gate

        original_begin = billing_runtime_gate.begin_attempt

        def close_gate_after_reservation(**kwargs):
            ticket = original_begin(**kwargs)
            monkeypatch.setattr(runtime, "_CROSS_SCOPE_ATTRIBUTION_VERIFIED", False)
            return ticket

        monkeypatch.setattr(billing_runtime_gate, "begin_attempt", close_gate_after_reservation)
    elif change_default not in {"none", "gate_after_send"}:
        from app.services import enterprise_org_service as org

        db.add(EnterpriseGroup(id=101, enterprise_id=10, name="其他组", status="active"))
        db.add(EnterpriseGroupMember(enterprise_id=10, group_id=101, user_id=2, role="member", status="active"))
        db.commit()

        def change_twice():
            with sessionmaker(bind=db.get_bind()).begin() as other:
                org.set_default_group(other, 10, 1, 2, 101)
                org.set_default_group(other, 10, 1, 2, 100)

        if change_default == "before_claim":
            change_twice()
        elif change_default == "after_hold":
            from app.services import billing_runtime_gate

            original_begin = billing_runtime_gate.begin_attempt

            def change_after_reservation(**kwargs):
                ticket = original_begin(**kwargs)
                change_twice()
                return ticket

            monkeypatch.setattr(billing_runtime_gate, "begin_attempt", change_after_reservation)
        else:
            from app.services import billing_runtime_gate

            original_before = billing_runtime_gate.before_stream_send

            def change_after_last_check(ticket):
                original_before(ticket)
                change_twice()

            monkeypatch.setattr(billing_runtime_gate, "before_stream_send", change_after_last_check)
    asyncio.run(runtime.execute_queued_run(db.get_bind(), enterprise_id=10, conversation_id=room.id,
                                           run_public_id=result["run_id"]))
    db.expire_all()
    run = db.query(EnterpriseAssistantRun).filter_by(public_id=result["run_id"]).one()
    assert run.group_id is None and run.billing_group_id == 100
    reservations = db.query(BillingReservation).all()
    if change_default == "after_last_check":
        assert sends == [True]
        assert len(reservations) == 1
        assert reservations[0].group_id == 100 and reservations[0].status == "settled"
        assert run.status == "completed"
        member = db.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=2).one()
        assert member.default_group_id == 100
        assert member.billing_group_version > run.billing_membership_version
    elif change_default == "gate_before_claim":
        assert not sends and not reservations
        status = runtime.get_run_status(db, enterprise_id=10, user_id=2, conversation_id=room.id,
                                        run_public_id=result["run_id"])
        assert status["error"] == "billing_attribution_unavailable"
        assert status["recoverable"] is False
        monkeypatch.setattr(runtime, "_CROSS_SCOPE_ATTRIBUTION_VERIFIED", True)
        assert runtime.get_run_status(db, enterprise_id=10, user_id=2, conversation_id=room.id,
                                      run_public_id=result["run_id"])["recoverable"] is True
    elif change_default in {"after_hold", "gate_after_hold"}:
        assert not sends and len(reservations) == 1
        assert reservations[0].group_id == 100 and reservations[0].status == "unknown"
    elif group_limit and change_default in {"none", "gate_after_send"}:
        assert sends == [True]
        assert len(reservations) == 1 and reservations[0].group_id == 100 and reservations[0].status == "settled"
        if change_default == "gate_after_send":
            assert run.status == "completed"
    else:
        assert not sends and not reservations


def test_group_trigger_is_rate_limited(db, monkeypatch):
    monkeypatch.setattr(proactive.settings, "billing_commercial_metering_ready", True)
    clock = datetime(2026, 11, 8, 10, tzinfo=timezone.utc).replace(tzinfo=None)
    monkeypatch.setattr(proactive, "_now", lambda: clock)
    proactive.set_policy(db, 10, 1, enabled=True, monthly_credits=10, expected_version=0, expected_token_version=0)
    db.commit()
    room = chat.open_conversation(db, 10, 2, "group", group_id=100)
    chat.send_message(db, 10, 2, room.id, "first", "第一个问题该如何处理？")
    chat.send_message(db, 10, 2, room.id, "second", "第二个问题该如何处理？")
    assert db.query(EnterpriseAssistantProactiveEvent).count() == 1
    monkeypatch.setattr(proactive, "_now", lambda: clock + timedelta(seconds=proactive.MIN_INTERVAL_SECONDS))
    chat.send_message(db, 10, 2, room.id, "third", "第三个问题该如何处理？")
    assert db.query(EnterpriseAssistantProactiveEvent).count() == 2


def test_proactive_executor_refuses_unadvertised_writes(db, monkeypatch):
    state = type("State", (), {"initiator_user_id": 2})()
    run = EnterpriseAssistantRun(trigger_kind="proactive")
    executor = EnterpriseAssistantToolExecutor(
        db, enterprise_id=10, conversation_id=20, run_public_id="ear-auto", lease_generation=1,
        actor_user_id=2,
    )
    monkeypatch.setattr(executor, "_current", lambda: (state, {}))
    monkeypatch.setattr(executor, "_is_proactive_run", lambda: True)
    call = ToolCall(call_id="x", name="create_private_agent_skill_draft",
                    arguments={"kind": "agent", "title": "x", "definition": {}}, raw_arguments="{}")
    result = asyncio.run(executor.execute(call))
    assert result.status != "success"
    assert run.trigger_kind == "proactive"


def test_policy_rejects_stale_enable_after_disable(db):
    from app.core.exceptions import ConflictError

    proactive.set_policy(db, 10, 1, enabled=True, monthly_credits=10, expected_version=0, expected_token_version=0)
    db.commit()
    proactive.set_policy(db, 10, 1, enabled=False, monthly_credits=10, expected_version=1, expected_token_version=0)
    db.commit()
    with pytest.raises(ConflictError):
        proactive.set_policy(db, 10, 1, enabled=True, monthly_credits=10, expected_version=1, expected_token_version=0)
    db.rollback()
    assert proactive.get_policy(db, 10, 1)["enabled"] is False


def test_event_queues_once_with_real_actor_and_no_tools(db, monkeypatch):
    from types import SimpleNamespace
    from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
    from app.services import enterprise_assistant_runtime_service as runtime
    import json

    monkeypatch.setattr(proactive.settings, "billing_commercial_metering_ready", True)
    proactive.set_policy(db, 10, 1, enabled=True, monthly_credits=10, expected_version=0, expected_token_version=0)
    db.commit()
    room = chat.open_conversation(db, 10, 2, "group", group_id=100)
    source = chat.send_message(db, 10, 2, room.id, "queue", "这个问题该如何处理？")
    event = db.query(EnterpriseAssistantProactiveEvent).one()
    event_id, room_id, source_id = event.id, room.id, source.id
    monkeypatch.setattr(runtime.assistant_service, "_require_subscription", lambda *_args: None)
    monkeypatch.setattr(runtime.assistant_service, "_model_config", lambda *_args: (SimpleNamespace(), "test-model"))
    monkeypatch.setattr(runtime.assistant_service.ledger, "available_milli", lambda *_args: 1000)
    monkeypatch.setattr(runtime, "_render_runtime_context", lambda *_args, **_kwargs: ("只针对当前会话提供建议", []))
    kwargs = dict(enterprise_id=10, user_id=2, conversation_id=room_id,
                  source_message_id=source_id, _proactive_event_id=event_id)
    result = asyncio.run(runtime.run_for_message(db, **kwargs))
    again = asyncio.run(runtime.run_for_message(db, **kwargs))
    db.expire_all()
    run = db.query(EnterpriseAssistantRun).one()
    state = db.query(EnterpriseAssistantRuntimeState).one()
    assert result["status"] == "queued" and again["status"] == "queued"
    assert (run.initiator_user_id, run.trigger_kind, run.source_message_id) == (2, "proactive", source_id)
    assert json.loads(state.checkpoint_json)["tools"] == []
    assert db.query(EnterpriseAssistantProactiveEvent).one().run_id == run.id
    assert chat.list_messages(db, 10, 2, room_id)[-1].id == source_id


def test_revocation_prevents_pending_event_queue(db, monkeypatch):
    from app.services import enterprise_assistant_runtime_service as runtime

    monkeypatch.setattr(proactive.settings, "billing_commercial_metering_ready", True)
    proactive.set_policy(db, 10, 1, enabled=True, monthly_credits=10, expected_version=0, expected_token_version=0)
    db.commit()
    room = chat.open_conversation(db, 10, 2, "group", group_id=100)
    source = chat.send_message(db, 10, 2, room.id, "revoke", "这个问题该如何处理？")
    event = db.query(EnterpriseAssistantProactiveEvent).one()
    event_id, room_id, source_id = event.id, room.id, source.id
    proactive.set_policy(db, 10, 1, enabled=False, monthly_credits=10, expected_version=1, expected_token_version=0)
    db.commit()
    with pytest.raises(ForbiddenError):
        asyncio.run(runtime.run_for_message(db, enterprise_id=10, user_id=2, conversation_id=room_id,
                                            source_message_id=source_id, _proactive_event_id=event_id))
    assert db.query(EnterpriseAssistantRun).count() == 0


@pytest.mark.parametrize("case", ["normal", "malicious_tool", "revoke_inflight", "unknown",
                                  "expired_queue", "expired_before_send", "expired_after_reservation"])
def test_proactive_provider_is_metered_once_and_cannot_run_tools(db, monkeypatch, case):
    from types import SimpleNamespace
    from app.models.billing_ledger import BillingCreditBatch, BillingReservation
    from app.models.enterprise_assistant_runtime import EnterpriseAssistantApproval
    from app.services import enterprise_assistant_runtime_service as runtime
    from app.services.billing_rate_catalog_service import seed_test_baseline

    seed_test_baseline(db)
    monkeypatch.setattr(proactive.settings, "billing_commercial_metering_ready", True)
    proactive.set_policy(db, 10, 1, enabled=True, monthly_credits=100, expected_version=0, expected_token_version=0)
    wallet = db.query(runtime.BillingWallet).filter_by(owner_type="enterprise", owner_id=10).one()
    db.add(BillingCreditBatch(wallet_id=wallet.id, grant_key="proactive-funded", kind="purchase",
                              issued_milli=1_000_000, remaining_milli=1_000_000, reserved_milli=0,
                              refund_reserved_milli=0, expires_at=None))
    db.commit()
    room = chat.open_conversation(db, 10, 2, "group", group_id=100)
    source = chat.send_message(db, 10, 2, room.id, "paid-once", "这个问题该如何处理？")
    event = db.query(EnterpriseAssistantProactiveEvent).one()
    event_id, room_id, source_id = event.id, room.id, source.id
    monkeypatch.setattr(runtime.assistant_service, "_model_config", lambda _db: (SimpleNamespace(
        api_key="fake", base_url="https://api.deepseek.com/v1", model="deepseek-v4-flash",
        provider="deepseek", source="system", timeout_seconds=5), "deepseek-v4-flash"))
    monkeypatch.setattr(runtime.enterprise_org_service, "require_enterprise_execution", lambda *_args: None)
    monkeypatch.setattr(runtime.enterprise_org_service, "_billing_access_suspended", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(runtime.billing_checkout_service, "enterprise_subscription_status", lambda *_args, **_kwargs: "active")
    monkeypatch.setattr(runtime.enterprise_knowledge_service, "search_entries", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(runtime.enterprise_knowledge_attachment_service, "search_attachments", lambda *_args, **_kwargs: [])
    payloads = []

    async def fake_stream(_transport, payload):
        payloads.append(payload)
        with sessionmaker(bind=db.get_bind())() as check:
            reserved = check.query(BillingReservation).one()
            assert reserved.provider_attempt_started_at is not None
        if case == "revoke_inflight":
            with sessionmaker(bind=db.get_bind()).begin() as other:
                proactive.set_policy(other, 10, 1, enabled=False, monthly_credits=100, expected_version=1, expected_token_version=0)
        if case == "unknown":
            raise ConnectionError("供应商结果未知")
        output = ([{"type": "function_call", "id": "evil", "call_id": "evil", "name": "ask_user",
                    "arguments": '{"question":"运行？"}'}] if case == "malicious_tool" else
                  [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "建议先定位问题。"}]}])
        yield {"type": "response.completed", "response": {"status": "completed", "model": "deepseek-v4-flash",
               "output": output, "usage": {"input_tokens": 80, "output_tokens": 20}}}

    monkeypatch.setattr(runtime.NativeResponsesTransport, "_stream_without_billing", fake_stream)
    result = asyncio.run(runtime.run_for_message(db, enterprise_id=10, user_id=2, conversation_id=room_id,
                                                source_message_id=source_id, _proactive_event_id=event_id))
    if case in {"expired_queue", "expired_before_send", "expired_after_reservation"}:
        def expire_event():
            with sessionmaker(bind=db.get_bind()).begin() as other:
                other.query(EnterpriseAssistantProactiveEvent).filter_by(id=event_id).one().create_time -= timedelta(minutes=11)
        if case == "expired_queue":
            expire_event()
        elif case == "expired_before_send":
            original_build = runtime._build_runtime
            def expire_after_claim(*args, **kwargs):
                expire_event()
                return original_build(*args, **kwargs)
            monkeypatch.setattr(runtime, "_build_runtime", expire_after_claim)
        else:
            from app.services import billing_runtime_gate
            original_begin = billing_runtime_gate.begin_attempt
            def expire_after_hold(**kwargs):
                ticket = original_begin(**kwargs)
                expire_event()
                return ticket
            monkeypatch.setattr(billing_runtime_gate, "begin_attempt", expire_after_hold)
    asyncio.run(runtime.execute_queued_run(db.get_bind(), enterprise_id=10, conversation_id=room_id,
                                            run_public_id=result["run_id"]))
    asyncio.run(runtime.execute_queued_run(db.get_bind(), enterprise_id=10, conversation_id=room_id,
                                            run_public_id=result["run_id"]))
    db.expire_all()
    if case in {"expired_queue", "expired_before_send"}:
        assert payloads == [] and db.query(BillingReservation).count() == 0
        return
    if case == "expired_after_reservation":
        reservation = db.query(BillingReservation).one()
        assert payloads == [] and reservation.status == "unknown"
        assert reservation.provider_attempt_started_at is not None
        return
    reservation = db.query(BillingReservation).one()
    assert reservation.user_id is None and reservation.group_id == 100
    assert reservation.usage_kind == "enterprise_auto"
    assert reservation.status == ("unknown" if case == "unknown" else "settled")
    assert len(payloads) == 1 and not payloads[0].get("tools")
    assert db.query(EnterpriseAssistantApproval).count() == 0
    if case == "unknown":
        with pytest.raises(runtime.BillingError):
            runtime.ledger.release_verified_not_sent(db, reservation.request_key)
        db.rollback()
        state = db.query(runtime.EnterpriseAssistantRuntimeState).one()
        run = db.query(EnterpriseAssistantRun).one()
        state.error_code = runtime._PROVIDER_ATTEMPT_PENDING_RECONCILIATION
        state.lease_expires_at = None
        monkeypatch.setattr(runtime, "_pending_provider_attempt_outcome", lambda *_args: "released_not_sent")
        visible = runtime._status_payload(db, run, state)
        assert visible["recoverable"] is False and visible["status"] == "failed"
    if case != "normal":
        assert db.query(runtime.EnterpriseChatMessage).filter_by(sender_type="assistant").count() == 0
    else:
        assert db.query(runtime.EnterpriseChatMessage).filter_by(sender_type="assistant").one().content == "建议先定位问题。"


def test_dispatcher_recovers_pending_event_once(db, monkeypatch):
    from types import SimpleNamespace
    from app.services import enterprise_assistant_dispatcher as dispatcher
    from app.services import enterprise_assistant_runtime_service as runtime

    monkeypatch.setattr(proactive.settings, "billing_commercial_metering_ready", True)
    proactive.set_policy(db, 10, 1, enabled=True, monthly_credits=10, expected_version=0, expected_token_version=0)
    db.commit()
    room = chat.open_conversation(db, 10, 2, "group", group_id=100)
    chat.send_message(db, 10, 2, room.id, "recover", "这个问题应该如何处理？")
    monkeypatch.setattr(dispatcher, "SessionLocal", sessionmaker(bind=db.get_bind(), expire_on_commit=False))
    monkeypatch.setattr(runtime.assistant_service, "_require_subscription", lambda *_args: None)
    monkeypatch.setattr(runtime.assistant_service, "_model_config", lambda *_args: (SimpleNamespace(), "test-model"))
    monkeypatch.setattr(runtime.assistant_service.ledger, "available_milli", lambda *_args: 1000)
    monkeypatch.setattr(runtime, "_render_runtime_context", lambda *_args, **_kwargs: ("测试建议", []))
    dispatcher._dispatch_proactive_events()
    dispatcher._dispatch_proactive_events()
    db.expire_all()
    assert db.query(EnterpriseAssistantRun).count() == 1
    assert db.query(EnterpriseAssistantProactiveEvent).one().status == "queued"


def test_settings_rejects_stale_authenticated_token(db):
    from app.api.v1.enterprise_chat import ProactivePolicyIn, update_proactive_settings
    from app.core.dependencies import authenticate_access_token
    from app.core.security import create_access_token

    token = create_access_token(1, "user", 0)
    actor = authenticate_access_token(token, db)
    with sessionmaker(bind=db.get_bind()).begin() as other:
        other.get(User, 1).token_version = 1
    with pytest.raises(ForbiddenError):
        update_proactive_settings(10, ProactivePolicyIn(enabled=True, monthly_credits=10, expected_version=0),
                                  db=db, user=actor)
    db.rollback()
    assert db.get(Enterprise, 10).proactive_enabled is False


def test_suspended_enterprise_cannot_enable_but_can_disable(db):
    company = db.get(Enterprise, 10)
    company.status = "suspended"
    db.commit()
    with pytest.raises(ForbiddenError):
        proactive.set_policy(db, 10, 1, enabled=True, monthly_credits=10, expected_version=0, expected_token_version=0)
    db.rollback()
    company = db.get(Enterprise, 10)
    company.status = "active"
    db.commit()
    proactive.set_policy(db, 10, 1, enabled=True, monthly_credits=10, expected_version=0, expected_token_version=0)
    db.commit()
    company.status = "suspended"
    db.commit()
    assert not proactive.set_policy(db, 10, 1, enabled=False, monthly_credits=10, expected_version=1, expected_token_version=0)["enabled"]


def test_revoked_token_cannot_send_message_or_create_auto_event(db, monkeypatch):
    from app.api.v1.enterprise_chat import MessageIn, send_message as send_api
    from app.core.dependencies import authenticate_access_token
    from app.core.security import create_access_token
    from app.models.enterprise_chat import EnterpriseChatMessage

    monkeypatch.setattr(proactive.settings, "billing_commercial_metering_ready", True)
    proactive.set_policy(db, 10, 1, enabled=True, monthly_credits=10, expected_version=0, expected_token_version=0)
    db.commit()
    room = chat.open_conversation(db, 10, 2, "group", group_id=100)
    actor = authenticate_access_token(create_access_token(2, "user", 0), db)
    with sessionmaker(bind=db.get_bind()).begin() as other:
        other.get(User, 2).token_version = 1
    with pytest.raises(ForbiddenError):
        send_api(10, room.id, MessageIn(client_message_id="stale", content="请讨论如何处理这个问题？"),
                 db=db, user=actor)
    db.rollback()
    assert db.query(EnterpriseChatMessage).count() == 0
    assert db.query(EnterpriseAssistantProactiveEvent).count() == 0


def test_disabled_metering_cannot_enable_but_can_disable(db, monkeypatch):
    proactive.set_policy(db, 10, 1, enabled=True, monthly_credits=10, expected_version=0, expected_token_version=0)
    db.commit()
    monkeypatch.setattr(proactive.settings, "billing_commercial_metering_ready", False)
    with pytest.raises(ForbiddenError):
        proactive.set_policy(db, 10, 1, enabled=True, monthly_credits=20, expected_version=1, expected_token_version=0)
    db.rollback()
    assert proactive.set_policy(db, 10, 1, enabled=False, monthly_credits=10, expected_version=1,
                                expected_token_version=0)["enabled"] is False


def test_demoted_admin_cannot_change_policy(db):
    member = db.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=2).one()
    member.role = "admin"
    db.commit()
    with sessionmaker(bind=db.get_bind()).begin() as other:
        other.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=2).one().role = "member"
    with pytest.raises(ForbiddenError):
        proactive.set_policy(db, 10, 2, enabled=True, monthly_credits=10, expected_version=0, expected_token_version=0)


def test_disabled_worker_cannot_enable_or_generate_event(db, monkeypatch):
    proactive.set_policy(db, 10, 1, enabled=True, monthly_credits=10, expected_version=0, expected_token_version=0)
    db.commit()
    room = chat.open_conversation(db, 10, 2, "group", group_id=100)
    monkeypatch.setattr(proactive.settings, "enterprise_assistant_worker_enabled", False)
    assert proactive.get_policy(db, 10, 1)["worker_ready"] is False
    chat.send_message(db, 10, 2, room.id, "worker-disabled", "请讨论这个问题如何处理？")
    assert db.query(EnterpriseAssistantProactiveEvent).count() == 0
    with pytest.raises(ForbiddenError):
        proactive.set_policy(db, 10, 1, enabled=True, monthly_credits=10, expected_version=1, expected_token_version=0)
    db.rollback()
    assert not proactive.set_policy(db, 10, 1, enabled=False, monthly_credits=10,
                                    expected_version=1, expected_token_version=0)["enabled"]
