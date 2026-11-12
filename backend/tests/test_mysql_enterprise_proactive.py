"""MySQL RR: opt-in versions, source events and auto-budget cannot race open."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
import test_fresh_database_chain as chain
from sqlalchemy.orm import sessionmaker

from app.core.exceptions import ConflictError
from app.models.billing_ledger import BillingCreditBatch, BillingReservation
from app.models.enterprise_chat import EnterpriseAssistantProactiveEvent
from app.models.enterprise_org import Enterprise, EnterpriseGroup, EnterpriseGroupMember, EnterpriseMember
from app.models.user import User
from app.services import billing_ledger_service as ledger
from app.services import enterprise_assistant_proactive_service as proactive
from app.services import enterprise_chat_service as chat

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url
verified_model_catalog = chain.verified_model_catalog


def test_mysql_proactive_policy_event_and_budget_are_serialized(fresh_chain, verified_model_catalog, monkeypatch):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    suffix = uuid4().hex[:10]
    monkeypatch.setattr(proactive.settings, "billing_commercial_metering_ready", True)
    with factory.begin() as db:
        owner = User(username=f"auto-owner-{suffix}", password="x", role="user", status=1)
        members = [User(username=f"auto-{n}-{suffix}", password="x", role="user", status=1) for n in range(2)]
        db.add_all([owner, *members]); db.flush()
        company = Enterprise(name=f"auto-{suffix}", owner_id=owner.id, seat_limit=4,
                             status="active", paid_order_id=f"auto-{suffix}")
        db.add(company); db.flush()
        group = EnterpriseGroup(enterprise_id=company.id, name="测试组", status="active")
        db.add(group); db.flush()
        db.add(EnterpriseMember(enterprise_id=company.id, user_id=owner.id, role="owner", status="active"))
        for member in members:
            db.add(EnterpriseMember(enterprise_id=company.id, user_id=member.id, role="member", status="active"))
            db.add(EnterpriseGroupMember(enterprise_id=company.id, group_id=group.id, user_id=member.id,
                                         role="member", status="active"))
        eid, oid, gid, member_ids = company.id, owner.id, group.id, [m.id for m in members]
    barrier = Barrier(2)
    def configure(_index):
        with factory() as db:
            barrier.wait(timeout=10)
            try:
                proactive.set_policy(db, eid, oid, enabled=True, monthly_credits=10, expected_version=0, expected_token_version=0)
                db.commit()
                return "ok"
            except ConflictError:
                db.rollback()
                return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(configure, range(2))) == ["conflict", "ok"]
    with factory() as db:
        room = chat.open_conversation(db, eid, member_ids[0], "group", group_id=gid)
        room_id = room.id
    barrier = Barrier(2)
    def send(index):
        with factory() as db:
            barrier.wait(timeout=10)
            chat.send_message(db, eid, member_ids[index], room_id, f"message-{index}", "请讨论这个问题如何处理？")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(send, range(2)))
    with factory.begin() as db:
        assert db.query(EnterpriseAssistantProactiveEvent).filter_by(enterprise_id=eid).count() == 1
        wallet = db.query(ledger.BillingWallet).filter_by(owner_type="enterprise", owner_id=eid).one()
        wallet_id = wallet.id
        db.add(BillingCreditBatch(wallet_id=wallet_id, grant_key=f"auto-fund-{suffix}", kind="purchase",
                                  issued_milli=100_000, remaining_milli=100_000, reserved_milli=0,
                                  refund_reserved_milli=0, expires_at=None))
    barrier = Barrier(2)
    def reserve(index):
        with factory() as db:
            barrier.wait(timeout=10)
            try:
                ledger.reserve(db, request_key=f"auto-reserve-{suffix}-{index}", model_name="deepseek-flash",
                               user_id=None, max_milli=6000, enterprise_wallet_id=wallet_id,
                               enterprise_id=eid, group_id=gid, usage_kind="enterprise_auto",
                               provider_identity=ledger.BillingProviderIdentity(
                                   "deepseek-official-v1", "deepseek", "api.deepseek.com", "system"))
                db.commit()
                return "ok"
            except ledger.BillingError as exc:
                db.rollback()
                return f"blocked:{exc}"
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(reserve, range(2)))
        assert outcomes.count("ok") == 1 and sum(item.startswith("blocked:") for item in outcomes) == 1, outcomes
    with factory() as db:
        rows = db.query(BillingReservation).filter_by(enterprise_id=eid, usage_kind="enterprise_auto").all()
        assert len(rows) == 1 and rows[0].user_id is None and rows[0].group_id == gid


def test_mysql_auth_revocation_serializes_with_settings_and_messages(fresh_chain, monkeypatch):
    from threading import Event
    from app.core.exceptions import ForbiddenError
    from app.models.enterprise_chat import EnterpriseChatMessage

    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    suffix = uuid4().hex[:10]
    monkeypatch.setattr(proactive.settings, "billing_commercial_metering_ready", True)
    with factory.begin() as db:
        owner = User(username=f"fence-owner-{suffix}", password="x", role="user", status=1, token_version=0)
        member = User(username=f"fence-member-{suffix}", password="x", role="user", status=1, token_version=0)
        db.add_all([owner, member])
        db.flush()
        company = Enterprise(name=f"fence-{suffix}", owner_id=owner.id, seat_limit=4,
                             status="active", paid_order_id=f"fence-{suffix}")
        db.add(company)
        db.flush()
        db.add_all([EnterpriseMember(enterprise_id=company.id, user_id=owner.id, role="owner", status="active"),
                    EnterpriseMember(enterprise_id=company.id, user_id=member.id, role="member", status="active")])
        eid, oid, mid = company.id, owner.id, member.id
    with factory() as db:
        proactive.set_policy(db, eid, oid, enabled=True, monthly_credits=10, expected_version=0, expected_token_version=0)
        db.commit()
        room_id = chat.open_conversation(db, eid, mid, "company").id
    with factory.begin() as db:
        db.query(User).filter(User.id.in_([oid, mid])).update({User.token_version: 1}, synchronize_session=False)
    with factory() as db:
        with pytest.raises(ForbiddenError):
            proactive.set_policy(db, eid, oid, enabled=False, monthly_credits=10,
                                 expected_version=1, expected_token_version=0)
        db.rollback()
        with pytest.raises(ForbiddenError):
            chat.send_message(db, eid, mid, room_id, "stale-jwt", "这是一条应当被拒绝的消息。",
                              expected_token_version=0)
        db.rollback()
        assert db.query(EnterpriseChatMessage).filter_by(conversation_id=room_id).count() == 0
        assert db.query(EnterpriseAssistantProactiveEvent).filter_by(enterprise_id=eid).count() == 0

    def revoke(user_id, started, finished):
        with factory.begin() as other:
            started.set()
            row = other.query(User).filter_by(id=user_id).with_for_update().one()
            row.token_version += 1
        finished.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with factory() as db:
            proactive.set_policy(db, eid, oid, enabled=False, monthly_credits=10,
                                 expected_version=1, expected_token_version=1)
            started, finished = Event(), Event()
            future = pool.submit(revoke, oid, started, finished)
            assert started.wait(5) and not finished.wait(0.2)
            db.commit()
            future.result(timeout=5)
        with factory() as db:
            proactive.set_policy(db, eid, oid, enabled=True, monthly_credits=10,
                                 expected_version=2, expected_token_version=2)
            db.commit()
            chat._lock_enterprise(db, eid)
            chat._require_locked_member(db, eid, mid, expected_token_version=1)
            started, finished = Event(), Event()
            future = pool.submit(revoke, mid, started, finished)
            assert started.wait(5) and not finished.wait(0.2)
            chat.send_message(db, eid, mid, room_id, "locked-jwt", "这条消息在撤销之前已经取得授权。",
                              expected_token_version=1)
            future.result(timeout=5)
    with factory() as db:
        assert db.query(EnterpriseChatMessage).filter_by(conversation_id=room_id, client_message_id="locked-jwt").count() == 1
        assert db.query(EnterpriseAssistantProactiveEvent).filter_by(enterprise_id=eid).count() == 1
    with factory.begin() as db:
        db.query(EnterpriseMember).filter_by(enterprise_id=eid, user_id=oid).one().role = "member"
    with factory() as db:
        with pytest.raises(ForbiddenError):
            proactive.set_policy(db, eid, oid, enabled=False, monthly_credits=10,
                                 expected_version=3, expected_token_version=2)
        db.rollback()
    with factory.begin() as db:
        db.query(EnterpriseMember).filter_by(enterprise_id=eid, user_id=oid).one().role = "owner"
        db.query(Enterprise).filter_by(id=eid).one().status = "suspended"
    with factory() as db:
        with pytest.raises(ForbiddenError):
            proactive.set_policy(db, eid, oid, enabled=True, monthly_credits=10,
                                 expected_version=3, expected_token_version=2)
        db.rollback()
        assert not proactive.set_policy(db, eid, oid, enabled=False, monthly_credits=10,
                                        expected_version=3, expected_token_version=2)["enabled"]


@pytest.mark.parametrize("expire_before_send", ["none", "after_claim", "after_hold"])
def test_mysql_proactive_unknown_has_committed_send_marker(fresh_chain, verified_model_catalog, monkeypatch,
                                                           expire_before_send):
    import asyncio
    from types import SimpleNamespace
    from app.services import enterprise_assistant_runtime_service as runtime
    from app.services import billing_reservation_reconciliation_service as c4

    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    suffix = uuid4().hex[:10]
    monkeypatch.setattr(proactive.settings, "billing_commercial_metering_ready", True)
    with factory.begin() as db:
        owner = User(username=f"attempt-owner-{suffix}", password="x", role="user", status=1, token_version=0)
        db.add(owner)
        db.flush()
        company = Enterprise(name=f"attempt-{suffix}", owner_id=owner.id, seat_limit=2,
                             status="active", paid_order_id=f"attempt-{suffix}")
        db.add(company)
        db.flush()
        db.add(EnterpriseMember(enterprise_id=company.id, user_id=owner.id, role="owner", status="active"))
        eid, oid = company.id, owner.id
    with factory() as db:
        proactive.set_policy(db, eid, oid, enabled=True, monthly_credits=100,
                             expected_version=0, expected_token_version=0)
        wallet = db.query(ledger.BillingWallet).filter_by(owner_type="enterprise", owner_id=eid).one()
        db.add(BillingCreditBatch(wallet_id=wallet.id, grant_key=f"attempt-fund-{suffix}", kind="purchase",
                                  issued_milli=1_000_000, remaining_milli=1_000_000, reserved_milli=0,
                                  refund_reserved_milli=0, expires_at=None))
        db.commit()
        room_id = chat.open_conversation(db, eid, oid, "company").id
        source_id = chat.send_message(db, eid, oid, room_id, "question", "请讨论这个问题如何处理？").id
        event_id = db.query(EnterpriseAssistantProactiveEvent).filter_by(source_message_id=source_id).one().id
    monkeypatch.setattr(runtime.assistant_service, "_model_config", lambda _db: (SimpleNamespace(
        api_key="fake", base_url="https://api.deepseek.com/v1", model="deepseek-v4-flash",
        provider="deepseek", source="system", timeout_seconds=5), "deepseek-v4-flash"))
    monkeypatch.setattr(runtime.enterprise_org_service, "require_enterprise_execution", lambda *_args: None)
    monkeypatch.setattr(runtime.enterprise_org_service, "_billing_access_suspended", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(runtime.billing_checkout_service, "enterprise_subscription_status", lambda *_args, **_kwargs: "active")
    monkeypatch.setattr(runtime.enterprise_knowledge_service, "search_entries", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(runtime.enterprise_knowledge_attachment_service, "search_attachments", lambda *_args, **_kwargs: [])
    sent = []
    async def fake_stream(_transport, _payload):
        with factory() as observer:
            reservation = observer.query(BillingReservation).filter_by(enterprise_id=eid).one()
            assert reservation.provider_attempt_started_at is not None
        sent.append(True)
        raise ConnectionError("unknown provider response")
        yield
    monkeypatch.setattr(runtime.NativeResponsesTransport, "_stream_without_billing", fake_stream)
    with factory() as db:
        result = asyncio.run(runtime.run_for_message(db, enterprise_id=eid, user_id=oid, conversation_id=room_id,
                                                     source_message_id=source_id, _proactive_event_id=event_id))
    with factory.begin() as db:
        run = db.query(runtime.EnterpriseAssistantRun).filter_by(public_id=result["run_id"]).one()
        event = db.query(EnterpriseAssistantProactiveEvent).filter_by(id=event_id).one()
        assert (event.run_id, event.status, event.policy_version) == (run.id, "queued", run.proactive_version)
        proactive.require_fresh_event(db, run)
    if expire_before_send != "none":
        from datetime import timedelta
        def expire_event():
            with factory.begin() as other:
                other.query(EnterpriseAssistantProactiveEvent).filter_by(id=event_id).one().create_time -= timedelta(minutes=11)
        if expire_before_send == "after_claim":
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
    asyncio.run(runtime.execute_queued_run(fresh_chain, enterprise_id=eid, conversation_id=room_id,
                                            run_public_id=result["run_id"]))
    if expire_before_send != "none":
        assert sent == []
        with factory() as db:
            reservations = db.query(BillingReservation).filter_by(enterprise_id=eid).all()
            if expire_before_send == "after_claim":
                assert reservations == []
            else:
                assert len(reservations) == 1 and reservations[0].status == "unknown"
                assert reservations[0].provider_attempt_started_at is not None
        return
    with factory() as db:
        state = db.query(runtime.EnterpriseAssistantRuntimeState).filter_by(enterprise_id=eid).one()
        diagnostic = (state.status, state.error_code, state.checkpoint_json)
    assert sent == [True], diagnostic
    with factory() as db:
        reservation = db.query(BillingReservation).filter_by(enterprise_id=eid).one()
        assert reservation.status == "unknown" and reservation.provider_attempt_started_at is not None
        with pytest.raises(ledger.BillingError):
            ledger.release_verified_not_sent(db, reservation.request_key)
        db.rollback()
        with pytest.raises(c4.ReconciliationError, match="已持久化"):
            c4.propose_reconciliation(db, reservation_id=reservation.id, actor_id=oid,
                resolution="release_not_sent", evidence_reference="test-evidence", evidence_sha256="a" * 64,
                note="test", provider_request_id=None, input_tokens=None, output_tokens=None)
